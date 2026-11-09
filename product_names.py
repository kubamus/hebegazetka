"""Names from Hebe's public quick-view, checked against the requested product ID."""
from __future__ import annotations

import asyncio
import logging
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


def parse_product(html: str, requested_id: str) -> dict[str, str]:
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
    return {"name": name, "description": " ".join(description.get_text(" ",strip=True).split())
            if description else "", "url": url}


def quick_view_url(raw_id: str) -> str:
    normalize_id(raw_id)
    return QUICK_VIEW + "?" + urlencode({"pid": raw_id})


async def fetch_names(context, raw_ids: list[str], *, headless: bool = False) -> dict[str, dict]:
    """Fetch each unique ID once; a failed name never removes its promotion row."""
    unique = {normalize_id(raw): raw for raw in raw_ids}
    if not unique:
        return {}
    page = await context.new_page()
    results: dict[str, dict] = {}
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
            results[first_pid] = parse_product(await page.content(),first_raw)
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
                                    results[pid] = parse_product(response["text"],raw)
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
                    LOG.warning("Nazwa produktu %s: %s",pid,exc)
                if len(results)%10 == 0 or len(results)==len(unique):
                    LOG.info("Nazwy produktów: %d/%d",len(results),len(unique))

        await asyncio.gather(*(read_one(pid,raw) for pid,raw in unique.items()
                               if not results.get(pid,{}).get("name")))
    except (RuntimeError, PlaywrightError) as exc:
        LOG.warning("Nie udało się pobrać nazw: %s",exc)
        for pid in unique:
            results.setdefault(pid,{"error":str(exc)})
    finally:
        await page.close()
    found = sum(bool(p.get("name")) for p in results.values())
    LOG.info("Pobrano nazwy: %d/%d",found,len(unique))
    return results


def enrich_offers(offers, products: dict[str, dict], *, skipped: bool = False):
    """The join uses IDs, so names cannot change the order or number of offers."""
    for offer in offers:
        product = products.get(offer.id,{})
        offer.product = product.get("name","")
        offer.product_description = product.get("description","")
        offer.product_url = product.get("url","")
        if not skipped and not offer.product:
            offer.warnings = list(offer.warnings or [])
            error = product.get("error","brak danych nazwy w zapisanym wydaniu")
            offer.warnings.append("Nie pobrano nazwy: " + error)
