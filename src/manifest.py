"""
Local state tracking: which PDFs have been parsed, and which
chapters/subchapters already have generated notes.

Backed by a single JSON file so re-runs can skip/update intelligently and
the picker can show what's already done vs. new.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MANIFEST_PATH = Path("manifest.json")


def book_id(pdf_path: str | Path) -> str:
    """Stable identifier for a book, based on its file content — not its
    path, so the same PDF is recognized as the same book regardless of
    where it lives (e.g. the CLI pointed at your local copy vs. the web
    app's uploaded copy in web/uploads/)."""
    return hashlib.sha1(Path(pdf_path).read_bytes()).hexdigest()[:16]


def _migrate_legacy_entries(manifest: dict) -> None:
    """Older manifests keyed books by resolved file path instead of
    content. Re-key any such entry (in place) under its correct
    content-based id, merging into an existing entry if one's already
    there, so already-processed subchapters aren't silently forgotten
    just because this identity scheme changed."""
    for old_key in list(manifest["books"].keys()):
        entry = manifest["books"][old_key]
        source_file = entry.get("source_file")
        if not source_file or not Path(source_file).exists():
            continue
        try:
            correct_key = book_id(source_file)
        except OSError:
            continue
        if correct_key == old_key:
            continue

        target = manifest["books"].setdefault(correct_key, entry)
        if target is not entry:
            target["subchapters"].update(entry["subchapters"])
        del manifest["books"][old_key]


def load_manifest(path: str | Path = DEFAULT_MANIFEST_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {"books": {}}
    manifest = json.loads(path.read_text())
    _migrate_legacy_entries(manifest)
    return manifest


def save_manifest(manifest: dict, path: str | Path = DEFAULT_MANIFEST_PATH) -> None:
    path = Path(path)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))


def ensure_book_entry(manifest: dict, pdf_path: str | Path, title: str) -> dict:
    bid = book_id(pdf_path)
    if bid not in manifest["books"]:
        manifest["books"][bid] = {
            "source_file": str(Path(pdf_path).resolve()),
            "title": title,
            "first_parsed": datetime.now(timezone.utc).isoformat(),
            "subchapters": {},  # "3.2" -> {status, note_path, updated}
        }
    return manifest["books"][bid]


def processed_subchapters(manifest: dict, pdf_path: str | Path) -> set[str]:
    bid = book_id(pdf_path)
    book = manifest["books"].get(bid)
    if not book:
        return set()
    return {num for num, info in book["subchapters"].items() if info.get("status") == "processed"}


def existing_note_path(manifest: dict, pdf_path: str | Path, sub_number: str) -> str | None:
    bid = book_id(pdf_path)
    book = manifest["books"].get(bid)
    if not book:
        return None
    entry = book["subchapters"].get(sub_number)
    return entry.get("note_path") if entry else None


def mark_processed(manifest: dict, pdf_path: str | Path, sub_number: str, note_path: str) -> None:
    bid = book_id(pdf_path)
    book = manifest["books"][bid]
    book["subchapters"][sub_number] = {
        "status": "processed",
        "note_path": note_path,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
