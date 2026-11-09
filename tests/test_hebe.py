from types import SimpleNamespace

import pymupdf
import pytest

from hebe import PageParser, Text, parse_period, product_id, minimum_assignment, paragraphs


@pytest.mark.parametrize("text,year,expected", [
    ("05-22.11", 2030, ("2030-11-05", "2030-11-22")),
    ("28.10-11.11.2027", 2026, ("2027-10-28", "2027-11-11")),
    ("5-22.11.26", 2040, ("2026-11-05", "2026-11-22")),
    ("28.12-05.01.2028", 2027, ("2027-12-28", "2028-01-05")),
    ("28.12.2027–5.01.2028", 2026, ("2027-12-28", "2028-01-05")),
    ("00.00-00.00", 2026, None),
    ("31-32.11", 2026, None),
    ("22-05.11", 2026, None),
])
def test_periods(text, year, expected):
    assert parse_period(text, year) == expected


def test_ids_are_identifiers_not_fixed_length_integers():
    assert product_id("000000000000740207") == "740207"
    assert product_id("83108078") == "83108078"
    with pytest.raises(ValueError):
        product_id("0000")


def parser(headers=(), footnotes=(), section=None):
    p = PageParser.__new__(PageParser)
    p.year = 2030
    p.width, p.height = 500, 800
    p.page = SimpleNamespace(rect=pymupdf.Rect(0, 0, 500, 800),
                             get_text=lambda _: footnotes)
    p.headers = list(headers)
    p.drawings = []
    p.rules = []
    p.validity = [Text(b[4],tuple(b[:4])) for b in footnotes if "Promocja obowiązuje" in b[4]]
    p.conditions = [Text(b[4],tuple(b[:4])) for b in footnotes if "Kup " in b[4]]
    if section:
        p.drawings = [{"rect": section, "fill": (1, 1, 1), "items": [("re", section)]}]
    return p


def test_side_headers_use_product_panel_not_nearest_date():
    # The lower date is closer to the product, but the product is in the upper panel.
    p = parser([Text("05-13.11", (475, 25, 490, 70), 14),
                Text("14-22.11", (475, 435, 490, 480), 14)],
               section=pymupdf.Rect(15, 15, 465, 355))
    assert p.period((.35, .42), None, None) == ("2030-11-05", "2030-11-13")


def test_product_footnote_overrides_header_and_exclusion_does_not():
    notes = [(10, 650, 490, 670,
              "***Promocja obowiązuje w dniach 21.10-22.11.2030 lub do wyczerpania zapasów.\n", 0, 0),
             (10, 700, 490, 720,
              "*Promocja nie łączy się z akcją obowiązującą w dniach 12-22.11.2030.", 0, 0)]
    p = parser([Text("05-22.11", (15, 15, 90, 35), 14)], notes)
    assert p.period((.3, .5), Text("MARKA Krem***", (20, 250, 110, 300)), None) == ("2030-10-21", "2030-11-22")
    assert p.period((.3, .5), Text("MARKA Żel*", (20, 250, 110, 300)), None) == ("2030-11-05", "2030-11-22")


def test_missing_date_is_not_guessed_from_today():
    assert parser().period((.3, .5), None, None) is None


def test_bundle_condition_keeps_qualifying_purchase_and_brand():
    blocks = [(10, 450, 200, 550, "Kup produkt marki ALFA, a drugi w tej samej cenie lub tańszy kupisz 60% taniej", 0, 0),
              (260, 450, 490, 550, "Kup produkt marki BETA, a drugi w tej samej cenie lub tańszy kupisz 40% taniej", 0, 0)]
    p = parser(footnotes=blocks)
    p.lines = []
    assert "ALFA" in p.bundle_promotion((.3, .65), None, "ALFA Krem")


def test_page_assignment_does_not_steal_a_neighbours_caption():
    assert set(minimum_assignment([[4,5],[1,100]])) == {(0,1),(1,0)}


def test_wrapped_campaign_text_includes_purchase_condition():
    lines = [Text("Kup produkt marki ALFA,",(10,10,110,24),10),
             Text("a drugi w tej samej",(10,21,100,35),10),
             Text("cenie lub tańszy",(10,32,90,46),10),
             Text("kupisz 60% taniej",(10,43,98,57),10)]
    result = paragraphs(lines,r"^Kup produkt")
    assert len(result)==1
    assert result[0].text.endswith("kupisz 60% taniej")


def test_price_does_not_escape_its_product_cell():
    p=parser()
    p.rules=[pymupdf.Rect(10,200,150,200),pymupdf.Rect(10,400,150,400)]
    p.prices=[(Text("999",(220,280,250,320)),9.99)]
    assert p.price((.2,.4),Text("Krem",(15,220,90,250))) is None
