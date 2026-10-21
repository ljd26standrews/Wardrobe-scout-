"""Application configuration loaded from environment variables / `.env`.

Only infrastructure settings live here. User-facing preferences (ranking
weights, taste profile, landed-cost assumptions, AI budget) are stored in the
database `settings` table so they can be edited from the UI and exported.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"),
        env_prefix="WS_",
        extra="ignore",
    )

    # Storage
    database_url: str = Field(default=f"sqlite:///{BACKEND_DIR / 'data' / 'wardrobe_scout.db'}")
    data_dir: Path = Field(default=BACKEND_DIR / "data")

    # HTTP / ingestion politeness
    user_agent: str = "Mozilla/5.0 (compatible; WardrobeScout/1.0; personal catalogue reader; +local)"
    request_delay_seconds: float = 2.0  # minimum delay between requests to the same host
    request_timeout_seconds: float = 25.0
    max_pages_per_scan: int = 40
    respect_robots_txt: bool = True
    download_images: bool = True
    max_images_per_product: int = 12

    # Optional components
    enable_scheduler: bool = True
    enable_playwright: bool = False  # browser rendering for JS-only pages (e.g. Weidian)
    embedding_backend: str = "auto"  # auto | clip | lite
    clip_model: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"

    # Optional AI provider. Key comes from env only, never stored in the DB.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"
    log_json: bool = False

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.images_dir, s.uploads_dir, s.backups_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
