"""Deterministic benchmark catalogues with ground truth.

Every image carries a ground-truth kind (bench.render) so selection, size-chart exclusion and
deduplication can be scored. Products carry the dataset class of their photo, which drives the
simulated shoppers in bench.quality.

Adversarial content on purpose: size charts in the first position, white-background garments,
watermarks, incidental measurement arrows on garment photos, collages, colourway siblings (must NOT be
merged) and cross-seller reposts of the same photos (SHOULD be matched).
"""

from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from pathlib import Path

DEFAULT_PHOTOS = Path(
    os.environ.get(
        "WS_BENCH_PHOTOS",
        str(Path(__file__).resolve().parent / "data" / "clothing-dataset-small"),
    )
)
CN_CLASS = {
    "dress": "连衣裙", "hat": "帽子", "longsleeve": "长袖T恤", "outwear": "外套", "pants": "长裤",
    "shirt": "衬衫", "shoes": "鞋", "shorts": "短裤", "skirt": "半身裙", "t-shirt": "短袖T恤",
}  # fmt: skip


def photo_pool(root: Path = DEFAULT_PHOTOS) -> list[tuple[str, str]]:
    """[(path, class)] sorted for determinism."""
    if not root.exists():
        raise SystemExit(f"benchmark photos not found at {root} - see bench/README.md")
    out = []
    for split in ("train", "validation", "test"):
        for cls_dir in sorted((root / split).glob("*")):
            out += [(str(p), cls_dir.name) for p in sorted(cls_dir.glob("*.jpg"))]
    return out


def build(n_products: int, n_sellers: int = 3, seed: int = 7, *, opaque_titles: bool = True) -> dict:
    """Return a JSON-serialisable catalogue spec."""
    rng = random.Random(seed)
    pool = photo_pool()
    rng.shuffle(pool)
    sellers = [f"bench{i + 1}" for i in range(n_sellers)]
    watermarking = {s: rng.random() < 0.5 for s in sellers}
    albums: dict[str, list[dict]] = defaultdict(list)
    images: dict[str, dict] = {}  # key -> recipe (+ label)
    next_album = {s: 100000 + 1000 * i for i, s in enumerate(sellers)}
    boiler_key = {s: f"{s}-boiler" for s in sellers}
    for s in sellers:
        images[boiler_key[s]] = {"kind": "boilerplate", "photos": [], "seed": hash(s) % 10**6, "seller": s}

    def new_key(seller: str) -> str:
        return f"{seller}-{len(images):06d}{rng.randrange(16**4):04x}"

    def add_album(seller: str, cls: str, photos: list[str], gallery: list[dict], **extra) -> dict:
        next_album[seller] += 1
        price = rng.randint(15, 160) * 10
        code = rng.randint(1000, 9999)
        title = f"【{code}】新款 ¥{price}" if opaque_titles else f"【{code}】{CN_CLASS[cls]} ¥{price}"
        a = {"id": str(next_album[seller]), "seller": seller, "cls": cls, "photos": photos, "title": title,
             "price": price, "images": gallery, **extra}  # fmt: skip
        albums[seller].append(a)
        return a

    def garment_gallery(seller: str, photos: list[str], colourway: bool = False) -> list[dict]:
        kinds = ["hero"]
        kinds += ["alt"] * rng.choice([0, 1, 2, 3])
        kinds += ["detail"] * rng.choice([0, 1, 2, 3, 4])
        kinds += ["neardup"] * rng.choice([0, 1, 2, 3, 4, 5])
        if rng.random() < 0.1:
            kinds.append("measure_overlay")
        if rng.random() < 0.1:
            kinds.append("collage")
        if watermarking[seller]:
            kinds = [k if (k != "alt" or rng.random() < 0.5) else "watermark" for k in kinds]
        head, tail = kinds[:1], kinds[1:]
        rng.shuffle(tail)
        out = []
        for k in head + tail:
            key = new_key(seller)
            kind = "colourway" if colourway and k in ("hero", "alt", "neardup") else k
            images[key] = {"kind": kind, "photos": photos, "seed": rng.randrange(10**9), "seller": seller}
            out.append({"key": key, "kind": kind})
        return out

    def add_non_garment(seller: str, gallery: list[dict]) -> list[dict]:
        n_size = rng.choices([0, 1, 2, 3], weights=[25, 35, 25, 15])[0]
        for _ in range(n_size):
            kind = rng.choice(
                ["size_table_cn", "size_table_cn", "size_table_en", "size_diagram", "size_photo", "size_dark"]
            )
            key = new_key(seller)
            images[key] = {"kind": kind, "photos": [], "seed": rng.randrange(10**9), "seller": seller}
            item = {"key": key, "kind": kind}
            r = rng.random()
            if r < 0.10:
                gallery.insert(0, item)  # adversarial: size chart as the cover image
            elif r < 0.25:
                gallery.insert(rng.randint(1, max(1, len(gallery))), item)
            else:
                gallery.append(item)
        if rng.random() < 0.2:
            key = new_key(seller)
            images[key] = {"kind": "text_card", "photos": [], "seed": rng.randrange(10**9), "seller": seller}
            gallery.append({"key": key, "kind": "text_card"})
        if rng.random() < 0.4:
            # Half the time the same URL, half re-uploaded under a new URL (same content).
            if rng.random() < 0.5:
                gallery.append({"key": boiler_key[seller], "kind": "boilerplate"})
            else:
                key = new_key(seller)
                images[key] = dict(images[boiler_key[seller]])
                gallery.append({"key": key, "kind": "boilerplate"})
        return gallery[:16]

    pi = 0
    made = 0
    while made < n_products:
        seller = sellers[made % n_sellers]
        path, cls = pool[pi % len(pool)]
        extra = [pool[(pi + 1) % len(pool)][0], pool[(pi + 2) % len(pool)][0]]
        pi += 3
        photos = [path] + extra
        a = add_album(seller, cls, photos, add_non_garment(seller, garment_gallery(seller, photos)))
        made += 1
        if made < n_products and rng.random() < 0.08:
            add_album(seller, cls, photos, add_non_garment(seller, garment_gallery(seller, photos, colourway=True)),
                      colourway_of=(seller, a["id"]))  # fmt: skip
            made += 1
        if made < n_products and n_sellers > 1 and rng.random() < 0.10:
            other = rng.choice([s for s in sellers if s != seller])
            g = [dict(x) for x in a["images"] if x["kind"] in ("hero", "alt", "detail", "neardup")]
            gallery = []
            for x in g:
                key = new_key(other)
                rec = dict(images[x["key"]], seller=other)
                if rec["kind"] == "hero" and rng.random() < 0.5:
                    rec = dict(rec, kind="neardup", seed=rng.randrange(10**9))
                images[key] = rec
                gallery.append({"key": key, "kind": rec["kind"]})
            rng.shuffle(gallery)
            add_album(other, cls, photos, add_non_garment(other, gallery), repost_of=(seller, a["id"]))
            made += 1
    # Newest first, like a catalogue listing
    for s in sellers:
        albums[s].sort(key=lambda a: -int(a["id"]))
    return {"seed": seed, "sellers": sellers, "albums": dict(albums), "images": images}


def save(spec: dict, path: Path) -> None:
    path.write_text(json.dumps(spec))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def stats(spec: dict) -> dict:
    from .render import label_of

    n_img = sum(len(a["images"]) for s in spec["albums"].values() for a in s)
    labels = defaultdict(int)
    for s in spec["albums"].values():
        for a in s:
            for im in a["images"]:
                labels[label_of(im["kind"])] += 1
    n = sum(len(s) for s in spec["albums"].values())
    return {"products": n, "images": n_img, "images_per_product": round(n_img / max(1, n), 2), **labels}


def mutate(
    spec: dict, seed: int = 8, new_frac: float = 0.06, price_frac: float = 0.05, removed_frac: float = 0.03
) -> dict:
    """A later state of the same catalogue: new albums (30% inserted mid-catalogue, i.e. not chronological),
    changed prices, removed albums and one unchanged album bumped to the top."""
    import copy
    import re

    rng = random.Random(seed)
    n = sum(len(v) for v in spec["albums"].values())
    bigger = build(int(n * (1 + new_frac)) + 3, len(spec["sellers"]), spec["seed"])
    out = copy.deepcopy(spec)
    have = {(s, a["id"]) for s, al in spec["albums"].items() for a in al}
    added = removed = repriced = 0
    for s, al in bigger["albums"].items():
        fresh = [a for a in al if (s, a["id"]) not in have]
        for a in fresh:
            for im in a["images"]:
                out["images"][im["key"]] = bigger["images"][im["key"]]
            pos = 0 if rng.random() < 0.7 else rng.randint(1, len(out["albums"][s]))
            out["albums"][s].insert(pos, a)
            added += 1
    for s, al in out["albums"].items():
        keep = []
        for a in al:
            if (s, a["id"]) in have and rng.random() < removed_frac:
                removed += 1
                continue
            if (s, a["id"]) in have and rng.random() < price_frac:
                a["price"] = a["price"] + 50
                a["title"] = re.sub(r"¥\d+", f"¥{a['price']}", a["title"])
                repriced += 1
            keep.append(a)
        if len(keep) > 10:
            keep.insert(0, keep.pop(len(keep) // 2))  # bump an old album to the top, unchanged
        out["albums"][s] = keep
    out["mutation"] = {"added": added, "removed": removed, "repriced": repriced}
    return out
