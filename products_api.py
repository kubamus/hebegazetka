"""Hebe's public productsInfo batch API; no copied browser cookies required."""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import html
import json
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from product_names import ORIGIN, normalize_id

LOG = logging.getLogger("hebe")
ENDPOINT = ORIGIN + "/productsInfo"
PRODUCT_PAGE = ORIGIN + "/on/demandware.store/Sites-Hebe-Site/pl_PL/Product-Show"
BATCH_SIZE = 20


def api_url(raw_ids):
    for raw in raw_ids:
        normalize_id(raw)
    return ENDPOINT + "?" + urlencode({"products": ",".join(raw_ids)},safe=",")


def money(value):
    if isinstance(value,bool) or not isinstance(value,(int,float,str)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return str(result) if result.is_finite() and result >= 0 else None


def parse_api_product(record, requested_id, *, fetched_at, url):
    if not isinstance(record,dict) or normalize_id(record.get("pid","")) != normalize_id(requested_id):
        raise ValueError("ID odpowiedzi productsInfo nie zgadza się z żądanym ID")
    analytics, prices = record.get("gtmProductData"),record.get("priceObj")
    if not isinstance(analytics,dict) or not isinstance(prices,dict):
        raise ValueError("Brak danych produktu lub ceny w productsInfo")
    if normalize_id(analytics.get("item_id","")) != normalize_id(requested_id):
        raise ValueError("ID gtmProductData nie zgadza się z żądanym ID")
    name = analytics.get("item_name")
    if not isinstance(name,str) or not name.strip():
        raise ValueError("Brak nazwy produktu w productsInfo")
    description = analytics.get("item_description") or ""
    if not isinstance(description,str):
        raise ValueError("Nieprawidłowy opis produktu w productsInfo")
    if analytics.get("currency") != "PLN":
        raise ValueError("Niepotwierdzona waluta ceny productsInfo")
    if not isinstance(analytics.get("item_brand") or "",str):
        raise ValueError("Nieprawidłowa marka produktu w productsInfo")
    # SalesPrice is the current unconditional price, even outside a sale. Never
    # substitute OmnibusPrice, a coupon/VIP amount, or the generic min/max fields.
    price = money(prices.get("SalesPrice"))
    if price is None:
        raise ValueError("Brak bieżącej ceny SalesPrice w productsInfo")
    promotions = []
    badges = record.get("badge") or {}
    if not isinstance(badges,dict):
        raise ValueError("Nieprawidłowe etykiety productsInfo")
    badge = badges.get("everyonePromotionBadge")
    if isinstance(badge,dict):
        if any(not isinstance(badge.get(key) or "",str) for key in ("asset","promotionBadge","calloutMsg")):
            raise ValueError("Nieprawidłowy opis promocji productsInfo")
        asset = BeautifulSoup(badge.get("asset") or "", "html.parser")
        text = " ".join(html.unescape(badge.get("promotionBadge") or "").split())
        callout = " ".join(html.unescape(badge.get("calloutMsg") or "").split())
        if not text:
            text = " ".join(asset.get_text(" ",strip=True).split())
        promotions.append({"id":badge.get("ID"),"text":text,"callout":callout,
            "images":[{"alt":i.get("alt",""),"src":i.get("src","")} for i in asset.select("img")]})
    raw = str(record["pid"])
    return {"name":" ".join(html.unescape(name).split()),
        "description":" ".join(html.unescape(description).split()),
        "brand":analytics.get("item_brand") or "",
        "categories":[analytics[key] for key in ("item_category","item_category2","item_category3") if analytics.get(key)],
        "url":PRODUCT_PAGE + "?" + urlencode({"pid":raw}),
        "online":{"source":"productsInfo","url":url,"fetched_at":fetched_at,
            "price":price,"currency":analytics.get("currency"),"promotions":promotions,
            "promotion_id":analytics.get("promotion_id"),
            "price_details":prices,"pricing_text":price + " " + (analytics.get("currency") or "")}}


async def request_batch(url):
    def send():
        request = Request(url,method="POST",data=b"",headers={
            "Accept":"application/json", "X-Requested-With":"XMLHttpRequest",
            "Origin":ORIGIN,"Referer":ORIGIN + "/gazetka-hebe?magazine=digital"})
        with urlopen(request,timeout=30) as response:
            return response.read()
    for attempt in range(3):
        try:
            return await asyncio.to_thread(send)
        except HTTPError as exc:
            if exc.code not in (429,500,502,503,504) or attempt == 2:
                raise
        except (URLError,TimeoutError):
            if attempt == 2:
                raise
        await asyncio.sleep(2 ** attempt)


async def fetch_products_info(raw_ids, *, archive_dir: Path | None = None, requester=None):
    """Validate by ID, never response order; retry abbreviated IDs as needed."""
    requester = requester or request_batch
    unique = {normalize_id(raw):str(raw) for raw in raw_ids}
    products = {}
    requests = 0

    async def read_batches(ids):
        nonlocal requests
        for start in range(0,len(ids),BATCH_SIZE):
            batch = ids[start:start+BATCH_SIZE]
            url = api_url(batch)
            requests += 1
            try:
                raw = await requester(url)
                if isinstance(raw,str):
                    raw = raw.encode("utf-8")
                fetched_at = datetime.now().astimezone().isoformat()
                payload = json.loads(raw)
                if not isinstance(payload,list):
                    raise ValueError("productsInfo nie zwróciło listy produktów")
                requested = {normalize_id(pid):pid for pid in batch}
                grouped = {}
                for record in payload:
                    if not isinstance(record,dict):
                        continue
                    try:
                        pid = normalize_id(record.get("pid",""))
                    except ValueError:
                        continue
                    if pid in requested:
                        grouped.setdefault(pid,[]).append(record)
                archive = None
                if archive_dir is not None:
                    archive_dir.mkdir(parents=True,exist_ok=True)
                    filename = f"batch-{requests:03d}.json"
                    (archive_dir / filename).write_bytes(raw)
                    archive = {"json_file":"productsInfo/" + filename,
                               "sha256":hashlib.sha256(raw).hexdigest()}
                for pid,records in grouped.items():
                    try:
                        if len(records) != 1:
                            raise ValueError("Powtórzone ID w odpowiedzi productsInfo")
                        product = parse_api_product(records[0],requested[pid],fetched_at=fetched_at,url=url)
                        if archive:
                            product["online"].update(archive)
                        products[pid] = product
                    except ValueError as exc:
                        LOG.warning("productsInfo %s: %s",pid,exc)
            except (ValueError,HTTPError,URLError,TimeoutError,OSError) as exc:
                LOG.warning("Nie pobrano partii productsInfo: %s",exc)
            LOG.info("productsInfo: %d/%d produktów",len(products),len(unique))
            await asyncio.sleep(.2)

    await read_batches(list(unique.values()))
    # Six-digit internal IDs are sometimes abbreviated by iPaper. Seller IDs
    # can use a separate eight-digit namespace, so use their exact source ID.
    padded = [raw.zfill(18) for pid,raw in unique.items() if pid not in products and len(raw)<18]
    if padded:
        await read_batches(padded)
    return products
