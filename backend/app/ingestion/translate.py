"""Chinese -> English translation.

Offline mode replaces known garment / material / colour terms using a curated
glossary and leaves unknown Chinese characters in place (so nothing is
silently invented). When the optional AI provider is enabled the full text is
translated by the model and cached.
"""

from __future__ import annotations

import re

from .parsing import has_cjk, normalise_space
from .vocab import GLOSSARY

_SORTED = sorted(GLOSSARY, key=lambda kv: -len(kv[0]))


def glossary_translate(text: str | None) -> str:
    if not text:
        return ""
    if not has_cjk(text):
        return normalise_space(text)
    out = text
    for zh, en in _SORTED:
        if zh in out:
            out = out.replace(zh, f" {en} ")
    out = out.replace("【", "[").replace("】", "]").replace("，", ", ").replace("。", ". ")
    out = out.replace("：", ": ").replace("；", "; ").replace("（", "(").replace("）", ")").replace("、", ", ")
    out = re.sub(r"\s+([,.;:)\]])", r"\1", out)
    return normalise_space(out)


def translate(text: str | None, *, use_ai: bool = False) -> tuple[str, str]:
    """Return (translation, method). Method is 'none' | 'glossary' | 'ai'."""
    if not text:
        return "", "none"
    if not has_cjk(text):
        return normalise_space(text), "none"
    if use_ai:
        from ..services import ai

        result = ai.translate(text)
        if result:
            return result, "ai"
    return glossary_translate(text), "glossary"
