"""LinkdAPI wrapper with retries (sync SDK run in thread pool)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")


def _with_retries(fn: Callable[[], T], max_retries: int = 3, label: str = "linkdapi") -> T:
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = any(
                x in msg
                for x in ("429", "rate", "timeout", "temporar", "503", "502", "connection")
            )
            wait = 2 ** (attempt + 1)
            log.warning(
                "linkdapi_retry",
                label=label,
                attempt=attempt + 1,
                error=str(e),
                transient=transient,
                wait_s=wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"{label} failed after {max_retries} retries: {last}") from last


class LinkdAPIWrapper:
    """Thin async-friendly wrapper around linkdapi sync client."""

    def __init__(self, api_key: str) -> None:
        try:
            from linkdapi import LinkdAPI
        except ImportError as e:
            raise ImportError("Install linkdapi: pip install linkdapi") from e
        self._client = LinkdAPI(api_key)

    async def search_people(self, **kwargs: Any) -> Any:
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return await asyncio.to_thread(_with_retries, lambda: self._client.search_people(**clean))

    async def get_full_profile(self, **kwargs: Any) -> Any:
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return await asyncio.to_thread(_with_retries, lambda: self._client.get_full_profile(**clean))

    async def get_social_matrix(self, username: str) -> Any:
        return await asyncio.to_thread(
            _with_retries,
            lambda: self._client.get_social_matrix(username),
            label="get_social_matrix",
        )
