"""Match a verified product to printed copy, allowing explicit variant families."""
from __future__ import annotations

import re
import unicodedata


def folded(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower().replace("ł", "l"))
    return " ".join(re.findall(r"[a-z0-9]+", "".join(
        char for char in value if not unicodedata.combining(char))))


def brand_matches(text: str, brand: str) -> bool:
    # Word boundaries keep LAQ separate from unrelated longer brand names.
    words = folded(brand).split()
    return bool(words and re.search(r"\b" + r"\s*".join(map(re.escape, words)) + r"\b",
                                    folded(text)))


STOP = set("do na dla i lub w z ze za o a the of to pod przy oraz ml g szt opak kaps".split())


def tokens(text: str) -> set[str]:
    return {word[:8] for word in folded(text).split()
            if len(word) >= 2 and word not in STOP and not word.isdigit()}


def caption_identity(caption: str, product: dict) -> dict | None:
    """No location/price is allowed to compensate for a different identity."""
    name, description = product.get("name", ""), product.get("description", "")
    brand = product.get("brand") or (name.split()[0] if name else "")
    if not name or not brand_matches(caption, brand):
        return None
    copy = re.split(r"\b\d+\s*(?:ml|g|szt\.?|kaps\.?)\s*=", caption, maxsplit=1)[0]
    available = tokens(copy)
    name_words = tokens(name) - tokens(brand)
    description_words = tokens(description)
    name_coverage = len(name_words & available) / len(name_words) if name_words else 0
    description_coverage = (len(description_words & available) / len(description_words)
                            if description_words else 0)
    shared = bool(re.search(r"(?:różne|wybrane) (?:rodzaje|odcienie|warianty)|"
                            r"\w+\s+lub\s+\w+\s+konsystencji", copy, re.I))
    # Units/volumes disambiguate neighbouring products from the same range.
    def quantities(text):
        return {(amount.replace(",", "."),unit.lower()) for amount,unit in
                re.findall(r"(\d+(?:[.,]\d+)?)\s*(ml|g)\b", text, re.I)}
    printed, actual = quantities(copy), quantities(description)
    if printed and actual and not printed & actual:
        return None
    if not ((name_coverage >= .5 and description_coverage >= .3)
            or (description_coverage >= .65 and len(description_words & available) >= 3)
            or (description_coverage >= .65 and len(description_words & available) >= 2
                and bool(printed & actual))):
        return None
    score = .5 * name_coverage + .4 * description_coverage + .1 * bool(printed & actual)
    return {"score": round(score, 4), "shared_variants": shared,
            "name_coverage": round(name_coverage, 4),
            "description_coverage": round(description_coverage, 4)}
