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

from src.note_generator import chapter_display_label, subchapter_title_rest
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
