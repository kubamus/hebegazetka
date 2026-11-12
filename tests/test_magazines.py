import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

import hebe
from hebe import PageParser, Text, text_campaign_sections


@pytest.mark.parametrize("argv,expected", [
    ([], ["digital"]),
    (["--magazine", "digital"], ["digital"]),
    (["--magazine", "plachta"], ["plachta"]),
    (["--magazine", "both"], ["digital", "plachta"]),
])
def test_cli_selects_current_landing_pages_and_separate_outputs(monkeypatch, tmp_path, argv, expected):
    calls = []
    async def process(args, output):
        calls.append((args.url, output, args.strict, args.skip_names))
        return 0
    monkeypatch.setattr(hebe, "process_flyer", process)
    output = tmp_path / "oferty.xlsx"
    result = asyncio.run(hebe.main(argv + ["--output", str(output), "--strict", "--skip-names"]))
    assert result == 0
    assert [call[0] for call in calls] == [hebe.MAGAZINES[kind] for kind in expected]
    assert [call[1] for call in calls] == ([output] if len(expected) == 1 else
        [tmp_path / f"oferty-{kind}.xlsx" for kind in expected])
    assert all(call[2:] == (True, True) for call in calls)


@pytest.mark.parametrize("first_result,expected", [(0, 0), (2, 2), (RuntimeError("failure"), 1)])
def test_both_processes_second_edition_after_first_warnings_or_failure(monkeypatch, tmp_path, first_result, expected):
    calls = []
    async def process(args, output):
        calls.append(args.url)
        if len(calls) == 1:
            if isinstance(first_result, Exception):
                raise first_result
            return first_result
        return 0
    monkeypatch.setattr(hebe, "process_flyer", process)
    assert asyncio.run(hebe.main(["--magazine", "both", "--output", str(tmp_path / "hebe.xlsx")])) == expected
    assert calls == list(hebe.MAGAZINES.values())


def test_custom_url_and_replay_remain_single_runs(monkeypatch, tmp_path):
    calls = []
    async def process(args, output):
        calls.append(args)
        return 0
    monkeypatch.setattr(hebe, "process_flyer", process)
    url = "https://viewer.ipaper.io/hebe/custom/"
    assert asyncio.run(hebe.main(["--url", url])) == 0
    assert calls[-1].url == url
    assert asyncio.run(hebe.main(["--replay", str(tmp_path)])) == 0
    assert calls[-1].replay == tmp_path


@pytest.mark.parametrize("argv", [
    ["--magazine", "both", "--url", "https://www.hebe.pl/"],
    ["--magazine", "plachta", "--replay", "data"],
    ["--magazine", "unknown"],
])
def test_cli_rejects_conflicting_or_unknown_sources(argv):
    with pytest.raises(SystemExit) as error:
        asyncio.run(hebe.main(argv))
    assert error.value.code == 2


def panel_parser(index):
    data = json.loads(Path("tests/fixtures/plachta-panels.json").read_text())[index]
    p = PageParser.__new__(PageParser)
    p.width, p.height, p.year = data["width"], data["height"], 2026
    p.page = SimpleNamespace(rect=pymupdf.Rect(0, 0, p.width, p.height),
                             get_text=lambda _: data["blocks"])
    p.lines = [Text(t["text"], tuple(t["box"]), t["size"]) for t in data["lines"]]
    p.legal_notes = data["legal_notes"]
    p.headers, p.conditions, p.validity, p.rules, p.drawings = [], [], [], [], []
    p.text_sections = text_campaign_sections(p.page, p.legal_notes)
    return p


@pytest.mark.parametrize("point,discount", [
    ((.42, .22), 80),  # Miya in the upper panel
    ((.33, .32), 80),  # Back To Comfort in the upper panel
    ((.34, .54), 60),  # Uzdrovisco near the next panel's top
    ((.474, .8795), 60),  # Hean, overlapping the decorative footer rectangle
    ((.56, .8795), 60),  # Lirene, same decorative rectangle
])
def test_plachta_uses_legal_panel_boundaries_instead_of_nearest_discount(point, discount):
    p = panel_parser(1)
    p.drawings = [{"rect": pymupdf.Rect(-31.1, 946.86, 859.9, 1103.82),
                   "fill": (1, 1, 1), "items": [("re",)]}]
    assert p.bundle_promotion(point, None, "Verified product", "Verified") == (
        f"Drugi produkt -{discount}% (w tej samej cenie lub tańszy). Miksuj w ramach marki.")
    dates = p.period_resolution(point, None, ("2026-11-01", "2026-11-30"))
    assert dates["period"] == ("2026-11-12", "2026-11-22")
    assert dates["legal_notes"][0]["text"].endswith(f"kupisz {discount}% taniej.")


def test_cover_does_not_extend_second_product_discount_to_perfumes():
    p = panel_parser(0)
    assert "-80%" in p.bundle_promotion((.65, .22), None, "Asoa", "Asoa")
    assert p.bundle_promotion((.25, .8), None, "Yves Saint Laurent", "Yves Saint Laurent") is None
    dates = p.period_resolution((.25, .8), None, None)
    assert dates["period"] == ("2026-11-16", "2026-12-20")


@pytest.mark.parametrize("replace_bottom", [False, True])
def test_text_bands_require_agreement_between_heading_and_legal_discount(replace_bottom):
    p = panel_parser(1)
    for note in p.legal_notes:
        note["text"] = note["text"].replace("80%", "40%")
        if replace_bottom:
            note["text"] = note["text"].replace("60%", "40%")
    assert text_campaign_sections(p.page, p.legal_notes) == []


def test_inferred_panel_keeps_brand_guard_for_brand_specific_campaign():
    p = panel_parser(1)
    p.conditions = [Text("Kup produkt marki BETA, a drugi kupisz 80% taniej", (30, 100, 450, 130))]
    assert p.bundle_promotion((.4, .3), None, "ALFA Krem", "ALFA") is None
