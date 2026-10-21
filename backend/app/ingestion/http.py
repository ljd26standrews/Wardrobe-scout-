"""Polite HTTP fetching for permitted public pages.

* Honours robots.txt (configurable, on by default).
* Enforces a minimum delay between requests to the same host.
* Retries only transient failures (timeouts, 5xx) with exponential backoff.
* Detects blocks (401/403/429, CAPTCHA / verification pages) and raises
  `BlockedError` instead of retrying. We never attempt to bypass these.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

BLOCK_MARKERS = (
    "captcha",
    "验证码",
    "滑动验证",
    "人机验证",
    "access denied",
    "are you a robot",
    "cf-challenge",
    "challenge-platform",
    "请完成安全验证",
    "安全验证",
)


class FetchError(Exception):
    """Non-retryable fetch failure (404, parse failure, etc.)."""


class BlockedError(FetchError):
    """Access is restricted (robots.txt, auth, rate limiting or anti-bot)."""


class PoliteClient:
    def __init__(
        self,
        *,
        delay: float | None = None,
        respect_robots: bool | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        s = get_settings()
        self.delay = s.request_delay_seconds if delay is None else delay
        self.respect_robots = s.respect_robots_txt if respect_robots is None else respect_robots
        self.user_agent = user_agent or s.user_agent
        self._client = httpx.Client(
            timeout=s.request_timeout_seconds,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.requests_made = 0

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> RobotFileParser | None:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp: RobotFileParser | None = RobotFileParser()
        try:
            self._throttle(p.netloc)
            r = self._client.get(origin + "/robots.txt")
            self.requests_made += 1
            if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                rp.parse(r.text.splitlines())
            elif r.status_code in (401, 403):
                # Standard interpretation: robots.txt forbidden => disallow all
                rp.parse(["User-agent: *", "Disallow: /"])
            else:
                rp = None  # no robots.txt => allowed
        except httpx.HTTPError as e:
            log.warning("robots_fetch_failed", url=origin, error=str(e))
            rp = None
        self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # ---------------------------------------------------------------- throttle
    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_request.get(host)
            now = time.monotonic()
            if last is not None and now - last < self.delay:
                time.sleep(self.delay - (now - last))
            self._last_request[host] = time.monotonic()

    # ------------------------------------------------------------------- fetch
    def get(
        self, url: str, *, referer: str | None = None, binary: bool = False, check_robots: bool = True, retries: int = 2
    ) -> httpx.Response:
        if check_robots and not self.allowed(url):
            raise BlockedError(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        headers = {"Referer": referer} if referer else {}
        attempt = 0
        while True:
            self._throttle(host)
            try:
                r = self._client.get(url, headers=headers)
                self.requests_made += 1
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt >= retries:
                    raise FetchError(f"network error for {url}: {e}") from e
                attempt += 1
                time.sleep(2**attempt)
                continue
            if r.status_code in (401, 403, 407, 429, 451):
                raise BlockedError(f"HTTP {r.status_code} for {url}")
            if r.status_code >= 500:
                if attempt >= retries:
                    raise FetchError(f"HTTP {r.status_code} for {url}")
                attempt += 1
                time.sleep(2**attempt)
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code} for {url}")
            if not binary:
                head = r.text[:20000].lower()
                if any(m in head for m in BLOCK_MARKERS) and len(r.text) < 60000:
                    raise BlockedError(f"verification / anti-bot page returned for {url}")
            return r

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
