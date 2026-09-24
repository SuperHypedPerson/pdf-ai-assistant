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
from src.llm_client import DEFAULT_MAX_TOKENS, generate as llm_generate
from src.structure_extractor import BookStructure, Chapter, SubChapter

MIN_SOURCE_CHARS = 50
TOKENS_PER_PAGE = 350  # extra token budget per source page, beyond the base
MAX_TOKENS_CAP = 8192

TRUNCATION_WARNING = (
    "\n\n> [!warning] This note was cut off — the model hit its token limit before "
    "finishing. Regenerate this subchapter with a higher --max-tokens, or split it "
    "into a smaller selection."
)


def max_tokens_for(subchapter: SubChapter) -> int:
    page_count = max(1, subchapter.page_end - subchapter.page_start + 1)
    return min(MAX_TOKENS_CAP, DEFAULT_MAX_TOKENS + TOKENS_PER_PAGE * page_count)

SYSTEM_PROMPT = (
    "You are an expert study-notes writer. You distill textbook content into "
    "concise, structured notes for spaced review in Obsidian. Your style is "
    "bullet-heavy and structured, never prose paragraphs. You base your notes "
    "strictly on the source text you are given — you never add outside facts, "
    "even if you recognize the topic. When the source contains mathematical "
    "notation, you reproduce it as Obsidian-compatible LaTeX ($...$ inline, "
    "$$...$$ for standalone equations/derivations) rather than flattening it "
    "into plain text or paraphrasing it away — a formula is exact content, "
    "not prose to summarize. When the source contains code, you reproduce it "
    "verbatim in a fenced code block with a language tag, never paraphrased "
    "or reformatted. Named theorems, definitions, and lemmas keep their label "
    "(e.g. \"**Theorem 3.2 (Name)**: ...\") rather than being folded into "
    "unlabeled prose."
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
- Reproduce equations as LaTeX ($...$ or $$...$$) and code as fenced code blocks with a \
language tag, exactly as they appear in the source — never paraphrase a formula or snippet \
into words, and never invent one that isn't in the source text.
- Keep theorem/definition/example labels and numbers when the source has them (e.g. \
"Theorem 3.2") instead of dropping the label.
- Do not include frontmatter, a top-level title, or a "## Related" section.
"""

BODY_SECTION_RE = re.compile(r"##\s*Summary.*", re.DOTALL | re.IGNORECASE)
CHAPTER_LABEL_RE = re.compile(r"^chapter\s+(\d+)\s*[:\-–—]?\s*(.*)$", re.IGNORECASE)
# Many embedded PDF outlines give chapter titles as a bare number, no
# "Chapter" word at all — e.g. "1 The Basics", "2 User-Defined Types".
# Safe to generalize (unlike a bare-letter pattern): real prose titles
# essentially never start with a bare digit unless it IS a chapter number.
BARE_NUMBERED_TITLE_RE = re.compile(r"^(\d{1,3})\.?\s+(\S.*)$")


def _match_chapter_number(chapter: Chapter) -> re.Match | None:
    title = chapter.title.strip()
    return CHAPTER_LABEL_RE.match(title) or BARE_NUMBERED_TITLE_RE.match(title)


def is_numbered_chapter(chapter: Chapter) -> bool:
    """True if the title itself carries a real chapter number — either
    "CHAPTER N ..." or a bare "N Title" from an embedded outline. Front/back
    matter (cover, contents, preface, bibliography, index, ...) has no real
    chapter number at all."""
    return _match_chapter_number(chapter) is not None


def real_chapter_number(chapter: Chapter) -> str:
    """The book's own chapter number (e.g. from a title like "CHAPTER 4
    Mean Reversion..." or "4 Mean Reversion..."), or the picker's positional
    index as a display fallback for front/back matter. NOT safe as a
    grouping/dict key on its own — a front-matter chapter's positional index
    can coincide with a real chapter's number (e.g. both "1"). Use
    chapter_group_key for that."""
    match = _match_chapter_number(chapter)
    return match.group(1) if match else chapter.number


def chapter_title_rest(chapter: Chapter) -> str:
    """Chapter title with a leading chapter-number prefix stripped, if present."""
    match = _match_chapter_number(chapter)
    if match:
        return match.group(2).strip() or chapter.title
    return chapter.title


# Some embedded outlines give subchapter titles as e.g. "1.1 Introduction",
# redundantly embedding the same numbering we compute separately — left
# alone, that doubles up everywhere the subchapter is displayed or named.
SUBCHAPTER_TITLE_PREFIX_RE = re.compile(r"^\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\.?\s+(\S.*)$")


def subchapter_title_rest(subchapter: SubChapter) -> str:
    """Subchapter title with a leading "N.M " (or "N.M.K ") numeric prefix
    stripped, if the title embeds its own numbering."""
    match = SUBCHAPTER_TITLE_PREFIX_RE.match(subchapter.title.strip())
    if match:
        return match.group(1).strip() or subchapter.title
    return subchapter.title


def chapter_group_key(chapter: Chapter) -> str:
    """Collision-free key for grouping subchapters by chapter (index
    sections, sibling Related links). Numbered and unnumbered chapters live
    in disjoint key spaces so front matter can never merge into a real
    chapter's section just because their numbers happen to coincide."""
    if is_numbered_chapter(chapter):
        return f"n{real_chapter_number(chapter)}"
    return f"u{chapter.number}"


def chapter_display_label(chapter: Chapter) -> str:
    """Human-facing chapter label: "Chapter N - Title" for real chapters;
    just the bare title for front/back matter, which has no real number to
    show and shouldn't be presented as if it does."""
    if is_numbered_chapter(chapter):
        return f"Chapter {real_chapter_number(chapter)} - {chapter_title_rest(chapter)}"
    return chapter.title


def real_subchapter_number(chapter: Chapter, subchapter: SubChapter) -> str:
    """Same idea as real_chapter_number, applied to the subchapter number:
    use the book's real chapter number as the prefix instead of the
    picker's positional index, so e.g. "7.1" (picker) renders as "4.1"
    (book) for a real chapter. Only meaningful when is_numbered_chapter."""
    _, _, sub_index = subchapter.number.partition(".")
    return f"{real_chapter_number(chapter)}.{sub_index}" if sub_index else subchapter.number


def _chapter_label(chapter: Chapter) -> str:
    if is_numbered_chapter(chapter):
        return f"{real_chapter_number(chapter)} — {chapter_title_rest(chapter)}"
    return chapter.title


def _subchapter_label(chapter: Chapter, subchapter: SubChapter) -> str:
    if is_numbered_chapter(chapter):
        return f"{real_subchapter_number(chapter, subchapter)} — {subchapter_title_rest(subchapter)}"
    return subchapter_title_rest(subchapter)


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
        sub_title=subchapter_title_rest(subchapter),
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
                 subject: str, body: str, related_links: list[str] | None = None) -> str:
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
    related_body = "\n".join(f"- {link}" for link in related_links) if related_links else \
        "<!-- no other notes in this chapter yet -->"
    return (
        f"{frontmatter}\n\n"
        f"# {subchapter_title_rest(subchapter)}\n\n"
        f"{body}\n\n"
        f"## Related\n"
        f"{related_body}\n"
    )


def generate_subchapter_note(pdf_path: str | Path, structure: BookStructure,
                              chapter: Chapter, subchapter: SubChapter, subject: str,
                              client, model: str, max_tokens: int | None = None,
                              related_links: list[str] | None = None) -> str:
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
        return render_note(structure.title, chapter, subchapter, subject, body, related_links)

    prompt = build_prompt(structure.title, chapter, subchapter, subject, source_text)
    tokens = max_tokens or max_tokens_for(subchapter)
    raw, truncated = llm_generate(client, model, SYSTEM_PROMPT, prompt, max_tokens=tokens)
    body = _extract_body(raw)
    if truncated:
        body += TRUNCATION_WARNING
    return render_note(structure.title, chapter, subchapter, subject, body, related_links)
