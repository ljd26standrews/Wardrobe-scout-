"""Deterministic image recipes for benchmark galleries.

Garment views are derived from REAL photographs (the CC0 "clothing-dataset-small", see bench/README.md):
the original photo (hero), a mirrored/re-framed view, close-up detail crops, re-encoded near-duplicates,
seller watermarks, a garment photo with a few incidental measurement arrows, and collages.

Non-garment images are rendered: Chinese and English size tables, garment measurement diagrams, a
"photographed" size chart, an inverted (white-on-dark) chart, and shop notice cards.
"""

from __future__ import annotations

import io
import random
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

GARMENT_KINDS = {"hero", "alt", "detail", "neardup", "watermark", "measure_overlay", "collage", "colourway"}
SIZE_KINDS = {"size_table_cn", "size_table_en", "size_diagram", "size_photo", "size_dark"}
OTHER_KINDS = {"text_card", "boilerplate"}


def label_of(kind: str) -> str:
    if kind in SIZE_KINDS:
        return "size_chart"
    if kind in OTHER_KINDS:
        return "non_garment"
    return "garment"


@lru_cache(maxsize=16)
def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


@lru_cache(maxsize=512)
def _photo(path: str) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _fit(img: Image.Image, long_side: int) -> Image.Image:
    w, h = img.size
    s = long_side / max(w, h)
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


# --------------------------------------------------------------------------- garment views
def garment(kind: str, photos: list[str], rng: random.Random, seller_mark: str) -> Image.Image:
    base = _photo(photos[0])
    w, h = base.size
    if kind == "hero":
        return base.copy()
    if kind == "alt":
        f = rng.uniform(0.8, 0.92)
        cw, ch = int(w * f), int(h * f)
        x, y = rng.randint(0, w - cw), rng.randint(0, h - ch)
        return ImageOps.mirror(base.crop((x, y, x + cw, y + ch))).resize((w, h))
    if kind == "detail":
        f = rng.uniform(0.28, 0.42)
        cw, ch = int(w * f), int(h * f)
        x = rng.randint(int(w * 0.2), int(w * 0.8) - cw)
        y = rng.randint(int(h * 0.2), int(h * 0.8) - ch)
        return base.crop((x, y, x + cw, y + ch)).resize((w, h), Image.BICUBIC).filter(ImageFilter.SHARPEN)
    if kind == "neardup":
        f = rng.uniform(0.95, 0.99)
        cw, ch = int(w * f), int(h * f)
        im = base.crop(((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2)).resize((w, h))
        im = ImageEnhance.Brightness(im).enhance(rng.uniform(0.95, 1.06))
        return Image.open(io.BytesIO(jpeg(im, rng.randint(55, 75)))).convert("RGB")
    if kind == "watermark":
        im = base.copy().convert("RGBA")
        layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        fs = max(14, w // 12)
        for i in range(3):
            d.text(
                (rng.randint(0, w // 3), int(h * (0.2 + 0.3 * i))),
                seller_mark,
                font=font(fs),
                fill=(255, 255, 255, 110),
            )
        return Image.alpha_composite(im, layer).convert("RGB")
    if kind == "measure_overlay":
        im = base.copy()
        d = ImageDraw.Draw(im)
        for _ in range(2):
            y = rng.randint(int(h * 0.3), int(h * 0.7))
            x0, x1 = int(w * 0.2), int(w * 0.8)
            d.line((x0, y, x1, y), fill=(220, 30, 30), width=3)
            d.polygon([(x0, y), (x0 + 10, y - 6), (x0 + 10, y + 6)], fill=(220, 30, 30))
            d.polygon([(x1, y), (x1 - 10, y - 6), (x1 - 10, y + 6)], fill=(220, 30, 30))
            d.text(((x0 + x1) // 2 - 20, y - 26), f"{rng.randint(48, 75)}cm", font=font(20), fill=(220, 30, 30))
        return im
    if kind == "collage":
        tiles = [base, ImageOps.mirror(base)] + [_photo(p) for p in photos[1:3]]
        while len(tiles) < 4:
            tiles.append(garment("detail", photos, rng, seller_mark))
        cell = (w // 2, h // 2)
        out = Image.new("RGB", (cell[0] * 2, cell[1] * 2), "white")
        for i, t in enumerate(tiles[:4]):
            out.paste(t.resize(cell), ((i % 2) * cell[0], (i // 2) * cell[1]))
        return out
    if kind == "colourway":
        # Same garment photo in another colour: hue rotation of the whole photo.
        hsv = base.convert("HSV")
        hch, s, v = hsv.split()
        shift = rng.choice([60, 100, 150])
        hch = hch.point(lambda x: (x + shift) % 256)
        s = s.point(lambda x: min(255, int(x * 1.6) + 40))
        return Image.merge("HSV", (hch, s, v)).convert("RGB")
    raise ValueError(kind)


# --------------------------------------------------------------------------- size charts and text cards
CN_COLS = ["尺码", "衣长", "胸围", "肩宽", "袖长", "下摆"]
EN_COLS = ["Size", "Length", "Chest", "Shoulder", "Sleeve", "Hem"]
SIZES = ["S", "M", "L", "XL", "XXL", "3XL"]


def _table(rng: random.Random, cols: list[str], inverted: bool = False) -> Image.Image:
    ncols = rng.randint(4, len(cols))
    nrows = rng.randint(4, 6)
    cw, rh = rng.randint(110, 150), rng.randint(46, 60)
    pad = 40
    title_h = 70 if rng.random() < 0.7 else 0
    W, H = cw * ncols + 2 * pad, rh * (nrows + 1) + 2 * pad + title_h
    bg, fg = (
        ((30, 30, 34), (240, 240, 240)) if inverted else (rng.choice([(255, 255, 255), (250, 248, 242)]), (30, 30, 30))
    )
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    if title_h:
        d.text(
            (pad, pad // 2),
            rng.choice(["尺码表 SIZE CHART", "尺寸表（单位：cm）", "Size Guide (cm)"]),
            font=font(34),
            fill=fg,
        )
    top = pad + title_h
    head_fg = fg
    if rng.random() < 0.6 and not inverted:
        band = rng.choice([(40, 40, 40), (200, 170, 120), (230, 230, 230)])
        d.rectangle((pad, top, pad + cw * ncols, top + rh), fill=band)
        head_fg = (255, 255, 255) if band[0] < 100 else fg
    for r in range(nrows + 2):
        d.line((pad, top + r * rh, pad + cw * ncols, top + r * rh), fill=fg, width=2)
    for c in range(ncols + 1):
        d.line((pad + c * cw, top, pad + c * cw, top + rh * (nrows + 1)), fill=fg, width=2)
    for c in range(ncols):
        d.text((pad + c * cw + 12, top + 10), cols[c], font=font(24), fill=head_fg)
        for r in range(nrows):
            val = SIZES[r] if c == 0 else f"{rng.randint(40, 80) + 2 * r}"
            d.text((pad + c * cw + 14, top + (r + 1) * rh + 10), val, font=font(24), fill=fg)
    if rng.random() < 0.5:
        d.text((pad, H - pad + 6), "手工测量，误差1-3cm属正常", font=font(20), fill=fg)
    return im


def _diagram(rng: random.Random) -> Image.Image:
    W, H = 900, rng.choice([900, 1100])
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    # flat garment outline (t-shirt or trousers) drawn with lines
    if rng.random() < 0.6:
        pts = [(300, 150), (400, 120), (500, 120), (600, 150), (760, 280), (690, 360), (610, 300), (610, 720),
               (290, 720), (290, 300), (210, 360), (140, 280)]  # fmt: skip
    else:
        pts = [(320, 120), (580, 120), (620, 760), (500, 760), (450, 330), (400, 760), (280, 760)]
    d.line(pts + [pts[0]], fill=(40, 40, 40), width=3)
    for x0, y0, x1, y1, lab in [
        (300, 780, 600, 780, "A 胸围"),
        (240, 140, 240, 720, "B 衣长"),
        (330, 90, 570, 90, "C 肩宽"),
    ]:
        d.line((x0, y0, x1, y1), fill=(200, 40, 40), width=2)
        d.text(((x0 + x1) // 2 - 30, (y0 + y1) // 2 + 6), lab, font=font(26), fill=(200, 40, 40))
    if H > 900:
        small = _table(rng, CN_COLS).resize((W - 80, 200))
        im.paste(small, (40, H - 230))
    return im


def _photographed(rng: random.Random) -> Image.Image:
    t = _table(rng, rng.choice([CN_COLS, EN_COLS]))
    paper = Image.new("RGB", (t.width + 160, t.height + 160), (rng.randint(150, 200),) * 3)
    t = ImageEnhance.Brightness(t).enhance(rng.uniform(0.82, 0.95))
    paper.paste(t, (80, 80))
    paper = paper.rotate(rng.uniform(-6, 6), resample=Image.BICUBIC, fillcolor=(120, 115, 110))
    return paper.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 1.0)))


def _text_card(rng: random.Random, seller: str) -> Image.Image:
    W, H = 800, rng.choice([600, 1000])
    im = Image.new("RGB", (W, H), rng.choice([(255, 255, 255), (246, 240, 230), (20, 20, 20)]))
    fg = (240, 240, 240) if im.getpixel((0, 0))[0] < 100 else (40, 40, 40)
    d = ImageDraw.Draw(im)
    lines = [
        f"{seller} 实拍",
        "下单请备注尺码颜色",
        "支持验货 · 七天无理由",
        "所有图片均为实物拍摄",
        "微信咨询客服",
        "发货时间 3-5 天",
    ]
    y = 80
    for ln in lines * 2:
        d.text((60, y), ln, font=font(34), fill=fg)
        y += 70
        if y > H - 80:
            break
    return im


def non_garment(kind: str, rng: random.Random, seller: str) -> Image.Image:
    if kind == "size_table_cn":
        return _table(rng, CN_COLS)
    if kind == "size_table_en":
        return _table(rng, EN_COLS)
    if kind == "size_diagram":
        return _diagram(rng)
    if kind == "size_photo":
        return _photographed(rng)
    if kind == "size_dark":
        return _table(rng, rng.choice([CN_COLS, EN_COLS]), inverted=True)
    if kind in ("text_card", "boilerplate"):
        return _text_card(rng, seller)
    raise ValueError(kind)


def render(recipe: dict) -> Image.Image:
    """recipe = {kind, photos, seed, seller}"""
    rng = random.Random(recipe["seed"])
    if recipe["kind"] in GARMENT_KINDS:
        return garment(recipe["kind"], recipe["photos"], rng, recipe["seller"])
    return non_garment(recipe["kind"], rng, recipe["seller"])


VARIANT_LONG_SIDE = {"big": 1280, "medium": 640, "small": 320}


def variant_bytes(recipe: dict, variant: str) -> bytes:
    img = _fit(render(recipe), VARIANT_LONG_SIDE[variant])
    return jpeg(img, 86 if variant == "big" else 80)
