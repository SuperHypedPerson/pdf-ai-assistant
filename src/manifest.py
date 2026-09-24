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
    """Stable identifier for a book, based on its resolved absolute path."""
    resolved = str(Path(pdf_path).resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def load_manifest(path: str | Path = DEFAULT_MANIFEST_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {"books": {}}
    return json.loads(path.read_text())


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
