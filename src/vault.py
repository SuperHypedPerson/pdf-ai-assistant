"""
Stage 4: vault/folder layout and the book-level index (MOC) note.

Folder structure:
    <Vault>/Textbooks/<Book Title>/<Chapter N - Title>/<N.M Subchapter Title>.md

The book index lives at <Vault>/Textbooks/<Book Title>/0 - Index.md and links
every subchapter note that exists so far for that book (from the manifest),
not just the current run's selection.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.note_generator import chapter_title_rest, real_chapter_number, real_subchapter_number
from src.structure_extractor import BookStructure, Chapter, SubChapter

INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
MAX_NAME_LENGTH = 150


def sanitize_filename(name: str) -> str:
    name = INVALID_CHARS_RE.sub("-", name).strip()
    name = name.rstrip(". ")  # Windows disallows trailing dots/spaces
    return name[:MAX_NAME_LENGTH].strip() or "untitled"


def book_folder(vault_root: Path, book_title: str) -> Path:
    return vault_root / "Textbooks" / sanitize_filename(book_title)


def chapter_folder_name(chapter: Chapter) -> str:
    return sanitize_filename(f"Chapter {real_chapter_number(chapter)} - {chapter_title_rest(chapter)}")


def chapter_folder(vault_root: Path, book_title: str, chapter: Chapter) -> Path:
    return book_folder(vault_root, book_title) / chapter_folder_name(chapter)


def subchapter_file_stem(chapter: Chapter, subchapter: SubChapter) -> str:
    return sanitize_filename(f"{real_subchapter_number(chapter, subchapter)} {subchapter.title}")


def note_path(vault_root: Path, book_title: str, chapter: Chapter, subchapter: SubChapter) -> Path:
    return chapter_folder(vault_root, book_title, chapter) / f"{subchapter_file_stem(chapter, subchapter)}.md"


def index_path(vault_root: Path, book_title: str) -> Path:
    return book_folder(vault_root, book_title) / "0 - Index.md"


def quiz_folder(vault_root: Path, book_title: str) -> Path:
    return vault_root / "Quizzes" / sanitize_filename(book_title)


def quiz_path(vault_root: Path, book_title: str, stem: str) -> Path:
    return quiz_folder(vault_root, book_title) / f"{sanitize_filename(stem)}.md"


def wikilink(vault_root: Path, target_note_path: Path) -> str:
    """Full-path wikilink relative to the vault root, so it stays
    unambiguous even if another book has a same-named subchapter file."""
    rel = target_note_path.relative_to(vault_root).with_suffix("")
    return f"[[{rel.as_posix()}]]"


def render_index(vault_root: Path, book_title: str, structure: BookStructure,
                  chapter_by_number: dict[str, Chapter],
                  notes_by_chapter: dict[str, list[tuple[SubChapter, Path]]]) -> str:
    """notes_by_chapter maps a chapter's real number -> [(subchapter, note_path), ...]
    for every subchapter that has a generated note so far, not just this run's."""
    lines = [f"# {book_title} — Index", ""]

    for real_num in sorted(notes_by_chapter, key=lambda n: [int(p) for p in n.split(".")]):
        entries = notes_by_chapter[real_num]
        if not entries:
            continue
        chapter = chapter_by_number.get(real_num)
        heading = f"Chapter {real_num} - {chapter_title_rest(chapter)}" if chapter else f"Chapter {real_num}"
        lines.append(f"## {heading}")

        def sort_key(entry: tuple[SubChapter, Path]) -> list[int]:
            subchapter = entry[0]
            if chapter is None:
                return [0]
            return [int(p) for p in real_subchapter_number(chapter, subchapter).split(".")]

        for subchapter, path in sorted(entries, key=sort_key):
            link = wikilink(vault_root, path)
            lines.append(f"- {link} — {subchapter.title}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
