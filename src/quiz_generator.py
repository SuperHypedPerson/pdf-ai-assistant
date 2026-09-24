"""
Stage 5: generates a quiz grounded strictly in the selected subchapters'
actual extracted PDF text (never the model's general knowledge of the
topic). Asks the model for structured JSON so mixed question types (MCQ +
short answer) can be rendered into a clean questions section plus a
separate, collapsible answer key.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from src.content_extractor import get_page_range_text
from src.llm_client import generate as llm_generate
from src.note_generator import real_subchapter_number
from src.structure_extractor import BookStructure, Chapter, SubChapter

BASE_MAX_TOKENS = 4096  # generous headroom for a reasoning model's "thinking" pass
TOKENS_PER_QUESTION = 300
MAX_TOKENS_CAP = 16384

MCQ_FRACTION = 0.7  # "mostly MCQ" mix


class QuizParseError(RuntimeError):
    """Raised when the model's response couldn't be parsed as the expected
    JSON quiz structure."""


SYSTEM_PROMPT = (
    "You are an expert quiz writer for spaced-review study quizzes. You write "
    "questions that are answerable strictly from the source text you are given "
    "— never from general knowledge of the topic, even if you recognize it. "
    "You respond with valid JSON only: no markdown code fences, no commentary "
    "before or after the JSON."
)

USER_PROMPT_TEMPLATE = """Book: {book_title}
Subject area: {subject}
Difficulty: {difficulty}

Source text (from the selected sections below):
\"\"\"
{source_text}
\"\"\"

Write a {num_questions}-question quiz over this source text: {num_mcq} multiple-choice \
questions and {num_short} short-answer questions, at {difficulty} difficulty. \
Every question must be answerable using only the source text above.

For multiple choice: exactly 4 options, exactly 1 correct, and the 3 distractors must be \
plausible (not obviously wrong) — ideally drawn from other details in the source text.

Respond with ONLY this JSON structure, nothing else:
{{
  "questions": [
    {{"type": "mcq", "question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0}},
    {{"type": "short", "question": "...", "model_answer": "..."}}
  ]
}}
"""


def _selection_label(selected: list[tuple[Chapter, SubChapter]]) -> str:
    parts = [real_subchapter_number(c, s) for c, s in selected]
    return ", ".join(parts)


def max_tokens_for(num_questions: int) -> int:
    return min(MAX_TOKENS_CAP, BASE_MAX_TOKENS + TOKENS_PER_QUESTION * num_questions)


def question_mix(num_questions: int) -> tuple[int, int]:
    """Returns (num_mcq, num_short), keeping at least one of each when
    num_questions >= 2."""
    num_mcq = round(num_questions * MCQ_FRACTION)
    num_mcq = max(0, min(num_questions, num_mcq))
    if num_questions >= 2:
        num_mcq = max(1, min(num_questions - 1, num_mcq))
    num_short = num_questions - num_mcq
    return num_mcq, num_short


def build_source_text(pdf_path: str | Path, structure: BookStructure,
                       selected: list[tuple[Chapter, SubChapter]]) -> str:
    blocks = []
    for chapter, subchapter in selected:
        text = get_page_range_text(pdf_path, subchapter.page_start, subchapter.page_end, structure.scanned_pages)
        label = f"{real_subchapter_number(chapter, subchapter)} — {subchapter.title} " \
                f"(pages {subchapter.page_start}-{subchapter.page_end})"
        blocks.append(f"### {label}\n{text}")
    return "\n\n".join(blocks)


def build_prompt(book_title: str, subject: str, difficulty: str,
                  num_mcq: int, num_short: int, source_text: str) -> str:
    return USER_PROMPT_TEMPLATE.format(
        book_title=book_title,
        subject=subject,
        difficulty=difficulty,
        num_questions=num_mcq + num_short,
        num_mcq=num_mcq,
        num_short=num_short,
        source_text=source_text,
    )


JSON_BLOB_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_questions(raw_response: str) -> list[dict]:
    match = JSON_BLOB_RE.search(raw_response)
    if not match:
        raise QuizParseError(f"No JSON object found in the model's response. Raw response: {raw_response[:300]!r}")

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise QuizParseError(f"Model's response wasn't valid JSON ({e}). Raw response: {raw_response[:300]!r}")

    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise QuizParseError(f"JSON had no non-empty 'questions' list. Raw response: {raw_response[:300]!r}")

    valid = []
    for q in questions:
        if not isinstance(q, dict) or "type" not in q or "question" not in q:
            continue
        if q["type"] == "mcq":
            options = q.get("options")
            idx = q.get("correct_index")
            if not isinstance(options, list) or len(options) != 4:
                continue
            if not isinstance(idx, int) or not (0 <= idx < 4):
                continue
        elif q["type"] == "short":
            if not q.get("model_answer"):
                continue
        else:
            continue
        valid.append(q)

    if not valid:
        raise QuizParseError(f"No valid questions survived validation. Raw response: {raw_response[:300]!r}")

    dropped = len(questions) - len(valid)
    return valid, dropped


def render_quiz(book_title: str, subject: str, difficulty: str,
                 selected: list[tuple[Chapter, SubChapter]], questions: list[dict]) -> str:
    frontmatter = "\n".join([
        "---",
        f'source: "{book_title}"',
        f'selection: "{_selection_label(selected)}"',
        f"difficulty: {difficulty}",
        f"num_questions: {len(questions)}",
        f"generated: {date.today().isoformat()}",
        f"tags: [quiz, textbook, {subject}]",
        "---",
    ])

    q_lines = ["## Questions", ""]
    for i, q in enumerate(questions, 1):
        q_lines.append(f"{i}. {q['question']}")
        if q["type"] == "mcq":
            letters = "ABCD"
            for letter, option in zip(letters, q["options"]):
                q_lines.append(f"   {letter}. {option}")
        q_lines.append("")

    a_lines = ["> [!note]- Answer Key"]
    for i, q in enumerate(questions, 1):
        if q["type"] == "mcq":
            letter = "ABCD"[q["correct_index"]]
            a_lines.append(f"> {i}. **{letter}** — {q['options'][q['correct_index']]}")
        else:
            a_lines.append(f"> {i}. {q['model_answer']}")

    return (
        f"{frontmatter}\n\n"
        f"# {book_title} — Quiz\n\n"
        f"{chr(10).join(q_lines)}\n"
        f"{chr(10).join(a_lines)}\n"
    )


def generate_quiz(pdf_path: str | Path, structure: BookStructure,
                   selected: list[tuple[Chapter, SubChapter]], subject: str, difficulty: str,
                   num_questions: int, client, model: str,
                   max_tokens: int | None = None) -> tuple[str, int]:
    """Returns (quiz_markdown, num_questions_delivered)."""
    num_mcq, num_short = question_mix(num_questions)
    source_text = build_source_text(pdf_path, structure, selected)
    prompt = build_prompt(structure.title, subject, difficulty, num_mcq, num_short, source_text)

    tokens = max_tokens or max_tokens_for(num_questions)
    raw, truncated = llm_generate(client, model, SYSTEM_PROMPT, prompt, max_tokens=tokens)
    questions, dropped = _parse_questions(raw)

    quiz_md = render_quiz(structure.title, subject, difficulty, selected, questions)
    if truncated:
        quiz_md += (
            "\n> [!warning] This quiz may be incomplete — the model hit its token limit "
            "while generating. Consider a smaller --num-questions or a shorter selection.\n"
        )
    if dropped:
        quiz_md += (
            f"\n> [!warning] {dropped} question(s) the model returned didn't match the expected "
            f"format and were dropped — {len(questions)} delivered out of {num_questions} requested.\n"
        )
    return quiz_md, len(questions)
