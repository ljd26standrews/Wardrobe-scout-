"""SQLAlchemy ORM models.

Design notes
------------
* Every extracted fact keeps provenance: products store `source_url` and
  `last_seen_at`; individual quality claims live in `Evidence` with their own
  source URL, capture timestamp and evidence *kind* (verified, seller claim,
  buyer report, AI visual assessment).
* `data_origin` records how a product entered the system (scraped, manual,
  csv, json, bookmarklet, seed) so the UI never presents a seed or manual
  record as a genuinely scraped listing.
* Embeddings are float32 numpy arrays stored as bytes with their backend name,
  so switching embedding model triggers a clean recompute.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class EvidenceKind(enum.StrEnum):
    verified = "verified"  # independently verified (e.g. lab test, label photo + multiple QC)
    seller_claim = "seller_claim"  # stated by the seller in the listing
    buyer_report = "buyer_report"  # independent buyer QC / in-hand review
    ai_visual = "ai_visual"  # AI assessment of photos - never proof of fibre content
    user_note = "user_note"  # your own observation (e.g. after purchase)


class FeedbackAction(enum.StrEnum):
    love = "love"
    like = "like"
    maybe = "maybe"
    dislike = "dislike"
    skip = "skip"
    purchased = "purchased"
    unsave = "unsave"


class Seller(Base):
    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(40), default="yupoo")  # yupoo|weidian|shopify|website|manual
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1 (highest) .. 5
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reputation is "unassessed" until evidence exists - never assumed from popularity.
    reputation_status: Mapped[str] = mapped_column(String(30), default="unassessed")
    scan_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scan_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_scan_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    products: Mapped[list[Product]] = relationship(back_populates="seller")


class WatchedUrl(Base):
    """Individual product / album / category links the user wants tracked."""

    __tablename__ = "watched_urls"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="SET NULL"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("seller_id", "external_id", name="uq_seller_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="SET NULL"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))  # album id / item id / hash of url
    seller_item_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    title_original: Mapped[str] = mapped_column(Text, default="")
    title_en: Mapped[str] = mapped_column(Text, default="")
    description_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    translation_method: Mapped[str | None] = mapped_column(String(30), nullable=True)

    brand: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    colours: Mapped[list] = mapped_column(JSON, default=list)
    materials: Mapped[list] = mapped_column(JSON, default=list)  # [{"fibre": "wool", "pct": 70}]
    material_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    gsm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sizes: Mapped[list] = mapped_column(JSON, default=list)
    seasons: Mapped[list] = mapped_column(JSON, default=list)
    fit_tags: Mapped[list] = mapped_column(JSON, default=list)
    logo_level: Mapped[str] = mapped_column(String(20), default="unknown")  # none|subtle|visible|unknown

    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    source_url: Mapped[str] = mapped_column(String(1000))
    links: Mapped[dict] = mapped_column(
        JSON, default=dict
    )  # {"weidian": url, "yupoo": url, "taobao": url, "seller": url}

    status: Mapped[str] = mapped_column(String(20), default="active")  # active|unavailable|removed
    data_origin: Mapped[str] = mapped_column(String(20), default="scraped")
    completeness: Mapped[str] = mapped_column(String(20), default="complete")  # complete|partial|seed_unverified
    is_new: Mapped[bool] = mapped_column(Boolean, default=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Progressive scanning: discovered (catalogue listing only) -> partial (product page OR photo analysed)
    # -> analysed (product page AND photos) -> deep (extra photos fetched, duplicates verified).
    analysis_state: Mapped[str] = mapped_column(String(20), default="analysed", server_default="analysed", index=True)
    detail_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    analysed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(200), nullable=True)  # HTTP validators for conditional refresh
    last_modified: Mapped[str | None] = mapped_column(String(80), nullable=True)

    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    image_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    text_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_backend: Mapped[str | None] = mapped_column(String(60), nullable=True)

    seller: Mapped[Seller | None] = relationship(back_populates="products")
    images: Mapped[list[ProductImage]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductImage.position"
    )
    evidence: Mapped[list[Evidence]] = relationship(back_populates="product", cascade="all, delete-orphan")
    observations: Mapped[list[ProductObservation]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductObservation.observed_at"
    )


class ProductImage(Base):
    __tablename__ = "product_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(1500), index=True)
    referer: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    local_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    phash: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_backend: Mapped[str | None] = mapped_column(String(60), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|ok|failed|uploaded
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Smaller variant shown by the listing / album grid (e.g. Yupoo medium.jpg). Analysis uses it; `url` stays
    # the original full-resolution source.
    thumb_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    # garment | detail | uncertain | size_chart | text_graphic | repeated (see services.image_select)
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    role_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    selected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # feeds the aesthetic product vector
    analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    product: Mapped[Product] = relationship(back_populates="images")


class ProductObservation(Base):
    """Price / size / availability snapshots for change tracking."""

    __tablename__ = "product_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sizes: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active")
    source_url: Mapped[str] = mapped_column(String(1000))
    change: Mapped[str | None] = mapped_column(String(200), nullable=True)

    product: Mapped[Product] = relationship(back_populates="observations")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # EvidenceKind
    # material | gsm | construction | stitching | hardware | fit | accuracy | defect | sizing | reputation | value | batch
    aspect: Mapped[str] = mapped_column(String(30))
    claim: Mapped[str] = mapped_column(Text)
    polarity: Mapped[int] = mapped_column(Integer, default=0)  # -1 negative, 0 neutral/informational, +1 positive
    strength: Mapped[float] = mapped_column(Float, default=1.0)  # 0..1, how strong/specific
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    is_affiliate: Mapped[bool] = mapped_column(Boolean, default=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(20), default="system")  # system|user|ai

    product: Mapped[Product | None] = relationship(back_populates="evidence")


class ProductRelation(Base):
    __tablename__ = "product_relations"
    __table_args__ = (UniqueConstraint("a_id", "b_id", name="uq_relation_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    a_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    b_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    # same_listing (shared photos) | same_batch_suspected | visually_similar | confirmed_identical | not_same
    relation: Mapped[str] = mapped_column(String(30))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    user_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(20))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"explore": true, "rank": 3}
    # Snapshot so feedback survives product deletion and can be exported meaningfully.
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class FeedRequest(Base):
    """One Discover feed as served: what the ranking offered, in which order, under which model.

    With ``Impression`` (what was actually displayed) and the feed id/position stored in each decision's
    context, this is the exposure record an honest evaluation needs: feedback alone is not a complete record
    of what was shown."""

    __tablename__ = "feed_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # uuid4 hex
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    engine: Mapped[str] = mapped_column(String(20))
    engine_version: Mapped[str] = mapped_column(String(40))
    decisions: Mapped[int] = mapped_column(Integer, default=0)  # informative decisions the model had seen
    candidates: Mapped[int] = mapped_column(Integer, default=0)  # undecided listings eligible for the feed
    exploration_rate: Mapped[float] = mapped_column(Float, default=0.0)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    # [{"product_id", "position", "score", "explore", "family_size"}] in served order; selective feeds add each
    # piece's raw logit "m", its s.d. "s", "estimate", "bound", the modes it "passes" and (Learning picks) the
    # inclusion "propensity"
    items: Mapped[list] = mapped_column(JSON, default=list)
    # ranked (the earlier feed with exploration slots) | selective (the gated main feed) | learning (Learning picks)
    policy: Mapped[str] = mapped_column(String(20), default="ranked", server_default="ranked")
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)  # balanced | high | extreme
    calibration_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None: no approval estimates
    gate: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # thresholds, state, counts, sampling details


class Impression(Base):
    """A product actually displayed to you: a Discover card on screen, or a tile in the Discover grid."""

    __tablename__ = "impressions"

    id: Mapped[int] = mapped_column(primary_key=True)
    feed_id: Mapped[str | None] = mapped_column(
        ForeignKey("feed_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)  # rank in the served feed
    surface: Mapped[str] = mapped_column(String(20))  # card | grid
    explore: Mapped[bool] = mapped_column(Boolean, default=False)
    shown_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Calibration(Base):
    """A fitted map from raw compatibility to an approval estimate, with how it was obtained.

    Fitted only on your own explicit decisions (out-of-fold, grouped by garment family); kept as a history so
    any version can be inspected or reactivated (rollback). Only one is active at a time."""

    __tablename__ = "calibrations"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    engine_version: Mapped[str] = mapped_column(String(40))
    method: Mapped[str] = mapped_column(String(40))
    n_labels: Mapped[int] = mapped_column(Integer)
    n_positive: Mapped[int] = mapped_column(Integer)
    label_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)  # {"a", "b", "cov": [[...], [...]], "folds"}
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)  # out-of-fold Brier, ECE, reliability bins


class Judgement(Base):
    """A taste label you gave in an evaluation session, kept to *measure* the recommender (it never trains it).

    Products are sampled uniformly from undecided ones (not chosen by the model), one per product family.
    Taste is graded on its own - 0 not for me, 1 unsure, 2 like, 3 love - and owning something similar, the
    price and unclear quality are separate flags, so a purchase objection is never mistaken for a dislike."""

    __tablename__ = "judgements"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    family_key: Mapped[str] = mapped_column(String(40), index=True)  # "g<group id>" or "p<product id>"
    taste: Mapped[int] = mapped_column(Integer)
    owned_similar: Mapped[bool] = mapped_column(Boolean, default=False)
    too_expensive: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_unclear: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sampled_by: Mapped[str] = mapped_column(String(20), default="uniform")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SavedItem(Base):
    """Shortlist / maybe list / wishlist state (latest decision per product)."""

    __tablename__ = "saved_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True)
    list_name: Mapped[str] = mapped_column(String(20))  # loved|liked|maybe|wishlist
    priority: Mapped[int] = mapped_column(Integer, default=2)  # 1 high, 2 medium, 3 low
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_size: Mapped[str | None] = mapped_column(String(20), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WardrobeItem(Base):
    __tablename__ = "wardrobe_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    colours: Mapped[list] = mapped_column(JSON, default=list)
    materials: Mapped[list] = mapped_column(JSON, default=list)
    seasons: Mapped[list] = mapped_column(JSON, default=list)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # how much you love it, 1-5
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_backend: Mapped[str | None] = mapped_column(String(60), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InspirationImage(Base):
    """User-uploaded inspiration photos / screenshots used as positive (or negative) style examples."""

    __tablename__ = "inspiration_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(500))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    polarity: Mapped[int] = mapped_column(Integer, default=1)  # +1 inspiration, -1 anti-inspiration
    tags: Mapped[list] = mapped_column(JSON, default=list)
    phash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    embedding_backend: Mapped[str | None] = mapped_column(String(60), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|succeeded|failed|partial
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    control: Mapped[str | None] = mapped_column(String(20), nullable=True)  # pause | cancel (set by the UI)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # live structured progress


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # running|succeeded|partial|failed|blocked|cancelled, plus invalid (layout not recognised) and empty (no albums)
    status: Mapped[str] = mapped_column(String(30), default="running")
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    products_seen: Mapped[int] = mapped_column(Integer, default=0)
    products_new: Mapped[int] = mapped_column(Integer, default=0)
    products_updated: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # True only when catalogue pagination was genuinely exhausted (the listing was read to its end).
    discovery_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AICall(Base):
    """Cache + spend ledger for optional AI provider calls."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    purpose: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    response: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class CommunityLead(Base):
    """Search hits from community sources awaiting your classification into Evidence."""

    __tablename__ = "community_leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"), nullable=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=True)
    source: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    title: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(120), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    possible_affiliate: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new|accepted|dismissed
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
