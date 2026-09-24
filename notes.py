#!/usr/bin/env python3
"""
notes.py --file book.pdf [--subject SUBJECT]

Stage 1-2: parses the PDF's structure, shows it to you, and lets you pick
which chapters/subchapters to process. Note generation itself (Stage 3)
is not implemented yet — this stub confirms the selection and stops.
"""

from __future__ import annotations

import argparse

from src import manifest as manifest_mod
from src.selection import confirm_text, parse_selection, render_tree
from src.structure_extractor import extract_structure


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
    parser = argparse.ArgumentParser(description="Generate Obsidian notes from selected chapters of a PDF textbook.")
    parser.add_argument("--file", required=True, help="Path to the PDF file")
    parser.add_argument("--subject", help="Subject-area tag applied to generated notes (e.g. finance)")
    parser.add_argument("--all-chapters", action="store_true",
                         help="Process every chapter without prompting (Stage 6 — not implemented yet)")
    parser.add_argument("--manifest", default=str(manifest_mod.DEFAULT_MANIFEST_PATH),
                         help="Path to the manifest JSON file")
    args = parser.parse_args()

    structure = extract_structure(args.file)

    manifest = manifest_mod.load_manifest(args.manifest)
    manifest_mod.ensure_book_entry(manifest, args.file, structure.title)
    processed = manifest_mod.processed_subchapters(manifest, args.file)

    print(render_tree(structure, processed))

    if args.all_chapters:
        print("\n--all-chapters is not implemented yet (Stage 6).")
        return

    selected = prompt_for_selection(structure)
    manifest_mod.save_manifest(manifest, args.manifest)

    print(f"\n{len(selected)} subchapter(s) confirmed. Note generation is Stage 3 — not implemented yet.")


if __name__ == "__main__":
    main()
