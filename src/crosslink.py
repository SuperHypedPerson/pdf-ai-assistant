"""
Backlog item: cross-book linking.

Vault-wide pass over every book tracked in the manifest: parses each
note's "## Key Concepts" terms (same parser as flashcards_generator.py),
builds a global term index, and for any term that appears in notes from
more than one distinct book, adds a "## Cross-Book Links" section to each
involved note pointing at the others — a new section, kept separate from
"## Related" (same-book sibling links from Stage 4) so it never clobbers
that logic.

Matching is exact, case/whitespace-normalized term matching only — no
LLM-judged topic similarity, for the same determinism/speed reasons as
the rest of this project.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from src.flashcards_generator import parse_key_concepts

CROSS_LINKS_HEADING = "## Cross-Book Links"
_SECTION_RE_CACHE: dict[str, re.Pattern] = {}


def normalize_term(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def _section_re(heading: str) -> re.Pattern:
    if heading not in _SECTION_RE_CACHE:
        _SECTION_RE_CACHE[heading] = re.compile(
            rf"^{re.escape(heading)}\n.*?(?=\n## |\Z)", re.DOTALL | re.MULTILINE)
    return _SECTION_RE_CACHE[heading]


def upsert_section(content: str, heading: str, body: str) -> str:
    """Replaces the named "## Heading" section in-place if present
    (idempotent re-runs), or appends it as a new section at the end if
    not — never touches any other section."""
    section = f"{heading}\n{body}\n"
    pattern = _section_re(heading)
    if pattern.search(content):
        return pattern.sub(section.rstrip("\n") + "\n", content, count=1)
    return content.rstrip("\n") + "\n\n" + section


class NoteEntry:
    __slots__ = ("book_id", "book_title", "path", "terms")

    def __init__(self, book_id: str, book_title: str, path: Path, terms: set[str]):
        self.book_id = book_id
        self.book_title = book_title
        self.path = path
        self.terms = terms


def collect_note_entries(manifest: dict, vault_root: Path) -> list[NoteEntry]:
    """Reads every processed note under vault_root and parses its Key
    Concepts terms. Notes recorded in the manifest but living outside
    vault_root (a stale entry from a different vault) are skipped."""
    entries: list[NoteEntry] = []
    vault_root = vault_root.resolve()

    for book_id, book in manifest["books"].items():
        for info in book.get("subchapters", {}).values():
            if info.get("status") != "processed":
                continue
            note_path = info.get("note_path")
            if not note_path:
                continue
            path = Path(note_path)
            try:
                path.resolve().relative_to(vault_root)
            except (ValueError, OSError):
                continue
            if not path.exists():
                continue

            text = path.read_text(encoding="utf-8")
            terms = {normalize_term(term) for term, _ in parse_key_concepts(text)}
            if terms:
                entries.append(NoteEntry(book_id, book["title"], path, terms))

    return entries


def build_term_index(entries: list[NoteEntry]) -> dict[str, list[NoteEntry]]:
    index: dict[str, list[NoteEntry]] = defaultdict(list)
    for entry in entries:
        for term in entry.terms:
            index[term].append(entry)
    return index


def find_cross_book_matches(entries: list[NoteEntry]) -> dict[Path, list[tuple[NoteEntry, set[str]]]]:
    """For each note, the other notes (from a DIFFERENT book) it shares at
    least one Key Concepts term with, and which terms matched."""
    index = build_term_index(entries)

    matches: dict[Path, dict[Path, tuple[NoteEntry, set[str]]]] = defaultdict(dict)
    for term, term_entries in index.items():
        distinct_books = {e.book_id for e in term_entries}
        if len(distinct_books) < 2:
            continue
        for entry in term_entries:
            for other in term_entries:
                if other.book_id == entry.book_id:
                    continue
                existing = matches[entry.path].get(other.path)
                if existing:
                    existing[1].add(term)
                else:
                    matches[entry.path][other.path] = (other, {term})

    return {path: sorted(others.values(), key=lambda pair: (pair[0].book_title, str(pair[0].path)))
            for path, others in matches.items()}


def render_cross_links_body(vault_root: Path, matches: list[tuple[NoteEntry, set[str]]]) -> str:
    from src.vault import wikilink
    lines = []
    for other, terms in matches:
        link = wikilink(vault_root, other.path)
        shared = ", ".join(sorted(terms))
        lines.append(f"- {link} — *{other.book_title}* (shared: {shared})")
    return "\n".join(lines)


def apply_cross_links(vault_root: Path, manifest: dict) -> dict[str, int]:
    """Updates every affected note's file in place. Returns a summary:
    {"notes_scanned", "notes_linked", "terms_matched"}."""
    entries = collect_note_entries(manifest, vault_root)
    matches = find_cross_book_matches(entries)

    all_terms: set[str] = set()
    for pair_list in matches.values():
        for _, terms in pair_list:
            all_terms.update(terms)

    for path, pair_list in matches.items():
        body = render_cross_links_body(vault_root, pair_list)
        content = path.read_text(encoding="utf-8")
        updated = upsert_section(content, CROSS_LINKS_HEADING, body)
        if updated != content:
            path.write_text(updated, encoding="utf-8")

    return {
        "notes_scanned": len(entries),
        "notes_linked": len(matches),
        "terms_matched": len(all_terms),
    }
