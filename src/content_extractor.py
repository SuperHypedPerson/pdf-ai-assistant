"""
Pulls the real extracted text for a page range, for use as LLM grounding
context (notes generation, quiz generation). Falls back to OCR per-page
for pages with no text layer, using the same detection as Stage 1.
"""

from __future__ import annotations

from pathlib import Path

import fitz

from src.structure_extractor import ocr_page_text


def get_page_range_text(pdf_path: str | Path, page_start: int, page_end: int,
                         scanned_pages: list[int] | None = None) -> str:
    """page_start/page_end are 1-indexed and inclusive."""
    scanned_set = set(scanned_pages or [])
    doc = fitz.open(pdf_path)
    try:
        parts = []
        for page_num in range(page_start, page_end + 1):
            if page_num < 1 or page_num > doc.page_count:
                continue
            if page_num in scanned_set:
                text = ocr_page_text(doc, page_num - 1)
            else:
                text = doc[page_num - 1].get_text("text")
            if text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts)
    finally:
        doc.close()
