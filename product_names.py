"""Product identity and live promotion evidence from Hebe's public QuickView."""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import hashlib
import logging
from pathlib import Path
import re
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError

LOG = logging.getLogger("hebe")
ORIGIN = "https://www.hebe.pl"
QUICK_VIEW = ORIGIN + "/on/demandware.store/Sites-Hebe-Site/pl_PL/Product-QuickView"


def normalize_id(value: str) -> str:
    if not re.fullmatch(r"\d+", str(value)) or not str(value).strip("0"):
        raise ValueError(f"Nieprawidłowe ID: {value!r}")
    return str(value).lstrip("0")


def parse_product(html: str, requested_id: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one(".product-tile__name")
    link = soup.select_one("a.product-tile__title[href], a.js-quick-view-go-to-product[href]")
    if title is None or link is None:
        raise ValueError("Brak nazwy lub linku produktu w odpowiedzi Hebe")
    url = urljoin(ORIGIN, link["href"])
    parsed = urlparse(url)
    match = re.search(r"-(\d+)\.html$", parsed.path)
    if (parsed.scheme != "https" or parsed.hostname != "www.hebe.pl" or not match
            or normalize_id(match[1]) != normalize_id(requested_id)):
        raise ValueError("ID w linku produktu nie zgadza się z żądanym ID")
    name = " ".join(title.get_text(" ", strip=True).split())
    if not name:
        raise ValueError("Pusta nazwa produktu")
    description = soup.select_one(".product-tile__description")
    product = {"name": name, "description": " ".join(description.get_text(" ",strip=True).split())
               if description else "", "url": url}
    tile = soup.select_one(".product-tile[data-pid]")
    if tile is None:
        # Older saved fragments can still supply identity, but not price evidence.
        return product
    if normalize_id(tile["data-pid"]) != normalize_id(requested_id):
        raise ValueError("ID kafelka QuickView nie zgadza się z żądanym ID")
    try:
        analytics = json.loads(tile.get("data-product-gtm", "{}"))
    except (ValueError, TypeError):
        analytics = {}
    if analytics.get("item_id") and normalize_id(analytics["item_id"]) != normalize_id(requested_id):
        raise ValueError("ID danych QuickView nie zgadza się z żądanym ID")
    product["brand"] = analytics.get("item_brand", "")
    product["categories"] = [analytics[key] for key in
                             ("item_category", "item_category2", "item_category3") if analytics.get(key)]
    badges = []
    for element in tile.select("[data-promotion]"):
        badges.append({"id": element["data-promotion"],
                       "text": " ".join(element.get_text(" ", strip=True).split()),
                       "images": [{"alt": img.get("alt", ""), "src": img.get("src", "")}
                                  for img in element.select("img")]})
    # The visible current price excludes regular/omnibus and coupon-only prices.
    current = tile.select_one(".price-tile__wrapper .price-tile__sales .price-tile__formatted, "
                              ".price-tile__wrapper .price-tile__standard .price-tile__formatted")
    price = None
    if current:
        whole, decimal = current.select_one(".price-tile__whole"), current.select_one(".price-tile__decimal")
        if whole:
            try:
                price = str(Decimal(re.sub(r"\s", "", whole.get_text()))
                            + Decimal("0." + (decimal.get_text(strip=True) if decimal else "00")))
            except InvalidOperation:
                pass
    pricing = tile.select_one(".product-tile__pricing")
    product["quickview"] = {"url": quick_view_url(requested_id),
        "fetched_at": datetime.now().astimezone().isoformat(), "price": price,
        "currency": "PLN" if price is not None else None, "promotions": badges,
        "promotion_id": analytics.get("promotion_id"),
        "pricing_text": " ".join(pricing.get_text(" ", strip=True).split()) if pricing else ""}
    return product


def quick_view_url(raw_id: str) -> str:
    normalize_id(raw_id)
    return QUICK_VIEW + "?" + urlencode({"pid": raw_id})


async def fetch_names(context, raw_ids: list[str], *, headless: bool = False,
                      archive_dir: Path | None = None) -> dict[str, dict]:
    """Fetch each unique ID once; preserve failed products and archive evidence."""
    unique = {normalize_id(raw): raw for raw in raw_ids}
    if not unique:
        return {}
    page = await context.new_page()
    results: dict[str, dict] = {}
    def read_product(html, raw):
        product = parse_product(html, raw)
        if archive_dir is not None:
            archive_dir.mkdir(parents=True, exist_ok=True)
            filename = normalize_id(raw) + ".html"
            (archive_dir / filename).write_text(html, encoding="utf-8")
            if product.get("quickview"):
                product["quickview"].update(html_file="quickview/" + filename,
                    sha256=hashlib.sha256(html.encode("utf-8")).hexdigest())
        return product
    try:
        first_pid, first_raw = next(iter(unique.items()))
        # A document navigation lets Hebe perform its browser verification. API-only
        # clients can receive a challenge HTML instead of product details.
        navigation = await page.goto(quick_view_url(first_raw),wait_until="domcontentloaded",timeout=60000)
        challenged = "Just a moment" in await page.title()
        if challenged:
            if headless:
                raise RuntimeError("Hebe wymaga weryfikacji nazw; uruchom bez --headless")
            LOG.warning("Hebe weryfikuje dostęp do nazw produktów. W razie potrzeby dokończ weryfikację w Chrome.")
            await page.wait_for_function("!document.title.includes('Just a moment')",timeout=120000)
        missing = navigation is not None and navigation.status in (404,410)
        if challenged and not missing:
            await page.locator(".product-tile__name").wait_for(state="attached",timeout=45000)
        # Read source HTML: the fragment need not be visibly rendered.
        try:
            if missing:
                raise ValueError(f"HTTP {navigation.status} przy pobieraniu nazwy")
            results[first_pid] = read_product(await page.content(),first_raw)
        except ValueError as exc:
            results[first_pid] = {"error": str(exc)}

        semaphore = asyncio.Semaphore(3)

        async def read_one(pid, raw):
            async with semaphore:
                results.pop(pid,None)
                try:
                    # SFCC catalogue IDs can be abbreviated in iPaper. Some endpoints
                    # need their 18-character representation; seller IDs also exist,
                    # so first use the exact source ID and verify every returned link.
                    variants = list(dict.fromkeys([raw,raw.zfill(18)]))
                    for variant_index, variant in enumerate(variants):
                        for attempt in range(3):
                            await asyncio.sleep(.2)
                            response = await page.evaluate("""async url => {
                                const r = await fetch(url, {
                                    headers: {'X-Requested-With': 'XMLHttpRequest'},
                                    signal: AbortSignal.timeout(45000)
                                });
                                return {status:r.status, text:await r.text()};
                            }""",quick_view_url(variant))
                            status = response["status"]
                            if status == 200:
                                try:
                                    results[pid] = read_product(response["text"],raw)
                                except ValueError:
                                    if variant_index == len(variants)-1:
                                        raise
                                break
                            if status in (404,410) and variant_index < len(variants)-1:
                                break
                            if status not in (429,500,502,503,504) or attempt == 2:
                                raise RuntimeError(f"HTTP {status} przy pobieraniu nazwy")
                            await asyncio.sleep(2**attempt)
                        if pid in results:
                            break
                except (ValueError, RuntimeError, PlaywrightError) as exc:
                    results[pid] = {"error": str(exc)}
                    LOG.warning("QuickView produktu %s: %s",pid,exc)
                if len(results)%10 == 0 or len(results)==len(unique):
                    LOG.info("QuickView produktów: %d/%d",len(results),len(unique))

        await asyncio.gather(*(read_one(pid,raw) for pid,raw in unique.items()
                               if not results.get(pid,{}).get("name")))
    except (RuntimeError, PlaywrightError) as exc:
        LOG.warning("Nie udało się pobrać QuickView: %s",exc)
        for pid in unique:
            results.setdefault(pid,{"error":str(exc)})
    finally:
        await page.close()
    found = sum(bool(p.get("name")) for p in results.values())
    LOG.info("Pobrano QuickView: %d/%d",found,len(unique))
    return results


def enrich_offers(offers, products: dict[str, dict], *, skipped: bool = False):
    """The join uses IDs, so names cannot change the order or number of offers."""
    for offer in offers:
        product = products.get(offer.id,{})
        offer.product = product.get("name","") if not skipped else ""
        offer.product_description = product.get("description","") if not skipped else ""
        offer.product_url = product.get("url","")
        if not skipped and not offer.product:
            offer.warnings = list(offer.warnings or [])
            error = product.get("error","brak danych nazwy w zapisanym wydaniu")
            offer.warnings.append("Nie pobrano nazwy: " + error)
