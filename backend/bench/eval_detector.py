"""Evaluate size-chart / text-graphic detection.

Negatives: every real photograph in the CC0 clothing dataset (3,781 photos, many on cluttered real
backgrounds) plus rendered garment variants (watermarks, measurement arrows, collages, close-ups).
Positives: rendered Chinese/English size tables, measurement diagrams, photographed charts, inverted
charts and shop notice cards. All images go through the same 640px "medium" variant as a scan.

Usage: .venv/bin/python -m bench.eval_detector [--n-rendered 1500] [--dump DIR]
"""

from __future__ import annotations

import argparse
import io
import random
import time
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

from app.services import image_select as sel

from . import catalogue, render

NON_AESTHETIC = sel.NON_AESTHETIC_ROLES


def _medium(img: Image.Image) -> Image.Image:
    im = render._fit(img, render.VARIANT_LONG_SIDE["medium"])
    return Image.open(io.BytesIO(render.jpeg(im, 80))).convert("RGB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-rendered", type=int, default=1500)
    ap.add_argument("--dump", help="write misclassified images here")
    args = ap.parse_args()
    rng = random.Random(3)
    pool = catalogue.photo_pool()
    conf: dict[str, Counter] = defaultdict(Counter)
    times = []
    errors: list[tuple[str, str, Image.Image, dict]] = []

    def run(truth: str, img: Image.Image, name: str) -> None:
        t0 = time.perf_counter()
        f = sel.analyse(img)
        role, _ = sel.classify(f)
        times.append(time.perf_counter() - t0)
        conf[truth][role] += 1
        wrong = (truth.startswith("garment") and role in NON_AESTHETIC) or (
            not truth.startswith("garment") and role not in NON_AESTHETIC
        )
        if wrong:
            errors.append((truth, name, img, f.as_dict() | {"role": role}))

    for path, _cls in pool:
        run("garment:real_photo", _medium(Image.open(path).convert("RGB")), Path(path).name)
    photos = [p for p, _ in pool]
    garment_kinds = ["detail", "watermark", "measure_overlay", "collage", "colourway", "alt"]
    other_kinds = sorted(render.SIZE_KINDS) + ["text_card"]
    for i in range(args.n_rendered):
        kind = garment_kinds[i % len(garment_kinds)]
        rec = {"kind": kind, "photos": rng.sample(photos, 3), "seed": i, "seller": "店铺"}
        run(f"garment:{kind}", _medium(render.render(rec)), f"{kind}_{i}")
    for i in range(args.n_rendered):
        kind = other_kinds[i % len(other_kinds)]
        rec = {"kind": kind, "photos": [], "seed": 10_000 + i, "seller": "店铺"}
        run(f"{render.label_of(kind)}:{kind}", _medium(render.render(rec)), f"{kind}_{i}")

    print(f"{'truth':<32}{'n':>6}  roles")
    for truth in sorted(conf):
        n = sum(conf[truth].values())
        print(f"{truth:<32}{n:>6}  " + ", ".join(f"{r}={c}" for r, c in conf[truth].most_common()))
    g = [t for t in conf if t.startswith("garment")]
    o = [t for t in conf if not t.startswith("garment")]
    g_n = sum(sum(conf[t].values()) for t in g)
    g_fp = sum(conf[t][r] for t in g for r in NON_AESTHETIC)
    o_n = sum(sum(conf[t].values()) for t in o)
    o_tp = sum(conf[t][r] for t in o for r in NON_AESTHETIC)
    real_n = sum(conf["garment:real_photo"].values())
    real_fp = sum(conf["garment:real_photo"][r] for r in NON_AESTHETIC)
    print(
        f"\ngarment photos wrongly excluded: {g_fp}/{g_n} ({100 * g_fp / g_n:.2f}%), of which real photos {real_fp}/{real_n}"
    )
    print(f"size charts / text graphics excluded: {o_tp}/{o_n} ({100 * o_tp / o_n:.1f}%)")
    print(f"analysis time per image: median {1000 * sorted(times)[len(times) // 2]:.1f} ms")
    if args.dump:
        d = Path(args.dump)
        d.mkdir(parents=True, exist_ok=True)
        for truth, name, img, f in errors[:60]:
            img.save(d / f"{truth.replace(':', '_')}__{name}.jpg")
            (d / f"{truth.replace(':', '_')}__{name}.txt").write_text(str(f))


if __name__ == "__main__":
    main()
