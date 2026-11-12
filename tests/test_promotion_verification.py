from decimal import Decimal
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from hebe import Offer, PageParser, Text, parse_offers, write_excel
from product_matching import brand_matches, caption_identity
from product_names import parse_product, fetch_names
from promotion_verification import verify_offers


def fixture_parser(data):
    parser = PageParser.__new__(PageParser)
    parser.width, parser.height, parser.year = data["width"], data["height"], 2026
    parser.page = SimpleNamespace(rect=pymupdf.Rect(0, 0, parser.width, parser.height),
                                  get_text=lambda _: [])
    parser.drawings, parser.rules = [], []
    parser.lines = []
    for key in ("captions", "headers", "validity", "conditions"):
        setattr(parser, key, [Text(**value) for value in data[key]])
    parser.prices = [(Text(**text), value) for text, value in data["prices"]]
    return parser


def test_real_flyer_shared_variants_do_not_shift_neighbours_prices_or_dates():
    # Printed copy, positions and hotspot IDs from the 18-page November flyer.
    fixtures = json.loads((Path(__file__).parent / "fixtures/variant-pages.json").read_text())
    expected = {"751678": (28.89, "2026-10-21"), "765122": (24.99, "2026-10-21"),
        "765124": (22.99, "2026-10-21"), "765126": (22.99, "2026-10-21"),
        "759330": (9.89, "2026-10-28"), "759329": (21.89, "2026-10-28"),
        "759328": (23.39, "2026-10-28"), "740820": (16.99, "2026-11-05"),
        "740822": (16.99, "2026-11-05")}
    for data in fixtures:
        parser = fixture_parser(data)
        matches = parser.assign_captions(data["hotspots"], data["products"])
        for index, hotspot in enumerate(data["hotspots"]):
            pid = hotspot["productId"].lstrip("0")
            if pid not in expected:
                continue
            caption = matches[index][0]
            point = (hotspot["x"]+hotspot["width"]/2, hotspot["y"]+hotspot["height"]/2)
            price, start = expected[pid]
            assert parser.price(point, caption) == price, pid
            assert parser.period(point, caption, None) == (start, "2026-11-22"), pid
            assert not parser.identity_matches[index]["ambiguous"]


def test_brand_campaign_does_not_attach_to_unrelated_product():
    data = json.loads((Path(__file__).parent / "fixtures/variant-pages.json").read_text())[1]
    parser = fixture_parser(data)
    assert parser.bundle_promotion((.26,.94), None, "Aquaselin Intensive", "Aquaselin") is None
    assert "WILKINSON" in parser.bundle_promotion((.28,.748), None, "Wilkinson Hydro 5", "Wilkinson")


def test_wide_validity_note_cannot_bypass_brand_identity():
    data = json.loads((Path(__file__).parent / "fixtures/variant-pages.json").read_text())[1]
    parser = fixture_parser(data)
    panel = pymupdf.Rect(0,0,parser.width,parser.height*.6)
    parser.drawings = [{"rect":panel,"fill":(1,1,1),"items":[("re",panel)]}]
    parser.validity = [Text("*Promocja obowiązuje 05-22.11.2026. Kup produkt marki ALFA, a drugi kupisz 60% taniej",
                           (0,panel.y1-5,parser.width,panel.y1+5))]
    assert parser.bundle_promotion((.2,.5),None,"BETA Krem","BETA") is None


def test_flyer_link_to_different_series_is_rejected_even_with_same_price_and_brand():
    assert caption_identity("LA MANIA HOME Lari Stripes Black koc, 150 x 200 cm, 1 szt.",
        {"name":"La Mania Home Bauhaus II", "brand":"La Mania Home", "description":"koc, 1 szt."}) is None
    assert not brand_matches("Kup produkt marki WILKINSON", "Aquaselin")
    assert not brand_matches("LAQ", "La")


def test_two_distinct_products_cannot_share_a_specific_caption():
    data = json.loads((Path(__file__).parent / "fixtures/variant-pages.json").read_text())[0]
    parser = fixture_parser(data)
    parser.captions = [Text("ALFA Glow krem do twarzy, 50 ml", (10,100,100,150))]
    hotspots = [dict(data["hotspots"][0], productId="1"), dict(data["hotspots"][0], productId="2")]
    products = {"1":{"name":"Alfa Glow", "brand":"Alfa", "description":"krem do twarzy, 50 ml"},
                "2":{"name":"Alfa Glow", "brand":"Alfa", "description":"krem do twarzy nocny, 50 ml"}}
    parser.assign_captions(hotspots, products)
    assert all(match["ambiguous"] for match in parser.identity_matches.values())


def quickview_html(pid="123",gtm_pid="123"):
    analytics = json.dumps({"item_id":gtm_pid,"item_brand":"Alfa", "promotion_id":"different-campaign",
                           "current_price":999, "regular_price":999})
    return f'''<div class="product-tile" data-pid="{pid}" data-product-gtm='{analytics}'>
      <a class="product-tile__title" href="/alfa-{pid}.html"><span class="product-tile__name">Alfa</span></a>
      <div class="product-tile__pricing"><div class="price-tile__wrapper">
      <div class="price-tile__sales"><div class="price-tile__formatted">
      <span class="price-tile__whole">24</span><span class="price-tile__decimal">99</span></div></div></div>
      <span class="price-omnibus-text__value">99,99 zł</span><span>Cena z kodem 12,49 zł</span></div>
      <div data-promotion="campaign-1"><span>1+1 -60%</span></div></div>'''


def test_quickview_reads_visible_current_price_instead_of_regular_coupon_or_analytics():
    product = parse_product(quickview_html(), "123")
    assert Decimal(product["quickview"]["price"]) == Decimal("24.99")
    assert product["quickview"]["promotions"] == [{"id":"campaign-1", "text":"1+1 -60%", "images":[]}]
    assert product["brand"] == "Alfa"


def test_quickview_checks_both_tile_and_analytics_ids():
    with pytest.raises(ValueError, match="ID danych"):
        parse_product(quickview_html(gtm_pid="124"), "123")
    body = quickview_html().replace('data-pid="123"', 'data-pid="124"')
    with pytest.raises(ValueError, match="ID kafelka"):
        parse_product(body, "123")


@pytest.mark.parametrize("start,end,price,status", [
    ("2026-11-05","2026-11-22", "24.99", "price_match"),
    ("2026-11-05","2026-11-22", "12.49", "online_difference"),
    ("2026-11-05","2026-11-11", "12.49", "expired"),
    ("2026-11-14","2026-11-22", "12.49", "upcoming"),
])
def test_live_prices_never_replace_flyer_prices_or_validity_dates(start,end,price,status):
    offer = Offer("printed dates", "123", "Cena promocyjna: 24,99 zł", 1, start,end,
                  evidence={"match_method":"product_identity", "printed_price":24.99})
    product = parse_product(quickview_html(), "123")
    product["quickview"].update(fetched_at="2026-11-12T12:00:00+01:00",price=price)
    verify_offers([offer], {"123":product}, "2026-11-12T12:00:00+01:00")
    assert offer.verification["status"] == status
    assert (offer.promotion,offer.start,offer.end) == ("Cena promocyjna: 24,99 zł",start,end)
    assert not offer.warnings
    assert not offer.verification["requires_review"]


def test_bundle_badge_confirms_mechanism_but_never_invents_dates_or_full_terms():
    product = parse_product(quickview_html(), "123")
    product["quickview"]["fetched_at"] = "2026-11-12T12:00:00+01:00"
    promotion = "Kup produkt marki ALFA, a drugi w tej samej cenie lub tańszy kupisz 60% taniej"
    offer = Offer("printed", "123", promotion, 1,"2026-11-05","2026-11-22",
                  evidence={"match_method":"campaign_panel", "printed_condition":promotion})
    verify_offers([offer], {"123":product}, "2026-11-12T12:00:00+01:00")
    assert offer.verification["status"] == "campaign_match"
    assert offer.promotion == promotion
    offer.start = offer.end = None
    verify_offers([offer], {"123":product}, "2026-11-12T12:00:00+01:00")
    assert offer.verification["status"] == "unverified"
    assert offer.start is offer.end is None


def test_old_archives_without_quickview_are_explicitly_unverified():
    offer = Offer("printed", "123", "Cena promocyjna: 24,99 zł", 1,"2026-11-05","2026-11-22",
                  evidence={"match_method":"product_identity", "printed_price":24.99})
    verify_offers([offer], {"123":{"name":"Alfa"}}, "2026-11-12T12:00:00+01:00")
    assert offer.verification["status"] == "unverified"
    assert not offer.warnings
    assert not offer.verification["requires_review"]


def test_source_warning_is_preserved_even_when_online_price_matches():
    product = parse_product(quickview_html(),"123")
    product["quickview"]["fetched_at"] = "2026-11-12T12:00:00+01:00"
    offer = Offer("printed","123","Cena promocyjna: 24,99 zł",1,"2026-11-05","2026-11-22",
                  warnings=["Niepewne przypisanie w PDF"],
                  evidence={"match_method":"product_identity","printed_price":24.99})
    verify_offers([offer],{"123":product},"2026-11-12T12:00:00+01:00")
    assert offer.verification["status"] == "price_match"
    assert offer.warnings == ["Niepewne przypisanie w PDF"]
    assert offer.verification["requires_review"]


def test_unmatched_identity_still_requires_review_without_online_price_conflict():
    product = parse_product(quickview_html(),"123")
    offer = Offer("printed","123","brak oferty",1,"2026-11-05","2026-11-22",
                  evidence={"match_method":"unmatched"})
    verify_offers([offer],{"123":product},"2026-11-12T12:00:00+01:00")
    assert offer.verification["status"] == "identity_conflict"
    assert offer.warnings and offer.verification["requires_review"]


def test_fetch_archives_exact_html_with_checksum(tmp_path):
    class Page:
        async def goto(self, *args, **kwargs):
            return None
        async def title(self):
            return ""
        async def content(self):
            return quickview_html().replace("\n", "\r\n")
        async def close(self):
            self.closed = True
    page = Page()
    async def new_page():
        return page
    products = asyncio.run(fetch_names(SimpleNamespace(new_page=new_page), ["123"],archive_dir=tmp_path))
    snapshot = products["123"]["quickview"]
    raw = quickview_html().replace("\n", "\r\n").encode()
    assert (tmp_path / "123.html").read_bytes() == raw
    assert snapshot["sha256"] == hashlib.sha256(raw).hexdigest()
    assert snapshot["html_file"] == "quickview/123.html"
    assert page.closed


def test_excel_merges_verification_without_losing_literal_badges_or_source_links(tmp_path):
    from openpyxl import load_workbook
    product = parse_product(quickview_html(), "123")
    product["quickview"]["fetched_at"] = "2026-11-12T12:00:00+01:00"
    product["quickview"]["promotions"] = [{"id":"label", "text":"=untrusted label"}]
    offers = [Offer("printed", "123", "Cena promocyjna: 24,99 zł", 1,
        "2026-11-05","2026-11-22",evidence={"match_method":"product_identity", "printed_price":24.99})]
    verify_offers(offers, {"123":product}, "2026-11-12T12:00:00+01:00")
    output = tmp_path / "audit.xlsx"
    write_excel(offers, output, "https://www.hebe.pl/gazetka-hebe")
    workbook = load_workbook(output)
    assert workbook.sheetnames == ["Promocje"]
    ws = workbook["Promocje"]
    assert ws["B2"].value == "123" and ws["B2"].data_type == "s"
    assert ws["G2"].hyperlink.target.endswith("pid=123")
    assert "=untrusted label" in ws["G2"].value and ws["G2"].data_type == "s"
    assert ws["F2"].value == "Cena zgodna z QuickView"
    assert ws["I2"].value == "1"
    assert ws.max_column == 9
    assert ws.freeze_panes == "C2" and ws.tables["PromocjeHebe"].ref == "A1:I2"



def test_standalone_pdf_price_is_not_claimed_to_be_a_discount(tmp_path):
    pdf = tmp_path / "regular-price.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=500, height=800)
        page.insert_text((20, 35), "05-22.11.2026", fontsize=14)
        page.insert_text((100, 530), "39900", fontsize=24)
        page.insert_text((100, 560), "STYLPRO", fontsize=10, fontname="hebo")
        page.insert_text((100, 573), "maska LED do twarzy, 1 opak.", fontsize=10)
        doc.save(pdf)
    products = {"80370095": {"name": "Stylpro", "brand": "Stylpro",
                "description": "maska LED do twarzy, 1 opak."}}
    hotspots = [{"id": 1, "productId": "80370095", "pageIndex": 0,
                 "x": .25, "y": .65, "width": .02, "height": .02}]
    offers = parse_offers({"pages": [{}]}, hotspots, pdf, products)
    assert len(offers) == 1
    assert offers[0].evidence["printed_price"] == 399
    assert offers[0].promotion == "Cena z gazetki: 399,00 zł"
    assert (offers[0].start, offers[0].end) == ("2026-11-05", "2026-11-22")
    assert not offers[0].warnings


@pytest.mark.parametrize("flag", [False, True, None, "false"])
def test_online_discount_flag_is_separate_from_matching_a_printed_price(flag):
    offer = Offer("printed", "80370095", "Cena z gazetki: 399,00 zł", 17,
                  "2026-11-05", "2026-11-22",
                  evidence={"match_method": "product_identity", "printed_price": 399})
    product = {"name": "Stylpro", "online": {
        "source": "productsInfo", "price": "399", "promotions": [],
        "promotion_id": "free-shipping-registered-unregistered-inpost",
        "fetched_at": "2026-11-12T12:00:00+01:00",
        "price_details": {"SalesPrice": 399, "StandardPrice": 399}}}
    if flag is not None:
        product["online"]["price_details"]["onPromotion"] = flag
    verify_offers([offer], {"80370095": product}, "2026-11-12T12:00:00+01:00")
    assert offer.verification["status"] == "price_match"
    assert offer.verification["online_on_promotion"] is (flag if isinstance(flag, bool) else None)
    if isinstance(flag, bool):
        assert f"onPromotion={str(flag).lower()}" in offer.verification["details"]
    else:
        assert "onPromotion=" not in offer.verification["details"]
    assert offer.promotion == "Cena z gazetki: 399,00 zł"
    assert not offer.warnings


def test_online_false_flag_does_not_remove_an_explicit_flyer_bundle():
    promotion = "Drugi produkt -60% (w tej samej cenie lub tańszy)."
    offer = Offer("printed", "123", promotion, 1, "2026-11-05", "2026-11-22",
                  evidence={"match_method": "campaign_panel", "printed_condition": promotion})
    product = {"name": "Alfa", "online": {"source": "productsInfo", "price": "24.99",
        "fetched_at": "2026-11-12T12:00:00+01:00", "price_details": {"onPromotion": False},
        "promotions": [{"text": "1+1 -60%"}]}}
    verify_offers([offer], {"123": product}, "2026-11-12T12:00:00+01:00")
    assert offer.promotion == promotion
    assert offer.verification["status"] == "campaign_match"
    assert offer.verification["online_on_promotion"] is False
    assert not offer.warnings


@pytest.mark.parametrize("flag,label", [(False, "Nie"), (True, "Tak"), (None, "Brak danych")])
def test_compact_excel_keeps_review_warning_and_online_flag(tmp_path, flag, label):
    from openpyxl import load_workbook
    offer = Offer("05.11.2026–22.11.2026", "83108078", "DO SPRAWDZENIA", 17,
        warnings=["Konflikt tożsamości"], product="La Mania Home Bauhaus II",
        evidence={"nearby_caption_unverified": "Lari Stripes Black",
            "date": {"page": 17, "legal_notes": [{"text": "* Promocja obowiązuje 05-22.11.2026."}],
                     "related_notes": [{"kind": "exclusion", "text": "Nie łączy się z kodami."}]}},
        verification={"label": "Nie potwierdzono produktu przy ofercie",
                      "online_on_promotion": flag})
    output = tmp_path / "merged.xlsx"
    write_excel([offer], output, "https://www.hebe.pl/gazetka-hebe")
    wb = load_workbook(output)
    assert wb.sheetnames == ["Promocje"]
    ws = wb.active
    assert ws["H2"].value == label
    assert "Konflikt tożsamości" in ws["E2"].value
    assert ws.max_column == 9
    assert [cell.value for cell in ws[1]][-1] == "Strona gazetki"
    assert all(cell.fill.fgColor.rgb == "00FFF2CC" for cell in ws[2])
