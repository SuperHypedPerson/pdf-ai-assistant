"""
Chapter/subchapter selection: shared by notes.py and quiz.py.

Selection grammar (comma-separated tokens):
    "3"          -> every subchapter in chapter 3
    "5.2"        -> just subchapter 5.2
    "5.1-5.4"    -> subchapters 5.1 through 5.4 (must be within one chapter)
    "3-5"        -> every subchapter in chapters 3 through 5

Core parsing is a pure function (no input()/print()) so a future UI layer
(e.g. a web frontend) can call it directly.
"""

from __future__ import annotations

from src.note_generator import subchapter_title_rest

from src.structure_extractor import BookStructure, Chapter, SubChapter

Selection = list[tuple[Chapter, SubChapter]]


def parse_selection(selection_str: str, structure: BookStructure) -> tuple[Selection, list[str]]:
    chapters_by_num = {c.number: c for c in structure.chapters}
    selected: Selection = []
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []

    def add(chap: Chapter, sub: SubChapter) -> None:
        key = (chap.number, sub.number)
        if key not in seen:
            seen.add(key)
            selected.append((chap, sub))

    tokens = [t.strip() for t in selection_str.split(",") if t.strip()]
    if not tokens:
        return selected, ["no selection provided"]

    for tok in tokens:
        if tok.count("-") == 1 and not tok.startswith("-") and not tok.endswith("-"):
            left, right = (p.strip() for p in tok.split("-"))

            if "." in left or "." in right:
                if "." not in left or "." not in right:
                    errors.append(f"'{tok}': subchapter range needs both sides in N.M form")
                    continue
                lchap, lsub = left.split(".", 1)
                rchap, rsub = right.split(".", 1)
                if lchap != rchap:
                    errors.append(f"'{tok}': subchapter range must stay within one chapter")
                    continue
                chap = chapters_by_num.get(lchap)
                if not chap:
                    errors.append(f"'{tok}': chapter {lchap} not found")
                    continue
                try:
                    lo, hi = sorted((float(lsub), float(rsub)))
                except ValueError:
                    errors.append(f"'{tok}': invalid subchapter range")
                    continue
                matched = False
                for sub in chap.subchapters:
                    _, _, subnum = sub.number.partition(".")
                    try:
                        val = float(subnum)
                    except ValueError:
                        continue
                    if lo <= val <= hi:
                        matched = True
                        add(chap, sub)
                if not matched:
                    errors.append(f"'{tok}': no subchapters in that range")
                continue

            try:
                lo, hi = sorted((int(left), int(right)))
            except ValueError:
                errors.append(f"'{tok}': invalid chapter range")
                continue
            matched = False
            for chap in structure.chapters:
                try:
                    cn = int(chap.number)
                except ValueError:
                    continue
                if lo <= cn <= hi:
                    matched = True
                    for sub in chap.subchapters:
                        add(chap, sub)
            if not matched:
                errors.append(f"'{tok}': no chapters in that range")
            continue

        if "." in tok:
            chap_num = tok.split(".", 1)[0]
            chap = chapters_by_num.get(chap_num)
            if not chap:
                errors.append(f"'{tok}': chapter {chap_num} not found")
                continue
            match = next((s for s in chap.subchapters if s.number == tok), None)
            if not match:
                errors.append(f"'{tok}': subchapter not found")
                continue
            add(chap, match)
            continue

        chap = chapters_by_num.get(tok)
        if not chap:
            errors.append(f"'{tok}': chapter not found")
            continue
        for sub in chap.subchapters:
            add(chap, sub)

    return selected, errors


def render_tree(structure: BookStructure, processed: set[str]) -> str:
    lines = [f"{structure.title}  ({structure.total_pages} pages)", ""]
    for chap in structure.chapters:
        conf = "" if chap.confidence == "high" else "  [low-confidence]"
        lines.append(f"{chap.number}. {chap.title}  (p.{chap.page_start}-{chap.page_end}){conf}")
        for sub in chap.subchapters:
            mark = "[x]" if sub.number in processed else "[ ]"
            sconf = "" if sub.confidence == "high" else "  [low-confidence]"
            lines.append(f"    {mark} {sub.number} {subchapter_title_rest(sub)}  (p.{sub.page_start}-{sub.page_end}){sconf}")
    return "\n".join(lines)


def confirm_text(selected: Selection) -> str:
    if not selected:
        return "You selected: nothing"
    lines = ["You selected:"]
    for chap, sub in selected:
        lines.append(f"  {sub.number} {subchapter_title_rest(sub)}  (p.{sub.page_start}-{sub.page_end})  [Ch.{chap.number} {chap.title}]")
    return "\n".join(lines)
