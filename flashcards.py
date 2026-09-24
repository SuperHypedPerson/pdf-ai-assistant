#!/usr/bin/env python3
"""
flashcards.py --file book.pdf

Parses the PDF's structure, lets you pick which chapters/subchapters to
turn into flashcards, and derives one Obsidian Spaced Repetition flashcard
file per selected subchapter from its ALREADY-GENERATED note (via notes.py)
— no LLM calls here, purely parsing the note's "## Key Concepts" section.

Subchapters with no generated note yet are skipped with a message; run
notes.py for them first.

Written into the vault at
<Vault>/Flashcards/<Book Title>/<Chapter N - Title>/<N.M Title>.md
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from src import manifest as manifest_mod
from src import run_log
from src import vault
from src.flashcards_generator import generate_subchapter_flashcards
from src.note_generator import is_numbered_chapter, subchapter_title_rest
from src.selection import confirm_text, parse_selection, render_tree
from src.structure_extractor import extract_structure

DEFAULT_VAULT_PATH = r"C:\Users\Marcus\Desktop\Textbook Notes Summarizer"


def prompt_for_selection(structure) -> list:
    while True:
        raw = input("\nSelect chapters/subchapters (e.g. '3, 5.1-5.4, 7'): ").strip()
        if not raw:
            print("Nothing entered, try again.")
            continue

        selected, errors = parse_selection(raw, structure)
        if errors:
            print("Couldn't parse:")
            for e in errors:
                print(f"  - {e}")
        if not selected:
            continue

        print()
        print(confirm_text(selected))
        confirm = input("\nProceed with this selection? [y/N]: ").strip().lower()
        if confirm == "y":
            return selected


def main():
    parser = argparse.ArgumentParser(
        description="Generate Obsidian Spaced Repetition flashcards from already-generated notes.")
    parser.add_argument("--file", required=True, help="Path to the PDF file")
    parser.add_argument("--vault", default=DEFAULT_VAULT_PATH, help="Path to your Obsidian vault")
    parser.add_argument("--all-chapters", action="store_true",
                         help="Process every not-yet-carded subchapter in the book's real numbered "
                              "chapters without prompting (front/back matter excluded by default; "
                              "see --include-front-matter)")
    parser.add_argument("--include-front-matter", action="store_true",
                         help="With --all-chapters, also process front/back matter (unnumbered chapters)")
    parser.add_argument("--manifest", default=str(manifest_mod.DEFAULT_MANIFEST_PATH),
                         help="Path to the manifest JSON file")
    parser.add_argument("--log", default=str(run_log.DEFAULT_LOG_PATH),
                         help="Path to the run-summary log file")
    args = parser.parse_args()

    vault_root = Path(args.vault)
    structure = extract_structure(args.file)

    manifest = manifest_mod.load_manifest(args.manifest)
    manifest_mod.ensure_book_entry(manifest, args.file, structure.title)
    processed_notes = manifest_mod.processed_subchapters(manifest, args.file)
    processed_cards = manifest_mod.processed_flashcards(manifest, args.file)

    print(render_tree(structure, processed_cards))

    start_time = time.monotonic()

    if args.all_chapters:
        eligible_chapters = structure.chapters if args.include_front_matter else \
            [c for c in structure.chapters if is_numbered_chapter(c)]
        full_selection = [(c, s) for c in eligible_chapters for s in c.subchapters]
        selected = [(c, s) for c, s in full_selection if s.number not in processed_cards]
        skipped = len(full_selection) - len(selected)
        print(f"\n--all-chapters: {len(selected)} new subchapter(s) to card "
              f"({skipped} already carded, skipped).")
        if not selected:
            print("Nothing new to process.")
            return
    else:
        selected = prompt_for_selection(structure)
        skipped = 0

    generated = 0
    no_note: list[str] = []
    no_concepts: list[str] = []

    for chapter, subchapter in selected:
        note_path = manifest_mod.existing_note_path(manifest, args.file, subchapter.number)
        if not note_path or not Path(note_path).exists():
            print(f"  {subchapter.number} {subchapter_title_rest(subchapter)} — no note yet, skipping "
                  f"(run notes.py for this subchapter first)")
            no_note.append(subchapter.number)
            continue

        content = generate_subchapter_flashcards(structure.title, chapter, subchapter, note_path)
        if content is None:
            print(f"  {subchapter.number} {subchapter_title_rest(subchapter)} — note has no Key Concepts, skipping")
            no_concepts.append(subchapter.number)
            continue

        path = vault.flashcards_path(vault_root, structure.title, chapter, subchapter)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        manifest_mod.mark_flashcards_processed(manifest, args.file, subchapter.number, str(path))
        print(f"  {subchapter.number} {subchapter_title_rest(subchapter)} -> {path}")
        generated += 1

    manifest_mod.save_manifest(manifest, args.manifest)

    print(f"\n{generated} flashcard file(s) written under "
          f"{vault.flashcards_book_folder(vault_root, structure.title)}/")
    if no_note:
        print(f"{len(no_note)} subchapter(s) skipped (no note yet): {', '.join(no_note)}")
    if no_concepts:
        print(f"{len(no_concepts)} subchapter(s) skipped (no Key Concepts in note): {', '.join(no_concepts)}")

    run_log.log_run({
        "script": "flashcards",
        "status": "completed",
        "source_file": args.file,
        "book_title": structure.title,
        "num_requested": len(selected),
        "num_generated": generated,
        "num_skipped_no_note": len(no_note),
        "num_skipped_no_concepts": len(no_concepts),
        "vault": str(vault_root),
        "duration_seconds": round(time.monotonic() - start_time, 1),
    }, args.log)


if __name__ == "__main__":
    main()
