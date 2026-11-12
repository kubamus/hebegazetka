from types import SimpleNamespace

import pymupdf
from hebe import PageParser, Text, parse_period
from legal_footnotes import extract_legal_notes, split_legal_text


def note(text,box=(10,740,490,760)):
    return split_legal_text(text,box,2026,parse_period)


def parser(notes, *, cell=None, section=None):
    p = PageParser.__new__(PageParser)
    p.width,p.height,p.year = 500,800,2026
    p.page = SimpleNamespace(rect=pymupdf.Rect(0,0,500,800))
    p.headers = [Text("05-22.11",(10,10,100,30),14)]
    p.lines,p.validity,p.rules,p.drawings = [],[],[],[]
    p.legal_notes = notes
    if cell:
        p.cell = lambda _: cell
    if section:
        p.section = lambda _: section
    return p


def test_exact_three_footnotes_from_the_cover_are_split_and_applied():
    notes = note("*Promocja obowiązuje w dniach 28.10-11.11.2026 lub do wyczerpania zapasów. "
                 "**Promocja obowiązuje w dniach 01.10-11.11.2026 lub do wyczerpania zapasów."
                 "*** Promocja obowiązuje w dniach 28.10-22.11.2026 lub do wyczerpania zapasów.")
    assert [n["marker"] for n in notes] == ["*","**","***"]
    for marker,expected in [("*",("2026-10-28","2026-11-11")),("**",("2026-10-01","2026-11-11")),
                            ("***",("2026-10-28","2026-11-22"))]:
        result = parser(notes).period_resolution((.3,.4),Text("ALFA Krem"+marker,(20,200,100,250)),None)
        assert result["period"] == expected
        assert result["source"] == "product_footnote"
        assert result["legal_notes"][0]["marker"] == marker
        assert result["legal_notes"][0]["stock_limited"]


def test_pdf_character_positions_separate_two_notes_in_one_block():
    texts = ["*Promocja obowiązuje 05-11.11.2026.","*Promocja obowiązuje 12-22.11.2026."]
    lines = []
    for text,x in zip(texts,[10,300]):
        chars = [{"c":c,"bbox":(x+i,740,x+i+1,750)} for i,c in enumerate(text)]
        lines.append({"spans":[{"chars":chars}]})
    page = SimpleNamespace(get_text=lambda _: {"blocks":[{"bbox":(10,740,490,750),"lines":lines}]})
    notes = extract_legal_notes(page,2026,parse_period)
    assert notes[0]["box"][2] < 100 and notes[1]["box"][0] == 300
    p = parser(notes,section=pymupdf.Rect(250,400,500,780))
    assert p.period((.7,.6),Text("ALFA Krem*",(300,500,400,530)),None) == ("2026-11-12","2026-11-22")


def test_reused_marker_stays_in_its_product_cell():
    notes = note("*Promocja obowiązuje w dniach 05-11.11.2026.",(20,300,140,320))
    notes += note("*Promocja obowiązuje w dniach 01.10-11.11.2026.",(300,450,480,470))
    p = parser(notes,cell=pymupdf.Rect(250,350,500,480))
    assert p.period((.7,.55),Text("KUNDAL Honey & Macadamia*",(300,380,430,410)),None) == ("2026-10-01","2026-11-11")


def test_previous_panels_footer_is_not_attached_to_the_next_panel():
    notes = note("*Promocja obowiązuje 28.10-11.11.2026.",(10,449,200,456))
    notes += note("*Promocja obowiązuje 12-22.11.2026.",(10,760,200,770))
    p = parser(notes,section=pymupdf.Rect(10,458,490,757))
    p.lines = [Text("*",(140,520,146,532),10)]
    result = p.period_resolution((.4,.8),None,None)
    assert result["period"] == ("2026-11-12","2026-11-22")
    assert result["source"] == "campaign_footnote"


def test_conflicting_or_missing_product_footnote_does_not_guess_from_header():
    notes = note("*Promocja obowiązuje 05-11.11.2026.")
    notes += note("*Promocja obowiązuje 12-22.11.2026.")
    result = parser(notes).period_resolution((.3,.4),Text("ALFA Krem*",(20,200,100,250)),None)
    assert result["period"] is None and result["source"] == "conflicting_footnotes"
    assert result["warnings"]
    missing = parser([]).period_resolution((.3,.4),Text("ALFA Krem***",(20,200,100,250)),None)
    assert missing["period"] is None and missing["source"] == "unresolved_footnote"


def test_exclusion_and_availability_dates_never_override_validity():
    notes = note("*Promocja nie łączy się z akcją obowiązującą w dniach 12-22.11.2026. "
                 "**Marka dostępna w aplikacji i w wybranych sklepach.")
    result = parser(notes).period_resolution((.3,.4),Text("ALFA Krem* Marka**",(20,200,100,250)),None)
    assert result["period"] == ("2026-11-05","2026-11-22")
    assert {n["kind"] for n in result["related_notes"]} == {"exclusion","availability"}


def test_multiple_references_use_validity_not_the_largest_asterisk_count():
    notes = note("*Promocja obowiązuje 28.10-11.11.2026. **Marka dostępna w aplikacji.")
    result = parser(notes).period_resolution((.3,.4),Text("ALFA** Krem*",(20,200,100,250)),None)
    assert result["period"] == ("2026-10-28","2026-11-11")


def test_separate_superscript_stars_and_wrapped_legal_text():
    notes = note("* * Promocja obowiązuje\nw dniach 28.12-05.01.2027\nlub do wyczerpania zapasów.")
    p = parser(notes)
    p.lines = [Text("ALFA Krem",(20,200,100,220),9),Text("**",(103,195,110,205),6)]
    assert p.period((.3,.4),Text("ALFA Krem",(20,200,100,230)),None) == ("2026-12-28","2027-01-05")


def test_unreadable_legal_date_is_reported_instead_of_using_header():
    p = parser(note("*Promocja obowiązuje w dniach 31-32.11.2026."))
    result = p.period_resolution((.3,.4),Text("ALFA Krem*",(20,200,100,250)),None)
    assert result["period"] is None and result["warnings"]


def test_single_common_page_footer_without_repeated_stars_overrides_catalogue_dates():
    p = parser(note("*Promocja obowiązuje w dniach 16.11-20.12.2026.", (20, 755, 280, 770)))
    p.headers = []
    result = p.period_resolution((.4, .5), Text("ALFA Perfumy", (30, 400, 150, 430)),
                                 ("2026-11-12", "2026-11-22"))
    assert result["period"] == ("2026-11-16", "2026-12-20")
    assert result["source"] == "page_footnote"
    assert result["legal_notes"][0]["marker"] == "*"
