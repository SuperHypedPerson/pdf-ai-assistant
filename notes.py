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
from src import retry as retry_mod
from src import run_log
from src import vault
from src.note_generator import chapter_title_rest, generate_subchapter_note, max_tokens_for, real_chapter_number
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

    # Start from only what's already on record — a subchapter joins known/
    # by_chapter (and so becomes visible as a sibling / index entry) only
    # once its note is actually written, so a failed subchapter never ends
    # up linked from the index or another note's Related section.
    known = build_known_notes(manifest, args.file, vault_root, structure, [])
    by_chapter = group_by_chapter(known)

    print(f"\nGenerating notes via LM Studio ({model})...")

    generated = 0
    failed: list[str] = []
    connection_lost = False
    current_chapter_num = None

    for chapter, subchapter in selected:
        chap_num = real_chapter_number(chapter)
        if chap_num != current_chapter_num:
            current_chapter_num = chap_num
            print(f"\n=== Chapter {chap_num} - {chapter_title_rest(chapter)} ===")

        print(f"  {subchapter.number} {subchapter.title} ...", end=" ", flush=True)

        siblings = by_chapter[chap_num]
        related_links = [vault.wikilink(vault_root, path) for sib, path in siblings if sib.number != subchapter.number]
        resolved_path = resolve_note_path(manifest, args.file, vault_root, structure.title, chapter, subchapter)

        def on_retry(attempt: int, reason: str, next_tokens: int) -> None:
            label = "hit its token limit" if reason == "token_limit" else "timed out"
            print(f"\n    {label} (attempt {attempt}/{retry_mod.MAX_ATTEMPTS}) "
                  f"— retrying with max_tokens={next_tokens}...", end=" ", flush=True)

        try:
            note_md = retry_mod.call_with_retry(
                lambda mt: generate_subchapter_note(
                    args.file, structure, chapter, subchapter, args.subject, client, model,
                    max_tokens=mt, related_links=related_links,
                ),
                initial_max_tokens=args.max_tokens or max_tokens_for(subchapter),
                on_retry=on_retry,
            )
        except llm_client.EmptyResponseError as e:
            print("FAILED (empty response, exhausted retries) — skipping, will retry on next run")
            failed.append(subchapter.number)
            continue
        except openai.APITimeoutError:
            print("FAILED (timed out, exhausted retries) — skipping, will retry on next run")
            failed.append(subchapter.number)
            continue
        except openai.APIConnectionError:
            msg = f"Couldn't reach LM Studio at {args.lmstudio_url or llm_client.DEFAULT_BASE_URL}."
            print("FAILED")
            print(f"\n{msg}")
            print("Make sure the LM Studio local server is running and the model is loaded. "
                  "Stopping here — re-running will pick up where this left off (already-processed "
                  "subchapters are skipped automatically).")
            connection_lost = True
            break

        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(note_md, encoding="utf-8")
        manifest_mod.mark_processed(manifest, args.file, subchapter.number, str(resolved_path))
        known[subchapter.number] = (chapter, subchapter, resolved_path)
        by_chapter[chap_num].append((subchapter, resolved_path))
        generated += 1
        print(f"-> {resolved_path}")

    chapter_by_number = {real_chapter_number(c): c for c in structure.chapters}
    index_md = vault.render_index(vault_root, structure.title, structure, chapter_by_number, by_chapter)
    index_out = vault.index_path(vault_root, structure.title)
    index_out.parent.mkdir(parents=True, exist_ok=True)
    index_out.write_text(index_md, encoding="utf-8")

    manifest_mod.save_manifest(manifest, args.manifest)
    print(f"\n{generated} note(s) written under {vault.book_folder(vault_root, structure.title)}/")
    print(f"Index updated: {index_out}")
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
