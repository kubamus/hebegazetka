"""Selectable PDF legal clauses, preserving the exact asterisk and its position."""
from __future__ import annotations

import re


LEGAL_START = re.compile(
    r"(?<!\*)(\*(?:[ \t\r\n]*\*)*)\s*(?=(?:Promocja|Marka|Oferta)\b)", re.I)


def markers(text: str) -> set[str]:
    return {"*" * match[0].count("*") for match in
            re.finditer(r"\*(?:[ \t]*\*)*", text)}


def split_legal_text(text, box, year, parse_period, char_boxes=None):
    text = text.replace("\x07", " ").replace("∗", "*").replace("⁎", "*")
    starts = list(LEGAL_START.finditer(text))
    notes = []
    for index, start in enumerate(starts):
        end = starts[index+1].start() if index+1 < len(starts) else len(text)
        body = " ".join(text[start.end():end].split())
        marker = "*" * start[1].count("*")
        affirmative = re.match(r"^Promocja\s+obowiązuje\b(?:\s+w\s+dniach)?\s*(.*)", body, re.I)
        if affirmative:
            kind, period = "validity", parse_period(affirmative[1], year)
        elif re.match(r"^Promocja\s+nie\b", body, re.I):
            kind, period = "exclusion", None
        elif re.match(r"^Marka\s+dostępna\b", body, re.I):
            kind, period = "availability", None
        else:
            kind, period = "other", None
        # Character boxes keep side-by-side footnotes separate even when the
        # PDF puts them in one text block or one long selectable line.
        boxes = [b for b in (char_boxes or [])[start.start():end] if b is not None]
        bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else tuple(box)
        notes.append({"marker": marker, "kind": kind, "period": period,
                      "text": marker + " " + body, "box": list(bounds),
                      "stock_limited": bool(re.search(r"do wyczerpania zapasów", body, re.I))})
    return notes


def extract_legal_notes(page, year, parse_period):
    raw = page.get_text("rawdict")
    notes = []
    if isinstance(raw, dict):
        for block in raw["blocks"]:
            chars, boxes = [], []
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for char in span["chars"]:
                        chars.append(char["c"])
                        boxes.append(char["bbox"])
                chars.append("\n")
                boxes.append(None)
            notes.extend(split_legal_text("".join(chars),block["bbox"],year,parse_period,boxes))
    else:
        # Also supports recorded text blocks and small regression fixtures.
        for block in page.get_text("blocks"):
            notes.extend(split_legal_text(block[4],block[:4],year,parse_period))
    return list({(n["marker"],n["text"],tuple(round(v,1) for v in n["box"])):n
                 for n in notes}.values())
