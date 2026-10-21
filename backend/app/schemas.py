"""Pydantic request models (input validation)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from .services.recommender import REASONS

Action = Literal["love", "like", "maybe", "dislike", "skip", "purchased", "unsave"]
EvidenceKindT = Literal["verified", "seller_claim", "buyer_report", "ai_visual", "user_note"]
Aspect = Literal[
    "material",
    "gsm",
    "construction",
    "stitching",
    "hardware",
    "fit",
    "accuracy",
    "defect",
    "sizing",
    "reputation",
    "service",
    "consistency",
    "batch",
    "value",
]


class JudgementIn(BaseModel):
    product_id: int
    taste: int = Field(ge=0, le=3)  # 0 not for me · 1 unsure · 2 like · 3 love
    owned_similar: bool = False
    too_expensive: bool = False
    quality_unclear: bool = False
    note: str | None = Field(default=None, max_length=1000)


class ViewedItem(BaseModel):
    product_id: int
    position: int | None = None  # informational; the server records the position it served


class ViewedIn(BaseModel):
    surface: Literal["card", "grid"] = "card"
    items: list[ViewedItem] = Field(default_factory=list, max_length=100)


class FeedbackIn(BaseModel):
    product_id: int
    action: Action
    reasons: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)
    context: dict = Field(default_factory=dict)

    @field_validator("reasons")
    @classmethod
    def _valid_reasons(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if r not in REASONS]
        if bad:
            raise ValueError(f"unknown reasons: {bad}")
        return v


class SavedPatch(BaseModel):
    list_name: Literal["loved", "liked", "maybe", "wishlist"] | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    note: str | None = Field(default=None, max_length=2000)
    target_size: str | None = Field(default=None, max_length=20)


class EvidenceIn(BaseModel):
    kind: EvidenceKindT
    aspect: Aspect
    claim: str = Field(min_length=3, max_length=4000)
    polarity: int = Field(default=0, ge=-1, le=1)
    strength: float = Field(default=0.7, ge=0, le=1)
    source_url: str | None = Field(default=None, max_length=1000)
    source_title: str | None = Field(default=None, max_length=300)
    is_affiliate: bool = False

    @field_validator("source_url")
    @classmethod
    def _web_link(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("source link must be a web address (http or https)")
        return v


class SellerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl | None = None

    @field_validator("base_url")
    @classmethod
    def _public(cls, v: HttpUrl | None) -> HttpUrl | None:
        return _public_url(v) if v else v

    platform: Literal["yupoo", "weidian", "shopify", "website", "manual"] = "yupoo"
    priority: int = Field(default=3, ge=1, le=5)
    notes: str | None = None
    scan_interval_hours: int = Field(default=24, ge=1, le=24 * 30)


class SellerPatch(BaseModel):
    name: str | None = None
    base_url: HttpUrl | None = None
    platform: Literal["yupoo", "weidian", "shopify", "website", "manual"] | None = None
    status: Literal["active", "paused"] | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None
    scan_interval_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    reputation_status: Literal["unassessed", "insufficient evidence", "trusted", "mixed", "concerns"] | None = None

    @field_validator("base_url")
    @classmethod
    def _public(cls, v: HttpUrl | None) -> HttpUrl | None:
        return _public_url(v) if v else v


class ScanIn(BaseModel):
    mode: Literal["incremental", "full"] = "incremental"


def _public_url(v: HttpUrl) -> HttpUrl:
    from .ingestion.http import UnsafeURLError, assert_public_url

    try:
        assert_public_url(str(v))
    except UnsafeURLError as e:
        raise ValueError(str(e)) from e
    return v


class UrlImportIn(BaseModel):
    url: HttpUrl
    seller_id: int | None = None
    watch: bool = True

    @field_validator("url")
    @classmethod
    def _public(cls, v: HttpUrl) -> HttpUrl:
        return _public_url(v)


class BookmarkletIn(BaseModel):
    url: HttpUrl
    html: str = Field(max_length=8_000_000)
    seller_id: int | None = None


class ProductPatch(BaseModel):
    title_en: str | None = None
    brand: str | None = None
    category: str | None = None
    colours: list[str] | None = None
    material_text: str | None = None
    gsm: int | None = Field(default=None, ge=50, le=1200)
    price: float | None = Field(default=None, ge=0)
    currency: str | None = None
    sizes: list[str] | None = None
    seasons: list[str] | None = None
    fit_tags: list[str] | None = None
    logo_level: Literal["none", "subtle", "visible", "unknown"] | None = None
    status: Literal["active", "unavailable", "removed"] | None = None


class MatchDecision(BaseModel):
    other_id: int
    same: bool


class LeadAccept(BaseModel):
    aspect: Aspect = "reputation"
    polarity: int = Field(ge=-1, le=1)
    claim: str = Field(min_length=3, max_length=4000)
    product_id: int | None = None
    seller_id: int | None = None
    is_affiliate: bool | None = None
    strength: float = Field(default=0.7, ge=0, le=1)


class CommunitySearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    seller_id: int | None = None
    product_id: int | None = None
