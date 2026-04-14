"""Structured logging setup."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import structlog


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def setup_logging(run_id: str | None = None) -> Path:
    """Configure structlog + file handler; return log file path."""
    rid = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = _project_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{rid}.log"

    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return log_path
