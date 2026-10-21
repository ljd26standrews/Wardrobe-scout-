"""Inputs the interface relies on: evidence links must be web addresses."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import EvidenceIn


def _ev(url):
    return EvidenceIn(kind="buyer_report", aspect="construction", claim="neat seams", source_url=url)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "ftp://example.org/x", "not a link"])
def test_evidence_link_must_be_a_web_address(url):
    with pytest.raises(ValidationError):
        _ev(url)


def test_evidence_link_web_or_empty_is_accepted():
    assert _ev(" https://example.org/qc ").source_url == "https://example.org/qc"
    assert _ev("").source_url is None and _ev(None).source_url is None


# ----------------------------------------------------------------- uploads and image size limits
import io  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from .conftest import AUTH, make_image  # noqa: E402


@pytest.fixture()
def small_cap_client(env, monkeypatch):
    monkeypatch.setenv("WS_MAX_DOWNLOAD_BYTES", str(256 * 1024))  # photos, uploaded or downloaded: 256 KB here
    monkeypatch.setenv("WS_DOWNLOAD_IMAGES", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app(), headers=AUTH) as c:
        yield c


def test_oversized_uploads_are_refused_before_storing(small_cap_client, env):
    big = b"\xff\xd8" + b"0" * (300 * 1024)  # larger than the cap; never decoded
    r = small_cap_client.post("/api/inspiration", files={"image": ("big.jpg", big, "image/jpeg")})
    assert r.status_code == 413
    r = small_cap_client.post("/api/import/manual", data={"title": "Grey wool coat", "price": "300"},
                              files=[("images", ("big.jpg", big, "image/jpeg"))])  # fmt: skip
    assert r.status_code == 413
    assert not list((env / "uploads").rglob("*.jpg"))
    ok = small_cap_client.post("/api/inspiration", files={"image": ("ok.jpg", make_image(), "image/jpeg")})
    assert ok.status_code == 200  # an ordinary photo is fine


def test_decompression_bomb_is_refused_from_its_header(env):
    from PIL import Image

    from app.services.images import ImageError, load_image, store_image_bytes

    buf = io.BytesIO()
    Image.new("1", (9000, 5000)).save(buf, "PNG")  # 45 megapixels in a tiny file
    assert len(buf.getvalue()) < 100_000
    for fn in (store_image_bytes, load_image):
        with pytest.raises(ImageError, match="too large"):
            fn(buf.getvalue())
