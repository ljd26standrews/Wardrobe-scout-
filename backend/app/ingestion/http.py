"""Polite HTTP fetching for permitted public pages.

* Honours robots.txt (configurable, on by default).
* Enforces a minimum delay between requests to the same host, with at most one request in flight per
  host. Different hosts (a seller's catalogue, the image CDN) proceed in parallel: a request only
  ever waits for its own host.
* Adapts: a host that answers 5xx, times out or slows down gets a longer delay (never shorter than the
  configured minimum), relaxing again gradually once it recovers.
* Retries only transient failures (timeouts, 5xx) with exponential backoff.
* Supports conditional requests (ETag / Last-Modified) so unchanged pages cost no body transfer.
* Detects blocks (401/403/429, CAPTCHA / verification pages) and raises
  `BlockedError` instead of retrying. We never attempt to bypass these.
"""

from __future__ import annotations

import ipaddress
import re
import socket
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


class InvalidPageError(FetchError):
    """The page was fetched, but it is not a product or catalogue page this app can read."""


class UnsafeURLError(FetchError):
    """The URL points at a private, loopback or metadata address (SSRF protection)."""


_resolve_cache: dict[str, tuple[float, list[str]]] = {}


def _resolve(host: str) -> list[str]:
    hit = _resolve_cache.get(host)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    try:
        addrs = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (socket.gaierror, UnicodeError):
        addrs = []  # unresolvable hosts simply fail later; nothing to protect
    _resolve_cache[host] = (time.monotonic(), addrs)
    return addrs


def is_public_url(url: str) -> bool:
    """True if the URL is http(s) and its host resolves only to public internet addresses."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.strip("[]")
    try:
        candidates = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            return False
        candidates = [ipaddress.ip_address(a.split("%")[0]) for a in _resolve(host)]
    return all(ip.is_global and not ip.is_multicast for ip in candidates)


def assert_public_url(url: str) -> None:
    if get_settings().allow_private_urls:
        return
    if not is_public_url(url):
        raise UnsafeURLError(f"refusing to fetch non-public address: {url}")


_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


class _Host:
    """Politeness state for one host: when its next request may start, its current (adaptive) delay, and
    a gate allowing one request in flight. Shared by every client that uses the same HostRegistry."""

    __slots__ = ("next_at", "delay", "gate")

    def __init__(self, delay: float) -> None:
        self.next_at = 0.0
        self.delay = delay
        self.gate = threading.Semaphore(1)  # one request in flight per host


class _Counts:
    """What one client fetched from one host (scan statistics)."""

    __slots__ = ("requests", "bytes", "errors", "slow")

    def __init__(self) -> None:
        self.requests = self.bytes = self.errors = self.slow = 0


class HostRegistry:
    """Per-host politeness shared across clients. The process-wide SHARED_HOSTS covers all real network
    traffic - a scan, an import job and photos fetched while you browse - so together they never exceed
    one request at a time per host or its delay. (Clients with a custom transport, i.e. tests and
    benchmarks, get a private registry unless one is passed.)"""

    def __init__(self) -> None:
        self._hosts: dict[str, _Host] = {}
        self.lock = threading.Lock()

    def get(self, host: str, delay: float) -> _Host:
        with self.lock:
            h = self._hosts.get(host)
            if h is None:
                h = self._hosts[host] = _Host(delay)
            return h


SHARED_HOSTS = HostRegistry()


class PoliteClient:
    MAX_DELAY = 30.0

    def __init__(
        self,
        *,
        delay: float | None = None,
        image_delay: float | None = None,
        respect_robots: bool | None = None,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
        hosts: HostRegistry | None = None,
    ) -> None:
        s = get_settings()
        self.delay = s.request_delay_seconds if delay is None else delay
        configured_image = getattr(s, "image_request_delay_seconds", None)
        self.image_delay = (
            image_delay if image_delay is not None else (self.delay if configured_image is None else configured_image)
        )
        self.respect_robots = s.respect_robots_txt if respect_robots is None else respect_robots
        self.user_agent = user_agent or s.user_agent
        self.max_bytes = s.max_download_bytes
        self._client = httpx.Client(
            timeout=s.request_timeout_seconds,
            follow_redirects=True,
            transport=transport,
            # Checked for every request, including each redirect hop.
            event_hooks={"request": [lambda req: assert_public_url(str(req.url))]},
            headers={
                "User-Agent": self.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        self._registry = hosts or (SHARED_HOSTS if transport is None else HostRegistry())
        self._counts: dict[str, _Counts] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self.requests_made = 0
        self.bytes_received = 0

    def host_state(self, host: str, binary: bool = False) -> _Host:
        return self._registry.get(host, self.image_delay if binary else self.delay)

    def _counts_for(self, host: str) -> _Counts:
        with self._lock:
            c = self._counts.get(host)
            if c is None:
                c = self._counts[host] = _Counts()
            return c

    def stats(self) -> dict[str, dict]:
        with self._lock:
            counts = dict(self._counts)
        return {
            k: {"requests": c.requests, "bytes": c.bytes, "delay": round(self.host_state(k).delay, 2),
                "errors": c.errors}
            for k, c in counts.items()
        }  # fmt: skip

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> RobotFileParser | None:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        with self._lock:
            olock = self._robots_locks.setdefault(origin, threading.Lock())
        with olock:  # concurrent first requests to a host fetch robots.txt once
            if origin in self._robots:
                return self._robots[origin]
            return self._fetch_robots(origin, p.netloc)

    def _fetch_robots(self, origin: str, netloc: str) -> RobotFileParser | None:
        rp: RobotFileParser | None = RobotFileParser()
        h = self.host_state(netloc)
        try:
            with h.gate:
                self._throttle(netloc, self.delay)
                r = self._client.get(origin + "/robots.txt")
            self._count(netloc, len(r.content))
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
    def _throttle(self, host: str, base: float) -> None:
        """Wait for this host's next slot: never sooner than this client's own delay or the host's current
        (possibly backed-off) delay. The lock is held only to book the slot, never while sleeping."""
        h = self.host_state(host)
        with self._registry.lock:
            now = time.monotonic()
            start = max(now, h.next_at)
            h.next_at = start + max(h.delay, base)
        if start > now:
            time.sleep(start - now)

    def _count(self, host: str, nbytes: int) -> None:
        c = self._counts_for(host)
        with self._lock:
            c.requests += 1
            c.bytes += nbytes
            self.requests_made += 1
            self.bytes_received += nbytes

    def _adapt(self, host: str, *, failed: bool, elapsed: float, base: float) -> None:
        """Back off on errors or slow answers - from the next request on, whichever client makes it - and relax
        gradually back towards the configured minimum."""
        h, c = self.host_state(host), self._counts_for(host)
        with self._registry.lock:
            if failed:
                c.errors += 1
                h.delay = min(self.MAX_DELAY, max(h.delay, base, 0.5) * 2)
            elif elapsed > max(5.0, 4 * base):
                c.slow += 1
                h.delay = min(self.MAX_DELAY, max(h.delay, elapsed / 2))
            elif h.delay > base:
                h.delay = max(base, h.delay * 0.9)
                return
            else:
                return
            h.next_at = max(h.next_at, time.monotonic() + h.delay)

    # ------------------------------------------------------------------- fetch
    def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        binary: bool = False,
        check_robots: bool = True,
        retries: int = 2,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> httpx.Response:
        """GET with politeness. With ``etag``/``last_modified`` an unchanged resource returns status 304."""
        if check_robots and not self.allowed(url):
            raise BlockedError(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        h = self.host_state(host, binary)
        base = self.image_delay if binary else self.delay
        headers = {"Referer": referer} if referer else {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        attempt = 0
        while True:
            with h.gate:
                self._throttle(host, base)
                t0 = time.monotonic()
                try:
                    r = self._bounded_get(url, headers)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    self._adapt(host, failed=True, elapsed=time.monotonic() - t0, base=base)
                    err: Exception | None = e
                else:
                    err = None
                    self._count(host, len(r.content))
                    self._adapt(host, failed=r.status_code >= 500, elapsed=time.monotonic() - t0, base=base)
            if err is not None:
                if attempt >= retries:
                    raise FetchError(f"network error for {url}: {err}") from err
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
            if r.status_code == 304:
                return r
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code} for {url}")
            if not binary:
                # Only visible text counts: a page that merely loads a captcha script is not a block page.
                visible = _SCRIPT.sub(" ", r.text[:60000]).lower()
                if any(m in visible[:20000] for m in BLOCK_MARKERS) and len(r.text) < 60000:
                    raise BlockedError(f"verification / anti-bot page returned for {url}")
            return r

    def _bounded_get(self, url: str, headers: dict) -> httpx.Response:
        """GET with a hard cap on response size (protects memory from huge or hostile responses)."""
        with self._client.stream("GET", url, headers=headers) as r:
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                raise FetchError(f"response too large ({int(declared)} bytes) for {url}")
            body = bytearray()
            for chunk in r.iter_bytes():
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    raise FetchError(f"response exceeded {self.max_bytes} bytes for {url}")
            headers_out = {
                k: v
                for k, v in r.headers.items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            }
            return httpx.Response(r.status_code, headers=headers_out, content=bytes(body), request=r.request)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
