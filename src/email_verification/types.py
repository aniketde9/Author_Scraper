"""Pydantic models for Email Truth Reactor output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VerificationResult(BaseModel):
    """Result of a full reactor pass (Stages 0–4)."""

    input: str
    timestamp: str = ""
    syntax_valid: bool = False
    normalized: str | None = None
    local_part: str | None = None
    domain: str | None = None
    syntax_error: str | None = None
    mx_valid: bool = False
    parked: bool = False
    role: bool = False
    disposable: bool = False
    free_provider: bool = False
    smtp_accepts: bool | None = None
    smtp_code: int | None = None
    smtp_message: str = ""
    smtp_duration_sec: float = 0.0
    primary_mx: str = ""
    catch_all_rate: float = 0.0
    is_catch_all: bool = False
    is_seg_like: bool = False
    timing_variance: float = 0.0
    confidence: float = 0.0
    confidence_range: str = ""
    breakdown: list[str] = Field(default_factory=list)
    # deliverable | risky | invalid | unknown
    category: str = "unknown"
    final_label: str = ""
    skipped: bool = False
    skip_reason: str = ""
    smtp_not_run: bool = False
    error: str | None = None
    raw_report: dict[str, Any] = Field(default_factory=dict)
