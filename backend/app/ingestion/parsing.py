"""Pure functions that turn listing text into structured attributes.

These are deliberately conservative: when a value is not clearly stated the
function returns None / [] rather than guessing. Everything extracted here is
treated downstream as a *seller claim*, never as verified fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlparse

from .vocab import (
    BRAND_ABBREVIATIONS,
    BRANDS,
    CATEGORIES,
    COLOURS,
    FIBRES,
    FIT_TAGS,
    LOGO_NONE,
    LOGO_SUBTLE,
    LOGO_VISIBLE,
    SIZE_TOKENS,
)

CJK = re.compile(r"[一-鿿]")


def has_cjk(text: str | None) -> bool:
    return bool(text and CJK.search(text))


def normalise_space(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("　", " ")).strip()


def _alias_in(alias: str, low: str) -> bool:
    """Match an alias; ASCII aliases need word boundaries, CJK aliases do not."""
    if has_cjk(alias):
        return alias in low
    return re.search(rf"(?<![a-z0-9-]){re.escape(alias)}(?![a-z0-9])", low) is not None


# --------------------------------------------------------------------------- price
_PRICE_PATTERNS = [
    re.compile(r"[¥￥]\s*(\d{2,6}(?:\.\d{1,2})?)"),
    re.compile(r"(\d{2,6}(?:\.\d{1,2})?)\s*(?:元|rmb|cny|RMB|CNY)"),
    # "(429￥)": ¥ directly after the number and not starting the next price ("8009¥919" is code + price)
    re.compile(r"(?<![\d.])(\d{2,6}(?:\.\d{1,2})?)[¥￥](?!\s*\d)"),
    re.compile(r"(?:价格|售价|price|Price|PRICE)\s*[:：]?\s*[¥￥]?\s*(\d{2,6}(?:\.\d{1,2})?)"),
    re.compile(r"(?<![A-Za-z0-9])[PpＰ]\s?(\d{2,5})(?![0-9])"),  # Yupoo convention: "P399"
]


# Labels meaning "boutique / original retail price" (not what this seller charges) and labels that
# explicitly mark the seller's own selling price.
_RETAIL_MARKERS = re.compile(r"(专柜|原价|官网|吊牌|市场价|零售|国内|代购|官方|正品价|rrp|msrp|retail|orig)", re.I)
_SELLING_MARKERS = re.compile(r"(现价|售价|到手|特价|活动价|折后|秒杀|批发|拿货|now|sale)", re.I)


def parse_price(text: str | None) -> float | None:
    """The seller's own price. Retail/boutique reference prices are ignored; if only a retail price is
    quoted the result is None (unknown) rather than a wrong number."""
    if not text:
        return None
    candidates: list[tuple[int, float, bool, bool]] = []  # (position, value, is_retail, is_selling)
    for pat in _PRICE_PATTERNS:
        for m in pat.finditer(text):
            value = float(m.group(1))
            if not 10 <= value <= 200000:
                continue
            before = text[max(0, m.start() - 8) : m.start() + 4]
            candidates.append(
                (m.start(), value, bool(_RETAIL_MARKERS.search(before)), bool(_SELLING_MARKERS.search(before)))
            )
    if not candidates:
        return None
    candidates.sort()
    selling = [c for c in candidates if c[3] and not c[2]]
    if selling:
        return selling[0][1]
    plain = [c for c in candidates if not c[2]]
    return plain[0][1] if plain else None


# ------------------------------------------------------------------- item code
_CODE_PATTERNS = [
    re.compile(r"(?:货号|款号|编号|item\s*(?:no|id|code)|ID)\s*[:：#]?\s*([A-Za-z]{0,3}\d{3,8})", re.I),
    re.compile(r"^[\[【(（]\s*([A-Za-z]{0,3}\d{3,6})\s*[\]】)）]"),
    re.compile(r"^#?\s*([A-Za-z]{0,2}\d{3,6})\b"),
    re.compile(r"#([A-Za-z]{0,2}\d{3,6})\b"),
]


def parse_item_code(title: str | None) -> str | None:
    if not title:
        return None
    t = title.strip()
    for pat in _CODE_PATTERNS:
        m = pat.search(t)
        if m:
            return m.group(1).upper()
    return None


# ------------------------------------------------------------------ materials
_PCT_BEFORE = r"(\d{{1,3}}(?:\.\d)?)\s*[%％]\s*({alias})"
_PCT_AFTER = r"({alias})\s*[:：]?\s*(\d{{1,3}}(?:\.\d)?)\s*[%％]"


def _strip_imitation_phrases(low: str, alias_re: str) -> str:
    """Remove phrases that *describe* a fibre without claiming it: 仿羊绒, 羊绒手感, cashmere-feel, faux wool…"""
    patterns = [
        rf"(仿|类|似|人造|假)\s*({alias_re})",
        rf"({alias_re})\s*(手感|感|质感|般|风|效果)",
        rf"(faux|imitation|mock|like|-like)\s+({alias_re})",
        rf"({alias_re})[\s-]*(feel|touch|like|look|effect|blend look)\b",
    ]
    for pat in patterns:
        low = re.sub(pat, " ", low)
    return low


def parse_materials(text: str | None) -> list[dict]:
    """Return [{"fibre": canonical, "pct": float|None}] in order of stated percentage."""
    if not text:
        return []
    low = text.lower()
    found: dict[str, float | None] = {}
    alias_to_fibre = [(a, f) for f, aliases in FIBRES.items() for a in aliases]
    alias_to_fibre.sort(key=lambda x: -len(x[0]))  # longest first
    alias_re = "|".join(re.escape(a) for a, _ in alias_to_fibre)
    low = _strip_imitation_phrases(low, alias_re)
    lookup = {a: f for a, f in alias_to_fibre}

    for m in re.finditer(_PCT_BEFORE.format(alias=alias_re), low):
        pct, alias = float(m.group(1)), m.group(2)
        found.setdefault(lookup[alias], pct)
    for m in re.finditer(_PCT_AFTER.format(alias=alias_re), low):
        alias, pct = m.group(1), float(m.group(2))
        found.setdefault(lookup[alias], pct)

    if not found:
        # Mentions without percentages ("100% cotton" handled above; "纯棉" = pure cotton)
        if "纯棉" in low or "全棉" in low:
            found["cotton"] = 100.0
        for alias, fibre in alias_to_fibre:
            if fibre in found:
                continue
            # "down" is too ambiguous in English (e.g. "button-down"), require CJK alias
            if fibre == "down" and not has_cjk(alias):
                continue
            if _alias_in(alias, low):
                # Skip generic "wool" when a more specific wool already matched
                if fibre == "wool" and any(k in found for k in ("merino wool", "virgin wool")):
                    continue
                if fibre == "cotton" and alias == "棉" and ("棉服" in low or "棉衣" in low):
                    continue
                found[fibre] = None

    # Drop implausible totals (e.g. sizes misread as percentages)
    total = sum(v for v in found.values() if v is not None)
    if total > 101:
        return [{"fibre": f, "pct": None} for f in found]
    return sorted(
        ({"fibre": f, "pct": p} for f, p in found.items()),
        key=lambda d: -(d["pct"] or 0),
    )


def materials_to_text(materials: list[dict]) -> str:
    parts = []
    for m in materials:
        parts.append(f"{m['pct']:g}% {m['fibre']}" if m.get("pct") is not None else m["fibre"])
    return ", ".join(parts)


_GSM = re.compile(r"(\d{3})\s*(?:g/m²|g/m2|gsm|GSM|克重|克|g)(?![a-z])")
_GSM2 = re.compile(r"(?:克重|weight)\s*[:：]?\s*(\d{3})")


def parse_gsm(text: str | None) -> int | None:
    if not text:
        return None
    for pat in (_GSM2, _GSM):
        m = pat.search(text)
        if m:
            v = int(m.group(1))
            if 100 <= v <= 900:
                return v
    return None


# ---------------------------------------------------------------------- sizes
def parse_sizes(text: str | None) -> list[str]:
    if not text:
        return []
    sizes: list[str] = []
    # Explicit size line
    m = re.search(r"(?:尺码|码数|size|sizes|Size|SIZE)\s*[:：]?\s*([A-Za-z0-9 /,，、\-~]+)", text)
    segment = m.group(1) if m else text
    rng = re.search(r"\b(XXS|XS|S|M|L|XL|XXL|2XL|3XL|4XL)\s*[-~至]\s*(XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|5XL)\b", segment)
    if rng:
        order = SIZE_TOKENS
        a, b = rng.group(1), rng.group(2)
        if a in order and b in order and order.index(a) <= order.index(b):
            return order[order.index(a) : order.index(b) + 1]
    for tok in re.findall(r"(?<![A-Za-z0-9])(XXS|XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|5XL)(?![A-Za-z0-9])", segment):
        if tok not in sizes:
            sizes.append(tok)
    if m and not sizes:
        for tok in re.findall(r"\b(4[4-9]|5[0-8])\b", segment):  # EU/IT tailoring sizes
            if tok not in sizes:
                sizes.append(tok)
    # Require at least two size tokens when scanning free text to avoid "S" false positives
    if not m and len(sizes) < 2:
        return []
    return sizes


# --------------------------------------------------------------------- colours
def parse_colours(text: str | None) -> list[str]:
    if not text:
        return []
    low = text.lower()
    out: list[str] = []
    # Check specific colours before generic ones (charcoal before grey, cream before white)
    for colour in [
        "charcoal",
        "cream",
        "taupe",
        "camel",
        "navy",
        "olive",
        "khaki",
        "burgundy",
        "black",
        "grey",
        "white",
        "beige",
        "brown",
        "blue",
        "green",
        "red",
        "pink",
        "yellow",
        "orange",
        "purple",
    ]:
        for alias in COLOURS[colour]:
            if _alias_in(alias, low):
                # avoid double counting "深灰" as grey too
                if colour == "grey" and "charcoal" in out and alias == "灰":
                    continue
                if colour == "white" and "cream" in out and alias in ("白",):
                    continue
                if colour == "blue" and "navy" in out:
                    continue
                if colour == "green" and "olive" in out:
                    continue
                if colour == "red" and "burgundy" in out:
                    continue
                out.append(colour)
                break
    return out


# -------------------------------------------------------------------- category
def parse_category(text: str | None) -> str | None:
    if not text:
        return None
    low = text.lower()
    for cat, aliases in CATEGORIES:
        for alias in aliases:
            if _alias_in(alias, low):
                # "zip" inside a knit hoodie listing should stay hoodie (handled by order)
                return cat
    return None


# ----------------------------------------------------------------------- brand
def parse_brand(text: str | None) -> str | None:
    if not text:
        return None
    low = text.lower()
    best: tuple[int, str] | None = None
    for brand, aliases in BRANDS.items():
        for alias in aliases:
            a = alias.lower().strip()
            idx = (
                low.find(a)
                if has_cjk(a)
                else (m.start() if (m := re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", low)) else -1)
            )
            if idx >= 0 and (best is None or idx < best[0]):
                best = (idx, brand)
    if best:
        return best[1]
    for abbr, brand in BRAND_ABBREVIATIONS.items():
        if re.search(rf"(?<![A-Za-z0-9]){abbr}(?![A-Za-z0-9])", text):
            return brand
    return None


# ------------------------------------------------------------ fit / logo / season
def parse_fit(text: str | None) -> list[str]:
    if not text:
        return []
    low = text.lower()
    return [tag for tag, aliases in FIT_TAGS.items() if any(_alias_in(a, low) for a in aliases)]


def parse_logo_level(text: str | None) -> str:
    if not text:
        return "unknown"
    low = text.lower()
    if any(_alias_in(a, low) for a in LOGO_VISIBLE):
        return "visible"
    if any(_alias_in(a, low) for a in LOGO_NONE):
        return "none"
    if any(_alias_in(a, low) for a in LOGO_SUBTLE):
        return "subtle"
    return "unknown"


WARM_FIBRES = {"cashmere", "wool", "merino wool", "virgin wool", "alpaca", "mohair", "camel hair", "down"}
WARM_CATEGORIES = {"knitwear", "cardigan", "overcoat", "down jacket", "hoodie", "sweatshirt", "gilet"}
LIGHT_FIBRES = {"linen", "silk", "lyocell"}


def infer_seasons(category: str | None, materials: list[dict], text: str | None = None) -> list[str]:
    fibres = {m["fibre"] for m in materials}
    low = (text or "").lower()
    if "秋冬" in low or "autumn" in low or "winter" in low or "aw2" in low or "fw2" in low:
        return ["autumn", "winter"]
    if "春夏" in low or "spring" in low or "summer" in low or "ss2" in low:
        return ["spring", "summer"]
    if fibres & WARM_FIBRES or category in {"overcoat", "down jacket"}:
        return ["autumn", "winter"] if category not in {"t-shirt", "shorts", "polo"} else ["spring", "autumn"]
    if fibres & LIGHT_FIBRES or category == "shorts":
        return ["spring", "summer"]
    if category in WARM_CATEGORIES:
        return ["autumn", "winter", "spring"]
    if category in {"t-shirt", "polo", "shirt", "trousers", "jeans"}:
        return ["spring", "summer", "autumn", "winter"]
    return []


# --------------------------------------------------------------- links in text
_URL = re.compile(r"https?://[^\s\"'<>，。）)]+")


_MAX_UNWRAP = 3
_MAX_URL = 2048


def canonical_url(url: str | None) -> str | None:
    """Resolve redirect wrappers (e.g. a catalogue's ``/external?url=<percent-encoded target>``) to the real
    destination. Bounded: at most three nested wrappers and 2,048 characters; only http(s) targets."""
    if not url:
        return None
    u = url.strip()[:_MAX_URL]
    for _ in range(_MAX_UNWRAP):
        target = None
        for _k, v in parse_qsl(urlparse(u).query, keep_blank_values=False):
            cand = v
            for _ in range(_MAX_UNWRAP):  # tolerate double-encoding
                if cand.lower().startswith(("http://", "https://")):
                    break
                cand = unquote(cand)
            if cand.lower().startswith(("http://", "https://")):
                target = cand[:_MAX_URL]
                break
        if target is None:
            break
        u = target
    p = urlparse(u)
    return u if p.scheme in ("http", "https") and p.hostname else None


def _host_is(url: str, *domains: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_link(url: str) -> str | None:
    """Which purchase/catalogue site a (canonical) link points to - by its host, not by substring."""
    if _host_is(url, "weidian.com"):
        return "weidian"
    if _host_is(url, "taobao.com", "tmall.com"):
        return "taobao"
    if _host_is(url, "yupoo.com"):
        return "yupoo"
    return None


def extract_purchase_links(text: str | None) -> dict[str, str]:
    links: dict[str, str] = {}
    for url in _URL.findall(text or ""):
        u = canonical_url(url.rstrip(".,;"))
        kind = classify_link(u) if u else None
        if kind and kind not in links:
            links[kind] = u
    return links


def weidian_item_id(url: str | None) -> str | None:
    if not url:
        return None
    u = canonical_url(url) or url
    for _ in range(_MAX_UNWRAP):  # an encoded query (itemID%3D123) still identifies the item
        m = re.search(r"itemI[Dd]=(\d+)", u) or re.search(r"/item/(\d+)", u)
        if m:
            return m.group(1)
        decoded = unquote(u)
        if decoded == u:
            break
        u = decoded
    return None


@dataclass
class ParsedAttributes:
    price: float | None = None
    item_code: str | None = None
    brand: str | None = None
    category: str | None = None
    colours: list[str] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)
    gsm: int | None = None
    sizes: list[str] = field(default_factory=list)
    fit_tags: list[str] = field(default_factory=list)
    logo_level: str = "unknown"
    seasons: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)


def parse_listing_text(
    title: str, description: str | None = None, title_en: str | None = None, description_en: str | None = None
) -> ParsedAttributes:
    """Parse all attributes from original + translated title/description."""
    full = "\n".join(x for x in [title, description, title_en, description_en] if x)
    title_both = " ".join(x for x in [title, title_en] if x)
    materials = parse_materials(full)
    category = parse_category(title_both) or parse_category(full)
    return ParsedAttributes(
        price=parse_price(title) or parse_price(description),
        item_code=parse_item_code(title),
        brand=parse_brand(title_both) or parse_brand(full),
        category=category,
        colours=parse_colours(full),
        materials=materials,
        gsm=parse_gsm(full),
        sizes=parse_sizes(description or "") or parse_sizes(title),
        fit_tags=parse_fit(full),
        logo_level=parse_logo_level(full),
        seasons=infer_seasons(category, materials, full),
        links=extract_purchase_links(full),
    )
