"""Security behaviour: API token, SSRF protection, download caps, file paths."""

import httpx
import pytest
from fastapi.testclient import TestClient

from .conftest import AUTH


@pytest.fixture()
def app(env):
    from app.main import create_app

    return create_app()


def test_api_requires_token(app):
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200  # liveness stays open
        r = c.get("/api/backup/export", headers={"Origin": "https://evil.example"})
        assert r.status_code == 401
        assert c.get("/api/sellers", headers={"X-WS-Token": "wrong"}).status_code == 401
        assert c.get("/api/sellers", headers=AUTH).status_code == 200


def test_bookmarklet_preflight_and_cors_on_errors(app):
    with TestClient(app) as c:
        pre = c.options(
            "/api/import/bookmarklet",
            headers={
                "Origin": "https://shop.x.yupoo.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-ws-token",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert pre.status_code == 200 and pre.headers["access-control-allow-private-network"] == "true"
        r = c.post(
            "/api/import/bookmarklet",
            json={"url": "https://a.x.yupoo.com/albums/1", "html": "x"},
            headers={"Origin": "https://a.x.yupoo.com"},
        )
        assert r.status_code == 401 and r.headers.get("access-control-allow-origin")


def test_token_file_generated_with_private_permissions(env, monkeypatch):
    monkeypatch.delenv("WS_API_TOKEN")
    from app.config import get_api_token, get_settings

    get_settings.cache_clear()
    tok = get_api_token()
    path = env / "api_token"
    assert len(tok) >= 32 and path.read_text() == tok
    if __import__("os").name != "nt":  # Windows uses inherited ACLs, not POSIX mode bits
        assert (path.stat().st_mode & 0o777) == 0o600
    assert get_api_token() == tok  # stable across calls


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/api/backup/export",
        "http://localhost/",
        "http://10.1.2.3/",
        "file:///etc/passwd",
    ],
)
def test_ssrf_urls_rejected_at_api(app, url):
    with TestClient(app, headers=AUTH) as c:
        assert c.post("/api/import/url", json={"url": url}).status_code == 422
        assert c.post("/api/sellers", json={"name": "x", "base_url": url}).status_code == 422


def test_ssrf_redirect_to_private_address_blocked(env):
    from app.ingestion.http import FetchError, PoliteClient

    def handler(req: httpx.Request):
        if req.url.host == "shop.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/secret"})
        return httpx.Response(200, text="SECRET")

    with PoliteClient(delay=0, respect_robots=False, transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(FetchError, match="non-public"):
            c.get("https://shop.example.com/item")


def test_download_size_cap(env, monkeypatch):
    from app.config import get_settings
    from app.ingestion.http import FetchError, PoliteClient

    monkeypatch.setattr(get_settings(), "max_download_bytes", 1000)
    big = httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 5000))
    with (
        PoliteClient(delay=0, respect_robots=False, transport=big) as c,
        pytest.raises(FetchError, match="exceeded|too large"),
    ):
        c.get("https://shop.example.com/huge", binary=True)


def test_captcha_script_reference_is_not_a_block(env):
    from app.ingestion.http import PoliteClient

    page = "<html><script src='https://cdn.example/captcha.js'></script><h1>Album</h1></html>"
    t = httpx.MockTransport(lambda req: httpx.Response(200, text=page))
    with PoliteClient(delay=0, respect_robots=False, transport=t) as c:
        assert "Album" in c.get("https://shop.example.com/a").text
