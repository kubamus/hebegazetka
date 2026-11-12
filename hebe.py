#!/usr/bin/env python3
"""Aktualna gazetka Hebe → nazwa produktu, ID, okres i promocja w Excelu."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import json
import logging
from pathlib import Path
import re
import shutil
import sys
import textwrap
import unicodedata
from urllib.parse import urljoin, urlparse

import pymupdf
from playwright.async_api import async_playwright, Error as PlaywrightError
from product_names import fetch_names, enrich_offers
from product_matching import brand_matches, caption_identity, folded
from promotion_verification import verify_offers
from legal_footnotes import extract_legal_notes, markers, split_legal_text
from products_api import fetch_products_info

LOG = logging.getLogger("hebe")
MAGAZINES = {
    "digital": "https://www.hebe.pl/gazetka-hebe?magazine=digital",
    "plachta": "https://www.hebe.pl/gazetka-hebe?magazine=plachta",
}
HOME = MAGAZINES["digital"]
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})(?:\.(\d{1,2})(?:\.(\d{4}|\d{2}))?)?\s*[-–—]\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?!\d)")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\x07", " ")).strip()


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower().replace("ł", "l"))
    return " ".join(re.findall(r"[a-z0-9]+", "".join(c for c in text if not unicodedata.combining(c))))


def product_id(value: str) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"\d+", value) or not value.strip("0"):
        raise ValueError(f"Nieprawidłowe ID produktu: {value!r}")
    return value.lstrip("0")


def parse_period(text: str, year: int) -> tuple[str, str] | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    sd, sm, sy, ed, em, ey = match.groups()
    end_year = int(ey or sy or year)
    end_year += 2000 if end_year < 100 else 0
    start_year = int(sy or end_year)
    start_year += 2000 if start_year < 100 else 0
    start_month = int(sm or em)
    if not sy and start_month > int(em):
        start_year -= 1
    try:
        start = date(start_year, start_month, int(sd))
        end = date(end_year, int(em), int(ed))
    except ValueError:
        return None
    if start > end:
        return None
    return start.isoformat(), end.isoformat()


def period_label(period: tuple[str, str] | None) -> str:
    if not period:
        return "DO SPRAWDZENIA — brak jednoznacznej daty"
    return "–".join(date.fromisoformat(d).strftime("%d.%m.%Y") for d in period)


@dataclass
class Text:
    text: str
    box: tuple[float, float, float, float]
    size: float = 0
    font: str = ""
    color: int = 0

    @property
    def x(self):
        return (self.box[0] + self.box[2]) / 2

    @property
    def y(self):
        return (self.box[1] + self.box[3]) / 2


@dataclass
class Offer:
    period: str
    id: str
    promotion: str
    page: int
    start: str | None = None
    end: str | None = None
    warnings: list[str] | None = None
    product: str = ""
    product_description: str = ""
    product_url: str = ""
    evidence: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)


def extract_lines(page) -> list[Text]:
    result = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = line["spans"]
            text = clean("".join(s["text"] for s in spans))
            if not text:
                continue
            representative = max(spans, key=lambda s: s["size"])
            result.append(Text(text, tuple(line["bbox"]), representative["size"],
                               representative["font"], representative["color"]))
    # PDF sometimes paints an identical piece of text twice.
    return list({(t.text, tuple(round(x, 1) for x in t.box)): t for t in result}.values())


def caption_groups(lines: list[Text]) -> list[Text]:
    """Group aligned name/description lines; do not include price/legal labels."""
    eligible = [t for t in lines if 5 <= t.size <= 16
                and re.search(r"[A-Za-zĄąĆćĘęŁłŃńÓóŚśŹźŻż]", t.text)
                and not re.search(r"Cond$|SemiCn$|BlackSemiCn$", t.font)
                and not re.search(r"promocj|cen[ayę]|obniżk|wyczerp|TYLKO|NOWOŚĆ|REKLAMA", t.text, re.I)
                and not DATE_RE.search(t.text)]
    remaining = sorted(eligible, key=lambda t: (t.box[1], t.box[0]))
    groups = []
    while remaining:
        first = remaining.pop(0)
        group = [first]
        while True:
            candidates = [t for t in remaining if abs(t.box[0] - first.box[0]) < 3
                          and -2 <= t.box[1] - group[-1].box[3] <= 6]
            if not candidates:
                break
            next_line = min(candidates, key=lambda t: t.box[1])
            # A new bold brand following a regular description starts a new caption.
            if "Semibold" in next_line.font and "Regular" in group[-1].font:
                break
            group.append(next_line)
            remaining.remove(next_line)
            if re.search(r"\d\s*(?:ml|g|szt\.)\s*=", next_line.text):
                break
        if "Semibold" not in first.font and "Bold" not in first.font:
            continue
        groups.append(Text(" ".join(t.text for t in group),
                           (min(t.box[0] for t in group), min(t.box[1] for t in group),
                            max(t.box[2] for t in group), max(t.box[3] for t in group))))
    return groups


def paragraphs(lines: list[Text], pattern: str) -> list[Text]:
    result = []
    for anchor in lines:
        if not re.search(pattern, anchor.text, re.I):
            continue
        group = [anchor]
        while len(group) < 12 and not re.search(r"(?:taniej|grosz|złotych|złoty|zapasów)\.?$", group[-1].text, re.I):
            previous = group[-1]
            candidates = [t for t in lines if t not in group and abs(t.x-anchor.x) < 25
                          and t.box[1] > previous.box[1]+1
                          and -8 <= t.box[1]-previous.box[3] <= 5
                          and abs(t.size-anchor.size) < 1]
            if not candidates:
                break
            group.append(min(candidates,key=lambda t:t.box[1]))
        result.append(Text(clean(" ".join(t.text for t in group)),
                           (min(t.box[0] for t in group),min(t.box[1] for t in group),
                            max(t.box[2] for t in group),max(t.box[3] for t in group))))
    return result


def minimum_assignment(costs):
    """Rectangular Hungarian assignment, with at least as many columns as rows."""
    if not costs:
        return []
    n, m = len(costs), len(costs[0])
    u, v, p, way = [0.]*(n+1), [0.]*(m+1), [0]*(m+1), [0]*(m+1)
    for i in range(1,n+1):
        p[0], j0 = i, 0
        distance, used = [float("inf") ]*(m+1), [False]*(m+1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1,m+1):
                if not used[j]:
                    cur = costs[i0-1][j-1]-u[i0]-v[j]
                    if cur < distance[j]:
                        distance[j], way[j] = cur, j0
                    if distance[j] < delta:
                        delta, j1 = distance[j], j
            for j in range(m+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    distance[j] -= delta
            j0 = j1
            if not p[j0]:
                break
        while j0:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
    return [(p[j]-1,j-1) for j in range(1,m+1) if p[j]]


def text_campaign_sections(page, notes):
    """Infer full-width campaign bands only when a heading agrees with its footer.

    Some flyers paint the entire page as one image, so vector rectangles do
    not describe its panels. Repeated legal footers delimit these bands;
    matching a second-product heading and its exact legal discount is required.
    """
    footers = [note for note in notes if note["kind"] == "validity"
               and note["box"][0] < page.rect.width * .12
               and note["box"][2] - note["box"][0] > page.rect.width * .25]
    footers.sort(key=lambda note: note["box"][3])
    # Separate clauses at the same height do not establish full-width bands.
    if any(abs(a["box"][3] - b["box"][3]) < 3 for a, b in zip(footers, footers[1:])):
        return []
    headings = [Text(clean(b[4]), tuple(b[:4])) for b in page.get_text("blocks")
                if re.search(r"DRUGI PRODUKT", clean(b[4]), re.I)
                and re.search(r"-\d{1,2}\s*%", clean(b[4]))]
    bands = []
    top = 0
    corroborated = False
    for note in footers:
        bottom = min(page.rect.height, note["box"][3] + 1)
        rect = pymupdf.Rect(0, top, page.rect.width, bottom)
        discount = re.search(r"Produkt w tej samej cenie lub tańszy kupisz\s+(\d{1,2})%\s+taniej",
                             note["text"], re.I)
        if discount:
            if not any(rect.contains(pymupdf.Point(h.x, h.y))
                       and re.search(r"-" + discount[1] + r"\s*%", h.text) for h in headings):
                return []
            corroborated = True
        bands.append(rect)
        top = bottom
    return bands if corroborated else []


class PageParser:
    def __init__(self, page, year: int):
        self.page = page
        self.width, self.height = page.rect.width, page.rect.height
        self.year = year
        self.lines = extract_lines(page)
        self.conditions = paragraphs(self.lines,r"^Kup (?:produkt|dwa|balsam|pastę)")
        self.validity = paragraphs(self.lines,r"\*+\s*Promocja obowiązuje")
        self.legal_notes = extract_legal_notes(page, year, parse_period)
        self.text_sections = text_campaign_sections(page, self.legal_notes)
        self.captions = caption_groups(self.lines)
        self.headers = [t for t in self.lines if DATE_RE.fullmatch(t.text.strip(" *"))
                        and parse_period(t.text, year) and t.size >= 10]
        self.drawings = page.get_drawings()
        self.rules = [d["rect"] for d in self.drawings
                      if (d["rect"].height < 2 and d["rect"].width > self.width*.12)
                      or (d["rect"].width < 2 and d["rect"].height > self.height*.10)]
        self.images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()
                       if 15 < pymupdf.Rect(i["bbox"]).width < self.width * .5
                       and 45 < pymupdf.Rect(i["bbox"]).height < self.height * .6]
        self.prices = []
        for t in self.lines:
            if t.size >= 16 and re.fullmatch(r"\d{3,6}", t.text):
                self.prices.append((t, int(t.text) / 100))

    def assign_captions(self, hotspots: list[dict], products: dict | None = None):
        """Match PDF captions to product images at cart coordinates, without product requests."""
        if products is not None:
            return self.assign_verified_captions(hotspots, products)
        pairs = []
        for index, h in enumerate(hotspots):
            px = (h["x"] + h["width"]/2) * self.width
            py = (h["y"] + h["height"]/2) * self.height
            def distance(rect, x, y):
                return (max(rect.x0-x, x-rect.x1, 0)**2 + max(rect.y0-y, y-rect.y1, 0)**2)**.5
            nearby = [r for r in self.images if distance(r, px, py) < self.width * .05
                      and abs(r.y1-py) < self.height * .12]
            image = min(nearby, key=lambda r: abs(r.x1-px)+abs(r.y1-py)) if nearby else None
            for ci, caption in enumerate(self.captions):
                if not re.search(r"\d\s*(?:ml|g|szt|opak|kaps|cm)|(?:krem|tusz|żel|szampon|pomadka|podkład|mask[ai])", caption.text, re.I):
                    continue
                rect = pymupdf.Rect(caption.box)
                if image:
                    gap = (max(rect.x0-image.x1, image.x0-rect.x1, 0)**2 + max(rect.y0-image.y1, image.y0-rect.y1, 0)**2)**.5
                    cost = gap + .07 * distance(rect, px, py)
                else:
                    cost = distance(rect, px, py)
                if cost < self.width * .35:
                    pairs.append((cost, index, ci, image is not None))
        assigned = {}
        # Optimize the entire page: a greedy nearest match can steal a neighbour's caption.
        costs = [[self.width * 3 for _ in hotspots] + [self.width*.35]*len(self.captions)
                 for _ in self.captions]
        pair_info = {}
        for cost, hi, ci, image_found in pairs:
            costs[ci][hi] = cost
            pair_info[(ci, hi)] = (cost, image_found)
        for ci, hi in minimum_assignment(costs):
            if hi < len(hotspots) and (ci, hi) in pair_info:
                cost, image_found = pair_info[(ci, hi)]
                assigned[hi] = (self.captions[ci], cost, image_found)
        # A ruled product cell is stronger evidence than a nearby photo.
        for hi, h in enumerate(hotspots):
            point = (h["x"]+h["width"]/2, h["y"]+h["height"]/2)
            cell = self.cell(point)
            if cell is not None:
                captions = [c for c in self.captions if cell.contains(pymupdf.Point(c.x,c.y))]
                if len(captions) == 1:
                    assigned[hi] = (captions[0], 0, True)
                else:
                    assigned.pop(hi,None)
        return assigned

    def assign_verified_captions(self, hotspots, products):
        """Identity first; several explicitly advertised variants may share copy."""
        assigned = {}
        self.identity_matches = {}
        for index, hotspot in enumerate(hotspots):
            product = products.get(product_id(hotspot["productId"]), {})
            px = (hotspot["x"] + hotspot["width"]/2) * self.width
            py = (hotspot["y"] + hotspot["height"]/2) * self.height
            candidates = []
            for ci, caption in enumerate(self.captions):
                identity = caption_identity(caption.text, product)
                if identity:
                    distance = abs(caption.x-px)/self.width + abs(caption.y-py)/self.height
                    candidates.append((identity["score"], -distance, ci, identity))
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            if not candidates:
                continue
            best = candidates[0]
            identity = dict(best[3])
            identity["ambiguous"] = bool(len(candidates) > 1
                and best[0]-candidates[1][0] < .08
                and abs(best[1]-candidates[1][1]) < .15)
            identity["caption_index"] = best[2]
            self.identity_matches[index] = identity
            assigned[index] = (self.captions[best[2]], 0, True)
        # Sharing a specific single-product caption is a conflict, not a reason
        # to shift one of the products to a less suitable neighbour.
        for index, identity in self.identity_matches.items():
            others = [hi for hi, other in self.identity_matches.items()
                      if other["caption_index"] == identity["caption_index"]]
            if len({product_id(hotspots[hi]["productId"]) for hi in others}) > 1:
                identities = {(folded(products[product_id(hotspots[hi]["productId"])]["name"]),
                               folded(products[product_id(hotspots[hi]["productId"])].get("description", "")))
                              for hi in others}
                if len(identities) == 1:
                    identity["shared_variants"] = True
                elif not identity["shared_variants"]:
                    identity["ambiguous"] = True
        return assigned

    def cell(self, point):
        px, py = point[0]*self.width, point[1]*self.height
        horizontal = [r for r in self.rules if r.height < 2 and r.x0-3 <= px <= r.x1+3]
        vertical = [r for r in self.rules if r.width < 2 and r.y0-15 <= py <= r.y1+15]
        # Inset horizontal rules also describe the column edges.
        near = [r for r in horizontal if abs(r.y0-py) < self.height*.24]
        if not near:
            return None
        row = min(near, key=lambda r: abs(r.y0-py))
        left = max([r.x0 for r in vertical if r.x0 < px] + [row.x0-3])
        right = min([r.x0 for r in vertical if r.x0 > px] + [row.x1+3])
        top = max([r.y0 for r in horizontal if r.y0 < py] + [self.section(point).y0])
        bottom = min([r.y0 for r in horizontal if r.y0 > py]
                     + [r.y1 for r in vertical if r.y1 > py]
                     + [self.section(point).y1])
        rect = pymupdf.Rect(left, top, right, bottom)
        return rect if rect.width > 30 and rect.height > 40 else None

    def section(self, point):
        x, y = point[0] * self.width, point[1] * self.height
        candidates = []
        for d in self.drawings:
            r = d["rect"]
            if (d["fill"] and min(d["fill"]) > .1 and len(d["items"]) == 1
                    and d["items"][0][0] == "re" and r.contains(pymupdf.Point(x, y))
                    and r.get_area() > self.width * self.height * .12):
                candidates.append(r)
        # Broadest panel below the full page avoids image clipping rectangles.
        candidates = [r for r in candidates if r.get_area() < self.width * self.height * .94]
        text_section = next((rect for rect in getattr(self, "text_sections", [])
                             if rect.contains(pymupdf.Point(x, y))), None)
        # A decorative footer rectangle may overlap product icons. It cannot
        # overrule a campaign band corroborated by its heading and legal note.
        if text_section is not None:
            candidates = [rect for rect in candidates if text_section.contains(rect)]
        if candidates:
            return max(candidates, key=lambda r: r.get_area())
        return text_section if text_section is not None else self.page.rect

    def period(self, point, caption: Text | None, default):
        return self.period_resolution(point,caption,default)["period"]

    def _legal_notes(self):
        if hasattr(self,"legal_notes"):
            return self.legal_notes
        notes = extract_legal_notes(self.page,self.year,parse_period)
        if not notes:
            for note in self.validity:
                notes.extend(split_legal_text(note.text,note.box,self.year,parse_period))
        return notes

    def _notes_for_marker(self, point, marker):
        notes = [n for n in self._legal_notes() if n["marker"] == marker]
        cell, section = self.cell(point), self.section(point)
        def inside(note, rect, margin=3):
            box = pymupdf.Rect(note["box"])
            expanded = pymupdf.Rect(rect.x0-3,rect.y0-3,rect.x1+3,rect.y1+margin)
            return expanded.contains(pymupdf.Point((box.x0+box.x1)/2,(box.y0+box.y1)/2))
        if cell:
            local = [n for n in notes if inside(n,cell)]
            if local:
                return local
        if section != self.page.rect:
            local = [n for n in notes if inside(n,section,12)]
            if local:
                return local
        # A common page footer can serve several products. A note inside a
        # different product cell cannot become an override just by proximity.
        return [n for n in notes if n["box"][1] >= self.height*.90
                or n["box"][2]-n["box"][0] >= self.width*.65]

    def _product_markers(self, caption):
        if caption is None:
            return set()
        found = markers(caption.text)
        # Superscript stars can be separate PDF lines rather than part of the name.
        for star in getattr(self,"lines",[]):
            if not re.fullmatch(r"[\s*]+",star.text):
                continue
            for line in getattr(self,"lines",[]):
                if (line.text != star.text and pymupdf.Rect(caption.box).contains(pymupdf.Point(line.x,line.y))
                        and 0 <= star.box[0]-line.box[2] <= 8
                        and line.box[1]-6 <= star.y <= line.box[3]):
                    found.update(markers(star.text))
        return found

    def _campaign_markers(self, point):
        cell, section = self.cell(point), self.section(point)
        found = set()
        for text in getattr(self,"lines",[]):
            if "*" not in text.text or re.search(r"Promocja|Marka dostępna",text.text,re.I):
                continue
            standalone = bool(re.fullmatch(r"[\s*]+",text.text))
            global_heading = (standalone and text.size >= 10) or text.size > 16
            local = (cell is not None and cell.contains(pymupdf.Point(text.x,text.y))) or (
                section != self.page.rect and pymupdf.Rect(section.x0-3,section.y0-12,
                section.x1+3,section.y1+12).contains(pymupdf.Point(text.x,text.y)))
            if global_heading or (standalone and local):
                found.update(markers(text.text))
        return found

    def period_resolution(self, point, caption: Text | None, default, default_evidence=None):
        related = []
        def resolve(references, source, require_note):
            chosen, unresolved = [], []
            for marker in sorted(references,key=len):
                notes = self._notes_for_marker(point,marker)
                related.extend(notes)
                valid = [n for n in notes if n["kind"] == "validity"]
                if (require_note and not notes) or any(n["period"] is None for n in valid):
                    unresolved.append(marker)
                chosen.extend(valid)
            periods = {tuple(n["period"]) for n in chosen if n["period"]}
            if unresolved or len(periods) > 1:
                warning = ("Nie odczytano przypisu daty " + ", ".join(unresolved)) if unresolved else "Sprzeczne daty w przypisach tej oferty"
                return {"period":None,"source":"unresolved_footnote" if unresolved else "conflicting_footnotes",
                        "markers":sorted(references,key=len),"legal_notes":chosen,"related_notes":list(related),
                        "warnings":[warning]}
            if periods:
                return {"period":next(iter(periods)),"source":source,
                        "markers":sorted({n["marker"] for n in chosen},key=len),"legal_notes":chosen,
                        "related_notes":list(related),"warnings":[]}
            return None

        # A product's exact marker overrides campaign headings and default dates.
        result = resolve(self._product_markers(caption),"product_footnote",True)
        if result:
            return result
        result = resolve(self._campaign_markers(point),"campaign_footnote",False)
        if result:
            return result
        # A dedicated product cell may contain its own legal validity paragraph.
        cell = self.cell(point)
        if cell:
            local = [n for n in self._legal_notes() if n["kind"] == "validity" and cell.contains(
                pymupdf.Point((n["box"][0]+n["box"][2])/2,(n["box"][1]+n["box"][3])/2))]
            if local:
                result = resolve({n["marker"] for n in local},"local_footnote",True)
                if result:
                    return result

        # A page can print one common legal footer without repeating its star
        # beside each item (e.g. the fragrance page of the large-format flyer).
        # Do not fall back to the catalogue-wide dates in that case.
        page_notes = [note for note in self._legal_notes() if note["kind"] == "validity"]
        if not self.headers and len(page_notes) == 1 and page_notes[0]["box"][1] >= self.height * .90:
            result = resolve({page_notes[0]["marker"]}, "page_footnote", True)
            if result:
                return result

        section = self.section(point)
        px, py = point[0] * self.width, point[1] * self.height
        headers = self.headers
        if headers:
            inside = [h for h in headers if section.contains(pymupdf.Point(h.x, h.y))]
            if not inside and section != self.page.rect:
                inside = [h for h in headers if section.y0 <= h.y <= section.y1]
            candidates = inside or headers
            if len({parse_period(h.text, self.year) for h in candidates}) == 1:
                header = candidates[0]
            else:
                # Headers run across the top or down the right edge of a panel.
                def score(h):
                    vertical = h.box[3] - h.box[1] > h.box[2] - h.box[0]
                    return (abs(h.y - py) / self.height if vertical else
                            abs(h.x - px) / self.width + max(0, h.y - py) / self.height)
                header = min(candidates, key=score)
            return {"period":parse_period(header.text,self.year),"source":"page_header",
                    "header_text":header.text,"header_box":list(header.box),
                    "legal_notes":[],"related_notes":related,"markers":[],"warnings":[]}
        else:
            return {"period":default,"source":"catalogue_legal" if default else "missing",
                    "legal_notes":[],"related_notes":related,"markers":[],"warnings":[],
                    **(default_evidence or {})}

    def price(self, point, caption: Text | None):
        cell = self.cell(point)
        if cell and (caption is None or cell.contains(pymupdf.Point(caption.x,caption.y))):
            prices = [(t,value) for t,value in self.prices if cell.contains(pymupdf.Point(t.x,t.y))]
            if len(prices) == 1:
                return prices[0][1]
            if not prices:
                return None
        if not caption:
            return None
        section = self.section(point)
        candidates = [(t, value) for t, value in self.prices
                      if section.contains(pymupdf.Point(t.x, t.y))]
        ranked = []
        for t, value in candidates:
            dx = abs(t.box[0] - caption.box[0]) / self.width
            dy = (abs(t.y-caption.box[1]-10) if t.box[2] < caption.box[0]
                  else abs(t.box[1]-caption.box[3])) / self.height
            # Prices commonly sit under the caption or to its left on the cover.
            if t.box[0] > caption.box[2] + 5 or dy > .17 or dx > .35:
                continue
            score = dx * .8 + dy * 2
            ranked.append((score, t, value))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][2]

    def bundle_promotion(self, point, caption: Text | None, name: str = "", brand: str = ""):
        """Use the printed purchase condition, rather than a nearby unrelated price."""
        brand = brand or next((t for t in name.split() if len(t) >= 3), "")
        blocks = [t for t in self.conditions if re.search(r"drugi|drugą|brelok",t.text,re.I)]
        px, py = point[0] * self.width, point[1] * self.height
        cell = self.cell(point)
        section = self.section(point)
        if cell:
            blocks = [b for b in blocks if cell.contains(pymupdf.Point(b.x,b.y))]
        elif section != self.page.rect:
            blocks = [b for b in blocks if section.contains(pymupdf.Point(b.x,b.y))]
            if not caption:
                for note in self.validity:
                    if ("Kup produkt" in note.text and abs(note.box[1]-section.y1) < 20
                            and note.box[2]-note.box[0] > section.width*.6):
                        if re.search(r"\bmarki\b",note.text,re.I) and not brand_matches(note.text,brand):
                            continue
                        return note.text[note.text.index("Kup produkt"):]
        # Offer panels at the bottom have a descriptive condition close to the icon.
        branded = [t for t in blocks if brand_matches(t.text, brand)]
        # Never assign a nearby brand's campaign to an unidentified product.
        candidates = [(abs(t.x-px)/self.width + abs(t.y-py)/self.height, t) for t in branded]
        if candidates:
            distance, text = min(candidates, key=lambda item: item[0])
            if distance < .40 and (caption is None or (branded and distance < .20)):
                return text.text
        # Large "second product" campaigns have no product captions/prices.
        if caption is None:
            labels = []
            for b in self.page.get_text("blocks"):
                text = clean(b[4])
                if "DRUGI PRODUKT" in text and re.search(r"-\d+\s*%", text):
                    labels.append(Text(text, tuple(b[:4])))
            labels = [label for label in labels
                      if section.contains(pymupdf.Point(label.x, label.y))]
            if labels:
                nearest = min(labels, key=lambda t: abs(t.x-px)/self.width + abs(t.y-py)/self.height)
                # Require the same panel when the page has multiple campaigns.
                section = self.section(point)
                if section != self.page.rect and section.contains(pymupdf.Point(nearest.x, nearest.y)):
                    local_brands = [b for b in self.conditions if re.search(r"\bmarki\b",b.text,re.I)
                                    and section.contains(pymupdf.Point(b.x,b.y))]
                    if local_brands and not any(brand_matches(b.text,brand) for b in local_brands):
                        return None
                    percent = re.search(r"-\d+\s*%", nearest.text)[0].replace(" ", "")
                    mix = " Miksuj w ramach marki." if any("MIKSUJ W RAMACH MARKI" in t.text and section.contains(pymupdf.Point(t.x,t.y)) for t in self.lines) else ""
                    return f"Drugi produkt {percent} (w tej samej cenie lub tańszy).{mix}"
        return None


def catalogue_period_evidence(document, year):
    for page in reversed(document):
        for block in page.get_text("blocks"):
            text = clean(block[4])
            match = re.search(r"Oferta obowiązuje w dniach\s+(.+)",text,re.I)
            if match and (period := parse_period(match[1],year)):
                return {"period":period,"source":"catalogue_legal","legal_text":text,
                        "legal_box":list(block[:4]),"page":page.number+1}
    return {}


def default_period(settings: dict, document):
    all_text = " ".join(settings.get("pageTexts", []))
    years = Counter(re.findall(r"\b20\d{2}\b", all_text))
    year = int(years.most_common(1)[0][0]) if years else date.today().year
    evidence = catalogue_period_evidence(document,year)
    # Do not turn the dates of an individual promotion into the catalogue's default.
    return year, evidence.get("period")


def parse_offers(settings: dict, hotspots: list[dict], pdf: Path, products: dict | None = None):
    offers = []
    with pymupdf.open(pdf) as doc:
        if len(doc) != len(settings["pages"]):
            raise ValueError("Liczba stron PDF nie zgadza się z aktualną gazetką.")
        year, default = default_period(settings, doc)
        default_evidence = catalogue_period_evidence(doc,year)
        parsers = {i: PageParser(doc[i], year) for i in range(len(doc))}
        by_page = {i: [h for h in hotspots if h["pageIndex"] == i] for i in parsers}
        assigned = {i: parsers[i].assign_captions(by_page[i], products) for i in parsers}
        for hotspot in hotspots:
            pid = product_id(hotspot["productId"])
            page_number = hotspot["pageIndex"]
            parser = parsers[page_number]
            point = (hotspot["x"] + hotspot["width"]/2,
                     hotspot["y"] + hotspot["height"]/2)
            local_index = by_page[page_number].index(hotspot)
            match = assigned[page_number].get(local_index)
            caption = match[0] if match else None
            product = (products or {}).get(pid, {})
            name = product.get("name") or (caption.text if caption else "")
            date_evidence = parser.period_resolution(point,caption,default,default_evidence)
            date_evidence.setdefault("page",page_number+1)
            period = date_evidence["period"]
            price = parser.price(point, caption) if caption else None
            bundle = None if price is not None else parser.bundle_promotion(point, caption, name, product.get("brand", ""))
            promotion = bundle or (f"Cena z gazetki: {price:.2f} zł".replace(".", ",") if price is not None else "DO SPRAWDZENIA — nie rozpoznano promocji")
            warnings = list(date_evidence["warnings"])
            if not period:
                warnings.append("Nie rozpoznano daty")
            if not bundle and price is None:
                warnings.append("Nie rozpoznano promocji")
            section = parser.section(point)
            identity = getattr(parser,"identity_matches",{}).get(local_index,{})
            if identity.get("ambiguous"):
                warnings.append("Niejednoznaczne dopasowanie nazwy lub wariantu w gazetce")
                promotion = "DO SPRAWDZENIA — niejednoznaczny produkt przy ofercie"
            if not product.get("name"):
                warnings.append("Nie zweryfikowano tożsamości produktu w danych Hebe")
                promotion = "DO SPRAWDZENIA — brak tożsamości produktu z Hebe"
            method = ("shared_variant_caption" if identity.get("shared_variants") else "product_identity") if caption else (
                       "campaign_panel" if bundle else "unmatched")
            evidence = {"hotspot_id": hotspot["id"], "match_method": method,
                        "flyer_caption": caption.text if caption else "",
                        "caption_box": list(caption.box) if caption else None,
                        "panel_box": list(section), "identity": identity,
                        "printed_price": price, "printed_condition": bundle,
                        "promotion_source": "flyer_pdf", "date":date_evidence}
            if method == "unmatched" and product.get("name"):
                branded = [c for c in parser.captions if brand_matches(c.text,
                    product.get("brand") or product["name"].split()[0])]
                if branded:
                    nearest = min(branded, key=lambda c: abs(c.x-point[0]*parser.width)/parser.width
                                  + abs(c.y-point[1]*parser.height)/parser.height)
                    evidence["nearby_caption_unverified"] = nearest.text
                    warnings.append("Opis w gazetce nie zgadza się z produktem wskazanym przez koszyk")
            offers.append(Offer(period_label(period), pid, promotion, page_number+1,
                                *(period or (None, None)), warnings, evidence=evidence))
    # The same ID may appear with several offers; remove only exact repeated occurrences.
    return list({(o.id, o.start, o.end, o.promotion): o for o in offers}.values())


async def request_text(context, url: str, attempts=3):
    for attempt in range(attempts):
        response = await context.request.get(url, timeout=60000)
        if response.ok:
            return await response.text()
        if response.status not in (429, 500, 502, 503, 504):
            raise RuntimeError(f"HTTP {response.status}: {url}")
        await asyncio.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Nie udało się pobrać {url}")


async def collect(args, work: Path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, channel=args.browser_channel)
        context = await browser.new_context(locale="pl-PL", accept_downloads=True)
        try:
            page = await context.new_page()
            LOG.info("Sprawdzam aktualną gazetkę: %s", args.url)
            await page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
            if "Just a moment" in await page.title():
                if not args.headless:
                    LOG.warning("Hebe wymaga weryfikacji w oknie Chrome. Dokończ ją w przeglądarce (do 120 s).")
                    await page.wait_for_function("document.title !== 'Just a moment...'", timeout=120000)
                else:
                    raise RuntimeError("Hebe wymaga weryfikacji przeglądarki. Uruchom ponownie bez --headless.")
            if "ipaper.io" not in urlparse(page.url).netloc:
                iframe = page.locator("iframe#IPaperMagazine")
                await iframe.wait_for(state="attached", timeout=45000)
                viewer_url = await iframe.get_attribute("src")
                if not viewer_url or "ipaper.io" not in urlparse(viewer_url).netloc:
                    raise RuntimeError("Strona Hebe nie udostępnia obsługiwanej gazetki iPaper.")
                page = await context.new_page()
                await page.goto(urljoin(args.url, viewer_url), wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_function("window.staticSettings?.enrichments?.chunkUrls", timeout=45000)
            settings = await page.evaluate("window.staticSettings")
            LOG.info("Gazetka: %s, %d stron", settings["name"], len(settings["pages"]))
            # URLs and signed CDN tokens are obtained anew for every run.
            hotspots = []
            for url in set(settings["enrichments"]["chunkUrls"].values()):
                payload = json.loads(await request_text(context, url))
                hotspots.extend(e for e in payload["enrichments"] if e.get("productId"))
            hotspots = list({(e["pageIndex"], e["id"]): e for e in hotspots}.values())
            if not hotspots:
                raise RuntimeError("Gazetka nie zawiera klikalnych produktów — przerywam bez pustego Excela.")
            async with page.expect_download(timeout=90000) as event:
                await page.get_by_role("button", name=re.compile("Download PDF|Pobierz PDF", re.I)).click()
            download = await event.value
            pdf = work / "gazetka.pdf"
            await download.save_as(pdf)
            products = await fetch_products_info([e["productId"] for e in hotspots],
                                                 archive_dir=work / "productsInfo")
            missing = [e["productId"] for e in hotspots if product_id(e["productId"]) not in products]
            if missing:
                LOG.info("QuickView uzupełni %d produktów pominiętych przez productsInfo",len(set(missing)))
                names_context = await browser.new_context(locale="pl-PL")
                try:
                    products.update(await fetch_names(names_context,missing,headless=args.headless,
                                                     archive_dir=work / "quickview"))
                finally:
                    await names_context.close()
            metadata = {"fetched_at": datetime.now().astimezone().isoformat(),
                        "source": args.url, "viewer": settings["paperCompleteUrl"],
                        "settings": settings, "hotspots": hotspots, "products": products,
                        "names_skipped": args.skip_names}
            (work / "source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
            return settings, hotspots, pdf, metadata
        finally:
            await browser.close()


def write_excel(offers: list[Offer], output: Path, source: str):
    """Portable exporter for subsequent runs outside the Codex environment."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = Workbook()
    ws = wb.active
    ws.title = "Promocje"
    ws.append(["Produkt", "ID", "Link produktu", "Data obowiązywania", "Promocja",
               "Porównanie oferty online", "Bieżąca oferta online", "Promocja cenowa online",
               "Strona gazetki"])
    widths = {"A":65, "B":15, "C":60, "D":29, "E":100, "F":39,
              "G":36, "H":24, "I":12}
    for i, offer in enumerate(offers, 2):
        product_label = offer.product or "ID " + offer.id
        if offer.product_description:
            product_label += " - " + offer.product_description
        product_label = product_label.replace("—", "-").replace("–", "-")
        verification = offer.verification
        live = []
        if verification.get("online_price") is not None:
            live.append(str(verification["online_price"]).replace(".", ",") + " zł")
        live.extend(b.get("text") or b["callout"] for b in verification.get("online_promotions", [])
                    if b.get("text") or b.get("callout"))
        on_promotion = verification.get("online_on_promotion")
        flag_label = "Tak" if on_promotion is True else "Nie" if on_promotion is False else "Brak danych"
        ws.append([product_label, offer.id, offer.product_url, offer.period, offer.promotion,
                   verification.get("label", "Brak danych"), "; ".join(live) or "Brak danych",
                   flag_label, str(offer.page)])
        for cell in ws[i]:
            # Source strings (including IDs and badges) must never become formulas.
            cell.data_type = "s"
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            cell.font = Font(name="Arial", size=11)
            if offer.warnings:
                cell.fill = PatternFill("solid", fgColor="FFF2CC")
        ws.cell(i, 2).number_format = "@"
        for col in (4, 8, 9):
            ws.cell(i, col).alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
        if offer.product_url:
            ws.cell(i, 3).hyperlink = offer.product_url
        if verification.get("online_url"):
            ws.cell(i, 7).hyperlink = verification["online_url"]
        if offer.warnings:
            ws.cell(i, 5).value += "\nDO SPRAWDZENIA: " + "; ".join(offer.warnings) + f" (strona {offer.page})"
        row_lines = 1
        for cell in ws[i]:
            wrap_width = max(1, int(widths[cell.column_letter] * .85))
            line_count = sum(max(1, len(textwrap.wrap(line, width=wrap_width)))
                             for line in str(cell.value or "").splitlines())
            row_lines = max(row_lines, line_count)
        ws.row_dimensions[i].height = max(36, 15 * row_lines)
    if offers:
        table = Table(displayName="PromocjeHebe", ref=f"A1:I{len(offers)+1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)
    for cell in ws[1]:
        cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="9D005D")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "C2"
    ws.sheet_view.showGridLines = False
    wb.properties.description = f"Źródło: {source}. Daty i promocje pochodzą z gazetki. Kolumny weryfikacji zawierają porównanie z API productsInfo lub QuickView. Szczegółowe dowody są zapisane w offers.json."
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.xlsx")
    wb.save(temporary)
    temporary.replace(output)


async def process_flyer(args, output: Path):
    work = output.parent / (output.stem + "-dane")
    work.mkdir(parents=True, exist_ok=True)
    if args.replay:
        metadata = json.loads((args.replay / "source.json").read_text())
        settings, hotspots, pdf = metadata["settings"], metadata["hotspots"], args.replay / "gazetka.pdf"
        if args.replay.resolve() != work.resolve():
            shutil.copy2(pdf,work / "gazetka.pdf")
            (work / "source.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")
            for folder in ("quickview","productsInfo"):
                if (args.replay / folder).is_dir():
                    shutil.copytree(args.replay / folder,work / folder,dirs_exist_ok=True)
    else:
        settings, hotspots, pdf, metadata = await collect(args, work)
    offers = parse_offers(settings, hotspots, pdf, metadata.get("products",{}))
    enrich_offers(offers,metadata.get("products",{}),skipped=args.skip_names or metadata.get("names_skipped",False))
    verify_offers(offers,metadata.get("products",{}),metadata["fetched_at"])
    report = {"source": metadata["source"], "viewer": metadata["viewer"],
              "fetched_at": metadata["fetched_at"], "catalogue": settings["name"],
              "count": len(offers), "offers": [asdict(o) for o in offers]}
    report["verification_summary"] = dict(Counter(o.verification["status"] for o in offers))
    report["date_sources"] = dict(Counter(o.evidence["date"]["source"] for o in offers))
    (work / "offers.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.json_only:
        write_excel(offers, output, metadata["viewer"])
        LOG.info("Zapisano %s (%d wierszy)", output.resolve(), len(offers))
    warnings = sum(bool(o.warnings) for o in offers)
    LOG.info("Weryfikacja oferty online: %s", report["verification_summary"])
    LOG.info("Źródła dat: %s",report["date_sources"])
    LOG.info("Wiersze do sprawdzenia: %d. Dane źródłowe: %s", warnings, work.resolve())
    return 2 if warnings and args.strict else 0


def selected_runs(args, output: Path):
    """Keep each edition's workbook and source archive separate."""
    if args.replay or args.url:
        return [(args.url or HOME, output)]
    magazine = args.magazine or "digital"
    if magazine != "both":
        return [(MAGAZINES[magazine], output)]
    return [(url, output.with_name(f"{output.stem}-{kind}{output.suffix}"))
            for kind, url in MAGAZINES.items()]


async def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--magazine", choices=["digital", "plachta", "both"],
                        help="Rodzaj gazetki: digital (domyślnie), plachta lub both (obie)")
    source.add_argument("--url", help="Własna strona gazetki Hebe lub bezpośredni adres iPaper")
    source.add_argument("--replay", type=Path, help="Ponownie przetwórz zachowane dane z katalogu uruchomienia")
    ap.add_argument("--output", type=Path, help="Docelowy plik .xlsx; dla obu gazetek dodaje -digital i -plachta do nazwy")
    ap.add_argument("--headless", action="store_true", help="Ukryj przeglądarkę (Hebe może zablokować ten tryb)")
    ap.add_argument("--browser-channel", default="chrome", choices=["chrome", "chromium"], help="Przeglądarka (domyślnie zainstalowany Chrome)")
    ap.add_argument("--json-only", action="store_true", help="Pomiń eksport Excel, zachowaj dane JSON")
    ap.add_argument("--skip-names", action="store_true", help="Ukryj nazwy; dane produktu są nadal pobierane do weryfikacji ofert")
    ap.add_argument("--strict", action="store_true", help="Zwróć kod 2, jeśli dowolny wiersz wymaga sprawdzenia")
    args = ap.parse_args(argv)
    output = args.output or Path("outputs") / datetime.now().strftime("%Y%m%d-%H%M%S-%f") / "hebe.xlsx"
    if output.suffix.lower() != ".xlsx":
        ap.error("--output musi kończyć się na .xlsx")
    runs = selected_runs(args, output)
    results = []
    failed = False
    for url, target in runs:
        run_args = argparse.Namespace(**(vars(args) | {"url": url}))
        try:
            results.append(await process_flyer(run_args, target))
        except (RuntimeError, ValueError, OSError, PlaywrightError) as exc:
            if len(runs) == 1:
                raise
            failed = True
            LOG.error("Nie przetworzono gazetki %s: %s", url, exc)
    # A failed edition outranks review warnings, even if the other export succeeded.
    return 1 if failed else max(results, default=0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        sys.exit(asyncio.run(main()))
    except (RuntimeError, ValueError, OSError, PlaywrightError) as exc:
        LOG.error("%s", exc)
        sys.exit(1)
