import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from hebe import Offer
from products_api import api_url, fetch_products_info, parse_api_product, request_batch
from promotion_verification import verify_offers

RECORDS = json.loads((Path(__file__).parent / "fixtures/products-info.json").read_text())
STAMP = "2026-11-12T12:00:00+01:00"


def record(pid="123"):
    value = deepcopy(RECORDS[0])
    value["pid"] = value["gtmProductData"]["item_id"] = pid
    return value


def parse(value):
    return parse_api_product(value,value["pid"],fetched_at=STAMP,url=api_url([value["pid"]]))


def test_live_api_sample_uses_salesprice_not_omnibus_minimum_or_analytics():
    value = deepcopy(RECORDS[0])
    value["priceObj"]["OmnibusPrice"] = 9.99
    value["priceObj"]["min"] = 1
    value["priceObj"]["LoyaltyPrice"] = {"price":5}
    value["priceObj"]["PriceWithCodeObj"] = {"price":7}
    value["gtmProductData"]["current_price"] = 99.99
    product = parse(value)
    assert product["name"] == "Nizoral Daily Care"
    assert product["online"]["price"] == "28.99"
    assert product["url"].endswith("pid=000000000000432599")
    assert product["online"]["price_details"]["PriceWithCodeObj"] == {"price":7}


@pytest.mark.parametrize("field,value",[("pid","999"),("currency","EUR"),("item_id","999"),
                                       ("item_name",None),("SalesPrice",{}),("SalesPrice","NaN")])
def test_mismatched_identity_missing_data_and_invalid_prices_are_rejected(field,value):
    item = record()
    if field == "pid":
        item[field] = value
    elif field == "SalesPrice":
        item["priceObj"][field] = value
    else:
        item["gtmProductData"][field] = value
    with pytest.raises(ValueError):
        parse_api_product(item,"123",fetched_at=STAMP,url=api_url(["123"]))


def test_callout_exposes_bundle_missing_from_quickview_badge_text():
    product = parse(RECORDS[2])
    assert not product["online"]["promotions"][0]["text"]
    assert "-50%" in product["online"]["promotions"][0]["callout"]
    promotion = "Kup produkt z kategorii profesjonalne, a drugi w tej samej cenie lub tańszy kupisz 50% taniej"
    offer = Offer("printed","764471",promotion,8,"2026-11-05","2026-11-22",
                  evidence={"match_method":"campaign_panel","printed_condition":promotion})
    verify_offers([offer],{"764471":product},STAMP)
    assert offer.verification["status"] == "campaign_match"
    assert offer.verification["online_source"] == "productsInfo"
    assert offer.promotion == promotion


def test_batch_joins_by_id_instead_of_response_order_and_archives_exact_response(tmp_path):
    calls = []
    raw = json.dumps([record("456"),record("123"),record("999")]).encode()
    async def request(url):
        calls.append(url)
        return raw
    products = asyncio.run(fetch_products_info(["123","123","456"],archive_dir=tmp_path,requester=request))
    assert set(products) == {"123","456"} and len(calls) == 1
    assert parse_qs(urlparse(calls[0]).query)["products"] == ["123,456"]
    assert (tmp_path / "batch-001.json").read_bytes() == raw
    assert products["123"]["online"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_internal_abbreviations_retry_but_seller_id_namespace_is_preserved():
    calls = []
    async def request(url):
        calls.append(url)
        ids = parse_qs(urlparse(url).query)["products"][0].split(",")
        return json.dumps([record(pid) for pid in ids if pid != "428006"]).encode()
    products = asyncio.run(fetch_products_info(["428006","83108078"],requester=request))
    assert set(products) == {"428006","83108078"}
    assert len(calls) == 2
    assert "products=428006,83108078" in calls[0]
    assert calls[1].endswith("products=000000000000428006")


def test_duplicate_api_records_are_not_arbitrarily_accepted():
    async def request(url):
        return json.dumps([record("000000000000000123"),record("000000000000000123")]).encode()
    assert asyncio.run(fetch_products_info(["000000000000000123"],requester=request)) == {}


def test_large_input_is_split_into_bounded_batches():
    calls = []
    async def request(url):
        ids = parse_qs(urlparse(url).query)["products"][0].split(",")
        calls.append(ids)
        return json.dumps([record(pid) for pid in reversed(ids)]).encode()
    products = asyncio.run(fetch_products_info([str(pid) for pid in range(100,121)],requester=request))
    assert len(products) == 21 and [len(ids) for ids in calls] == [20,1]


def test_api_block_or_challenge_leaves_products_available_for_quickview_fallback():
    async def blocked(url):
        raise HTTPError(url,403,"blocked",{},None)
    assert asyncio.run(fetch_products_info(["000000000000000123"],requester=blocked)) == {}
    async def challenge(url):
        return b"<title>Just a moment...</title>"
    assert asyncio.run(fetch_products_info(["000000000000000123"],requester=challenge)) == {}


def test_http_uses_empty_post_and_no_pasted_cookies(monkeypatch):
    captured = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def read(self):
            return b"[]"
    def send(request,timeout):
        captured.append(request)
        return Response()
    monkeypatch.setattr("products_api.urlopen",send)
    assert asyncio.run(request_batch(api_url(["123","456"]))) == b"[]"
    assert captured[0].get_method() == "POST" and captured[0].data == b""
    assert captured[0].get_header("Cookie") is None


def test_api_snapshot_takes_precedence_over_old_quickview_snapshot():
    product = parse(record())
    product["quickview"] = {"price":"1.00","fetched_at":STAMP}
    offer = Offer("printed","123","Cena promocyjna: 28,99 zł",1,"2026-11-05","2026-11-22",
                  evidence={"match_method":"product_identity","printed_price":28.99})
    verify_offers([offer],{"123":product},STAMP)
    assert offer.verification["status"] == "price_match"
    assert offer.verification["online_price"] == "28.99"
