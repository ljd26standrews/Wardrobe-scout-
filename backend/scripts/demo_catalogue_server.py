"""Serve SYNTHETIC Yupoo-style catalogues on localhost for end-to-end testing.

Every product title is prefixed with "[DEMO]" and images are drawn shapes, so
nothing here can be mistaken for real seller data. Use it only with a separate
data directory, e.g.:

    WS_DATA_DIR=./e2e-data python scripts/demo_catalogue_server.py   # ports 8765 + 8766

Two sellers are served (one per port). Some albums deliberately share photos
across the two sellers so cross-seller comparison can be exercised. A third port,
if given, serves awkward listings for layout and honesty checks: a very long
title, no price, a landscape photo, photos that fail to load, and one garment
re-listed several times (many offers).
"""

from __future__ import annotations

import io
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw

PRODUCTS = [
    # (id, title, desc, colour rgb, shape)
    (
        "2001",
        "[DEMO] 【A01】羊毛羊绒连帽卫衣 黑色 ¥899",
        "成分：70%羊毛 30%羊绒\n尺码：S-XL\n落肩宽松版型 无logo",
        (24, 24, 24),
        "hoodie",
    ),
    (
        "2002",
        "[DEMO] 初剪羊毛休闲裤 深灰 宽松直筒 P399",
        "面料：100%初剪羊毛\n尺码 46 48 50 52",
        (60, 60, 64),
        "trousers",
    ),
    (
        "2003",
        "[DEMO] 280g重磅纯棉短袖T恤 白色 无logo P168",
        "克重280g 100%长绒棉 尺码 S M L XL",
        (248, 247, 243),
        "tee",
    ),
    ("2004", "[DEMO] 羊绒圆领毛衣 燕麦色 ¥1280", "100%羊绒 12针 尺码 S-XL 宽松", (214, 198, 170), "knit"),
    ("2005", "[DEMO] MM 羊毛双头拉链夹克 黑色 ¥560", "80%羊毛 20%羊绒 双头拉链 尺码 46 48 50", (30, 30, 32), "jacket"),
    ("2006", "[DEMO] 大logo印花卫衣 红色 P259", "100%棉 大logo印花 尺码 S-XL", (170, 40, 40), "hoodie"),
    ("2007", "[DEMO] 美利奴羊毛开衫 藏青 ¥690", "100%美利奴羊毛 贝壳扣 尺码 S-XL", (31, 42, 68), "knit"),
    ("2008", "[DEMO] 双面呢大衣 驼色 ¥1680", "90%羊毛 10%羊绒 双面呢 手工 尺码 46-52", (180, 138, 90), "coat"),
    ("2009", "[DEMO] 修身牛仔裤 蓝色 P199", "98%棉 2%氨纶 修身 尺码 28-34", (60, 90, 140), "trousers"),
    ("2010", "[DEMO] 华夫格长袖T恤 米白 P229", "100%棉 华夫格 尺码 S M L XL", (236, 228, 212), "tee"),
    ("2011", "[DEMO] 涤纶运动夹克 黄色 P159", "100%涤纶 尺码 S-XL", (220, 180, 50), "jacket"),
    ("2012", "[DEMO] 羊绒混纺卫裤 炭灰 ¥780", "60%羊毛 40%羊绒 宽松 尺码 S-XL", (55, 55, 58), "trousers"),
]
# Seller B re-lists some of seller A's albums with the same photos at different prices
SELLER_B = [
    ("3001", "[DEMO] 羊毛羊绒连帽卫衣 黑色 原单 ¥720", "70%羊毛 30%羊绒 尺码 M-XL", "2001"),
    ("3002", "[DEMO] 双头拉链羊毛夹克 黑 ¥610", "羊毛混纺 尺码 48 50", "2005"),
    ("3003", "[DEMO] 重磅白T 260g P139", "260g 纯棉", None),
]


# Seller C: (id, title, description, photo keys). "missing-*" photos answer 404 on purpose.
SELLER_C = [
    (
        "4001",
        "[DEMO] 【C01】超长标题版式测试 Extra-long title for layout checks: 意大利进口双面羊绒羊毛混纺落肩宽松版型长款大衣 "
        "oversized double-faced wool and cashmere long coat with hand-finished seams, horn buttons and a deep back vent ¥2380",
        "90%羊毛 10%羊绒 尺码 46-54",
        ["4001-0", "4001-1"],
    ),
    ("4002", "[DEMO] 羊毛针织背心 灰色 询价", "100%羊毛 尺码 S-XL 价格请咨询", ["4002-0"]),  # no price stated
    ("4003", "[DEMO] 平铺拍摄 羊毛直筒裤 炭灰 P329", "100%羊毛 尺码 46-52", ["4003-0", "4003-1"]),
    ("4004", "[DEMO] 图片加载失败测试 棉质衬衫 白色 P219", "100%棉 尺码 S-XL", ["missing-0", "missing-1"]),
    ("4005", "[DEMO] 双面呢大衣 驼色 再上架 ¥1590", "90%羊毛 10%羊绒 尺码 46-52", ["2008-0", "2008-1"]),
    ("4006", "[DEMO] 双面呢大衣 驼色 现货 ¥1720", "90%羊毛 10%羊绒 尺码 48 50", ["2008-0", "2008-1"]),
    ("4007", "[DEMO] 双面呢大衣 驼色 补货 ¥1650", "90%羊毛 10%羊绒 尺码 46-52", ["2008-0", "2008-1"]),
]
# Photos drawn only for seller C: (colour, shape, landscape)
DRAWINGS_C = {
    "4001": ((96, 84, 72), "coat", False),
    "4002": ((128, 128, 132), "knit", False),
    "4003": ((55, 55, 58), "trousers", True),  # photographed flat: a wide (landscape) picture
}


def draw(colour, shape, variant=0, landscape=False) -> bytes:
    img = Image.new("RGB", (600, 750), (236, 233, 227) if variant % 2 == 0 else (226, 222, 214))
    d = ImageDraw.Draw(img)
    w, h = img.size
    if shape in ("hoodie", "knit"):
        d.rectangle([w * 0.22, h * 0.25, w * 0.78, h * 0.88], fill=colour)
        d.rectangle([w * 0.06, h * 0.28, w * 0.22, h * 0.78], fill=colour)
        d.rectangle([w * 0.78, h * 0.28, w * 0.94, h * 0.78], fill=colour)
        if shape == "hoodie":
            d.ellipse([w * 0.36, h * 0.08, w * 0.64, h * 0.34], fill=colour)
        else:
            d.ellipse([w * 0.4, h * 0.2, w * 0.6, h * 0.3], fill=(236, 233, 227))
    elif shape in ("jacket", "coat"):
        bottom = 0.95 if shape == "coat" else 0.8
        d.rectangle([w * 0.2, h * 0.15, w * 0.8, h * bottom], fill=colour)
        d.rectangle([w * 0.05, h * 0.18, w * 0.2, h * 0.75], fill=colour)
        d.rectangle([w * 0.8, h * 0.18, w * 0.95, h * 0.75], fill=colour)
        d.line([w * 0.5, h * 0.15, w * 0.5, h * bottom], fill=(200, 200, 200), width=4)
    elif shape == "trousers":
        d.rectangle([w * 0.28, h * 0.05, w * 0.72, h * 0.18], fill=colour)
        d.polygon([(w * 0.28, h * 0.18), (w * 0.49, h * 0.18), (w * 0.46, h * 0.96), (w * 0.26, h * 0.96)], fill=colour)
        d.polygon([(w * 0.51, h * 0.18), (w * 0.72, h * 0.18), (w * 0.74, h * 0.96), (w * 0.54, h * 0.96)], fill=colour)
    else:
        d.polygon(
            [
                (w * 0.25, h * 0.15),
                (w * 0.75, h * 0.15),
                (w * 0.95, h * 0.33),
                (w * 0.8, h * 0.4),
                (w * 0.75, h * 0.33),
                (w * 0.75, h * 0.9),
                (w * 0.25, h * 0.9),
                (w * 0.25, h * 0.33),
                (w * 0.2, h * 0.4),
                (w * 0.05, h * 0.33),
            ],
            fill=colour,
            outline=(190, 190, 190),
        )
    if variant:  # other views are close-ups of a different part, so each is a genuinely different photograph
        boxes = [(0, 0, 0.6, 0.6), (0.4, 0.4, 1, 1), (0, 0.4, 0.6, 1), (0.4, 0, 1, 0.6)]
        x0, y0, x1, y1 = boxes[(variant - 1) % len(boxes)]
        img = img.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1))).resize((w, h))
        d = ImageDraw.Draw(img)
    if landscape:
        img = img.rotate(90, expand=True)
        d = ImageDraw.Draw(img)
    d.text((18, img.size[1] - 30), f"SYNTHETIC DEMO IMAGE {variant}", fill=(150, 150, 150))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def make_handler(catalogue: list[dict], label: str):
    by_id = {p["id"]: p for p in catalogue}
    per_page = 6

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def send(self, body: bytes, ctype="text/html; charset=utf-8", status=200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/robots.txt":
                return self.send(b"User-agent: *\nAllow: /\n", "text/plain")
            if u.path == "/albums":
                page = int(parse_qs(u.query).get("page", ["1"])[0])
                pages = (len(catalogue) + per_page - 1) // per_page
                items = catalogue[(page - 1) * per_page : page * per_page]
                cards = "".join(
                    f'<a class="album__main" title="{p["title"]}" href="/albums/{p["id"]}?uid=1">'
                    f'<img class="album__img" data-origin-src="/photo/{p["photos"][0]}.jpg"><div class="album__title">{p["title"]}</div></a>'
                    for p in items
                )
                nav = "".join(f'<a href="/albums?page={i}">{i}</a>' for i in range(1, pages + 1))
                html = (
                    f"<html><body><!-- SYNTHETIC DEMO {label} --><div class='showindex__children'>{cards}</div>"
                    f"<div class='pagination__main'>{nav}<form class='pagination__jumpwrap'><input name='page' max='{pages}'></form></div></body></html>"
                )
                return self.send(html.encode())
            if u.path.startswith("/albums/"):
                p = by_id.get(u.path.split("/")[-1])
                if not p:
                    return self.send(b"not found", status=404)
                imgs = "".join(
                    f'<div class="showalbum__children"><img data-origin-src="/photo/{ph}.jpg"></div>'
                    for ph in p["photos"]
                )
                desc = p["desc"].replace("\n", "<br>")
                html = (
                    f"<html><body><span class='showalbumheader__gallerytitle'>{p['title']}</span>"
                    f"<div class='showalbumheader__gallerysubtitle'>{desc}</div>{imgs}</body></html>"
                )
                return self.send(html.encode())
            if u.path.startswith("/photo/"):
                key = u.path.split("/")[-1].removesuffix(".jpg")
                pid, variant = key.rsplit("-", 1)
                if pid in DRAWINGS_C:
                    colour, shape, landscape = DRAWINGS_C[pid]
                    return self.send(draw(colour, shape, int(variant), landscape), "image/jpeg")
                if pid not in PRODUCTS_BY_ID:
                    return self.send(b"not found", status=404)  # a photo that fails to load
                src = PRODUCTS_BY_ID[pid]
                return self.send(draw(src[3], src[4], int(variant)), "image/jpeg")
            self.send(b"not found", status=404)

    return H


PRODUCTS_BY_ID = {p[0]: p for p in PRODUCTS}


def catalogues():
    a = [{"id": pid, "title": t, "desc": d, "photos": [f"{pid}-0", f"{pid}-1"]} for pid, t, d, _, _ in PRODUCTS]
    b = []
    for pid, t, d, same in SELLER_B:
        photos = [f"{same}-0", f"{same}-1"] if same else ["2003-1"]
        b.append({"id": pid, "title": t, "desc": d, "photos": photos})
    c = [{"id": pid, "title": t, "desc": d, "photos": photos} for pid, t, d, photos in SELLER_C]
    return a, b, c


def main(port_a: int = 8765, port_b: int = 8766, port_c: int | None = None) -> None:
    a, b, c = catalogues()
    servers = [
        ThreadingHTTPServer(("127.0.0.1", port_a), make_handler(a, "seller A")),
        ThreadingHTTPServer(("127.0.0.1", port_b), make_handler(b, "seller B")),
    ]
    if port_c:
        servers.append(ThreadingHTTPServer(("127.0.0.1", port_c), make_handler(c, "seller C (edge cases)")))
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    ports = ", ".join(f"http://127.0.0.1:{s.server_address[1]}" for s in servers)
    print(f"Synthetic demo catalogues on {ports}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main(*(int(x) for x in sys.argv[1:4]))
