"""Test fixtures.

All HTML/JSON fixtures in tests/fixtures are SYNTHETIC test pages modelled on
the public markup of the respective platforms. They are never loaded into the
real database and contain no real seller data.
"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WS_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("WS_ENABLE_SCHEDULER", "false")
    monkeypatch.setenv("WS_REQUEST_DELAY_SECONDS", "0")
    monkeypatch.setenv("WS_EMBEDDING_BACKEND", "lite")
    from app.config import get_settings
    from app.services import embeddings

    get_settings.cache_clear()
    embeddings.reset_backend()
    from app.db import create_all, init_engine

    init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    create_all()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def db(env):
    from app.db import SessionLocal

    s = SessionLocal()
    yield s
    s.close()


def make_image(colour=(30, 30, 30), shape="hoodie", size=(400, 500), seed=0) -> bytes:
    """Draw a simple garment-like silhouette so phash/embeddings have structure."""
    img = Image.new("RGB", size, (235, 232, 226))
    d = ImageDraw.Draw(img)
    w, h = size
    if shape == "hoodie":
        d.rectangle([w * 0.2, h * 0.25, w * 0.8, h * 0.9], fill=colour)
        d.ellipse([w * 0.35, h * 0.08, w * 0.65, h * 0.35], fill=colour)
        d.rectangle([w * 0.05, h * 0.3, w * 0.2, h * 0.75], fill=colour)
        d.rectangle([w * 0.8, h * 0.3, w * 0.95, h * 0.75], fill=colour)
    elif shape == "trousers":
        d.rectangle([w * 0.3, h * 0.05, w * 0.7, h * 0.2], fill=colour)
        d.polygon([(w * 0.3, h * 0.2), (w * 0.48, h * 0.2), (w * 0.45, h * 0.95), (w * 0.28, h * 0.95)], fill=colour)
        d.polygon([(w * 0.52, h * 0.2), (w * 0.7, h * 0.2), (w * 0.72, h * 0.95), (w * 0.55, h * 0.95)], fill=colour)
    else:  # tee
        d.polygon(
            [
                (w * 0.25, h * 0.15),
                (w * 0.75, h * 0.15),
                (w * 0.95, h * 0.35),
                (w * 0.8, h * 0.42),
                (w * 0.75, h * 0.35),
                (w * 0.75, h * 0.9),
                (w * 0.25, h * 0.9),
                (w * 0.25, h * 0.35),
                (w * 0.2, h * 0.42),
                (w * 0.05, h * 0.35),
            ],
            fill=colour,
        )
    d.text((10 + seed * 3, 10), f"#{seed}", fill=(120, 120, 120))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeWeb:
    """Routes URL -> (status, content, content-type) for httpx.MockTransport."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes | str, str]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, body: bytes | str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        self.routes[url] = (status, body, ctype)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url not in self.routes:
            return httpx.Response(404, text="not found")
        status, body, ctype = self.routes[url]
        return httpx.Response(
            status, content=body.encode() if isinstance(body, str) else body, headers={"content-type": ctype}
        )

    def client(self, **kw):
        from app.ingestion.http import PoliteClient

        return PoliteClient(delay=0, transport=httpx.MockTransport(self.handler), **kw)


@pytest.fixture()
def web():
    return FakeWeb()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
