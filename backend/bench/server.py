"""A local HTTP server that serves a benchmark catalogue with Yupoo-like markup.

Runs in its own process so the scanner's CPU and memory measurements are not polluted by image
generation. Routes on the Host header:

* ``<seller>.x.yupoo.com``: ``/robots.txt``, ``/albums?page=N`` (40 albums per page), ``/albums/<id>?uid=1``
* ``photo.yupoo.com``: ``/<seller>/<key>/{big,medium,small}.jpg``

Network model per response: ``scale * (rtt + bytes / bandwidth)`` (generation time is subtracted, so as
long as rendering is faster than the modelled transfer the latency is exactly as configured).
Faults: ``--error-rate`` (HTTP 503), ``--missing-rate`` (image 404), ``--malformed-rate`` (truncated album
HTML), ``--slow-rate`` (x10 latency). ETag / If-None-Match is supported for every response.

Usage: python -m bench.server SPEC.json --port 8765 --scale 0.05
Control: GET /__control/load?path=SPEC.json (swap catalogue), GET /__control/stats, GET /__control/reset
"""

from __future__ import annotations

import argparse
import hashlib
import html
import random
import threading
import time
from collections import OrderedDict, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import catalogue, render

PER_PAGE = 40


class State:
    def __init__(self, spec_path: str, args) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self.load(spec_path)
        self.reset()

    def load(self, path: str) -> None:
        spec = catalogue.load(Path(path))
        with self.lock:
            self.spec = spec
            self.by_id = {(s, a["id"]): a for s, al in spec["albums"].items() for a in al}

    def reset(self) -> None:
        with self.lock:
            self.hits: dict[str, int] = defaultdict(int)
            self.bytes: dict[str, int] = defaultdict(int)

    def image(self, key: str, variant: str) -> bytes | None:
        recipe = self.spec["images"].get(key)
        if recipe is None or variant not in render.VARIANT_LONG_SIDE:
            return None
        ck = (key, variant)
        with self.lock:
            if ck in self.cache:
                self.cache.move_to_end(ck)
                return self.cache[ck]
        data = render.variant_bytes(recipe, variant)
        with self.lock:
            self.cache[ck] = data
            while len(self.cache) > 4000:
                self.cache.popitem(last=False)
        return data


def listing_html(seller: str, albums: list[dict], page: int) -> str:
    pages = max(1, (len(albums) + PER_PAGE - 1) // PER_PAGE)
    chunk = albums[(page - 1) * PER_PAGE : page * PER_PAGE]
    cards = []
    for a in chunk:
        cover = a["images"][0]["key"]
        t = html.escape(a["title"])
        cards.append(
            f'<div class="showindex__parent"><a class="album__main" title="{t}" href="/albums/{a["id"]}?uid=1">'
            f'<div class="album__imgwrap"><img class="album__img autocover" '
            f'data-src="//photo.yupoo.com/{seller}/{cover}/medium.jpg" '
            f'data-origin-src="//photo.yupoo.com/{seller}/{cover}/big.jpg"></div>'
            f'<div class="album__title">{t}</div></a></div>'
        )
    nav = "".join(f'<a class="pagination__number" href="/albums?page={i}">{i}</a>' for i in range(1, pages + 1))
    nxt = f'<a class="pagination__next" href="/albums?page={page + 1}">下一页</a>' if page < pages else ""
    return (
        f"<!DOCTYPE html><html><head><title>{seller} | 又拍图片管家</title></head><body>"
        f'<div class="showindex__children">{"".join(cards)}</div>'
        f'<div class="pagination__main">{nav}{nxt}<form class="pagination__jumpwrap"><span>共 {pages} 页</span>'
        f'<input name="page" type="number" max="{pages}"></form></div></body></html>'
    )


def album_html(seller: str, a: dict) -> str:
    imgs = "".join(
        f'<div class="showalbum__children image__main"><div class="image__imagewrap">'
        f'<img class="autocover image__img" data-src="//photo.yupoo.com/{seller}/{im["key"]}/medium.jpg" '
        f'data-origin-src="//photo.yupoo.com/{seller}/{im["key"]}/big.jpg"></div></div>'
        for im in a["images"]
    )
    t = html.escape(a["title"])
    return (
        f"<!DOCTYPE html><html><head><title>{t} | {seller}</title></head><body>"
        f'<div class="showalbumheader__main"><span class="showalbumheader__gallerytitle">{t}</span>'
        f'<div class="showalbumheader__gallerysubtitle htmlwrap__main">面料舒适 做工精细<br>尺码 S M L XL</div></div>'
        f'<div class="showalbum__parent showalbum__nor nor">{imgs}</div></body></html>'
    )


def make_handler(state: State):
    a = state.args

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # silence
            pass

        def _send(self, status: int, body: bytes, ctype: str, t0: float, host: str) -> None:
            etag = '"' + hashlib.md5(body).hexdigest() + '"'
            if status == 200 and self.headers.get("If-None-Match") == etag:
                status, body = 304, b""
            slow = 10 if random.random() < a.slow_rate else 1
            target = a.scale * slow * (a.rtt + len(body) / a.bandwidth)
            spent = time.perf_counter() - t0
            if target > spent:
                time.sleep(target - spent)
            with state.lock:
                state.hits[host] += 1
                state.bytes[host] += len(body)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if status in (200, 304):
                self.send_header("ETag", etag)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            t0 = time.perf_counter()
            host = (self.headers.get("Host") or "").split(":")[0]
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path.startswith("/__control/"):
                cmd = u.path.rsplit("/", 1)[-1]
                if cmd == "load":
                    state.load(q["path"][0])
                elif cmd == "reset":
                    state.reset()
                import json

                body = json.dumps({"hits": state.hits, "bytes": state.bytes}).encode()
                return self._send(200, body, "application/json", t0, "__control")
            if host != "photo.yupoo.com" and random.random() < a.error_rate:
                return self._send(503, b"busy", "text/plain", t0, host)
            if host == "photo.yupoo.com":
                parts = u.path.strip("/").split("/")
                if len(parts) == 3 and random.random() >= a.missing_rate:
                    data = state.image(parts[1], parts[2].removesuffix(".jpg"))
                    if data is not None:
                        return self._send(200, data, "image/jpeg", t0, host)
                return self._send(404, b"not found", "text/plain", t0, host)
            seller = host.split(".")[0]
            albums = state.spec["albums"].get(seller)
            if albums is None:
                return self._send(404, b"no such seller", "text/plain", t0, host)
            if u.path == "/robots.txt":
                return self._send(200, b"User-agent: *\nAllow: /\n", "text/plain", t0, host)
            if u.path == "/albums":
                page = int(q.get("page", ["1"])[0] or 1)
                return self._send(
                    200, listing_html(seller, albums, page).encode(), "text/html; charset=utf-8", t0, host
                )
            if u.path.startswith("/albums/"):
                alb = state.by_id.get((seller, u.path.rsplit("/", 1)[-1]))
                if alb is None:
                    return self._send(404, b"gone", "text/plain", t0, host)
                body = album_html(seller, alb).encode()
                if random.random() < a.malformed_rate:
                    body = body[: len(body) // 3]
                return self._send(200, body, "text/html; charset=utf-8", t0, host)
            return self._send(404, b"not found", "text/plain", t0, host)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scale", type=float, default=0.05, help="multiplier on modelled network time")
    ap.add_argument("--rtt", type=float, default=0.12, help="seconds per request before scaling")
    ap.add_argument("--bandwidth", type=float, default=3e6, help="bytes/second before scaling")
    ap.add_argument("--error-rate", type=float, default=0.0)
    ap.add_argument("--missing-rate", type=float, default=0.0)
    ap.add_argument("--malformed-rate", type=float, default=0.0)
    ap.add_argument("--slow-rate", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    random.seed(args.seed)
    state = State(args.spec, args)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    srv.daemon_threads = True
    print(f"serving on {args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
