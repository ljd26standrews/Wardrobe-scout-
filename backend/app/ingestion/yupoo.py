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
from .http import BlockedError, InvalidPageError
from .parsing import (
    canonical_url,
    classify_link,
    extract_purchase_links,
    normalise_space,
    parse_category,
    parse_price,
)

log = get_logger(__name__)

ALBUM_HREF = re.compile(r"/albums/(\d+)")


class ParserError(InvalidPageError):
    """Page fetched fine but its structure was not recognised."""


_CLOSED = re.compile(r"</html\s*>", re.I)


def looks_truncated(html: str) -> bool:
    """A page whose transfer was cut short has no closing tag; whatever it lacks is unknown, not removed."""
    return not _CLOSED.search(html[-4096:])


def _abs(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("data:"):  # inline placeholder shown while the real photo lazy-loads
        return None
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)


def yupoo_base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


# Shop-notice albums: contact details, ordering guides, "about us". Matched only when the title also
# carries no price and names no garment, and the album has few photos, so a product is never hidden.
_NOTICE = re.compile(
    r"\b(whats\s?app|wechat|telegram|discord|instagram|contact|about|server|follow|guide|read before|notice|"
    r"important|catalogue|catalog|manufacturers?|how to|shipping|payment)\b|公告|须知|联系|微信|关于|指南|通知|说明",
    re.I,
)
MAX_NOTICE_PHOTOS = 15


def is_notice_album(title: str | None, photo_count: int | None = None) -> bool:
    if not title or not _NOTICE.search(title):
        return False
    if parse_price(title) is not None or parse_category(title) is not None:
        return False
    return photo_count is None or photo_count <= MAX_NOTICE_PHOTOS


def is_locked_catalogue(html: str) -> bool:
    """The catalogue's home page asks for a password (the album list is not shown at all)."""
    return "indexlock__main" in html or "主页已加密" in html


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
        cover = thumb = None
        if img is not None:
            cover = _abs(img.get("data-origin-src"), page_url) or _abs(img.get("data-src"), page_url)
            cover = cover or _abs(img.get("src"), page_url)
            thumb = _abs(img.get("data-src"), page_url) or _abs(img.get("src"), page_url)
        count_el = a.select_one("[class*='photonumber']")
        count_txt = normalise_space(count_el.get_text()) if count_el else ""
        photo_count = int(count_txt) if count_txt.isdigit() else None
        seen.add(album_id)
        albums.append(
            AlbumRef(
                external_id=album_id,
                url=_abs(f"/albums/{album_id}?uid=1", page_url) or page_url,
                title=title or None,
                cover_url=cover,
                cover_thumb=thumb if thumb and thumb != cover else None,
                photo_count=photo_count,
                notice=is_notice_album(title, photo_count),
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


_PHOTO_PATH = re.compile(r"//photo\.yupoo\.com/([^/]+)/([^/]+)/")


def photo_key(url: str) -> str:
    """One photo is served at several sizes (``<seller>/<key>/small.jpg``, ``medium.jpg``, original): same key."""
    m = _PHOTO_PATH.search(url)
    return f"{m.group(1)}/{m.group(2)}" if m else url


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
            href = canonical_url(_abs(a["href"], page_url))
            kind = classify_link(href) if href else None
            if kind in ("weidian", "taobao"):
                links.setdefault(kind, href)
    links.update({k: v for k, v in extract_purchase_links(description).items() if k not in links})
    links["yupoo"] = page_url

    images: list[str] = []
    thumbs: list[str | None] = []
    keys: set[str] = set()
    imgs = (
        soup.select(".showalbum__children img")
        or soup.select(".image__imagewrap img")
        or soup.select("img[data-origin-src]")
    )
    for img in imgs:
        if img.find_parent("noscript") is not None:  # the no-JavaScript copy of a photo already listed
            continue
        u = _abs(img.get("data-origin-src"), page_url) or _abs(img.get("data-src"), page_url)
        u = u or _abs(img.get("src"), page_url)
        if not u or u.endswith((".gif", ".svg")):
            continue
        key = photo_key(u)
        if key in keys:  # another size of the same photo
            continue
        keys.add(key)
        images.append(u)
        small = _abs(img.get("data-src"), page_url) or _abs(img.get("src"), page_url)
        thumbs.append(small if small and small != u else None)

    if not title and not images:
        raise ParserError(f"album structure not recognised: {page_url}")

    return RawListing(
        external_id=album_id,
        source_url=page_url,
        title=title,
        description=description,
        image_urls=images,
        image_thumbs=thumbs,
        image_referer=page_url,
        price=parse_price(title) or parse_price(description),
        links=links,
        fetched_at=datetime.now(UTC).replace(tzinfo=None),
        partial=looks_truncated(html),
    )


class YupooAdapter(SellerAdapter):
    platform = "yupoo"

    @classmethod
    def handles(cls, url: str) -> bool:
        return "yupoo.com" in urlparse(url).netloc

    def list_page(self, url: str) -> ListingPage:
        r = self.client.get(url)
        if is_locked_catalogue(r.text):
            raise BlockedError("this catalogue is password-protected; no password was tried")
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
        self.exhausted = self.empty = False
        while url and pages < max_pages:
            page = self.list_page(url)
            pages += 1
            self.pages_read = pages
            self.total_pages = page.total_pages or self.total_pages
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
                if pages == 1:
                    # Nothing listed at all: unrecognised or restricted, not proof of an empty catalogue.
                    self.empty = True
                    return
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
            listing.image_thumbs = [ref_or_url.cover_thumb]
        return listing

    def fetch_listing_conditional(
        self, ref: AlbumRef, etag: str | None, last_modified: str | None
    ) -> tuple[RawListing | None, dict]:
        """Fetch a product page, or return (None, validators) if the server says it is unchanged (304)."""
        r = self.client.get(ref.url, etag=etag, last_modified=last_modified)
        validators = {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified")}
        if r.status_code == 304:
            return None, validators
        listing = parse_album_page(r.text, str(r.url))
        if not listing.image_urls and ref.cover_url:
            listing.image_urls = [ref.cover_url]
            listing.image_thumbs = [ref.cover_thumb]
        return listing, validators
