"""Prices with an explicit currency.

A price is an amount in a currency. Prices are compared, averaged or filtered only after converting both into one
currency (pounds) at a rate you stated in Settings → Currency. A price in an unstated currency, or one without a
rate, stays unknown: it is left out of medians, judgements and filters, never treated as yuan or as pounds. The
original amount and currency are always kept and shown.

Conversion is not landed cost. Landed cost (landed_cost.py) estimates what buying in yuan through an agent costs in
total; it builds on the conversion and adds shipping, fees and import charges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy.orm import Session

from .settings_store import get_setting

# Spellings seen on listings. "¥" alone is left unknown: it is also the yen sign.
ALIASES = {"RMB": "CNY", "CNH": "CNY", "YUAN": "CNY", "元": "CNY", "UKP": "GBP", "STG": "GBP", "£": "GBP",
           "US$": "USD", "€": "EUR"}  # fmt: skip


@lru_cache(maxsize=512)
def code(currency: str | None) -> str | None:
    """The ISO code of a stated currency (RMB and CNH are CNY), or None when none is stated or it is ambiguous.
    (Remembered per spelling: a feed converts every listing's price.)"""
    c = (currency or "").strip().upper()
    c = ALIASES.get(c, c)
    return c if re.fullmatch(r"[A-Z]{3}", c) else None


@dataclass(frozen=True)
class Rates:
    """Pounds per unit of each currency with a stated rate."""

    to_gbp: dict[str, float] = field(default_factory=lambda: {"GBP": 1.0})
    source: str = ""
    updated_at: str | None = None

    @classmethod
    def from_settings(cls, cfg: dict) -> Rates:
        rates = {"GBP": 1.0}
        if cfg.get("cny_to_gbp"):
            rates["CNY"] = float(cfg["cny_to_gbp"])
        for k, v in (cfg.get("other_to_gbp") or {}).items():
            if (c := code(k)) and c != "GBP" and v and float(v) > 0:
                rates[c] = float(v)
        return cls(rates, cfg.get("rate_source") or "", cfg.get("rate_updated_at"))

    def gbp(self, amount: float | None, currency: str | None) -> float | None:
        """``amount`` in pounds, or None when the amount, the currency or its rate is unknown."""
        c = code(currency)
        if amount is None or c is None or c not in self.to_gbp:
            return None
        return float(amount) * self.to_gbp[c]

    def convert(self, amount: float | None, currency: str | None, to: str | None) -> float | None:
        g, t = self.gbp(amount, currency), code(to)
        if g is None or t is None or t not in self.to_gbp:
            return None
        return g / self.to_gbp[t]

    def known(self, currency: str | None) -> bool:
        return code(currency) in self.to_gbp


def rates(db: Session) -> Rates:
    return Rates.from_settings(get_setting(db, "currency"))
