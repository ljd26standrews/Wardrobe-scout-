"""Image download, local caching and perceptual hashing."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import imagehash
from PIL import Image, ImageOps, UnidentifiedImageError

from ..config import get_settings
from ..ingestion.http import FetchError, PoliteClient
from ..logging_setup import get_logger

log = get_logger(__name__)

MAX_SIDE = 1400


class ImageError(Exception):
    pass


def load_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        raise ImageError(f"not a valid image: {e}") from e


def phash_of(img: Image.Image) -> str:
    return str(imagehash.phash(img))


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def store_image_bytes(data: bytes, subdir: str = "images") -> tuple[str, Image.Image]:
    """Store under the data dir (content-addressed), return (relative path, PIL image).

    JPEGs that are already small enough are written byte-for-byte (no decode/re-encode cost, no extra
    quality loss); anything else is normalised to a JPEG of at most MAX_SIDE pixels.
    """
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt == "JPEG":
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # decode at reduced scale when the file is much larger
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError) as e:
        raise ImageError(f"not a valid image: {e}") from e
    as_is = fmt == "JPEG" and max(img.size) <= MAX_SIDE
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    digest = hashlib.sha1(data).hexdigest()
    s = get_settings()
    base = s.images_dir if subdir == "images" else s.uploads_dir
    rel = f"{digest[:2]}/{digest}.jpg"
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        if as_is:
            path.write_bytes(data)
        else:
            img.save(path, "JPEG", quality=88)
    return f"{subdir}/{rel}", img


def resolve_path(rel: str) -> Path:
    return get_settings().data_dir / rel


def fetch_image_bytes(client: PoliteClient, url: str, referer: str | None) -> bytes:
    # Image CDNs are fetched as page resources (robots.txt governs crawling of pages).
    try:
        r = client.get(url, referer=referer, binary=True, check_robots=False, retries=1)
    except FetchError as e:
        raise ImageError(str(e)) from e
    ctype = r.headers.get("content-type", "")
    if "image" not in ctype and r.content[:4] not in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\x89PNG"):
        if not ctype.startswith("application/octet"):
            raise ImageError(f"unexpected content-type {ctype!r} for {url}")
    return r.content


def download_image(client: PoliteClient, url: str, referer: str | None) -> tuple[str, Image.Image]:
    return store_image_bytes(fetch_image_bytes(client, url, referer))
