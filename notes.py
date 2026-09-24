#!/usr/bin/env python3
"""
notes.py --file book.pdf --subject SUBJECT

Parses the PDF's structure, shows it to you, lets you pick which
chapters/subchapters to process, and generates one Obsidian note per
selected subchapter via a local LM Studio model.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openai

from src import manifest as manifest_mod
from src import llm_client
from src.note_generator import generate_subchapter_note
from src.selection import confirm_text, parse_selection, render_tree
from src.structure_extractor import extract_structure

OUTPUT_DIR = Path("notes_output")


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
    parser.add_argument("--lmstudio-url", default=None,
                         help=f"LM Studio base URL (default: {llm_client.DEFAULT_BASE_URL})")
    parser.add_argument("--model", default=None,
                         help=f"Model name as loaded in LM Studio (default: {llm_client.DEFAULT_MODEL})")
    parser.add_argument("--timeout", type=float, default=llm_client.DEFAULT_TIMEOUT_SECONDS,
                         help=f"Seconds to wait for LM Studio per request (default: {llm_client.DEFAULT_TIMEOUT_SECONDS:.0f})")
    args = parser.parse_args()

    if not args.all_chapters and not args.subject:
        parser.error("--subject is required (e.g. --subject finance)")

    structure = extract_structure(args.file)

    manifest = manifest_mod.load_manifest(args.manifest)
    manifest_mod.ensure_book_entry(manifest, args.file, structure.title)
    processed = manifest_mod.processed_subchapters(manifest, args.file)

    print(render_tree(structure, processed))

    if args.all_chapters:
        print("\n--all-chapters is not implemented yet (Stage 6).")
        return

    selected = prompt_for_selection(structure)

    client = llm_client.get_client(args.lmstudio_url, timeout=args.timeout)
    model = args.model or llm_client.get_model_name()

    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"\nGenerating notes via LM Studio ({model})...\n")

    generated = 0
    for chapter, subchapter in selected:
        print(f"  {subchapter.number} {subchapter.title} ...", end=" ", flush=True)
        try:
            note_md = generate_subchapter_note(
                args.file, structure, chapter, subchapter, args.subject, client, model,
            )
        except llm_client.EmptyResponseError as e:
            print("FAILED (empty response)")
            print(f"\n{e}")
            sys.exit(1)
        except openai.APITimeoutError:
            print("FAILED (timed out)")
            print(f"\nLM Studio didn't respond within {args.timeout:.0f}s.")
            print("Check the LM Studio server window/log for this request — if it's still "
                  "generating, your hardware may just be slow for this model; re-run with "
                  "--timeout 600 (or higher). If the log shows it finished or errored, that's "
                  "a different problem — paste the log here.")
            sys.exit(1)
        except openai.APIConnectionError:
            print("FAILED")
            print(f"\nCouldn't reach LM Studio at {args.lmstudio_url or llm_client.DEFAULT_BASE_URL}.")
            print("Make sure the LM Studio local server is running and the model is loaded, then re-run.")
            sys.exit(1)

        safe_title = "".join(c for c in subchapter.title if c.isalnum() or c in " -_").strip()
        out_path = OUTPUT_DIR / f"{subchapter.number} {safe_title}.md"
        out_path.write_text(note_md)
        manifest_mod.mark_processed(manifest, args.file, subchapter.number, str(out_path))
        generated += 1
        print(f"-> {out_path}")

    manifest_mod.save_manifest(manifest, args.manifest)
    print(f"\n{generated} note(s) written to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
