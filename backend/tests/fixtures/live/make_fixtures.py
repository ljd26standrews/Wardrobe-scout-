"""Trim real captures to the structure the parsers read; redact phone numbers. Usage: make_fixtures.py SRC OUT"""

import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
PHONE = re.compile(r"(?<![\w-])(\+?86[\s-]?)?1\d{2}[\s-]?\d{4}[\s-]?\d{4}(?![\w-])")
WA = re.compile(r"wa\.me(%252F|%2F|/)\d+")


def redact(s):
    s = re.sub(
        r"data:image/png;base64,[A-Za-z0-9+/=]{40,}", "data:image/png;base64,iVBORw0KGgo=", s
    )  # 1x1 placeholder, shortened
    s = WA.sub(lambda m: "wa.me" + m.group(1) + "00000000000", s)
    return PHONE.sub("[phone redacted]", s)


def page(head, body):
    return f'<!doctype html>\n<html><head><meta charset="utf-8">{head}</head><body>\n{body}\n</body></html>\n'


def head_of(soup):
    return "".join(str(x) for x in [soup.title, soup.select_one("link[rel=canonical]")] if x)


def album(name, outname):
    soup = BeautifulSoup((src / name).read_text("utf-8"), "lxml")
    hdr = soup.select_one(".showalbumheader__main") or soup.select_one(".showalbumheader__gallerytitle").parent
    kids = soup.select(".showalbum__children")
    body = str(hdr) + '\n<div class="showalbum__parent">' + "".join(str(k) for k in kids) + "</div>"
    (out / outname).write_text(redact(page(head_of(soup), body)), "utf-8")


def listing(name, outname):
    soup = BeautifulSoup((src / name).read_text("utf-8"), "lxml")
    parts = [str(x) for x in soup.select(".indexlock__main")]
    seen = set()
    for a in soup.select("a[href*='/albums/']"):
        if a.get("href") not in seen and re.search(r"/albums/\d+", a.get("href", "")):
            seen.add(a["href"])
            parts.append(str(a))
    parts += [str(x) for x in soup.select(".pagination__main, .pagination__jumpwrap")[:1]]
    parts.append('<div class="passwordmodal__div"><p>此相册已加密</p></div>')  # present on every capture
    (out / outname).write_text(redact(page(head_of(soup), "\n".join(parts))), "utf-8")


def weidian(name, outname):
    soup = BeautifulSoup((src / name).read_text("utf-8"), "lxml")
    el = soup.select_one("#__rocker-render-inject__")
    d = json.loads(el["data-obj"])
    m = d["result"]["default_model"]
    keep = {
        "status": d["status"],
        "result": {
            "default_model": {
                "item_info": {k: v for k, v in m["item_info"].items() if k not in ("risk_info", "source_id")},
                "sku_properties": m["sku_properties"],
                "shop_info": {"shopName": m["shop_info"].get("shopName")},
            }
        },
    }
    tag = soup.new_tag("script", id="__rocker-render-inject__", crossorigin="anonymous")
    tag["data-obj"] = json.dumps(keep, ensure_ascii=False, separators=(",", ":"))
    (out / outname).write_text(page(str(soup.title), str(tag)), "utf-8")


album("detail-0.html", "yupoo_madebykungfu_album_259067099.html")
album("detail-1.html", "yupoo_aristide_album_224673345.html")
album("detail-2.html", "yupoo_stitches_album_250109320.html")
weidian("detail-3.html", "weidian_item_7707075268.html")
for s in ["madebykungfu", "aristide", "stitches-reps-2", "ethanhomme"]:
    listing(f"{s}-listing.html", f"yupoo_{s.replace('-', '_')}_listing.html")
