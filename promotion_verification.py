"""Compare each flyer offer with dated, ID-checked API or QuickView evidence.

QuickView is a live online offer. Its price never replaces a printed flyer price
and its campaign IDs are never interpreted as validity dates.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re


STATUS_LABELS = {
    "price_match": "Cena zgodna z QuickView",
    "campaign_match": "Mechanizm promocji zgodny z QuickView",
    "online_difference": "Różnica oferty online (informacja)",
    "expired": "Oferta z gazetki już zakończona",
    "upcoming": "Oferta z gazetki jeszcze się nie rozpoczęła",
    "identity_conflict": "Nie potwierdzono produktu przy ofercie",
    "unverified": "Brak danych do weryfikacji",
}


def bundle_signature(text: str):
    text = " ".join(text.lower().split())
    if not re.search(r"drugi|drugą|1\s*\+\s*1", text):
        return None
    if re.search(r"1\s*(?:grosz|gr\b)", text):
        return "second_for_0.01"
    percentage = re.search(r"(?:-|kupisz\s+)(\d+)\s*%", text)
    return f"second_discount_{percentage[1]}" if percentage else None


def verify_offers(offers, products: dict, fetched_at: str):
    for offer in offers:
        product = products.get(offer.id, {})
        snapshot = product.get("online") or product.get("quickview", {})
        source = snapshot.get("source","QuickView")
        source_label = "API productsInfo" if source == "productsInfo" else source
        checked_at = snapshot.get("fetched_at", fetched_at)
        badges = snapshot.get("promotions", [])
        # This flag describes the dated online snapshot, not the printed offer.
        # Missing flags (including QuickView archives) are unknown, never false.
        on_promotion = snapshot.get("price_details", {}).get("onPromotion")
        if not isinstance(on_promotion, bool):
            on_promotion = None
        verification = {"checked_at": checked_at, "online_source":source,
                        "online_url":snapshot.get("url"),"online_price":snapshot.get("price"),
                        "online_promotions":badges,"online_promotion_id":snapshot.get("promotion_id"),
                        "online_pricing_text":snapshot.get("pricing_text", ""),
                        "online_on_promotion":on_promotion}
        if source == "QuickView":
            verification.update(quickview_url=snapshot.get("url"),quickview_price=snapshot.get("price"),
                quickview_promotions=badges,quickview_promotion_id=snapshot.get("promotion_id"),
                quickview_pricing_text=snapshot.get("pricing_text",""))
        status, details = "unverified", "Brak zapisanego podglądu cen i promocji QuickView."
        if not product.get("name") or offer.evidence.get("match_method") == "unmatched" or offer.evidence.get("identity",{}).get("ambiguous"):
            status = "identity_conflict"
            details = "Brak jednoznacznego powiązania ID z opisem lub panelem promocji w gazetce."
            if offer.evidence.get("nearby_caption_unverified"):
                details = "QuickView: " + product.get("name", "brak nazwy") + ". Opis przy koszyku nie pasuje do produktu. Nie przypisano jego ceny do tego ID."
        elif snapshot:
            today = datetime.fromisoformat(checked_at).date().isoformat()
            if offer.end and today > offer.end:
                status, details = "expired", "Bieżąca oferta online nie potwierdza historycznej ceny ani warunków z gazetki."
            elif offer.start and today < offer.start:
                status, details = "upcoming", "Bieżąca oferta online nie potwierdza przyszłej ceny ani warunków z gazetki."
            elif not offer.start or not offer.end:
                details = "Brak dat gazetki uniemożliwia porównanie ofert z tego samego okresu."
            else:
                price = offer.evidence.get("printed_price")
                expected = bundle_signature(offer.promotion)
                signatures = {bundle_signature(b.get(field, "")) for b in badges for field in ("text","callout")}
                if expected and expected in signatures:
                    status, details = "campaign_match", "Zgodny rabat na drugi produkt. Pełne warunki i daty pochodzą z gazetki."
                elif price is not None and snapshot.get("price") is not None:
                    try:
                        equal = Decimal(str(price)) == Decimal(str(snapshot["price"]))
                    except InvalidOperation:
                        equal = False
                    status = "price_match" if equal else "online_difference"
                    details = ("Cena drukowana jest zgodna z ceną produktu o tym samym ID." if equal else
                               "Bieżąca cena online różni się od ceny z gazetki. Oferty mogą zależeć od kanału sprzedaży. Zachowano cenę drukowaną; sama różnica nie oznacza błędu przypisania.")
                elif expected or offer.evidence.get("printed_condition"):
                    status, details = "online_difference", "Bieżące dane online pokazują inną promocję lub nie zawierają etykiety tej kampanii. Zachowano warunki i daty z gazetki. Sam brak zgodności online nie oznacza błędu przypisania."
                else:
                    details = "QuickView nie zawiera ceny ani odczytywalnej etykiety tej promocji."
        if on_promotion is not None:
            flag = "true" if on_promotion else "false"
            details += (f"\n{source_label}: onPromotion={flag} w chwili pobrania danych. "
                        "Zgodność ceny ani obecność w gazetce nie potwierdza obniżki. "
                        "Ta flaga dotyczy oferty online; osobne warunki z gazetki pozostają zachowane.")
        label = STATUS_LABELS[status].replace("QuickView",source_label)
        # Only a problem with the flyer extraction needs human correction.
        # A live online difference (or unavailable online comparison) is an
        # observation and must never be promoted to an extraction warning.
        if not offer.start or not offer.end:
            offer.warnings = list(offer.warnings or [])
            if "Nie rozpoznano daty" not in offer.warnings:
                offer.warnings.append("Nie rozpoznano daty")
        if status == "identity_conflict" and not offer.warnings:
            offer.warnings = [label]
        verification.update(status=status,label=label,details=details.replace("QuickView",source_label),
                            requires_review=bool(offer.warnings))
        offer.verification = verification
