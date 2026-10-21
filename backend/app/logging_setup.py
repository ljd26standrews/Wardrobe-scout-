"""Structured logging via structlog (console in dev, JSON optional)."""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "wardrobe_scout"):
    return structlog.get_logger(name)
