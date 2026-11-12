import asyncio
from types import SimpleNamespace

import pytest

from hebe import Offer
from product_names import enrich_offers, fetch_names, parse_product, quick_view_url


def html(pid="000000000000740207",name="&amp;honey <b>Melty</b> Moist Repair"):
    return f'<a class="product-tile__title" href="/produkt-{pid}.html"><span class="product-tile__name">{name}</span></a><span class="product-tile__description">odżywka, 445 g</span>'


def test_names_decode_entities_and_nested_html():
    product=parse_product(html(),"740207")
    assert product["name"]=="&honey Melty Moist Repair"
    assert product["description"]=="odżywka, 445 g"
    assert product["url"].startswith("https://www.hebe.pl/")


@pytest.mark.parametrize("body",[
    html(pid="740208"),html(name="  "),"<title>Just a moment...</title>",
    html().replace('/produkt-','https://example.com/produkt-'),
])
def test_wrong_id_empty_name_and_challenges_are_rejected(body):
    with pytest.raises(ValueError):
        parse_product(body,"740207")


def test_external_seller_id_and_original_request_id_are_preserved():
    assert parse_product(html(pid="83108078"),"83108078")["name"]
    assert quick_view_url("000000000000740207").endswith("pid=000000000000740207")


def test_names_join_by_id_without_changing_promotions_or_duplicate_offers():
    offers=[Offer("a","740207","cena 1",1),Offer("b","740207","cena 2",2),Offer("c","999","oferta",3)]
    before=[(o.id,o.period,o.promotion) for o in offers]
    enrich_offers(offers,{"740207":parse_product(html(),"740207"),"999":{"error":"HTTP 404"}})
    assert before==[(o.id,o.period,o.promotion) for o in offers]
    assert offers[0].product==offers[1].product=="&honey Melty Moist Repair"
    assert "HTTP 404" in offers[2].warnings[0]
    assert not offers[2].product


def test_explicit_skip_does_not_add_missing_name_warning():
    offer=Offer("a","740207","oferta",1)
    enrich_offers([offer],{},skipped=True)
    assert not offer.warnings


class FakePage:
    def __init__(self):
        self.requests=[]
        self.closed=False

    async def goto(self,url,**kwargs):
        self.requests.append(url)

    async def title(self):
        return ""

    async def content(self):
        return html()

    def locator(self,selector):
        return self

    async def wait_for(self,**kwargs):
        return None

    async def evaluate(self,expression,url):
        self.requests.append(url)
        return {"status":200,"text":html(pid="83108078",name="Inny produkt")}

    async def close(self):
        self.closed=True


def test_names_fetch_each_id_once_and_close_browser_page():
    page=FakePage()
    async def new_page():
        return page
    products=asyncio.run(fetch_names(SimpleNamespace(new_page=new_page),
        ["000000000000740207","740207","83108078"]))
    assert set(products)=={"740207","83108078"}
    assert len(page.requests)==2
    assert page.closed


def test_abbreviated_ids_retry_with_sfcc_padding():
    class ShortIdPage(FakePage):
        async def evaluate(self,expression,url):
            self.requests.append(url)
            return {"status":200,"text": html(pid="000000000000428006",name="Trec Collagen")
                    if url.endswith("pid=000000000000428006") else "<div></div>"}
    page=ShortIdPage()
    async def new_page():
        return page
    products=asyncio.run(fetch_names(SimpleNamespace(new_page=new_page),["740207","428006"]))
    assert products["428006"]["name"]=="Trec Collagen"
    assert len(page.requests)==3
    assert page.closed


def test_first_abbreviated_id_is_also_retried():
    class FirstShortPage(FakePage):
        async def content(self):
            return "<div></div>"
        async def evaluate(self,expression,url):
            self.requests.append(url)
            return {"status":200,"text":html(pid="000000000000428006")
                    if url.endswith("pid=000000000000428006") else "<div></div>"}
    page=FirstShortPage()
    async def new_page():
        return page
    products=asyncio.run(fetch_names(SimpleNamespace(new_page=new_page),["428006"]))
    assert products["428006"]["name"]=="&honey Melty Moist Repair"
    assert len(page.requests)==3


def test_excel_has_names_links_text_ids_and_literal_values(tmp_path):
    from hebe import write_excel
    from openpyxl import load_workbook
    offer=Offer("05.11.2030–22.11.2030","740207","oferta",1,
                product="=Literalna nazwa",product_url="https://www.hebe.pl/produkt-740207.html")
    output=tmp_path/"wynik.xlsx"
    write_excel([offer],output,"https://www.hebe.pl/gazetka-hebe")
    ws=load_workbook(output).active
    assert [c.value for c in ws[1]][:4]==["Produkt","ID","Data obowiązywania","Promocja"]
    assert ws["A2"].data_type=="s"
    assert ws["A2"].value=="=Literalna nazwa"
    assert ws["A2"].hyperlink.target==offer.product_url
    assert ws["B2"].data_type=="s"
    assert ws["B2"].value=="740207"
    assert ws.tables["PromocjeHebe"].ref=="A1:H2"
    assert ws["G2"].value == "Brak danych"
    assert ws.freeze_panes == "C2"
