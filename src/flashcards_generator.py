"""
Backlog item: spaced-repetition flashcards, generated from already-written
notes rather than a fresh LLM call.

Parses each note's "## Key Concepts" section (rendered by note_generator.py
as `- **<Term>**: <definition>` bullets) and turns every term into an
Obsidian Spaced Repetition plugin flashcard — multi-line "Question / ? /
Answer" blocks, tagged with a hierarchical #flashcards/<Book>/<Chapter> tag
so the plugin's deck browser groups them sensibly.

Purely a parser/formatter over existing note content: if a subchapter has
no generated note yet, there is nothing to derive cards from, and the
caller should skip it rather than triggering note generation as a side
effect.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from src import manifest as manifest_mod
from src import vault
from src.note_generator import chapter_display_label, chapter_group_key, subchapter_title_rest
from src.structure_extractor import Chapter, SubChapter

KEY_CONCEPT_RE = re.compile(r"^-\s*\*\*(.+?)\*\*:\s*(.+)$")
SECTION_HEADING_RE = re.compile(r"^##\s+(.+)$")


def parse_key_concepts(note_text: str) -> list[tuple[str, str]]:
    """Extract (term, definition) pairs from a note's "## Key Concepts"
    section. Returns [] if the section is missing or has no parseable
    bullets (e.g. the "no source text" placeholder note)."""
    lines = note_text.splitlines()
    concepts: list[tuple[str, str]] = []
    in_section = False

    for line in lines:
        heading = SECTION_HEADING_RE.match(line.strip())
        if heading:
            in_section = heading.group(1).strip().lower() == "key concepts"
            continue
        if not in_section:
            continue
        match = KEY_CONCEPT_RE.match(line.strip())
        if match:
            term, definition = match.group(1).strip(), match.group(2).strip()
            if term and definition:
                concepts.append((term, definition))

    return concepts


def render_flashcards(book_title: str, chapter: Chapter, subchapter: SubChapter,
                       concepts: list[tuple[str, str]]) -> str:
    deck_tag = "#flashcards/" + "/".join(
        re.sub(r"\s+", "-", part.strip()) for part in
        (book_title, chapter_display_label(chapter)) if part.strip()
    )
    lines = [
        deck_tag,
        "",
        f"# {subchapter_title_rest(subchapter)} — Flashcards",
        "",
    ]
    for term, definition in concepts:
        lines.append(f"{term}?")
        lines.append(definition)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_subchapter_flashcards(book_title: str, chapter: Chapter, subchapter: SubChapter,
                                    note_path: str | Path) -> str | None:
    """Reads the existing note at note_path and renders its flashcards file
    content, or None if the note has no Key Concepts to turn into cards."""
    note_text = Path(note_path).read_text(encoding="utf-8")
    concepts = parse_key_concepts(note_text)
    if not concepts:
        return None
    return render_flashcards(book_title, chapter, subchapter, concepts)


def run_flashcards_generation(pdf_path: str, structure, selected: list, vault_root: Path,
                               manifest: dict) -> Iterator[dict]:
    """Shared pipeline used by both flashcards.py (CLI) and the web app's
    flashcards endpoint — mirrors notes_pipeline.run_notes_generation's
    shape so both callers can render its events the same way. Mutates
    `manifest` in place (mark_flashcards_processed) but does not save it to
    disk; the caller does that once the generator is exhausted. Yields:
      {"type": "chapter_start", "label": str}
      {"type": "subchapter_start", "number": str, "title": str}
      {"type": "subchapter_done", "number": str, "path": str}
      {"type": "subchapter_skipped", "number": str, "reason": "no_note" | "no_concepts"}
      {"type": "done", "generated": int, "skipped_no_note": list[str], "skipped_no_concepts": list[str]}
    """
    generated = 0
    skipped_no_note: list[str] = []
    skipped_no_concepts: list[str] = []
    current_group_key = None

    for chapter, subchapter in selected:
        group_key = chapter_group_key(chapter)
        if group_key != current_group_key:
            current_group_key = group_key
            yield {"type": "chapter_start", "label": chapter_display_label(chapter)}

        title = subchapter_title_rest(subchapter)
        yield {"type": "subchapter_start", "number": subchapter.number, "title": title}

        note_path = manifest_mod.existing_note_path(manifest, pdf_path, subchapter.number)
        if not note_path or not Path(note_path).exists():
            skipped_no_note.append(subchapter.number)
            yield {"type": "subchapter_skipped", "number": subchapter.number, "reason": "no_note"}
            continue

        content = generate_subchapter_flashcards(structure.title, chapter, subchapter, note_path)
        if content is None:
            skipped_no_concepts.append(subchapter.number)
            yield {"type": "subchapter_skipped", "number": subchapter.number, "reason": "no_concepts"}
            continue

        path = vault.flashcards_path(vault_root, structure.title, chapter, subchapter)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        manifest_mod.mark_flashcards_processed(manifest, pdf_path, subchapter.number, str(path))
        generated += 1
        yield {"type": "subchapter_done", "number": subchapter.number, "path": str(path)}

    yield {"type": "done", "generated": generated,
           "skipped_no_note": skipped_no_note, "skipped_no_concepts": skipped_no_concepts}
