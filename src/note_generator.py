"""
Stage 3: generates one Obsidian markdown note per selected subchapter.

Grounds the LLM strictly in the subchapter's actual extracted PDF text
(via content_extractor) and asks for only the body content (Summary, Key
Concepts, Notes); frontmatter and the title heading are built
programmatically so they're always well-formed, and Related is left for
Stage 4 (which has visibility into the rest of the book's notes).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from src.content_extractor import get_page_range_text
from src.llm_client import generate as llm_generate
from src.structure_extractor import BookStructure, Chapter, SubChapter

MIN_SOURCE_CHARS = 50

SYSTEM_PROMPT = (
    "You are an expert study-notes writer. You distill textbook content into "
    "concise, structured notes for spaced review in Obsidian. Your style is "
    "bullet-heavy and structured, never prose paragraphs. You base your notes "
    "strictly on the source text you are given — you never add outside facts, "
    "even if you recognize the topic."
)

USER_PROMPT_TEMPLATE = """Book: {book_title}
Chapter: {chapter_number} — {chapter_title}
Subchapter: {sub_number} — {sub_title}
Subject area: {subject}

Source text (pages {page_start}-{page_end}):
\"\"\"
{source_text}
\"\"\"

Write study notes for this subchapter using EXACTLY this structure and nothing else \
(no text before "## Summary" or after the last bullet of "## Notes"):

## Summary
<2-4 sentence overview of what this subchapter covers>

## Key Concepts
- **<Term>**: <definition/explanation>
- **<Term>**: <definition/explanation>
(3-8 key concepts — only ones actually covered in the source text)

## Notes
<Structured bullet-hierarchy notes distilling the content for studying — not a \
copy-paste summary. Use nested bullets where useful.>

Rules:
- Use only the source text above. Do not add outside knowledge not present in it.
- Be concise and bullet-heavy, not prose.
- Do not include frontmatter, a top-level title, or a "## Related" section.
"""

BODY_SECTION_RE = re.compile(r"##\s*Summary.*", re.DOTALL | re.IGNORECASE)
CHAPTER_LABEL_RE = re.compile(r"^chapter\s+(\d+)\s*[:\-–—]?\s*(.*)$", re.IGNORECASE)


def _chapter_label(chapter: Chapter) -> str:
    """Prefer the book's own chapter number (e.g. from a title like
    "CHAPTER 4 Mean Reversion...") over the picker's positional index,
    which includes front matter and so won't match the book's numbering."""
    match = CHAPTER_LABEL_RE.match(chapter.title.strip())
    if match:
        num, rest = match.group(1), match.group(2).strip()
        return f"{num} — {rest or chapter.title}"
    return f"{chapter.number} — {chapter.title}"


def _real_chapter_number(chapter: Chapter) -> str:
    match = CHAPTER_LABEL_RE.match(chapter.title.strip())
    return match.group(1) if match else chapter.number


def _subchapter_label(chapter: Chapter, subchapter: SubChapter) -> str:
    """Same fix as _chapter_label, applied to the subchapter number: use the
    book's real chapter number as the prefix instead of the picker's
    positional index, so e.g. "7.1" (picker) renders as "4.1" (book)."""
    _, _, sub_index = subchapter.number.partition(".")
    real_num = f"{_real_chapter_number(chapter)}.{sub_index}" if sub_index else subchapter.number
    return f"{real_num} — {subchapter.title}"


def _yaml_str(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _yaml_tag(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_-]+$", value):
        return value
    return _yaml_str(value)


def build_prompt(book_title: str, chapter: Chapter, subchapter: SubChapter,
                  subject: str, source_text: str) -> str:
    return USER_PROMPT_TEMPLATE.format(
        book_title=book_title,
        chapter_number=chapter.number,
        chapter_title=chapter.title,
        sub_number=subchapter.number,
        sub_title=subchapter.title,
        subject=subject,
        page_start=subchapter.page_start,
        page_end=subchapter.page_end,
        source_text=source_text,
    )


def _extract_body(raw_response: str) -> str:
    match = BODY_SECTION_RE.search(raw_response)
    if match:
        return match.group(0).strip()
    # Model didn't follow the format; keep what it gave us rather than
    # silently dropping content, but it needs a human look.
    return raw_response.strip()


def render_note(book_title: str, chapter: Chapter, subchapter: SubChapter,
                 subject: str, body: str) -> str:
    frontmatter = "\n".join([
        "---",
        f"source: {_yaml_str(book_title)}",
        f"chapter: {_yaml_str(_chapter_label(chapter))}",
        f"subchapter: {_yaml_str(_subchapter_label(chapter, subchapter))}",
        f"pages: {_yaml_str(f'{subchapter.page_start}-{subchapter.page_end}')}",
        f"tags: [textbook, {_yaml_tag(subject)}]",
        f"processed: {date.today().isoformat()}",
        "status: unreviewed",
        "---",
    ])
    return (
        f"{frontmatter}\n\n"
        f"# {subchapter.title}\n\n"
        f"{body}\n\n"
        f"## Related\n"
        f"<!-- populated when this book's other notes exist (Stage 4) -->\n"
    )


def generate_subchapter_note(pdf_path: str | Path, structure: BookStructure,
                              chapter: Chapter, subchapter: SubChapter, subject: str,
                              client, model: str) -> str:
    source_text = get_page_range_text(
        pdf_path, subchapter.page_start, subchapter.page_end, structure.scanned_pages,
    )

    if len(source_text.strip()) < MIN_SOURCE_CHARS:
        body = (
            "## Summary\n"
            "_Not enough extractable text was found for these pages to generate notes "
            "automatically — this subchapter needs manual review._\n\n"
            "## Key Concepts\n"
            "- (none — source text unavailable)\n\n"
            "## Notes\n"
            f"- Pages {subchapter.page_start}-{subchapter.page_end} yielded "
            f"{len(source_text.strip())} characters of extractable text."
        )
        return render_note(structure.title, chapter, subchapter, subject, body)

    prompt = build_prompt(structure.title, chapter, subchapter, subject, source_text)
    raw = llm_generate(client, model, SYSTEM_PROMPT, prompt)
    body = _extract_body(raw)
    return render_note(structure.title, chapter, subchapter, subject, body)
