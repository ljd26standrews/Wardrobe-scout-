"""Generic product-page and storefront adapters.

* ``GenericPageAdapter`` reads schema.org JSON-LD ``Product`` data, then
  OpenGraph / meta tags. Works for most independent seller websites.
* ``ShopifyAdapter`` uses Shopify's public ``/products.json`` storefront feed,
  which is an official, paginated catalogue interface.
* ``WeidianAdapter`` extracts what a Weidian item page exposes in its HTML.
  Weidian pages are mostly rendered client-side; when the static HTML carries
  no product data it optionally renders with Playwright (if enabled), and
  otherwise reports the page as needing manual / bookmarklet import.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..config import get_settings
from ..logging_setup import get_logger
from .base import AlbumRef, ProgressFn, RawListing, SellerAdapter
from .http import FetchError
from .parsing import normalise_space, parse_price, weidian_item_id

log = get_logger(__name__)


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def url_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def _iter_jsonld(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(tag.string or tag.get_text() or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "@graph" in item:
                    stack.extend(item["@graph"])
                yield item
            elif isinstance(item, list):
                stack.extend(item)


def parse_generic_product(html: str, url: str) -> RawListing:
    soup = BeautifulSoup(html, "lxml")
    title = ""
    description = None
    images: list[str] = []
    price = None
    currency = "CNY"
    available = None
    external_id = None

    for item in _iter_jsonld(soup):
        types = item.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue
        title = normalise_space(item.get("name")) or title
        description = item.get("description") or description
        img = item.get("image")
        for u in img if isinstance(img, list) else [img]:
            if isinstance(u, dict):
                u = u.get("url")
            if u and isinstance(u, str):
                images.append(urljoin(url, u))
        offers = item.get("offers")
        offers = offers if isinstance(offers, list) else [offers] if offers else []
        for off in offers:
            if not isinstance(off, dict):
                continue
            p = off.get("price") or off.get("lowPrice")
            if p is not None:
                try:
                    price = float(p)
                except (TypeError, ValueError):
                    pass
                currency = off.get("priceCurrency") or currency
            av = str(off.get("availability", ""))
            if av:
                available = "InStock" in av or "PreOrder" in av
        external_id = str(item.get("sku") or item.get("productID") or "") or None
        break

    def meta(prop: str) -> str | None:
        el = soup.select_one(f"meta[property='{prop}']") or soup.select_one(f"meta[name='{prop}']")
        return el.get("content") if el else None

    if not title:
        title = normalise_space(meta("og:title") or (soup.title.get_text() if soup.title else ""))
    if not description:
        description = meta("og:description") or meta("description")
    if not images:
        for prop in ("og:image", "og:image:secure_url", "twitter:image"):
            if (u := meta(prop)) and urljoin(url, u) not in images:
                images.append(urljoin(url, u))
    if price is None:
        p = meta("product:price:amount") or meta("og:price:amount")
        if p:
            try:
                price = float(p)
                currency = meta("product:price:currency") or meta("og:price:currency") or currency
            except ValueError:
                pass
    if price is None:
        price = parse_price(title) or parse_price(description)

    if not title:
        raise FetchError(f"no product data recognised at {url}")
    return RawListing(
        external_id=external_id or url_id(url),
        source_url=url,
        title=title,
        description=normalise_space(description) if description else None,
        image_urls=images,
        image_referer=url,
        price=price,
        currency=currency.upper() if currency else "CNY",
        available=available,
        links={"seller": url},
        fetched_at=_now(),
    )


class GenericPageAdapter(SellerAdapter):
    platform = "website"

    @classmethod
    def handles(cls, url: str) -> bool:
        return url.startswith("http")

    def iter_album_refs(self, base_url: str, max_pages: int, progress: ProgressFn | None = None) -> Iterator[AlbumRef]:
        # A generic website has no known catalogue structure; try sitemap product URLs.
        try:
            r = self.client.get(urljoin(base_url, "/sitemap.xml"))
        except FetchError:
            return
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
        product_urls = [u for u in locs if re.search(r"/(product|products|item|p)/", u)]
        if progress:
            progress(f"sitemap: {len(product_urls)} product-like URLs")
        for u in product_urls[: max_pages * 25]:
            yield AlbumRef(external_id=url_id(u), url=u)

    def fetch_listing(self, ref_or_url: AlbumRef | str) -> RawListing:
        url = ref_or_url.url if isinstance(ref_or_url, AlbumRef) else ref_or_url
        r = self.client.get(url)
        return parse_generic_product(r.text, str(r.url))


def shopify_product_to_listing(p: dict, base_url: str) -> RawListing:
    handle = p.get("handle", "")
    url = urljoin(base_url, f"/products/{handle}")
    variants = p.get("variants") or []
    prices = [float(v["price"]) for v in variants if v.get("price")]
    sizes = []
    for opt in p.get("options") or []:
        if str(opt.get("name", "")).lower() in ("size", "尺码", "taille"):
            sizes = [str(v) for v in opt.get("values", [])]
    body = BeautifulSoup(p.get("body_html") or "", "lxml").get_text("\n")
    return RawListing(
        external_id=str(p.get("id") or handle),
        source_url=url,
        title=normalise_space(p.get("title")),
        description=normalise_space(body) or None,
        image_urls=[i["src"] for i in p.get("images") or [] if i.get("src")],
        image_referer=url,
        price=min(prices) if prices else None,
        currency="",  # Shopify feed has no currency; caller sets from seller config
        sizes=sizes,
        available=any(v.get("available") for v in variants) if variants else None,
        links={"seller": url},
        category_hint=p.get("product_type") or None,
        fetched_at=_now(),
        extra={"vendor": p.get("vendor"), "tags": p.get("tags")},
    )


class ShopifyAdapter(SellerAdapter):
    platform = "shopify"

    @classmethod
    def handles(cls, url: str) -> bool:
        return False  # chosen explicitly via seller.platform

    def iter_album_refs(self, base_url: str, max_pages: int, progress: ProgressFn | None = None) -> Iterator[AlbumRef]:
        # Shopify returns full product objects; we stash them for fetch_listing.
        self._cache: dict[str, RawListing] = {}
        for page in range(1, max_pages + 1):
            r = self.client.get(urljoin(base_url, f"/products.json?limit=250&page={page}"))
            products = r.json().get("products", [])
            if progress:
                progress(f"products.json page {page}: {len(products)} products")
            if not products:
                break
            for p in products:
                listing = shopify_product_to_listing(p, base_url)
                self._cache[listing.external_id] = listing
                yield AlbumRef(external_id=listing.external_id, url=listing.source_url, title=listing.title)

    def fetch_listing(self, ref_or_url: AlbumRef | str) -> RawListing:
        if isinstance(ref_or_url, AlbumRef) and ref_or_url.external_id in getattr(self, "_cache", {}):
            return self._cache[ref_or_url.external_id]
        url = ref_or_url.url if isinstance(ref_or_url, AlbumRef) else ref_or_url
        r = self.client.get(url.split("?")[0] + ".json")
        return shopify_product_to_listing(r.json()["product"], url)


# ------------------------------------------------------------------- Weidian
_JSON_FIELDS = {
    "title": [
        r'"itemName"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"item_name"\s*:\s*"((?:[^"\\]|\\.)*)"',
        r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"',
    ],
    "price": [
        r'"itemPrice"\s*:\s*"?(\d+(?:\.\d+)?)',
        r'"price"\s*:\s*"?(\d+(?:\.\d+)?)',
        r'"originalPrice"\s*:\s*"?(\d+(?:\.\d+)?)',
    ],
    "shop": [r'"shopName"\s*:\s*"((?:[^"\\]|\\.)*)"'],
}
_IMG = re.compile(r"https?:(?:\\/\\/|//)si\.geilicdn\.com(?:\\?/[^\"'\s\\?]+)+")


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        return s


def parse_weidian_html(html: str, url: str) -> RawListing:
    item_id = weidian_item_id(url) or url_id(url)
    title = None
    price = None
    for pat in _JSON_FIELDS["title"]:
        if m := re.search(pat, html):
            title = normalise_space(_unescape(m.group(1)))
            if title:
                break
    for pat in _JSON_FIELDS["price"]:
        if m := re.search(pat, html):
            price = float(m.group(1))
            # Some Weidian payloads express price in fen (cents)
            if price > 100000:
                price = price / 100
            break
    images = []
    for m in _IMG.finditer(html):
        u = m.group(0).replace("\\/", "/")
        u = u.split("?")[0]
        if u not in images and not u.endswith(".gif"):
            images.append(u)
    if not title:
        try:
            generic = parse_generic_product(html, url)
            title, images = generic.title, images or generic.image_urls
            price = price or generic.price
        except FetchError:
            pass
    if not title or title.lower() in ("微店", "weidian"):
        raise FetchError(
            "Weidian page did not expose product data in static HTML. Enable Playwright rendering "
            "or import this item with the bookmarklet / manual form."
        )
    return RawListing(
        external_id=item_id,
        source_url=url,
        title=title,
        image_urls=images[:20],
        image_referer="https://weidian.com/",
        price=price,
        links={"weidian": url},
        fetched_at=_now(),
    )


def render_with_playwright(url: str, user_agent: str) -> str:  # pragma: no cover - needs a browser
    """Render a JS page in headless Chromium. Only used if WS_ENABLE_PLAYWRIGHT=true.

    It loads the page like a normal visitor; it does not solve CAPTCHAs or log in.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=user_agent, locale="zh-CN")
            page.goto(url, wait_until="networkidle", timeout=45000)
            return page.content()
        finally:
            browser.close()


class WeidianAdapter(SellerAdapter):
    platform = "weidian"

    @classmethod
    def handles(cls, url: str) -> bool:
        return "weidian.com" in urlparse(url).netloc

    def iter_album_refs(self, base_url: str, max_pages: int, progress: ProgressFn | None = None) -> Iterator[AlbumRef]:
        # Weidian shop listings are rendered by authenticated JS APIs; we do not traverse them.
        if progress:
            progress("Weidian shop traversal is not supported; add individual item links instead.")
        return iter(())

    def fetch_listing(self, ref_or_url: AlbumRef | str) -> RawListing:
        url = ref_or_url.url if isinstance(ref_or_url, AlbumRef) else ref_or_url
        r = self.client.get(url)
        try:
            return parse_weidian_html(r.text, str(r.url))
        except FetchError:
            s = get_settings()
            if not s.enable_playwright:
                raise
            if not self.client.allowed(url):
                raise
            html = render_with_playwright(url, self.client.user_agent)
            return parse_weidian_html(html, url)
