"""
Stage 1 — shared PDF structure-extraction layer.

Extracts a chapter -> subchapter -> page-range tree from a textbook PDF.
Used by both notes.py and quiz.py (not built yet).

Priority order:
    1. Embedded PDF outline/bookmarks (fitz TOC), if present and usable.
    2. Heuristic heading detection (font size, boldness, numbering patterns).
Scanned/image-only pages are detected and OCR'd (pytesseract) so heading
text can still be found on them.

Anything that can't be confidently placed is recorded in `ambiguous`
rather than silently dropped or guessed.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

# Fraction of extractable characters below which a page is considered
# to have no usable text layer (i.e. likely scanned).
OCR_TEXT_THRESHOLD_CHARS = 20

# A span must be at least this many times the document's median body
# font size to be considered a heading candidate on size alone.
HEADING_SIZE_RATIO = 1.15

# An OCR'd "Chapter N ..." title longer than this is more likely a fused
# header+body-text OCR artifact than a real (normally short) chapter title.
OCR_TITLE_SUSPECT_LENGTH = 50

CHAPTER_NUMBER_RE = re.compile(
    r"^(chapter|ch\.?|unit|part)\s+(\d+)\b[\s:.\-–—]*(.*)$", re.IGNORECASE
)
BARE_CHAPTER_RE = re.compile(r"^(\d{1,3})\.?\s+(\S.*)$")
SUBCHAPTER_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.?\s+(\S.*)$")
DEEPER_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.?\s+(\S.*)$")

# Appendices are lettered (A, B, ...), not numbered, and common enough in
# math/CS textbooks that dropping them silently would be a real data loss.
APPENDIX_CHAPTER_RE = re.compile(r"^appendix\s+([A-Z])\b[\s:.\-–—]*(.*)$", re.IGNORECASE)
APPENDIX_SUBCHAPTER_RE = re.compile(r"^([A-Z])\.(\d{1,3})\.?\s+(\S.*)$")


@dataclass
class SubChapter:
    number: str
    title: str
    page_start: int
    page_end: int
    confidence: str  # "high" | "low"


@dataclass
class Chapter:
    number: str
    title: str
    page_start: int
    page_end: int
    confidence: str
    subchapters: list[SubChapter] = field(default_factory=list)


@dataclass
class BookStructure:
    source_file: str
    title: str
    total_pages: int
    method: str  # "outline" | "heuristic"
    scanned_pages: list[int]
    chapters: list[Chapter] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Scanned-page / OCR handling
# --------------------------------------------------------------------------

def detect_scanned_pages(doc: fitz.Document) -> list[int]:
    """Return 1-indexed page numbers that have no usable extractable text."""
    scanned = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if len(text) < OCR_TEXT_THRESHOLD_CHARS:
            scanned.append(i + 1)
    return scanned


def ocr_page_text(doc: fitz.Document, page_index: int, zoom: float = 2.0) -> str:
    """OCR a single page (0-indexed) via pytesseract. Returns '' if OCR unavailable."""
    try:
        import pytesseract
        from PIL import Image
        import io
    except ImportError:
        return ""

    page = doc[page_index]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Method 1: embedded outline / bookmarks
# --------------------------------------------------------------------------

def extract_from_outline(doc: fitz.Document) -> Optional[BookStructure]:
    toc = doc.get_toc(simple=True)  # [[level, title, page], ...], page is 1-indexed
    if not toc:
        return None

    # Need at least a two-level hierarchy to be useful; a flat single-level
    # TOC (all level 1) is treated as chapters-only with synthetic subchapters.
    levels_present = sorted(set(entry[0] for entry in toc))
    total_pages = doc.page_count
    chapter_level = levels_present[0]
    sub_level = levels_present[1] if len(levels_present) > 1 else None

    ambiguous: list[dict] = []
    chapters: list[Chapter] = []

    # Filter to top-two levels only; deeper levels are folded/flagged.
    entries = [e for e in toc if e[0] in (chapter_level, sub_level)] if sub_level else \
        [e for e in toc if e[0] == chapter_level]

    deeper = [e for e in toc if sub_level and e[0] not in (chapter_level, sub_level)]
    for lvl, title, page in deeper:
        ambiguous.append({
            "reason": "outline entry deeper than 2 levels was folded into its parent subchapter",
            "title": title,
            "page": page,
        })

    current_chapter: Optional[Chapter] = None
    chapter_idx = 0
    for i, (lvl, title, page) in enumerate(entries):
        next_page = entries[i + 1][2] if i + 1 < len(entries) else total_pages + 1
        page_end = max(page, next_page - 1)

        if lvl == chapter_level:
            chapter_idx += 1
            current_chapter = Chapter(
                number=str(chapter_idx),
                title=title.strip(),
                page_start=page,
                page_end=page_end,  # provisional; corrected below once we know last subchapter
                confidence="high",
            )
            chapters.append(current_chapter)
        else:
            if current_chapter is None:
                ambiguous.append({
                    "reason": "subchapter-level outline entry found before any chapter-level entry",
                    "title": title,
                    "page": page,
                })
                continue
            sub_num = f"{current_chapter.number}.{len(current_chapter.subchapters) + 1}"
            current_chapter.subchapters.append(SubChapter(
                number=sub_num,
                title=title.strip(),
                page_start=page,
                page_end=page_end,
                confidence="high",
            ))

    # Fix chapter page_end to cover its last subchapter, and give
    # chapter-only entries (no subchapters) a synthetic single subchapter
    # so downstream selection always has a subchapter to point at.
    for idx, chap in enumerate(chapters):
        next_chap_start = chapters[idx + 1].page_start if idx + 1 < len(chapters) else total_pages + 1
        chap.page_end = max(chap.page_end, next_chap_start - 1)
        if chap.subchapters:
            chap.subchapters[-1].page_end = max(chap.subchapters[-1].page_end, chap.page_end)
        else:
            chap.subchapters.append(SubChapter(
                number=f"{chap.number}.1",
                title=chap.title,
                page_start=chap.page_start,
                page_end=chap.page_end,
                confidence="high",
            ))

    return BookStructure(
        source_file="",
        title="",
        total_pages=total_pages,
        method="outline",
        scanned_pages=[],
        chapters=chapters,
        ambiguous=ambiguous,
    )


# --------------------------------------------------------------------------
# Method 2: heuristic heading detection
# --------------------------------------------------------------------------

def _page_text_for_heading_scan(doc: fitz.Document, page_index: int, scanned_pages: set[int]) -> dict:
    """Return page dict structure; OCR fallback only supplies plain text,
    so OCR'd pages produce line-level candidates without font metadata."""
    page_num = page_index + 1
    if page_num in scanned_pages:
        text = ocr_page_text(doc, page_index)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return {"ocr": True, "lines": lines}
    return {"ocr": False, "raw": doc[page_index].get_text("dict")}


def _median_body_size(doc: fitz.Document, scanned_pages: set[int]) -> float:
    sizes = []
    for i, page in enumerate(doc):
        if (i + 1) in scanned_pages:
            continue
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    txt = span["text"].strip()
                    if len(txt) >= 3:
                        sizes.append(span["size"])
    return statistics.median(sizes) if sizes else 10.0


# Minimum fraction of a candidate title's characters that must be alphabetic
# for it to be trusted as a real heading rather than an equation number that
# happens to be numbered like a section (e.g. Hull's "(5.1)" equations vs.
# "5.1 Black-Scholes-Merton"). Equation lines are dominated by digits,
# symbols and single-letter variables, so this cheaply tells them apart
# without needing page position or layout info.
TITLE_MIN_ALPHA_RATIO = 0.6
TITLE_MIN_ALPHA_CHARS = 3


def _looks_like_prose_title(title: str) -> bool:
    title = title.strip()
    alpha_chars = sum(c.isalpha() for c in title)
    if alpha_chars < TITLE_MIN_ALPHA_CHARS:
        return False
    return (alpha_chars / len(title)) >= TITLE_MIN_ALPHA_RATIO


def _classify_heading_text(text: str) -> Optional[tuple[str, str, str]]:
    """Returns (kind, number, title) where kind is 'chapter' or 'subchapter', or None."""
    text = text.strip()
    if not text or len(text) > 120:
        return None

    m = DEEPER_RE.match(text)
    if m:
        title = m.group(4).strip()
        if not _looks_like_prose_title(title):
            return None
        return ("deeper", f"{m.group(1)}.{m.group(2)}.{m.group(3)}", title)

    m = APPENDIX_SUBCHAPTER_RE.match(text)
    if m:
        title = m.group(3).strip()
        if not _looks_like_prose_title(title):
            return None
        return ("subchapter", f"{m.group(1).upper()}.{m.group(2)}", title)

    m = SUBCHAPTER_RE.match(text)
    if m:
        title = m.group(3).strip()
        if not _looks_like_prose_title(title):
            return None
        return ("subchapter", f"{m.group(1)}.{m.group(2)}", title)

    m = APPENDIX_CHAPTER_RE.match(text)
    if m:
        return ("chapter", m.group(1).upper(), (m.group(2) or "").strip() or text)

    m = CHAPTER_NUMBER_RE.match(text)
    if m:
        return ("chapter", m.group(2), (m.group(3) or "").strip() or text)

    m = BARE_CHAPTER_RE.match(text)
    if m:
        title = m.group(2).strip()
        if not _looks_like_prose_title(title):
            return None
        return ("chapter", m.group(1), title)

    return None


def extract_heuristic(doc: fitz.Document, scanned_pages: list[int]) -> BookStructure:
    scanned_set = set(scanned_pages)
    median_size = _median_body_size(doc, scanned_set)
    total_pages = doc.page_count

    candidates: list[dict] = []  # {kind, number, title, page}

    for i in range(total_pages):
        page_num = i + 1
        info = _page_text_for_heading_scan(doc, i, scanned_set)

        if info["ocr"]:
            for line in info["lines"]:
                # OCR'd text carries no font-size/bold info, so there's no
                # visual-distinction gate to fall back on the way there is
                # for real text (below). Without one, bare-number patterns
                # (BARE_CHAPTER_RE, SUBCHAPTER_RE, ...) false-positive
                # constantly on OCR'd body text — a TOC line whose dot
                # leaders collapsed into "1. Basic Properties of Numbers 3",
                # an exercise ("5. Express each of the following..."), a
                # misread equation. Only accept an OCR'd heading when it's
                # unambiguously chapter/appendix-shaped ("Chapter N ...",
                # "Appendix A ...") — anything else is silently untrusted
                # rather than guessed, consistent with flagging ambiguity
                # instead of dropping/misreading content.
                if not (CHAPTER_NUMBER_RE.match(line) or APPENDIX_CHAPTER_RE.match(line)):
                    continue
                cls = _classify_heading_text(line)
                if cls:
                    kind, number, title = cls
                    candidates.append({
                        "kind": kind, "number": number, "title": title,
                        "page": page_num, "source": "ocr",
                    })
            continue

        d = info["raw"]
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join(s["text"] for s in spans).strip()
                if not line_text:
                    continue
                max_size = max(s["size"] for s in spans)
                is_bold = any((s["flags"] & 16) for s in spans)
                is_large = max_size >= median_size * HEADING_SIZE_RATIO

                cls = _classify_heading_text(line_text)
                if not cls:
                    continue
                kind, number, title = cls

                # Require visual distinction (size or bold) to avoid false
                # positives from numbered list items / body text that happens
                # to start with digits, UNLESS it's an unambiguous "Chapter N" form.
                looks_like_explicit_chapter = bool(CHAPTER_NUMBER_RE.match(line_text)) or \
                    bool(APPENDIX_CHAPTER_RE.match(line_text))
                if not (is_large or is_bold or looks_like_explicit_chapter):
                    continue

                candidates.append({
                    "kind": kind, "number": number, "title": title,
                    "page": page_num, "source": "text",
                    "large": is_large, "bold": is_bold,
                })

    ambiguous: list[dict] = []
    chapters: list[Chapter] = []
    current_chapter: Optional[Chapter] = None
    seen_chapter_numbers = set()

    for idx, cand in enumerate(candidates):
        if cand["kind"] == "deeper":
            ambiguous.append({
                "reason": "heading detected below subchapter depth (x.y.z); folded into containing subchapter",
                "title": cand["title"],
                "page": cand["page"],
            })
            continue

        if cand["kind"] == "chapter":
            if cand["number"] in seen_chapter_numbers:
                ambiguous.append({
                    "reason": "duplicate chapter number detected by heuristic; possible false-positive heading",
                    "title": cand["title"],
                    "page": cand["page"],
                })
                continue
            seen_chapter_numbers.add(cand["number"])
            # Tesseract's plain image_to_string has no layout awareness, so a
            # page's running-header text and the first line of body text
            # directly below it sometimes get fused into one OCR'd line —
            # "Chapter 4" + adjacent sentence fragment. A genuine chapter
            # title is normally short; a long, sentence-shaped one is a sign
            # this happened, so flag it rather than trust it silently.
            ocr_title_suspect = cand["source"] == "ocr" and len(cand["title"]) > OCR_TITLE_SUSPECT_LENGTH
            if ocr_title_suspect:
                ambiguous.append({
                    "reason": "OCR'd chapter title is unusually long, likely fused with adjacent body "
                              "text rather than a real title (Tesseract has no page-layout awareness)",
                    "title": cand["title"],
                    "page": cand["page"],
                })
            current_chapter = Chapter(
                number=cand["number"],
                title=cand["title"],
                page_start=cand["page"],
                page_end=cand["page"],  # corrected in second pass
                confidence="low" if ocr_title_suspect else
                    ("high" if (cand.get("large") or cand.get("bold") or cand["source"] == "ocr") else "low"),
            )
            chapters.append(current_chapter)
        else:  # subchapter
            if current_chapter is None:
                ambiguous.append({
                    "reason": "subchapter heading found before any chapter heading was detected",
                    "title": cand["title"],
                    "page": cand["page"],
                })
                continue
            current_chapter.subchapters.append(SubChapter(
                number=cand["number"],
                title=cand["title"],
                page_start=cand["page"],
                page_end=cand["page"],  # corrected below
                confidence="high" if (cand.get("large") or cand.get("bold") or cand["source"] == "ocr") else "low",
            ))

    # Second pass: fill in page_end ranges from the next heading's start page.
    flat: list = []
    for chap in chapters:
        flat.append(("chapter", chap))
        for sub in chap.subchapters:
            flat.append(("sub", sub))

    for i, (kind, item) in enumerate(flat):
        # find next item's page_start regardless of kind, to bound this one
        next_start = total_pages + 1
        for j in range(i + 1, len(flat)):
            next_start = flat[j][1].page_start
            break
        item.page_end = max(item.page_start, next_start - 1)

    # Chapters with no detected subchapters get a synthetic one so the
    # picker always has a leaf to select.
    for chap in chapters:
        if not chap.subchapters:
            chap.subchapters.append(SubChapter(
                number=f"{chap.number}.1",
                title=chap.title,
                page_start=chap.page_start,
                page_end=chap.page_end,
                confidence="low",
            ))
            ambiguous.append({
                "reason": "no subchapter headings detected within this chapter; treated as a single section",
                "title": chap.title,
                "page": chap.page_start,
            })

    if not chapters:
        ambiguous.append({
            "reason": "no chapter headings could be confidently detected anywhere in the document",
            "title": None,
            "page": 1,
        })
        chapters.append(Chapter(
            number="1",
            title="(entire document — structure not detected)",
            page_start=1,
            page_end=total_pages,
            confidence="low",
            subchapters=[SubChapter(
                number="1.1",
                title="(entire document)",
                page_start=1,
                page_end=total_pages,
                confidence="low",
            )],
        ))

    return BookStructure(
        source_file="",
        title="",
        total_pages=total_pages,
        method="heuristic",
        scanned_pages=scanned_pages,
        chapters=chapters,
        ambiguous=ambiguous,
    )


# --------------------------------------------------------------------------
# Title detection + top-level entry point
# --------------------------------------------------------------------------

def _guess_title(doc: fitz.Document, pdf_path: Path) -> str:
    meta_title = (doc.metadata or {}).get("title", "").strip()
    if meta_title:
        return meta_title
    return pdf_path.stem.replace("_", " ").replace("-", " ").strip()


def extract_structure(pdf_path: str | Path) -> BookStructure:
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)

    scanned_pages = detect_scanned_pages(doc)

    result = extract_from_outline(doc)
    if result is None:
        result = extract_heuristic(doc, scanned_pages)
    else:
        result.scanned_pages = scanned_pages
        if scanned_pages:
            result.ambiguous.append({
                "reason": "pages have no extractable text layer (likely scanned); "
                          "outline page ranges are used but OCR was not needed for headings",
                "title": None,
                "page": scanned_pages,
            })

    result.source_file = str(pdf_path)
    result.title = _guess_title(doc, pdf_path)
    doc.close()
    return result


# --------------------------------------------------------------------------
# CLI for standalone validation (Stage 1 checkpoint)
# --------------------------------------------------------------------------

def print_tree(structure: BookStructure) -> None:
    print(f"\n{structure.title}  ({structure.total_pages} pages, method={structure.method})\n")
    for chap in structure.chapters:
        flag = "" if chap.confidence == "high" else "  [LOW CONFIDENCE]"
        print(f"{chap.number}. {chap.title}  (p.{chap.page_start}-{chap.page_end}){flag}")
        for sub in chap.subchapters:
            sflag = "" if sub.confidence == "high" else "  [LOW CONFIDENCE]"
            print(f"    {sub.number} {sub.title}  (p.{sub.page_start}-{sub.page_end}){sflag}")
    if structure.scanned_pages:
        print(f"\nScanned/no-text-layer pages ({len(structure.scanned_pages)}): {structure.scanned_pages}")
    if structure.ambiguous:
        print(f"\nAmbiguous / flagged items ({len(structure.ambiguous)}):")
        for item in structure.ambiguous:
            print(f"  - p.{item['page']}: {item['reason']}" + (f" ({item['title']})" if item.get("title") else ""))


def main():
    parser = argparse.ArgumentParser(description="Extract chapter/subchapter structure from a PDF (Stage 1).")
    parser.add_argument("--file", required=True, help="Path to the PDF file")
    parser.add_argument("--out", help="Where to write the structure JSON (default: <file>.structure.json)")
    args = parser.parse_args()

    structure = extract_structure(args.file)
    print_tree(structure)

    out_path = Path(args.out) if args.out else Path(args.file).with_suffix(".structure.json")
    out_path.write_text(json.dumps(structure.to_dict(), indent=2, ensure_ascii=False))
    print(f"\nFull structure JSON written to: {out_path}")


if __name__ == "__main__":
    main()
