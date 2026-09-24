"""
Shared notes-generation pipeline used by both notes.py (CLI) and the web
app's generation endpoint — the exact loop that turns a selection into
written note files, an updated book index, and manifest updates, kept in
exactly one place so the two interfaces can never drift apart.

Yields structured progress events rather than printing, so each caller
renders them its own way: notes.py prints terminal lines, the web app
turns them into Server-Sent Events for a live progress view in the browser.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterator

import openai

from src import manifest as manifest_mod
from src import retry as retry_mod
from src import vault
from src.llm_client import EmptyResponseError
from src.note_generator import (
    chapter_display_label,
    chapter_group_key,
    generate_subchapter_note,
    max_tokens_for,
    subchapter_title_rest,
)


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
        by_chapter[chapter_group_key(chap)].append((sub, path))
    return by_chapter


def run_notes_generation(pdf_path: str, structure, selected: list, subject: str, vault_root: Path,
                          manifest: dict, client, model: str, max_tokens: int | None = None,
                          lmstudio_url: str | None = None) -> Iterator[dict]:
    """Generates one note per (chapter, subchapter) in `selected`, in order.
    Mutates `manifest` in place (mark_processed) but does not save it to
    disk — the caller does that once the generator is exhausted, so it
    controls the manifest file path. Yields:
      {"type": "chapter_start", "label": str}
      {"type": "subchapter_start", "number": str, "title": str}
      {"type": "retry", "number": str, "attempt": int, "reason": str, "next_max_tokens": int}
      {"type": "subchapter_done", "number": str, "path": str}
      {"type": "subchapter_failed", "number": str, "reason": str}
      {"type": "connection_lost", "message": str}
      {"type": "index_written", "path": str}
      {"type": "done", "generated": int, "failed": list[str], "connection_lost": bool}
    """
    known = build_known_notes(manifest, pdf_path, vault_root, structure, [])
    by_chapter = group_by_chapter(known)

    generated = 0
    failed: list[str] = []
    connection_lost = False
    current_group_key = None

    for chapter, subchapter in selected:
        group_key = chapter_group_key(chapter)
        if group_key != current_group_key:
            current_group_key = group_key
            yield {"type": "chapter_start", "label": chapter_display_label(chapter)}

        title = subchapter_title_rest(subchapter)
        yield {"type": "subchapter_start", "number": subchapter.number, "title": title}

        siblings = by_chapter[group_key]
        related_links = [vault.wikilink(vault_root, path) for sib, path in siblings if sib.number != subchapter.number]
        resolved_path = resolve_note_path(manifest, pdf_path, vault_root, structure.title, chapter, subchapter)

        def attempt(mt: int) -> str:
            return generate_subchapter_note(
                pdf_path, structure, chapter, subchapter, subject, client, model,
                max_tokens=mt, related_links=related_links,
            )

        try:
            stream = retry_mod.call_with_retry_stream(attempt, max_tokens or max_tokens_for(subchapter))
            note_md = None
            for event in stream:
                if event[0] == "retry":
                    _, attempt_num, reason, next_tokens = event
                    yield {"type": "retry", "number": subchapter.number, "attempt": attempt_num,
                           "reason": reason, "next_max_tokens": next_tokens}
                elif event[0] == "success":
                    note_md = event[1]
        except EmptyResponseError:
            yield {"type": "subchapter_failed", "number": subchapter.number, "reason": "empty_response"}
            failed.append(subchapter.number)
            continue
        except openai.APITimeoutError:
            yield {"type": "subchapter_failed", "number": subchapter.number, "reason": "timeout"}
            failed.append(subchapter.number)
            continue
        except openai.APIConnectionError:
            yield {"type": "connection_lost",
                   "message": f"Couldn't reach LM Studio at {lmstudio_url or 'the configured URL'}."}
            connection_lost = True
            break

        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_text(note_md, encoding="utf-8")
        manifest_mod.mark_processed(manifest, pdf_path, subchapter.number, str(resolved_path))
        known[subchapter.number] = (chapter, subchapter, resolved_path)
        by_chapter[group_key].append((subchapter, resolved_path))
        generated += 1
        yield {"type": "subchapter_done", "number": subchapter.number, "path": str(resolved_path)}

    chapter_by_key = {chapter_group_key(c): c for c in structure.chapters}
    index_md = vault.render_index(vault_root, structure.title, structure, chapter_by_key, by_chapter)
    index_out = vault.index_path(vault_root, structure.title)
    index_out.parent.mkdir(parents=True, exist_ok=True)
    index_out.write_text(index_md, encoding="utf-8")
    yield {"type": "index_written", "path": str(index_out)}

    yield {"type": "done", "generated": generated, "failed": failed, "connection_lost": connection_lost}
