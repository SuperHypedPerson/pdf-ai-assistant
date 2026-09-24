#!/usr/bin/env python3
"""
Local web UI for the textbook notes/quiz pipeline. Wraps the same plain
functions the CLI (notes.py/quiz.py) calls — no logic is duplicated here,
this is purely a presentation layer.

Run: python3 web/server.py
Then open http://localhost:8420
"""

from __future__ import annotations

import hashlib
import json
import queue as queue_mod
import sys
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread

from fastapi import FastAPI, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import crosslink as crosslink_mod  # noqa: E402
from src import llm_client  # noqa: E402
from src import manifest as manifest_mod  # noqa: E402
from src import notes_pipeline  # noqa: E402
from src import retry as retry_mod  # noqa: E402
from src import vault  # noqa: E402
from src.flashcards_generator import run_flashcards_generation  # noqa: E402
from src.note_generator import is_numbered_chapter  # noqa: E402
from src.quiz_generator import QuizParseError, generate_quiz, max_tokens_for as quiz_max_tokens_for  # noqa: E402
from src.structure_extractor import extract_structure  # noqa: E402

UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
MANIFEST_PATH = REPO_ROOT / "manifest.json"
DEFAULT_VAULT_PATH = r"C:\Users\Marcus\Desktop\Textbook Notes Summarizer"

app = FastAPI(title="Textbook Notes + Quiz Generator")

# In-memory job tracking — fine for a single-user local app.
jobs: dict[str, dict] = {}
# book_id -> parsed BookStructure (the dataclass, not its serialized dict) —
# reused by generation so it doesn't have to re-parse (and, for a scanned
# book, re-OCR the whole thing) after the picker already did it once.
structure_cache: dict[str, object] = {}


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


def _resolve_selection(structure, numbers: set[str]) -> list:
    return [(c, s) for c in structure.chapters for s in c.subchapters if s.number in numbers]


class JobStream:
    """Fan-out event log for one generation job: every event is kept, so a
    browser tab that reconnects mid-job (a refresh, a dropped connection)
    replays everything it missed before continuing live, instead of losing
    progress it can't get back. Multiple simultaneous subscribers are each
    given their own queue fed from the same published events."""

    def __init__(self):
        self.events: list = []
        self.subscribers: list[queue_mod.Queue] = []
        self.lock = Lock()

    def publish(self, event) -> None:
        with self.lock:
            self.events.append(event)
            for q in self.subscribers:
                q.put(event)

    def subscribe(self) -> queue_mod.Queue:
        q: queue_mod.Queue = queue_mod.Queue()
        with self.lock:
            for event in self.events:
                q.put(event)
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue_mod.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)


def _sse_stream(job_stream: "JobStream"):
    q = job_stream.subscribe()
    try:
        while True:
            item = q.get()
            if item is None:
                yield 'data: {"type": "stream_end"}\n\n'
                break
            yield f"data: {json.dumps(item)}\n\n"
    finally:
        job_stream.unsubscribe(q)


@app.get("/api/config")
def get_config():
    return {
        "default_vault": DEFAULT_VAULT_PATH,
        "default_model": llm_client.DEFAULT_MODEL,
        "default_lmstudio_url": llm_client.DEFAULT_BASE_URL,
        "default_timeout": llm_client.DEFAULT_TIMEOUT_SECONDS,
    }


@app.post("/api/upload")
async def upload_pdf(file: UploadFile):
    contents = await file.read()
    # Stable id from content, so re-uploading the same PDF reuses its manifest state.
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


def _run_parse_job(job_id: str, book_id: str, pdf_path: Path):
    try:
        structure = extract_structure(pdf_path)
        structure_cache[book_id] = structure
        structure_dict = asdict(structure)
        # Flag which chapters are the book's real numbered content vs.
        # front/back matter, so the picker can offer a --all-chapters-style
        # "select all real chapters" shortcut without re-deriving the
        # is_numbered_chapter() regex logic client-side in JS.
        for chap_dict, chap_obj in zip(structure_dict["chapters"], structure.chapters):
            chap_dict["is_numbered"] = is_numbered_chapter(chap_obj)
        jobs[job_id] = {"status": "done", "structure": structure_dict}
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI rather than crash silently
        jobs[job_id] = {"status": "error", "error": str(e)}


@app.post("/api/parse/{book_id}")
def start_parse(book_id: str):
    pdf_path = _book_path(book_id)
    if pdf_path is None:
        return JSONResponse({"error": "book not found"}, status_code=404)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running"}
    Thread(target=_run_parse_job, args=(job_id, book_id, pdf_path), daemon=True).start()
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


@app.get("/api/preview")
def preview_file(path: str):
    p = Path(path)
    if not p.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return {"content": p.read_text(encoding="utf-8")}


# --------------------------------------------------------------------------
# Notes generation
# --------------------------------------------------------------------------

class NotesGenerateRequest(BaseModel):
    book_id: str
    selection: list[str]
    subject: str
    vault: str | None = None
    lmstudio_url: str | None = None
    model: str | None = None
    timeout: float | None = None
    max_tokens: int | None = None


@app.post("/api/notes/generate")
def start_notes_generation(body: NotesGenerateRequest):
    pdf_path = _book_path(body.book_id)
    if pdf_path is None:
        return JSONResponse({"error": "book not found"}, status_code=404)

    structure = structure_cache.get(body.book_id) or extract_structure(pdf_path)
    structure_cache[body.book_id] = structure
    selected = _resolve_selection(structure, set(body.selection))
    if not selected:
        return JSONResponse({"error": "no matching subchapters in selection"}, status_code=400)

    vault_root = Path(body.vault or DEFAULT_VAULT_PATH)
    manifest = manifest_mod.load_manifest(MANIFEST_PATH)
    manifest_mod.ensure_book_entry(manifest, pdf_path, structure.title)

    client = llm_client.get_client(body.lmstudio_url, timeout=body.timeout or llm_client.DEFAULT_TIMEOUT_SECONDS)
    model = body.model or llm_client.get_model_name()

    job_id = str(uuid.uuid4())
    job_stream = JobStream()
    jobs[job_id] = job_stream

    def worker():
        try:
            for event in notes_pipeline.run_notes_generation(
                str(pdf_path), structure, selected, body.subject, vault_root,
                manifest, client, model, max_tokens=body.max_tokens, lmstudio_url=body.lmstudio_url,
            ):
                job_stream.publish(event)
            manifest_mod.save_manifest(manifest, MANIFEST_PATH)
        except Exception as e:  # noqa: BLE001
            job_stream.publish({"type": "fatal_error", "error": str(e)})
        finally:
            job_stream.publish(None)

    Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/notes/generate/stream/{job_id}")
def stream_notes_generation(job_id: str):
    job_stream = jobs.get(job_id)
    if job_stream is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return StreamingResponse(_sse_stream(job_stream), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Quiz generation
# --------------------------------------------------------------------------

class QuizGenerateRequest(BaseModel):
    book_id: str
    selection: list[str]
    subject: str
    difficulty: str = "medium"
    num_questions: int = 8
    vault: str | None = None
    lmstudio_url: str | None = None
    model: str | None = None
    timeout: float | None = None
    max_tokens: int | None = None


@app.post("/api/quiz/generate")
def start_quiz_generation(body: QuizGenerateRequest):
    pdf_path = _book_path(body.book_id)
    if pdf_path is None:
        return JSONResponse({"error": "book not found"}, status_code=404)

    structure = structure_cache.get(body.book_id) or extract_structure(pdf_path)
    structure_cache[body.book_id] = structure
    selected = _resolve_selection(structure, set(body.selection))
    if not selected:
        return JSONResponse({"error": "no matching subchapters in selection"}, status_code=400)

    vault_root = Path(body.vault or DEFAULT_VAULT_PATH)
    client = llm_client.get_client(body.lmstudio_url, timeout=body.timeout or llm_client.DEFAULT_TIMEOUT_SECONDS)
    model = body.model or llm_client.get_model_name()

    job_id = str(uuid.uuid4())
    job_stream = JobStream()
    jobs[job_id] = job_stream

    def worker():
        try:
            job_stream.publish({"type": "generating", "num_questions": body.num_questions,
                                 "difficulty": body.difficulty})
            initial_tokens = body.max_tokens or quiz_max_tokens_for(body.num_questions)
            stream = retry_mod.call_with_retry_stream(
                lambda mt: generate_quiz(str(pdf_path), structure, selected, body.subject, body.difficulty,
                                          body.num_questions, client, model, max_tokens=mt),
                initial_tokens,
            )
            quiz_md, delivered = None, 0
            for event in stream:
                if event[0] == "retry":
                    _, attempt, reason, next_tokens = event
                    job_stream.publish({"type": "retry", "attempt": attempt, "reason": reason,
                                         "next_max_tokens": next_tokens})
                elif event[0] == "success":
                    quiz_md, delivered = event[1]

            stem = f"{structure.title} Quiz {datetime.now().strftime('%Y-%m-%d %H%M')}"
            out_path = vault.quiz_path(vault_root, structure.title, stem)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(quiz_md, encoding="utf-8")
            job_stream.publish({"type": "done", "path": str(out_path), "delivered": delivered,
                                 "requested": body.num_questions})
        except QuizParseError as e:
            job_stream.publish({"type": "fatal_error", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            job_stream.publish({"type": "fatal_error", "error": str(e)})
        finally:
            job_stream.publish(None)

    Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/quiz/generate/stream/{job_id}")
def stream_quiz_generation(job_id: str):
    job_stream = jobs.get(job_id)
    if job_stream is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return StreamingResponse(_sse_stream(job_stream), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Flashcards generation — derived from already-generated notes, no LLM call
# --------------------------------------------------------------------------

class FlashcardsGenerateRequest(BaseModel):
    book_id: str
    selection: list[str]
    vault: str | None = None


@app.post("/api/flashcards/generate")
def start_flashcards_generation(body: FlashcardsGenerateRequest):
    pdf_path = _book_path(body.book_id)
    if pdf_path is None:
        return JSONResponse({"error": "book not found"}, status_code=404)

    structure = structure_cache.get(body.book_id) or extract_structure(pdf_path)
    structure_cache[body.book_id] = structure
    selected = _resolve_selection(structure, set(body.selection))
    if not selected:
        return JSONResponse({"error": "no matching subchapters in selection"}, status_code=400)

    vault_root = Path(body.vault or DEFAULT_VAULT_PATH)
    manifest = manifest_mod.load_manifest(MANIFEST_PATH)
    manifest_mod.ensure_book_entry(manifest, pdf_path, structure.title)

    job_id = str(uuid.uuid4())
    job_stream = JobStream()
    jobs[job_id] = job_stream

    def worker():
        try:
            for event in run_flashcards_generation(str(pdf_path), structure, selected, vault_root, manifest):
                job_stream.publish(event)
            manifest_mod.save_manifest(manifest, MANIFEST_PATH)
        except Exception as e:  # noqa: BLE001
            job_stream.publish({"type": "fatal_error", "error": str(e)})
        finally:
            job_stream.publish(None)

    Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/flashcards/generate/stream/{job_id}")
def stream_flashcards_generation(job_id: str):
    job_stream = jobs.get(job_id)
    if job_stream is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return StreamingResponse(_sse_stream(job_stream), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Cross-book linking — vault-wide, not scoped to one book's selection
# --------------------------------------------------------------------------

class CrosslinkRequest(BaseModel):
    vault: str | None = None


@app.post("/api/crosslink/run")
def run_crosslink(body: CrosslinkRequest):
    vault_root = Path(body.vault or DEFAULT_VAULT_PATH)
    if not vault_root.is_dir():
        return JSONResponse({"error": f"vault path not found: {vault_root}"}, status_code=400)
    manifest = manifest_mod.load_manifest(MANIFEST_PATH)
    summary = crosslink_mod.apply_cross_links(vault_root, manifest)
    return summary


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    print("Starting server at http://localhost:8420")
    uvicorn.run(app, host="127.0.0.1", port=8420)
