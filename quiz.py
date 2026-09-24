#!/usr/bin/env python3
"""
quiz.py --file book.pdf --subject SUBJECT [--num-questions N] [--difficulty easy|medium|hard]

Parses the PDF's structure, shows it to you, lets you pick which
chapters/subchapters to quiz yourself on (same picker as notes.py), and
generates one quiz grounded strictly in that selection's actual extracted
text via a local LM Studio model.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import openai

from src import llm_client
from src import manifest as manifest_mod
from src import retry as retry_mod
from src import run_log
from src import vault
from src.quiz_generator import QuizParseError, generate_quiz, max_tokens_for
from src.selection import confirm_text, parse_selection, render_tree
from src.structure_extractor import extract_structure

DEFAULT_VAULT_PATH = r"C:\Users\Marcus\Desktop\Textbook Notes Summarizer"


def prompt_for_selection(structure) -> list:
    while True:
        raw = input("\nSelect chapters/subchapters to quiz on (e.g. '3, 5.1-5.4, 7'): ").strip()
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
    parser = argparse.ArgumentParser(description="Generate a quiz from selected chapters of a PDF textbook.")
    parser.add_argument("--file", required=True, help="Path to the PDF file")
    parser.add_argument("--subject", required=True, help="Subject-area tag applied to the quiz (e.g. finance)")
    parser.add_argument("--vault", default=DEFAULT_VAULT_PATH, help="Path to your Obsidian vault")
    parser.add_argument("--num-questions", type=int, default=8, help="Total number of questions (default: 8)")
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard"], default="medium",
                         help="Question difficulty (default: medium)")
    parser.add_argument("--manifest", default=str(manifest_mod.DEFAULT_MANIFEST_PATH),
                         help="Path to the manifest JSON file (used only to show already-noted subchapters)")
    parser.add_argument("--log", default=str(run_log.DEFAULT_LOG_PATH),
                         help="Path to the run-summary log file")
    parser.add_argument("--lmstudio-url", default=None,
                         help=f"LM Studio base URL (default: {llm_client.DEFAULT_BASE_URL})")
    parser.add_argument("--model", default=None,
                         help=f"Model name as loaded in LM Studio (default: {llm_client.DEFAULT_MODEL})")
    parser.add_argument("--timeout", type=float, default=llm_client.DEFAULT_TIMEOUT_SECONDS,
                         help=f"Seconds to wait for LM Studio (default: {llm_client.DEFAULT_TIMEOUT_SECONDS:.0f})")
    parser.add_argument("--max-tokens", type=int, default=None,
                         help="Fixed output token budget (default: auto-scaled by --num-questions)")
    args = parser.parse_args()

    if args.num_questions < 1:
        parser.error("--num-questions must be at least 1")

    vault_root = Path(args.vault)
    structure = extract_structure(args.file)

    manifest = manifest_mod.load_manifest(args.manifest)
    manifest_mod.ensure_book_entry(manifest, args.file, structure.title)
    processed = manifest_mod.processed_subchapters(manifest, args.file)

    print(render_tree(structure, processed))

    selected = prompt_for_selection(structure)
    start_time = time.monotonic()

    client = llm_client.get_client(args.lmstudio_url, timeout=args.timeout)
    model = args.model or llm_client.get_model_name()

    def finish(status: str, delivered: int = 0, error: str | None = None):
        entry = run_log.log_run({
            "script": "quiz",
            "status": status,
            "source_file": args.file,
            "book_title": structure.title,
            "subject": args.subject,
            "difficulty": args.difficulty,
            "selection": ", ".join(s.number for _, s in selected),
            "num_requested": args.num_questions,
            "num_delivered": delivered,
            "model": model,
            "vault": str(vault_root),
            "duration_seconds": round(time.monotonic() - start_time, 1),
            **({"error": error} if error else {}),
        }, args.log)
        run_log.print_summary(entry)

    print(f"\nGenerating a {args.num_questions}-question ({args.difficulty}) quiz via LM Studio ({model})...")

    def on_retry(attempt: int, reason: str, next_tokens: int) -> None:
        label = "hit its token limit" if reason == "token_limit" else "timed out"
        print(f"  {label} (attempt {attempt}/{retry_mod.MAX_ATTEMPTS}) — retrying with max_tokens={next_tokens}...")

    try:
        quiz_md, delivered = retry_mod.call_with_retry(
            lambda mt: generate_quiz(
                args.file, structure, selected, args.subject, args.difficulty,
                args.num_questions, client, model, max_tokens=mt,
            ),
            initial_max_tokens=args.max_tokens or max_tokens_for(args.num_questions),
            on_retry=on_retry,
        )
        if delivered < args.num_questions:
            print(f"\nNote: {delivered}/{args.num_questions} questions delivered — "
                  f"the model returned some in an unexpected format; see the warning in the quiz file.")
    except QuizParseError as e:
        print("FAILED (couldn't parse the model's response)")
        print(f"\n{e}")
        finish("failed", error=str(e))
        sys.exit(1)
    except llm_client.EmptyResponseError as e:
        print("FAILED (empty response, exhausted retries)")
        print(f"\n{e}")
        finish("failed", error=str(e))
        sys.exit(1)
    except openai.APITimeoutError:
        msg = f"LM Studio didn't respond within {args.timeout:.0f}s (exhausted retries)."
        print("FAILED (timed out)")
        print(f"\n{msg}")
        print("Check the LM Studio server window/log — try --timeout 600 if it's just slow, "
              "or a smaller --num-questions/selection if the context is too large.")
        finish("timed_out", error=msg)
        sys.exit(1)
    except openai.APIConnectionError:
        msg = f"Couldn't reach LM Studio at {args.lmstudio_url or llm_client.DEFAULT_BASE_URL}."
        print("FAILED")
        print(f"\n{msg}")
        print("Make sure the LM Studio local server is running and the model is loaded, then re-run.")
        finish("connection_failed", error=msg)
        sys.exit(1)

    stem = f"{structure.title} Quiz {datetime.now().strftime('%Y-%m-%d %H%M')}"
    out_path = vault.quiz_path(vault_root, structure.title, stem)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(quiz_md, encoding="utf-8")

    print(f"\nQuiz written to {out_path}")
    finish("completed", delivered)


if __name__ == "__main__":
    main()
