"""Adapter interface shared by all seller ingestion adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from .http import PoliteClient


@dataclass
class RawListing:
    """A product listing exactly as found on a page (before normalisation)."""

    external_id: str
    source_url: str
    title: str
    description: str | None = None
    image_urls: list[str] = field(default_factory=list)
    image_referer: str | None = None
    price: float | None = None
    currency: str = "CNY"
    sizes: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    category_hint: str | None = None
    available: bool | None = None
    fetched_at: datetime | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class AlbumRef:
    """A pointer to a product page found on a listing page."""

    external_id: str
    url: str
    title: str | None = None
    cover_url: str | None = None


@dataclass
class ListingPage:
    albums: list[AlbumRef]
    next_url: str | None
    total_pages: int | None = None
    categories: list[tuple[str, str]] = field(default_factory=list)  # (name, url)


ProgressFn = Callable[[str], None]


class SellerAdapter(ABC):
    platform: str = "base"

    def __init__(self, client: PoliteClient) -> None:
        self.client = client
        # Set by iter_album_refs: True only when the catalogue's pagination was genuinely
        # exhausted (not stopped by a page limit). Removal detection relies on this.
        self.exhausted = False

    @classmethod
    @abstractmethod
    def handles(cls, url: str) -> bool: ...

    @abstractmethod
    def iter_album_refs(self, base_url: str, max_pages: int, progress: ProgressFn | None = None) -> Iterator[AlbumRef]:
        """Yield product references from catalogue listing pages (newest first if possible)."""

    @abstractmethod
    def fetch_listing(self, ref_or_url: AlbumRef | str) -> RawListing:
        """Fetch and parse one product page."""
