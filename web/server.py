#!/usr/bin/env python3
"""
Local web UI for the textbook notes/quiz pipeline. Wraps the same plain
functions the CLI (notes.py/quiz.py) calls — no logic is duplicated here,
this is purely a presentation layer.

Run: python3 web/server.py
Then open http://localhost:8420
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import manifest as manifest_mod  # noqa: E402
from src.structure_extractor import extract_structure  # noqa: E402

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
MANIFEST_PATH = REPO_ROOT / "manifest.json"

app = FastAPI(title="Textbook Notes + Quiz Generator")

# In-memory job tracking — fine for a single-user local app.
jobs: dict[str, dict] = {}


def _book_path(book_id: str) -> Path | None:
    # One subdirectory per book id, so the stored file keeps its original,
    # clean filename — extract_structure()'s title fallback reads the
    # file's own stem, and a hash-prefixed filename would leak into that
    # title whenever a PDF has no embedded metadata title.
    book_dir = UPLOAD_DIR / book_id
    if not book_dir.is_dir():
        return None
    matches = list(book_dir.glob("*.pdf"))
    return matches[0] if matches else None


@app.post("/api/upload")
async def upload_pdf(file: UploadFile):
    contents = await file.read()
    # Stable id from content, so re-uploading the same PDF reuses its manifest state.
    import hashlib
    content_hash = hashlib.sha1(contents).hexdigest()[:16]
    safe_name = "".join(c for c in file.filename if c.isalnum() or c in " ._-()") or "book.pdf"
    book_dir = UPLOAD_DIR / content_hash
    book_dir.mkdir(exist_ok=True)
    dest = book_dir / safe_name
    if not dest.exists():
        dest.write_bytes(contents)
    return {"book_id": content_hash, "filename": safe_name}


@app.get("/api/books")
def list_books():
    books = []
    for book_dir in UPLOAD_DIR.iterdir():
        if not book_dir.is_dir():
            continue
        pdfs = list(book_dir.glob("*.pdf"))
        if pdfs:
            books.append({"book_id": book_dir.name, "filename": pdfs[0].name})
    return books


def _run_parse_job(job_id: str, pdf_path: Path):
    try:
        structure = extract_structure(pdf_path)
        jobs[job_id] = {"status": "done", "structure": asdict(structure)}
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI rather than crash silently
        jobs[job_id] = {"status": "error", "error": str(e)}


@app.post("/api/parse/{book_id}")
def start_parse(book_id: str):
    pdf_path = _book_path(book_id)
    if pdf_path is None:
        return JSONResponse({"error": "book not found"}, status_code=404)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running"}
    Thread(target=_run_parse_job, args=(job_id, pdf_path), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/parse/status/{job_id}")
def parse_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return job


@app.get("/api/manifest/{book_id}")
def get_manifest(book_id: str):
    pdf_path = _book_path(book_id)
    if pdf_path is None:
        return JSONResponse({"error": "book not found"}, status_code=404)
    manifest = manifest_mod.load_manifest(MANIFEST_PATH)
    processed = manifest_mod.processed_subchapters(manifest, pdf_path)
    return {"processed": sorted(processed)}


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    print("Starting server at http://localhost:8420")
    uvicorn.run(app, host="127.0.0.1", port=8420)
