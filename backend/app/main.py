"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import feedback, products, sellers, system, wardrobe
from .config import get_api_token, get_settings, limit_blas_threads
from .db import SessionLocal, init_engine, migrate
from .logging_setup import configure_logging, get_logger
from .models import AppSetting
from .seed import seed
from .services import jobs

log = get_logger(__name__)


def bootstrap(run_seed: bool = True) -> None:
    s = get_settings()
    import numpy  # noqa: F401 - load BLAS so the thread limit applies to it

    limit_blas_threads()
    if s.database_location_note:
        log.warning("database_location", note=s.database_location_note)
    get_api_token()  # create the token file up front so the app proxy can read it
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
        jobs.enqueue_upgrade_jobs()
        jobs.start_scheduler()
    yield
    jobs.stop_scheduler()


class APITokenMiddleware:
    """Require the per-install token on every /api request (except /api/health and CORS preflights).

    Without this, any website open in your browser could read or change your data through the
    localhost API. The Next.js proxy adds the token server-side; the bookmarklet carries it explicitly.
    """

    OPEN_PATHS = {"/api/health"}

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith("/api/") and path not in self.OPEN_PATHS:
            import hmac

            sent = dict(scope["headers"]).get(b"x-ws-token", b"").decode("latin-1")
            if not hmac.compare_digest(sent, get_api_token()):
                from starlette.responses import JSONResponse

                resp = JSONResponse({"detail": "missing or invalid API token"}, status_code=401)
                return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="Wardrobe Scout API", version="1.0.0", lifespan=lifespan)
    # Added first so CORS wraps it: even 401 responses carry CORS headers for the bookmarklet.
    app.add_middleware(APITokenMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        # Chrome sends a Private Network Access preflight when a public page (the bookmarklet on a
        # seller site) calls localhost; without this the preflight is rejected with 400.
        allow_private_network=True,
    )
    for r in (products.router, feedback.router, sellers.router, wardrobe.router, system.router):
        app.include_router(r)

    return app


app = create_app()
