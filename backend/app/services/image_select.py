"""Cheap image analysis: size-chart / text-graphic detection and informative-photo selection.

Everything here runs on a small analysis copy (long side ``ANALYSIS_SIDE`` px) and costs a few
milliseconds per image, so it can run on every gallery image a scan downloads.

Size charts and other text graphics (shop notices, shipping cards) are recognised from structure,
not content: a dominant flat background (white, beige or dark), "ink" that is almost entirely thin
strokes, and either ruled table lines or many small text-like marks - with no large solid region.
A garment photo, even a white T-shirt on a white background, contains a large solid region, so incidental
measurement arrows or a watermark on a garment photo do not make it a chart. When the evidence is mixed
the image is labelled ``uncertain`` and is still used (conservative fallback).

Selection picks a small, diverse set for the product's aesthetic vector: the best whole-garment view
first, then views that add information (close-ups of fabric/construction, other angles), skipping
near-duplicates. Size charts stay attached to the product for the sizing section but never enter the
aesthetic vector or photo-based duplicate matching.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

ANALYSIS_SIDE = 320
FRAMED_BORDER = 0.55  # share of the image border that must be flat for a chart / text graphic

# Roles
GARMENT = "garment"  # whole garment / worn view
DETAIL = "detail"  # close-up: fabric, knit, stitching, hardware
UNCERTAIN = "uncertain"  # mixed evidence: kept (conservative)
SIZE_CHART = "size_chart"
TEXT_GRAPHIC = "text_graphic"  # shop notices, shipping cards, text-only graphics
REPEATED = "repeated"  # the same graphic reused across many of a seller's listings (banner, chart, notice)
AESTHETIC_ROLES = {GARMENT, DETAIL, UNCERTAIN}
NON_AESTHETIC_ROLES = {SIZE_CHART, TEXT_GRAPHIC, REPEATED}


@dataclass
class Features:
    bg_frac: float  # share of the analysed region close to the dominant flat colour
    flat_frac: float  # share of the analysed region that is *exactly* flat (paper, not floor or fabric)
    bg_lum: float
    ink_frac: float  # share of pixels contrasting strongly with the background
    thin_frac: float  # share of ink that disappears under erosion (strokes, text, lines)
    h_lines: int  # ruled horizontal lines spanning >= 45% of the region width
    v_lines: int
    small_marks: int  # text-sized connected components of ink
    row_align: float  # how strongly those marks line up in rows (text) rather than scatter (texture)
    blob_frac: float  # largest solid, non-elongated, non-background region / region area
    rect_fill: float  # how rectangular the background region is (paper ~1; a garment shape much less)
    border_flat: float  # share of the image border that is flat (margins / plain surround vs carpet, sheets)
    sharpness: float  # normalised Laplacian energy
    texture: float  # mean gradient magnitude (close-ups of knit/fabric score high)
    aspect: float

    def as_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(self).items()}


def _small(img: Image.Image) -> Image.Image:
    im = img.convert("RGB")
    if max(im.size) > ANALYSIS_SIDE:
        im = im.copy()
        im.thumbnail((ANALYSIS_SIDE, ANALYSIS_SIDE), Image.BILINEAR)
    return im


def _line_bands(mask: np.ndarray, min_frac: float) -> int:
    """Number of distinct bands of rows whose longest ink run spans >= min_frac of the width."""
    h, w = mask.shape
    if not mask.any():
        return 0
    idx = np.broadcast_to(np.arange(1, w + 1, dtype=np.int32), (h, w))
    last_gap = np.maximum.accumulate(np.where(mask, 0, idx), axis=1)
    longest = (idx - last_gap).max(axis=1)
    is_line = longest >= min_frac * w
    return int(np.count_nonzero(is_line[1:] & ~is_line[:-1]) + int(is_line[0]))


def analyse(img: Image.Image) -> Features:
    im = _small(img)
    rgb = np.asarray(im, dtype=np.float32) / 255.0
    H, W, _ = rgb.shape
    lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    # Dominant flat colour: mode of a coarse colour quantisation.
    q = (rgb * 7.999).astype(np.int32)
    codes = q[..., 0] * 64 + q[..., 1] * 8 + q[..., 2]
    mode = int(np.bincount(codes.ravel(), minlength=512).argmax())
    sel_mode = codes == mode
    bg_rgb = rgb[sel_mode].mean(axis=0)
    dist = np.abs(rgb - bg_rgb).max(axis=2)
    bg_full = dist < 0.12
    b = max(2, round(0.03 * min(H, W)))
    border = np.concatenate([lum[:b].ravel(), lum[-b:].ravel(), lum[:, :b].ravel(), lum[:, -b:].ravel()])
    border_flat = float((np.abs(border - np.median(border)) < 0.04).mean())
    gy, gx = np.gradient(lum)
    texture = float(np.hypot(gx, gy).mean())
    sharpness = float(min(1.0, ndimage.laplace(lum).var() * 400))
    if border_flat < FRAMED_BORDER:
        # Fast path (most photos): a chart or text graphic needs a flat frame, so the document
        # measurements cannot change the outcome. Only the photo-quality features are needed.
        bg_frac = float(bg_full.mean())
        return Features(
            bg_frac, 0.0, float(lum[bg_full].mean()) if bg_full.any() else float(lum.mean()), 0.0, 0.0, 0, 0, 0,
            0.0, 1 - bg_frac, 0.0, border_flat, sharpness, texture, W / H,
        )  # fmt: skip

    # Analyse inside the largest background region (the paper of a photographed chart; usually the frame).
    y0, y1, x0, x1 = 0, H, 0, W
    rect_fill = 0.0
    lab, n = ndimage.label(bg_full)
    if n:
        sizes = np.bincount(lab.ravel())[1:]
        k = int(sizes.argmax()) + 1
        sl = ndimage.find_objects(lab)[k - 1]
        box = (sl[0].stop - sl[0].start) * (sl[1].stop - sl[1].start)
        rect_fill = float(ndimage.binary_fill_holes(lab[sl] == k).sum() / max(1, box))
        if box >= 0.3 * H * W:
            y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
    lum_r, dist_r = lum[y0:y1, x0:x1], dist[y0:y1, x0:x1]
    h, w = lum_r.shape
    area = float(h * w)
    bg = dist_r < 0.12
    bg_frac = float(bg.mean())
    bg_lum = float(lum_r[bg].mean()) if bg.any() else float(lum_r.mean())
    flat_frac = float((np.abs(lum_r - bg_lum) < 0.035).mean())

    ink = np.abs(lum_r - bg_lum) > 0.3
    ink_frac = float(ink.mean())
    if ink.any():
        thin_frac = float(1 - ndimage.binary_erosion(ink, iterations=1).sum() / ink.sum())
    else:
        thin_frac = 0.0
    h_lines = _line_bands(ink, 0.45)
    v_lines = _line_bands(ink.T, 0.45)

    labels, n = ndimage.label(ink)
    small_marks, row_align = 0, 0.0
    if n:
        sizes = np.bincount(labels.ravel())[1:]
        rows = np.bincount(labels.ravel(), weights=np.repeat(np.arange(h), w))[1:] / np.maximum(sizes, 1)
        small = (sizes >= 3) & (sizes <= 0.004 * area)
        small_marks = int(small.sum())
        if small_marks >= 8:
            bins = np.bincount((rows[small] / max(1, h) * 60).astype(int), minlength=61)
            top = np.sort(bins)[::-1][:15]  # a quarter of the 60 row bins
            row_align = float(top.sum() / small_marks)

    # Largest solid region that is not background and not a thin band (header rows, rules).
    fg = ndimage.binary_opening(~bg, iterations=2)
    blob_frac = 0.0
    if fg.any():
        fl, fn = ndimage.label(fg)
        fsizes = np.bincount(fl.ravel())
        for i, s in enumerate(ndimage.find_objects(fl), start=1):
            if s is None or fsizes[i] <= blob_frac * area:
                continue
            bh, bw = s[0].stop - s[0].start, s[1].stop - s[1].start
            if bh >= 0.2 * h and bw >= 0.2 * w:
                blob_frac = fsizes[i] / area

    return Features(
        bg_frac, flat_frac, bg_lum, ink_frac, thin_frac, h_lines, v_lines, small_marks, row_align, float(blob_frac),
        rect_fill, border_flat, sharpness, texture, W / H,
    )  # fmt: skip


def classify(f: Features) -> tuple[str, float]:
    """Return (role, confidence in [0,1])."""
    # A document: a rectangular sheet of flat background (paper fills its bounding box; a garment that happens
    # to be the dominant colour does not) and a flat image border (margins or plain surround, not carpet).
    framed = f.rect_fill >= 0.8 and f.border_flat >= FRAMED_BORDER
    paper = f.flat_frac >= 0.35 or (f.bg_frac >= 0.6 and f.rect_fill >= 0.85)  # blurred photos of charts
    document = framed and paper and f.bg_frac >= 0.45 and (f.bg_lum >= 0.72 or f.bg_lum <= 0.28)
    # Mostly thin strokes; a text-dense sheet may carry thick shadow/edge ink (photographed charts).
    strokes = f.ink_frac >= 0.005 and (f.thin_frac >= 0.55 or (f.thin_frac >= 0.35 and f.small_marks >= 80))
    ruled = f.h_lines >= 3 and f.v_lines >= 3
    # Text: marks lined up in rows; a slightly rotated photo of a chart smears rows, so dense marks count too.
    texty = (f.small_marks >= 20 and f.row_align >= 0.7) or (f.small_marks >= 80 and f.row_align >= 0.4)
    diagram = f.ink_frac < 0.08 and f.thin_frac >= 0.8 and f.small_marks >= 6 and f.flat_frac >= 0.75
    # A ruled table is unambiguous even when a filled header band makes some ink thick.
    ruled_table = ruled and f.thin_frac >= 0.3 and (texty or f.small_marks >= 30)
    if document and f.blob_frac < 0.1 and (ruled_table or (strokes and (ruled or texty or diagram))):
        conf = 0.6 + 0.1 * ruled + 0.1 * texty + 0.2 * min(1.0, (f.flat_frac - 0.35) / 0.5)
        role = SIZE_CHART if (ruled or diagram or f.h_lines >= 2) else TEXT_GRAPHIC
        return role, round(min(conf, 1.0), 2)
    if document and strokes and (ruled or texty) and f.blob_frac < 0.25:
        return UNCERTAIN, 0.4  # graphic-like but with a solid region: keep, with low priority
    if f.bg_frac < 0.25 and f.texture > 0.035:
        return DETAIL, 0.6
    return GARMENT, 0.7


def quality(f: Features, position: int) -> float:
    """Usefulness of a photo as a garment view (0..1). Position prior: sellers put the hero shot first."""
    coverage = min(1.0, f.blob_frac / 0.35) if f.bg_frac > 0.25 else 0.8
    text_penalty = min(0.3, f.small_marks / 200)
    pos = 0.15 if position == 0 else 0.08 * max(0.0, 1 - position / 8)
    return 0.4 * f.sharpness + 0.35 * coverage + pos - text_penalty + 0.1


@dataclass
class Candidate:
    index: int  # position in the gallery
    role: str
    quality: float
    vector: np.ndarray


def select(cands: list[Candidate], max_k: int = 4, redundant_cos: float = 0.97, min_gain: float = 0.02) -> list[int]:
    """Greedy diverse selection (maximal marginal relevance). Returns gallery indices in priority order."""
    pool = [c for c in cands if c.role in AESTHETIC_ROLES]
    if not pool:
        return []
    # Hero: best whole-garment view (close-ups only if nothing else exists).
    wholes = [c for c in pool if c.role == GARMENT] or pool
    chosen = [max(wholes, key=lambda c: c.quality)]
    while len(chosen) < max_k:
        best, best_gain = None, -1.0
        for c in pool:
            if c in chosen:
                continue
            sim = max(float(c.vector @ s.vector) for s in chosen)
            if sim >= redundant_cos:
                continue  # near-duplicate of a selected view
            gain = c.quality * (1 - sim) + (0.08 if c.role == DETAIL and all(s.role != DETAIL for s in chosen) else 0)
            if gain > best_gain:
                best, best_gain = c, gain
        if best is None or best_gain < min_gain:
            break
        chosen.append(best)
    return [c.index for c in chosen]


def product_vector(vectors: list[np.ndarray]) -> np.ndarray:
    """Aesthetic product vector from selected views: hero weighted double (as before)."""
    stack = np.vstack(vectors)
    weights = np.ones(len(vectors), dtype=np.float32)
    weights[0] = 2.0
    v = (stack * weights[:, None]).sum(0)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v
