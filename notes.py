#!/usr/bin/env python3
"""
notes.py --file book.pdf --subject SUBJECT

Parses the PDF's structure, shows it to you, lets you pick which
chapters/subchapters to process, and generates one Obsidian note per
selected subchapter via a local LM Studio model — written into the vault
at <Vault>/Textbooks/<Book Title>/<Chapter N - Title>/<N.M Title>.md, with
a book-level index note kept up to date alongside them.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

import openai

from src import llm_client
from src import manifest as manifest_mod
from src import run_log
from src import vault
from src.note_generator import generate_subchapter_note, real_chapter_number
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


def resolve_note_path(manifest: dict, pdf_path: str, vault_root: Path, book_title: str, chapter, subchapter) -> Path:
    """Reuse the path already on record for this subchapter (update in
    place), or compute a fresh one from the vault layout if it's new — or
    if the recorded path predates the vault (e.g. Stage 3's notes_output/
    folder) and so falls outside vault_root."""
    stored = manifest_mod.existing_note_path(manifest, pdf_path, subchapter.number)
    if stored:
        stored_path = Path(stored)
        try:
            stored_path.resolve().relative_to(vault_root.resolve())
            return stored_path
        except ValueError:
            pass  # stale pre-vault path; fall through to recompute
    return vault.note_path(vault_root, book_title, chapter, subchapter)


def build_known_notes(manifest: dict, pdf_path: str, vault_root: Path, structure, selected: list) -> dict:
    """Every subchapter with a note that will exist after this run —
    previously processed ones (from the manifest) plus this run's
    selection — mapped to its (chapter, subchapter, resolved path)."""
    sub_lookup = {s.number: (c, s) for c in structure.chapters for s in c.subchapters}

    known = {}
    for num in manifest_mod.processed_subchapters(manifest, pdf_path):
        if num in sub_lookup:
            chap, sub = sub_lookup[num]
            known[num] = (chap, sub, resolve_note_path(manifest, pdf_path, vault_root, structure.title, chap, sub))

    for chap, sub in selected:
        known[sub.number] = (chap, sub, resolve_note_path(manifest, pdf_path, vault_root, structure.title, chap, sub))

    return known


def group_by_chapter(known: dict) -> dict:
    by_chapter = defaultdict(list)
    for chap, sub, path in known.values():
        by_chapter[real_chapter_number(chap)].append((sub, path))
    return by_chapter


def main():
    parser = argparse.ArgumentParser(description="Generate Obsidian notes from selected chapters of a PDF textbook.")
    parser.add_argument("--file", required=True, help="Path to the PDF file")
    parser.add_argument("--subject", help="Subject-area tag applied to generated notes (e.g. finance)")
    parser.add_argument("--vault", default=DEFAULT_VAULT_PATH, help="Path to your Obsidian vault")
    parser.add_argument("--all-chapters", action="store_true",
                         help="Process every not-yet-processed subchapter in the book without prompting")
    parser.add_argument("--manifest", default=str(manifest_mod.DEFAULT_MANIFEST_PATH),
                         help="Path to the manifest JSON file")
    parser.add_argument("--log", default=str(run_log.DEFAULT_LOG_PATH),
                         help="Path to the run-summary log file")
    parser.add_argument("--lmstudio-url", default=None,
                         help=f"LM Studio base URL (default: {llm_client.DEFAULT_BASE_URL})")
    parser.add_argument("--model", default=None,
                         help=f"Model name as loaded in LM Studio (default: {llm_client.DEFAULT_MODEL})")
    parser.add_argument("--timeout", type=float, default=llm_client.DEFAULT_TIMEOUT_SECONDS,
                         help=f"Seconds to wait for LM Studio per request (default: {llm_client.DEFAULT_TIMEOUT_SECONDS:.0f})")
    parser.add_argument("--max-tokens", type=int, default=None,
                         help="Fixed output token budget per note (default: auto-scaled by subchapter page count)")
    args = parser.parse_args()

    if not args.all_chapters and not args.subject:
        parser.error("--subject is required (e.g. --subject finance)")

    vault_root = Path(args.vault)
    structure = extract_structure(args.file)
    model = args.model or llm_client.get_model_name()

    manifest = manifest_mod.load_manifest(args.manifest)
    manifest_mod.ensure_book_entry(manifest, args.file, structure.title)
    processed = manifest_mod.processed_subchapters(manifest, args.file)

    print(render_tree(structure, processed))

    start_time = time.monotonic()

    def finish(status: str, generated: int, requested: int, skipped: int = 0, error: str | None = None):
        entry = run_log.log_run({
            "script": "notes",
            "status": status,
            "source_file": args.file,
            "book_title": structure.title,
            "subject": args.subject,
            "num_requested": requested,
            "num_generated": generated,
            "num_skipped_already_processed": skipped,
            "model": model,
            "vault": str(vault_root),
            "duration_seconds": round(time.monotonic() - start_time, 1),
            **({"error": error} if error else {}),
        }, args.log)
        run_log.print_summary(entry)

    if args.all_chapters:
        full_selection = [(c, s) for c in structure.chapters for s in c.subchapters]
        selected = [(c, s) for c, s in full_selection if s.number not in processed]
        skipped = len(full_selection) - len(selected)
        print(f"\n--all-chapters: {len(selected)} new subchapter(s) to process "
              f"({skipped} already processed, skipped).")
        if not selected:
            print("Nothing new to process.")
            finish("completed", 0, 0, skipped=skipped)
            return
    else:
        selected = prompt_for_selection(structure)
        skipped = 0

    client = llm_client.get_client(args.lmstudio_url, timeout=args.timeout)

    known = build_known_notes(manifest, args.file, vault_root, structure, selected)
    by_chapter = group_by_chapter(known)

    print(f"\nGenerating notes via LM Studio ({model})...\n")

    generated = 0
    for chapter, subchapter in selected:
        print(f"  {subchapter.number} {subchapter.title} ...", end=" ", flush=True)

        siblings = by_chapter[real_chapter_number(chapter)]
        related_links = [vault.wikilink(vault_root, path) for sib, path in siblings if sib.number != subchapter.number]

        try:
            note_md = generate_subchapter_note(
                args.file, structure, chapter, subchapter, args.subject, client, model,
                max_tokens=args.max_tokens, related_links=related_links,
            )
        except llm_client.EmptyResponseError as e:
            print("FAILED (empty response)")
            print(f"\n{e}")
            manifest_mod.save_manifest(manifest, args.manifest)
            finish("failed", generated, len(selected), skipped=skipped, error=str(e))
            sys.exit(1)
        except openai.APITimeoutError:
            msg = f"LM Studio didn't respond within {args.timeout:.0f}s."
            print("FAILED (timed out)")
            print(f"\n{msg}")
            print("Check the LM Studio server window/log for this request — if it's still "
                  "generating, your hardware may just be slow for this model; re-run with "
                  "--timeout 600 (or higher). If the log shows it finished or errored, that's "
                  "a different problem — paste the log here.")
            manifest_mod.save_manifest(manifest, args.manifest)
            finish("timed_out", generated, len(selected), skipped=skipped, error=msg)
            sys.exit(1)
        except openai.APIConnectionError:
            msg = f"Couldn't reach LM Studio at {args.lmstudio_url or llm_client.DEFAULT_BASE_URL}."
            print("FAILED")
            print(f"\n{msg}")
            print("Make sure the LM Studio local server is running and the model is loaded, then re-run.")
            manifest_mod.save_manifest(manifest, args.manifest)
            finish("connection_failed", generated, len(selected), skipped=skipped, error=msg)
            sys.exit(1)

        out_path = known[subchapter.number][2]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(note_md, encoding="utf-8")
        manifest_mod.mark_processed(manifest, args.file, subchapter.number, str(out_path))
        generated += 1
        print(f"-> {out_path}")

    chapter_by_number = {real_chapter_number(c): c for c in structure.chapters}
    index_md = vault.render_index(vault_root, structure.title, structure, chapter_by_number, by_chapter)
    index_out = vault.index_path(vault_root, structure.title)
    index_out.parent.mkdir(parents=True, exist_ok=True)
    index_out.write_text(index_md, encoding="utf-8")

    manifest_mod.save_manifest(manifest, args.manifest)
    print(f"\n{generated} note(s) written under {vault.book_folder(vault_root, structure.title)}/")
    print(f"Index updated: {index_out}")
    finish("completed", generated, len(selected), skipped=skipped)


if __name__ == "__main__":
    main()
