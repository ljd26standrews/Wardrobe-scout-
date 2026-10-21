"""Yupoo public catalogue adapter.

Yupoo catalogues (``https://<seller>.x.yupoo.com``) are server-rendered HTML:

* listing pages: ``/albums?page=N`` and ``/categories/<id>?page=N``
* album pages:   ``/albums/<album_id>?uid=1``

Selectors are layered (primary class names, then structural fallbacks) because
Yupoo occasionally renames classes. If no albums are found on a page that
returned HTTP 200 the scan reports a *parser* failure rather than silently
reporting "0 products".

Image hosts (photo.yupoo.com) check the Referer header for hotlink
protection; we send the album page URL as Referer, exactly as a browser
viewing the album would.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ..logging_setup import get_logger
from .base import AlbumRef, ListingPage, ProgressFn, RawListing, SellerAdapter
from .http import FetchError
from .parsing import extract_purchase_links, normalise_space, parse_price

log = get_logger(__name__)

ALBUM_HREF = re.compile(r"/albums/(\d+)")


class ParserError(FetchError):
    """Page fetched fine but its structure was not recognised."""


def _abs(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)


def yupoo_base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def parse_listing_page(html: str, page_url: str) -> ListingPage:
    soup = BeautifulSoup(html, "lxml")
    albums: list[AlbumRef] = []
    seen: set[str] = set()

    anchors = soup.select("a.album__main") or soup.select("a[href*='/albums/']")
    for a in anchors:
        href = a.get("href") or ""
        m = ALBUM_HREF.search(href)
        if not m or m.group(1) in seen:
            continue
        # Skip header/nav links that point to the album index itself
        album_id = m.group(1)
        title_el = a.select_one(".album__title") or a.select_one("[class*='title']")
        title = normalise_space(a.get("title") or (title_el.get_text(" ") if title_el else a.get_text(" ")))
        img = a.select_one("img")
        cover = None
        if img is not None:
            cover = img.get("data-origin-src") or img.get("data-src") or img.get("src")
        seen.add(album_id)
        albums.append(
            AlbumRef(
                external_id=album_id,
                url=_abs(f"/albums/{album_id}?uid=1", page_url) or page_url,
                title=title or None,
                cover_url=_abs(cover, page_url),
            )
        )

    # Pagination
    total_pages = None
    jump = soup.select_one(".pagination__jumpwrap input[name='page']") or soup.select_one("input[name='page'][max]")
    if jump is not None and (jump.get("max") or "").isdigit():
        total_pages = int(jump["max"])
    if total_pages is None:
        m = re.search(r"共\s*(\d+)\s*页", soup.get_text(" "))
        if m:
            total_pages = int(m.group(1))

    current = int(parse_qs(urlparse(page_url).query).get("page", ["1"])[0] or 1)
    next_url = None
    nxt = soup.select_one("a.pagination__next") or soup.select_one("a[class*='next']")
    if nxt is not None and nxt.get("href"):
        next_url = _abs(nxt["href"], page_url)
    if next_url is None:
        for a in soup.select("a[href*='page=']"):
            qs = parse_qs(urlparse(a.get("href", "")).query)
            if qs.get("page", [""])[0] == str(current + 1):
                next_url = _abs(a["href"], page_url)
                break
    if next_url is None and total_pages and current < total_pages:
        p = urlparse(page_url)
        q = parse_qs(p.query)
        q["page"] = [str(current + 1)]
        query = "&".join(f"{k}={v[0]}" for k, v in q.items())
        next_url = f"{p.scheme}://{p.netloc}{p.path}?{query}"

    categories: list[tuple[str, str]] = []
    cat_seen: set[str] = set()
    for a in soup.select("a[href*='/categories/']"):
        href = a.get("href", "")
        m = re.search(r"/categories/(\d+)", href)
        if m and m.group(1) not in cat_seen:
            cat_seen.add(m.group(1))
            name = normalise_space(a.get_text(" ")) or f"Category {m.group(1)}"
            categories.append((name, _abs(f"/categories/{m.group(1)}", page_url) or href))

    return ListingPage(albums=albums, next_url=next_url, total_pages=total_pages, categories=categories)


def parse_album_page(html: str, page_url: str) -> RawListing:
    soup = BeautifulSoup(html, "lxml")
    m = ALBUM_HREF.search(urlparse(page_url).path)
    if not m:
        raise ParserError(f"not an album URL: {page_url}")
    album_id = m.group(1)

    title_el = (
        soup.select_one(".showalbumheader__gallerytitle")
        or soup.select_one("[class*='gallerytitle']")
        or soup.select_one("h1")
    )
    title = normalise_space(title_el.get_text(" ")) if title_el else ""
    if not title:
        og = soup.select_one("meta[property='og:title']")
        title = normalise_space(og.get("content")) if og else ""
    if not title and soup.title:
        title = normalise_space(soup.title.get_text()).split("|")[0].strip()

    desc_el = soup.select_one(".showalbumheader__gallerysubtitle") or soup.select_one("[class*='gallerysubtitle']")
    description = None
    links: dict[str, str] = {}
    if desc_el is not None:
        for br in desc_el.find_all("br"):
            br.replace_with("\n")
        description = "\n".join(normalise_space(x) for x in desc_el.get_text().splitlines() if x.strip())
        for a in desc_el.select("a[href]"):
            href = a["href"]
            if "weidian.com" in href:
                links.setdefault("weidian", href)
            elif "taobao.com" in href or "tmall.com" in href:
                links.setdefault("taobao", href)
    links.update({k: v for k, v in extract_purchase_links(description).items() if k not in links})
    links["yupoo"] = page_url

    images: list[str] = []
    imgs = (
        soup.select(".showalbum__children img")
        or soup.select(".image__imagewrap img")
        or soup.select("img[data-origin-src]")
    )
    for img in imgs:
        src = img.get("data-origin-src") or img.get("data-src") or img.get("src")
        u = _abs(src, page_url)
        if u and u not in images and not u.endswith((".gif", ".svg")):
            images.append(u)

    if not title and not images:
        raise ParserError(f"album structure not recognised: {page_url}")

    return RawListing(
        external_id=album_id,
        source_url=page_url,
        title=title,
        description=description,
        image_urls=images,
        image_referer=page_url,
        price=parse_price(title) or parse_price(description),
        links=links,
        fetched_at=datetime.now(UTC).replace(tzinfo=None),
    )


class YupooAdapter(SellerAdapter):
    platform = "yupoo"

    @classmethod
    def handles(cls, url: str) -> bool:
        return "yupoo.com" in urlparse(url).netloc

    def list_page(self, url: str) -> ListingPage:
        r = self.client.get(url)
        page = parse_listing_page(r.text, str(r.url))
        if not page.albums and "album" not in r.text.lower():
            raise ParserError(f"no albums recognised on {url} (layout may have changed)")
        return page

    def discover_categories(self, base_url: str) -> list[tuple[str, str]]:
        page = self.list_page(yupoo_base(base_url) + "/albums")
        return page.categories

    def iter_album_refs(self, base_url: str, max_pages: int, progress: ProgressFn | None = None) -> Iterator[AlbumRef]:
        url: str | None = (
            base_url
            if ("/albums" in base_url or "/categories/" in base_url)
            else yupoo_base(base_url) + "/albums?page=1"
        )
        pages = 0
        seen: set[str] = set()
        self.exhausted = False
        while url and pages < max_pages:
            page = self.list_page(url)
            pages += 1
            if progress:
                progress(
                    f"listing page {pages}{f'/{page.total_pages}' if page.total_pages else ''}: {len(page.albums)} albums"
                )
            new_on_page = 0
            for ref in page.albums:
                if ref.external_id in seen:
                    continue
                seen.add(ref.external_id)
                new_on_page += 1
                yield ref
            if new_on_page == 0:
                url = None  # a page with nothing new means we've wrapped around: end of catalogue
                break
            url = page.next_url
        self.exhausted = url is None

    def fetch_listing(self, ref_or_url: AlbumRef | str) -> RawListing:
        url = ref_or_url.url if isinstance(ref_or_url, AlbumRef) else ref_or_url
        r = self.client.get(url)
        listing = parse_album_page(r.text, str(r.url))
        if isinstance(ref_or_url, AlbumRef) and not listing.image_urls and ref_or_url.cover_url:
            listing.image_urls = [ref_or_url.cover_url]
        return listing
