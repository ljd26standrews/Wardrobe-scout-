"""Image and text embeddings.

Image backends
--------------
* ``clip``  - OpenCLIP (default ViT-B-32 / laion2b). Best quality. Requires
  ``pip install -r requirements-ml.txt`` and a one-off ~600 MB weight download.
* ``lite``  - deterministic, dependency-free features (colour histogram in
  HSV + gradient-orientation histogram + coarse layout). Captures colour,
  tone and rough silhouette/texture. Used automatically when CLIP is not
  installed so the app always works offline.

``WS_EMBEDDING_BACKEND=auto`` picks CLIP when available.

Text embeddings use a hashing vectoriser over word uni/bi-grams of the
English text and structured attributes - deterministic, no model download.
"""

from __future__ import annotations

import threading
from functools import lru_cache

import numpy as np
from PIL import Image

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)


def to_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_bytes(b: bytes | None) -> np.ndarray | None:
    if not b:
        return None
    return np.frombuffer(b, dtype=np.float32)


def normalise(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class LiteImageBackend:
    name = "lite-v1"
    dim = 8 * 4 * 4 + 9 * 16 + 48

    def embed(self, img: Image.Image) -> np.ndarray:
        im = img.convert("RGB")
        im.thumbnail((256, 256))
        # Centre crop to focus on the garment rather than background margins
        w, h = im.size
        cw, ch = int(w * 0.8), int(h * 0.8)
        im = im.crop(((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2)).resize((96, 96))
        hsv = np.asarray(im.convert("HSV"), dtype=np.float32) / 255.0
        hist, _ = np.histogramdd(hsv.reshape(-1, 3), bins=(8, 4, 4), range=((0, 1), (0, 1), (0, 1)))
        colour = hist.flatten() / hist.sum()
        colour = np.sqrt(colour)  # Hellinger mapping

        gray = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
        gy, gx = np.gradient(gray)
        mag = np.hypot(gx, gy)
        ang = (np.arctan2(gy, gx) % np.pi) / np.pi
        hog = []
        cells = 4
        cs = gray.shape[0] // cells
        for i in range(cells):
            for j in range(cells):
                a = ang[i * cs : (i + 1) * cs, j * cs : (j + 1) * cs].ravel()
                m = mag[i * cs : (i + 1) * cs, j * cs : (j + 1) * cs].ravel()
                hh, _ = np.histogram(a, bins=9, range=(0, 1), weights=m)
                hog.append(hh / (hh.sum() + 1e-6))
        hog_v = np.concatenate(hog) * 0.6

        small = np.asarray(im.convert("L").resize((4, 4)), dtype=np.float32).ravel() / 255.0
        lab_small = np.asarray(im.resize((4, 4)), dtype=np.float32).reshape(-1) / 255.0
        layout = np.concatenate([small - small.mean(), lab_small[:32] - lab_small[:32].mean()]) * 0.8
        return normalise(np.concatenate([colour, hog_v, layout]).astype(np.float32))


class ClipImageBackend:  # pragma: no cover - requires optional heavy deps
    def __init__(self, model_name: str, pretrained: str) -> None:
        import open_clip
        import torch

        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer(model_name)
        self.name = f"clip-{model_name}-{pretrained}"
        self._lock = threading.Lock()

    def embed(self, img: Image.Image) -> np.ndarray:
        with self._lock, self.torch.no_grad():
            t = self.preprocess(img.convert("RGB")).unsqueeze(0)
            v = self.model.encode_image(t)[0].cpu().numpy().astype(np.float32)
        return normalise(v)

    def embed_text(self, text: str) -> np.ndarray:
        with self._lock, self.torch.no_grad():
            v = self.model.encode_text(self.tokenizer([text]))[0].cpu().numpy().astype(np.float32)
        return normalise(v)


_backend_lock = threading.Lock()
_backend = None


def get_image_backend():
    global _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        s = get_settings()
        choice = s.embedding_backend.lower()
        if choice in ("auto", "clip"):
            try:
                _backend = ClipImageBackend(s.clip_model, s.clip_pretrained)
                log.info("embedding_backend", backend=_backend.name)
                return _backend
            except Exception as e:  # ImportError or weight download failure
                if choice == "clip":
                    log.error("clip_unavailable", error=str(e))
                else:
                    log.info("clip_not_available_using_lite", reason=str(e)[:200])
        _backend = LiteImageBackend()
        return _backend


def reset_backend() -> None:
    global _backend
    with _backend_lock:
        _backend = None


def backend_info() -> dict:
    b = get_image_backend()
    return {
        "image_backend": b.name,
        "is_clip": b.name.startswith("clip"),
        "note": None
        if b.name.startswith("clip")
        else "Using the lightweight offline image features (colour, tone, texture, rough shape). "
        "Install the optional CLIP extras for much better visual similarity.",
    }


# ------------------------------------------------------------------ text
TEXT_DIM = 2048


@lru_cache(maxsize=1)
def _vectoriser():
    from sklearn.feature_extraction.text import HashingVectorizer

    return HashingVectorizer(
        n_features=TEXT_DIM,
        alternate_sign=False,
        ngram_range=(1, 2),
        norm="l2",
        token_pattern=r"(?u)\b[\w'-]{2,}\b",
        lowercase=True,
    )


def embed_text(text: str) -> np.ndarray:
    v = _vectoriser().transform([text or ""]).toarray()[0].astype(np.float32)
    return v


def product_text(p) -> str:
    """Text used for semantic similarity: English text + structured attributes, attributes repeated for weight."""
    attrs = []
    if p.brand:
        attrs.append(f"brand {p.brand}")
    if p.category:
        attrs += [p.category] * 2
    attrs += [f"{c} colour" for c in (p.colours or [])]
    attrs += [m["fibre"] for m in (p.materials or [])] * 2
    attrs += [f"{t} fit" for t in (p.fit_tags or [])]
    if p.logo_level and p.logo_level != "unknown":
        attrs.append(f"logo {p.logo_level}")
    return " ".join([p.title_en or p.title_original or "", (p.description_en or "")[:1500], " ".join(attrs)])
