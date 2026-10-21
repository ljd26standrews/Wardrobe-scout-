"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import feedback, products, sellers, system, wardrobe
from .config import get_settings
from .db import SessionLocal, init_engine, migrate
from .logging_setup import configure_logging, get_logger
from .models import AppSetting
from .seed import seed
from .services import jobs

log = get_logger(__name__)


def bootstrap(run_seed: bool = True) -> None:
    s = get_settings()
    init_engine(s.database_url)
    migrate()
    if run_seed:
        db = SessionLocal()
        try:
            if db.get(AppSetting, "_seeded") is None:
                result = seed(db)
                db.add(AppSetting(key="_seeded", value=True))
                db.commit()
                log.info("seeded", **result)
        finally:
            db.close()
    jobs.mark_interrupted_jobs()


def _recompute_if_backend_changed() -> None:
    """If the image model changed (e.g. CLIP was installed), re-embed everything in the background."""
    from .models import Product
    from .services import embeddings

    db = SessionLocal()
    try:
        current = embeddings.get_image_backend().name
        stale = (
            db.query(Product.id)
            .filter(Product.embedding_backend.isnot(None), Product.embedding_backend != current)
            .first()
        )
    finally:
        db.close()
    if stale:
        log.info("embedding_backend_changed_recomputing", backend=current)
        jobs.enqueue("recompute", {"reason": "embedding backend changed"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    bootstrap()
    _recompute_if_backend_changed()
    if s.enable_scheduler:
        jobs.start_scheduler()
    yield
    jobs.stop_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title="Wardrobe Scout API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in (products.router, feedback.router, sellers.router, wardrobe.router, system.router):
        app.include_router(r)

    @app.middleware("http")
    async def private_network_access(request, call_next):
        # Lets the bookmarklet (running on a public https page in *your* browser) reach this local API.
        response = await call_next(request)
        if request.headers.get("access-control-request-private-network"):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    return app


app = create_app()
