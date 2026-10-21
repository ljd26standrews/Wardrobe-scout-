"""Size-chart / text-graphic detection and informative-photo selection (services.image_select).

Negatives are REAL garment photos (tests/fixtures/photos, CC0); positives are rendered size charts
(bench.render). The full-scale evaluation (3,781 real photos) is bench/eval_detector.py.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import image_select as sel
from app.services.embeddings import LiteImageBackend
from bench import render

PHOTOS = sorted((Path(__file__).parent / "fixtures" / "photos").glob("*.jpg"))


def _as_scanned(img: Image.Image) -> Image.Image:
    """The 640px JPEG variant a scan downloads."""
    im = render._fit(img, 640)
    return Image.open(io.BytesIO(render.jpeg(im, 80))).convert("RGB")


def _role(img: Image.Image) -> str:
    return sel.classify(sel.analyse(img))[0]


@pytest.mark.parametrize("path", PHOTOS, ids=lambda p: p.name)
def test_real_garment_photos_are_never_excluded(path):
    role = _role(Image.open(path).convert("RGB"))
    assert role in sel.AESTHETIC_ROLES, f"{path.name} classified {role}"


@pytest.mark.parametrize("kind", ["size_table_cn", "size_table_en", "size_dark", "size_diagram", "text_card"])
def test_rendered_size_charts_and_notices_are_excluded(kind):
    roles = [
        _role(_as_scanned(render.render({"kind": kind, "photos": [], "seed": s, "seller": "店铺"}))) for s in range(8)
    ]
    assert sum(r in sel.NON_AESTHETIC_ROLES for r in roles) >= 7, roles


def test_photographed_size_charts_mostly_excluded():
    roles = [
        _role(_as_scanned(render.render({"kind": "size_photo", "photos": [], "seed": s, "seller": "x"})))
        for s in range(12)
    ]
    assert sum(r in sel.NON_AESTHETIC_ROLES for r in roles) >= 7, roles  # hardest case; ~83% at scale


@pytest.mark.parametrize("kind", ["measure_overlay", "watermark", "collage", "detail"])
def test_garment_photos_with_text_or_arrows_are_kept(kind):
    photos = [str(p) for p in PHOTOS[7:10]]
    for seed in range(4):
        img = _as_scanned(render.render({"kind": kind, "photos": photos, "seed": seed, "seller": "店铺"}))
        assert _role(img) in sel.AESTHETIC_ROLES, (kind, seed)


def _cands(images: list[Image.Image], roles: list[str] | None = None) -> list[sel.Candidate]:
    b = LiteImageBackend()
    out = []
    for i, im in enumerate(images):
        f = sel.analyse(im)
        role = roles[i] if roles else sel.classify(f)[0]
        out.append(sel.Candidate(i, role, sel.quality(f, i), b.embed(im)))
    return out


def test_selection_skips_size_charts_and_near_duplicates():
    hero = Image.open(PHOTOS[12]).convert("RGB")
    dup = render.garment("neardup", [str(PHOTOS[12])], __import__("random").Random(1), "x")
    chart = _as_scanned(render.render({"kind": "size_table_cn", "photos": [], "seed": 1, "seller": "x"}))
    other = Image.open(PHOTOS[13]).convert("RGB")
    chosen = sel.select(_cands([chart, hero, dup, other]), max_k=4)
    assert 0 not in chosen, "size chart selected"
    assert chosen[0] == 1, "the first whole-garment photo should lead"
    assert not {1, 2} <= set(chosen), "near-duplicate selected twice"


def test_selection_respects_max_k_and_falls_back_conservatively():
    imgs = [Image.open(p).convert("RGB") for p in PHOTOS[:5]]
    assert len(sel.select(_cands(imgs), max_k=2)) <= 2
    # Uncertain images are still usable when nothing better exists.
    assert sel.select(_cands(imgs[:1], roles=[sel.UNCERTAIN]), max_k=1) == [0]
    # Only non-garment images: no aesthetic vector at all.
    assert sel.select(_cands(imgs[:2], roles=[sel.SIZE_CHART, sel.REPEATED]), max_k=3) == []


def test_product_vector_is_normalised_and_hero_weighted():
    a, b = np.array([1.0, 0, 0], dtype=np.float32), np.array([0, 1.0, 0], dtype=np.float32)
    v = sel.product_vector([a, b])
    assert abs(float(np.linalg.norm(v)) - 1) < 1e-6 and v[0] > v[1]
