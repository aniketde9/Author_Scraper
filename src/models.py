"""Pydantic models for pipeline data."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Pool(StrEnum):
    A = "A"
    B = "B"


class ProfessionTier(StrEnum):
    CEO_FOUNDER_INVESTOR = "ceo_founder_investor"
    VP_CTO_DOCTOR_ENGINEER = "vp_cto_doctor_engineer"
    OTHER_HIGH_INCOME = "other_high_income"
    NONE = "none"


class VisibilityBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AmazonBook(BaseModel):
    asin: str
    title: str
    authors: list[str]
    amazon_url: str
    review_count: int | None = None
    pool: Pool
    publish_date: date | None = None
    is_preorder: bool = False
    raw_snippet: str | None = None


class EnrichedLead(BaseModel):
    book: AmazonBook
    linkedin_url: str | None = None
    linkedin_username: str | None = None
    full_name: str | None = None
    headline: str | None = None
    location: str | None = None
    company: str | None = None
    follower_count: int | None = None
    profession_tier: ProfessionTier = ProfessionTier.NONE
    profile_text_blob: str = ""
    fuzzy_title_score: float = 0.0
    contact_email: str | None = None
    contact_website: str | None = None
    contact_method: str | None = None
    raw_profile: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class VerifiedLead(BaseModel):
    enriched: EnrichedLead
    amazon_review_verified: bool = True
    linkedin_verified: bool = True
    contact_verified: bool = True
    dedupe_cluster_id: str | None = None


class ScoredLead(BaseModel):
    verified: VerifiedLead
    score: int = 0
    confidence_score: float = 0.0
    notes: str = ""
