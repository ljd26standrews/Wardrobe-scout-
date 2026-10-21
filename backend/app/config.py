"""Application configuration loaded from environment variables / `.env`.

Only infrastructure settings live here. User-facing preferences (ranking
weights, taste profile, landed-cost assumptions, AI budget) are stored in the
database `settings` table so they can be edited from the UI and exported.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_FILENAME = "wardrobe_scout.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"),
        env_prefix="WS_",
        extra="ignore",
    )

    # Storage. Without WS_DATABASE_URL the database lives in the data folder (WS_DATA_DIR).
    database_url: str = ""
    data_dir: Path = Field(default=BACKEND_DIR / "data")
    database_location_note: str | None = None  # set when an older layout is being kept

    # HTTP / ingestion politeness
    user_agent: str = "Mozilla/5.0 (compatible; WardrobeScout/1.0; personal catalogue reader; +local)"
    request_delay_seconds: float = 2.0  # minimum delay between requests to the same host
    # Minimum delay between requests to an image host (CDN). Unset = same as request_delay_seconds.
    image_request_delay_seconds: float | None = None
    request_timeout_seconds: float = 25.0
    # Catalogue listing pages are cheap (one request per ~40 products), so a scan reads the whole listing.
    max_pages_per_scan: int = 250
    respect_robots_txt: bool = True
    download_images: bool = True
    max_images_per_product: int = 12  # upper bound for imports and deep analysis
    # Progressive photo analysis (see services/image_select.py and bench/ablate_selection.py):
    # photos are fetched in gallery order until `analysis_images` useful, non-redundant views are found
    # (size charts, text graphics and repeated banners are skipped) or `max_examine_images` were fetched.
    analysis_images: int = 1
    max_examine_images: int = 6
    deep_images: int = 8  # photos fetched for products that get deep analysis (duplicate checks, your saves)
    refresh_days: int = 14  # re-read an unchanged product page after this long (spread across scans)
    deep_on_feedback: bool = True  # loving/saving a scanned product fetches its full gallery for comparisons
    # numpy's BLAS spins extra threads for small matrix products; with the scanner's own worker threads
    # that oversubscribes the CPU (measured: 3x more CPU time for a 3,000-product scan). 0 = library default.
    blas_threads: int = 1

    # Optional components
    enable_scheduler: bool = True
    enable_playwright: bool = False  # browser rendering for JS-only pages (e.g. Weidian)
    embedding_backend: str = "auto"  # auto | clip | lite
    clip_model: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"

    # Optional AI provider. Key comes from env only, never stored in the DB.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Security. Every /api request must carry this token (X-WS-Token). If unset, a random token is
    # generated on first start and stored in <data_dir>/api_token; the Next.js proxy reads the same file.
    api_token: str | None = None
    # Allow fetching private/LAN/loopback addresses (only needed for local test servers).
    allow_private_urls: bool = False
    max_download_bytes: int = 25 * 1024 * 1024
    # The API token is the access control; CORS stays open so the bookmarklet works from seller pages.
    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"
    log_json: bool = False

    @model_validator(mode="after")
    def _default_database(self) -> Settings:
        if self.database_url:
            return self
        in_data_dir = self.data_dir / DB_FILENAME
        legacy = BACKEND_DIR / "data" / DB_FILENAME
        if not in_data_dir.exists() and legacy.exists() and legacy.resolve() != in_data_dir.resolve():
            # Earlier versions kept the database in backend/data even when WS_DATA_DIR pointed elsewhere.
            # Keep using it rather than starting an empty one; say how to move it.
            self.database_url = f"sqlite:///{legacy}"
            self.database_location_note = (
                f"Using the existing database at {legacy} although WS_DATA_DIR is {self.data_dir}. "
                f"To keep everything together, stop the app and move it to {in_data_dir} "
                f"(or set WS_DATABASE_URL explicitly)."
            )
        else:
            self.database_url = f"sqlite:///{in_data_dir}"
        return self

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"


def limit_blas_threads() -> None:
    """Apply ``blas_threads`` to the numerical libraries already loaded in this process."""
    n = get_settings().blas_threads
    if n > 0:
        try:
            from threadpoolctl import threadpool_limits

            threadpool_limits(limits=n, user_api="blas")
        except ImportError:  # pragma: no cover - threadpoolctl ships with scikit-learn
            pass


def get_api_token() -> str:
    """The per-install API token (env WS_API_TOKEN, else generated once and stored with mode 0600)."""
    s = get_settings()
    if s.api_token:
        return s.api_token
    path = s.data_dir / "api_token"
    if not path.exists():
        import os
        import secrets

        path.write_text(secrets.token_urlsafe(32))
        os.chmod(path, 0o600)
    return path.read_text().strip()


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.images_dir, s.uploads_dir, s.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
