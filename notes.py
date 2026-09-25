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
from pathlib import Path

import openai

from src import llm_client
from src import manifest as manifest_mod
from src import run_log
from src import vault
from src.note_generator import is_numbered_chapter
from src.notes_pipeline import run_notes_generation
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
    parser = argparse.ArgumentParser(description="Generate Obsidian notes from selected chapters of a PDF textbook.")
    parser.add_argument("--file", required=True, help="Path to the PDF file")
    parser.add_argument("--subject", help="Subject-area tag applied to generated notes (e.g. finance)")
    parser.add_argument("--vault", default=DEFAULT_VAULT_PATH, help="Path to your Obsidian vault")
    parser.add_argument("--all-chapters", action="store_true",
                         help="Process every not-yet-processed subchapter in the book's real numbered "
                              "chapters without prompting (front/back matter — cover, contents, preface, "
                              "conclusion, bibliography, index, etc. — is skipped by default; see "
                              "--include-front-matter)")
    parser.add_argument("--include-front-matter", action="store_true",
                         help="With --all-chapters, also process front/back matter (unnumbered chapters)")
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

    def finish(status: str, generated: int, requested: int, skipped: int = 0,
               failed: list | None = None, error: str | None = None):
        entry = run_log.log_run({
            "script": "notes",
            "status": status,
            "source_file": args.file,
            "book_title": structure.title,
            "subject": args.subject,
            "num_requested": requested,
            "num_generated": generated,
            "num_skipped_already_processed": skipped,
            "num_failed": len(failed) if failed else 0,
            "failed_subchapters": failed or [],
            "model": model,
            "vault": str(vault_root),
            "duration_seconds": round(time.monotonic() - start_time, 1),
            **({"error": error} if error else {}),
        }, args.log)
        run_log.print_summary(entry)

    if args.all_chapters:
        eligible_chapters = structure.chapters if args.include_front_matter else \
            [c for c in structure.chapters if is_numbered_chapter(c)]
        front_matter_excluded = len(structure.chapters) - len(eligible_chapters)
        full_selection = [(c, s) for c in eligible_chapters for s in c.subchapters]
        selected = [(c, s) for c, s in full_selection if s.number not in processed]
        skipped = len(full_selection) - len(selected)
        print(f"\n--all-chapters: {len(selected)} new subchapter(s) to process "
              f"({skipped} already processed, skipped)"
              + (f", {front_matter_excluded} front/back-matter chapter(s) excluded" if front_matter_excluded else "")
              + ".")
        if not selected:
            print("Nothing new to process.")
            finish("completed", 0, 0, skipped=skipped)
            return
    else:
        selected = prompt_for_selection(structure)
        skipped = 0

    client = llm_client.get_client(args.lmstudio_url, timeout=args.timeout)

    print(f"\nGenerating notes via LM Studio ({model})...")

    generated = 0
    failed: list[str] = []
    connection_lost = False

    for event in run_notes_generation(args.file, structure, selected, args.subject, vault_root,
                                       manifest, client, model, max_tokens=args.max_tokens,
                                       lmstudio_url=args.lmstudio_url):
        etype = event["type"]
        if etype == "chapter_start":
            print(f"\n=== {event['label']} ===")
        elif etype == "subchapter_start":
            print(f"  {event['number']} {event['title']} ...", end=" ", flush=True)
        elif etype == "retry":
            label = "hit its token limit" if event["reason"] == "token_limit" else "timed out"
            print(f"\n    {label} (attempt {event['attempt']}/3) "
                  f"— retrying with max_tokens={event['next_max_tokens']}...", end=" ", flush=True)
        elif etype == "subchapter_done":
            print(f"-> {event['path']}")
            # Save immediately rather than waiting for the whole run to
            # finish, so a kill mid-run doesn't lose track of subchapters
            # already written to disk (which would otherwise get silently
            # regenerated and overwritten on the next run).
            manifest_mod.save_manifest(manifest, args.manifest)
        elif etype == "subchapter_failed":
            reason = "empty response" if event["reason"] == "empty_response" else "timed out"
            print(f"FAILED ({reason}, exhausted retries) — skipping, will retry on next run")
        elif etype == "connection_lost":
            print("FAILED")
            print(f"\n{event['message']}")
            print("Make sure the LM Studio local server is running and the model is loaded. "
                  "Stopping here — re-running will pick up where this left off (already-processed "
                  "subchapters are skipped automatically).")
        elif etype == "index_written":
            print(f"Index updated: {event['path']}")
        elif etype == "done":
            generated = event["generated"]
            failed = event["failed"]
            connection_lost = event["connection_lost"]

    manifest_mod.save_manifest(manifest, args.manifest)
    print(f"\n{generated} note(s) written under {vault.book_folder(vault_root, structure.title)}/")
    if failed:
        print(f"{len(failed)} subchapter(s) failed and were skipped: {', '.join(failed)}")
        print("Re-run the same command to retry just those (everything else will be skipped).")

    if connection_lost:
        finish("connection_failed", generated, len(selected), skipped=skipped, failed=failed,
               error="LM Studio became unreachable mid-run")
        sys.exit(1)
    finish("completed" if not failed else "completed_with_failures",
           generated, len(selected), skipped=skipped, failed=failed)


if __name__ == "__main__":
    main()
