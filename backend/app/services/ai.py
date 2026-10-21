"""Optional AI provider (Anthropic Claude) - off by default.

Guarantees
----------
* Disabled unless Settings → AI → enabled is on AND ``ANTHROPIC_API_KEY`` is set.
* Every response is cached in ``ai_calls`` keyed by (purpose, model, input);
  identical requests never cost twice.
* A monthly spend cap (USD) is a hard limit: before each call the worst-case cost (a conservative
  input-token estimate plus ``max_tokens`` of output) is *reserved*; the call is refused if that reservation
  would exceed what is left. Reservations are recorded atomically, so concurrent calls cannot jointly
  overshoot, and are reconciled to the actual cost from the response (released if the call fails).
  A minimum interval between calls rate-limits usage.
* AI visual assessments are stored as ``ai_visual`` evidence with explicit
  caveats; they are never used as proof of fibre content or durability.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from datetime import datetime

from ..config import get_settings
from ..db import session_scope
from ..logging_setup import get_logger
from ..models import AICall, utcnow
from .settings_store import get_setting

log = get_logger(__name__)
_lock = threading.Lock()
_budget_lock = threading.Lock()  # one process (the app) owns the budget; see README "Optional AI"
_last_call = 0.0
RESERVATION_PREFIX = "reservation:"
IMAGE_TOKEN_ALLOWANCE = 5000  # generous upper bound per image block


def worst_case_cost(cfg: dict, system: str, content: list[dict], max_tokens: int) -> float:
    """Upper bound for one call: every character counted as a token, a large allowance per image."""
    chars = len(system) + sum(len(c.get("text", "")) for c in content if c.get("type") == "text")
    images = sum(1 for c in content if c.get("type") == "image")
    input_tokens = chars + images * IMAGE_TOKEN_ALLOWANCE + 200
    return (
        input_tokens * float(cfg.get("price_input_per_mtok", 4.0))
        + max_tokens * float(cfg.get("price_output_per_mtok", 20.0))
    ) / 1_000_000


def _reserve(purpose: str, model: str, amount: float, cap: float) -> int:
    """Atomically reserve ``amount`` of this month's budget; returns the reservation id."""
    import uuid

    with _budget_lock:
        if month_spend() + amount > cap + 1e-12:
            raise AIUnavailable(
                f"Monthly AI spending cap would be exceeded (this call may cost up to ${amount:.4f}; "
                f"${max(0.0, cap - month_spend()):.4f} left)"
            )
        with session_scope() as db:
            row = AICall(cache_key=f"{RESERVATION_PREFIX}{uuid.uuid4().hex}", purpose=purpose, model=model,
                         response="", input_tokens=0, output_tokens=0, cost_usd=amount)  # fmt: skip
            db.add(row)
            db.flush()
            return row.id


def _release(reservation_id: int) -> None:
    with _budget_lock, session_scope() as db:
        row = db.get(AICall, reservation_id)
        if row is not None and row.cache_key.startswith(RESERVATION_PREFIX):
            db.delete(row)


class AIUnavailable(Exception):
    pass


def _config() -> dict:
    with session_scope() as db:
        return get_setting(db, "ai")


def status() -> dict:
    cfg = _config()
    key = bool(get_settings().anthropic_api_key)
    spent = month_spend()
    return {
        "enabled": bool(cfg.get("enabled")) and key,
        "configured_enabled": bool(cfg.get("enabled")),
        "api_key_present": key,
        "model": cfg.get("model"),
        "month_spend_usd": round(spent, 4),
        "monthly_cap_usd": cfg.get("monthly_cap_usd"),
        "remaining_usd": round(max(0.0, (cfg.get("monthly_cap_usd") or 0) - spent), 4),
    }


def month_spend() -> float:
    start = datetime(utcnow().year, utcnow().month, 1)
    with session_scope() as db:
        rows = db.query(AICall.cost_usd).filter(AICall.created_at >= start).all()
        return float(sum(r[0] for r in rows))


def is_enabled(purpose: str | None = None) -> bool:
    cfg = _config()
    if not cfg.get("enabled") or not get_settings().anthropic_api_key:
        return False
    flag = {
        "translate": "use_for_translation",
        "visual": "use_for_visual_assessment",
        "research": "use_for_research_summaries",
    }.get(purpose or "")
    return bool(cfg.get(flag, True)) if flag else True


def _call(purpose: str, system: str, content: list[dict], max_tokens: int = 2000, effort: str = "low") -> str:
    cfg = _config()
    if not is_enabled(purpose):
        raise AIUnavailable("AI features are disabled or no API key is configured")
    model = cfg.get("model") or "claude-opus-5-5"
    key_src = json.dumps([purpose, model, system, content], sort_keys=True, ensure_ascii=False)
    cache_key = hashlib.sha256(key_src.encode()).hexdigest()
    with session_scope() as db:
        hit = db.query(AICall).filter_by(cache_key=cache_key).first()
        if hit:
            return hit.response
    reserved = worst_case_cost(cfg, system, content, max_tokens)
    reservation = _reserve(purpose, model, reserved, float(cfg.get("monthly_cap_usd") or 0))
    try:
        return _call_reserved(cfg, purpose, model, system, content, max_tokens, effort, cache_key, reservation)
    except BaseException:
        _release(reservation)
        raise


def _call_reserved(cfg, purpose, model, system, content, max_tokens, effort, cache_key, reservation) -> str:
    import anthropic

    global _last_call
    with _lock:
        wait = float(cfg.get("min_seconds_between_calls", 2.0)) - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError as e:
        raise AIUnavailable("AI provider rate limit hit; try later") from e
    except anthropic.AuthenticationError as e:
        raise AIUnavailable("Invalid Anthropic API key") from e
    except anthropic.APIStatusError as e:
        raise AIUnavailable(f"AI provider error {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise AIUnavailable("Could not reach the AI provider") from e
    if resp.stop_reason == "refusal":
        raise AIUnavailable("The model declined this request")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    usage = resp.usage
    cost = (
        usage.input_tokens * float(cfg.get("price_input_per_mtok", 4.0))
        + usage.output_tokens * float(cfg.get("price_output_per_mtok", 20.0))
    ) / 1_000_000
    with _budget_lock, session_scope() as db:
        row = db.get(AICall, reservation)  # reconcile the reservation to the actual cost
        row.cache_key, row.response = cache_key, text
        row.input_tokens, row.output_tokens, row.cost_usd = usage.input_tokens, usage.output_tokens, cost
    if cost > 0 and cost > worst_case_cost(cfg, system, content, max_tokens):
        log.warning("ai_cost_above_reservation", purpose=purpose, cost_usd=cost)
    log.info("ai_call", purpose=purpose, model=model, cost_usd=round(cost, 5))
    return text


def translate(text: str) -> str | None:
    try:
        return _call(
            "translate",
            "You translate Chinese clothing-seller listings into concise, natural British English. "
            "Preserve numbers, sizes, prices, item codes and material percentages exactly. Do not add "
            "information that is not in the source. Output only the translation.",
            [{"type": "text", "text": text}],
            max_tokens=1500,
        )
    except AIUnavailable as e:
        log.info("ai_translate_unavailable", reason=str(e))
        return None


VISUAL_PROMPT = """You are assisting a menswear buyer. Look at these product photographs from a seller listing.
Describe ONLY what is visible. For each point, say how confident you are and why.
Never infer fibre content, cashmere percentage, fabric weight or durability from photos.
Return strict JSON: {"observations":[{"aspect":"construction|stitching|hardware|fit|logo|accuracy",
"observation":"...", "polarity":-1|0|1, "confidence":0.0-1.0}], "logo_visibility":"none|subtle|visible|unclear",
"silhouette":"short description", "caveats":"..."}"""


def visual_assessment(image_paths: list[str], title: str) -> dict:
    from .images import resolve_path

    content: list[dict] = []
    for rel in image_paths[:4]:
        data = resolve_path(rel).read_bytes()
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(data).decode(),
                },
            }
        )
    content.append({"type": "text", "text": f"Listing title: {title}\n\n{VISUAL_PROMPT}"})
    raw = _call(
        "visual",
        "You are a careful, sceptical garment construction analyst.",
        content,
        max_tokens=2000,
        effort="medium",
    )
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return {"observations": [], "caveats": raw[:500]}


def summarise_research(subject: str, snippets: list[dict]) -> str:
    text = "\n\n".join(f"[{i + 1}] {s['title']} ({s['url']})\n{s.get('excerpt') or ''}" for i, s in enumerate(snippets))
    return _call(
        "research",
        "Summarise community buyer reports about a clothing seller or product. Distinguish clearly between "
        "independent buyer experiences and promotional/affiliate content. Cite sources as [n]. Flag "
        "contradictions and missing information. Never overstate certainty.",
        [{"type": "text", "text": f"Subject: {subject}\n\nSources:\n{text}"}],
        max_tokens=1500,
        effort="medium",
    )
