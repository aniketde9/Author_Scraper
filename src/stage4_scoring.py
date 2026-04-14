"""Stage 4: Score, rank, export CSV."""

from __future__ import annotations

import pandas as pd
import structlog

from src.config import ScoringConfig, load_env_settings, merge_keys_into_bundle, get_settings_bundle
from src.models import ProfessionTier, ScoredLead, VerifiedLead, VisibilityBand
from src.run_report import get_report
from src.utils.checkpoint import DATA_DIR, LEADS_FINAL_CSV

log = structlog.get_logger(__name__)


def _visibility_band(followers: int | None) -> VisibilityBand:
    if followers is None:
        return VisibilityBand.MEDIUM
    if followers < 1000:
        return VisibilityBand.LOW
    if followers < 5000:
        return VisibilityBand.MEDIUM
    return VisibilityBand.HIGH


def score_lead(vl: VerifiedLead, scoring: ScoringConfig) -> tuple[int, float, str]:
    e = vl.enriched
    notes: list[str] = []
    total = 0

    tier = e.profession_tier
    if tier == ProfessionTier.CEO_FOUNDER_INVESTOR:
        total += scoring.profession.ceo_founder_investor
    elif tier == ProfessionTier.VP_CTO_DOCTOR_ENGINEER:
        total += scoring.profession.vp_cto_doctor_engineer
    elif tier == ProfessionTier.OTHER_HIGH_INCOME:
        total += scoring.profession.other_high_income
    else:
        notes.append("profession_tier_low")

    rc = e.book.review_count
    if rc is not None and 10 <= rc <= 20:
        total += scoring.reviews.band_10_20
    elif rc is not None and 21 <= rc <= 40:
        total += scoring.reviews.band_21_40

    if e.location:
        total += scoring.location

    band = _visibility_band(e.follower_count)
    total += getattr(scoring.visibility, band.value)

    if e.paying_capacity_score >= 55.0:
        total += 3
        notes.append("paying_capacity_high")
    elif e.paying_capacity_score >= 30.0:
        total += 1
        notes.append("paying_capacity_mid")

    total = min(total, 8)

    conf = min(
        100.0,
        e.fuzzy_title_score
        + (e.paying_capacity_score * 0.25)
        + (20.0 if vl.amazon_review_verified else 0.0)
        + (15.0 if vl.linkedin_verified else 0.0)
        + (10.0 if vl.contact_verified else 0.0)
        + (5.0 if e.contact_email else 0.0)
        + (3.0 if e.contact_website else 0.0),
    )
    return total, conf, "; ".join(notes) if notes else ""


def score_and_export(verified: list[VerifiedLead]) -> list[ScoredLead]:
    bundle = merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    scoring = bundle.scoring
    min_keep = bundle.pipeline.min_score_to_keep
    report = get_report()

    scored: list[ScoredLead] = []
    for vl in verified:
        s, conf, n = score_lead(vl, scoring)
        if s < min_keep:
            report.record_discard("stage4", "below_min_score", f"score={s}")
            continue
        scored.append(ScoredLead(verified=vl, score=s, confidence_score=conf, notes=n))

    scored.sort(key=lambda x: (-x.score, -x.confidence_score))
    top = scored[:10]

    rows = []
    for sl in top:
        e = sl.verified.enriched
        b = e.book
        email_or = e.contact_email or e.contact_website or ""
        rows.append(
            {
                "full_name": e.full_name or "",
                "linkedin_url": e.linkedin_url or "",
                "profession": e.headline or "",
                "company": e.company or "",
                "book_title": b.title,
                "amazon_url": b.amazon_url,
                "review_count": b.review_count if b.review_count is not None else "",
                "publish_date": b.publish_date.isoformat() if b.publish_date else "",
                "contact_method": e.contact_method or "",
                "email_or_website": email_or,
                "follower_count": e.follower_count if e.follower_count is not None else "",
                "paying_capacity_score": e.paying_capacity_score,
                "paying_capacity_tier": e.paying_capacity_tier,
                "paying_capacity_summary": e.paying_capacity_summary,
                "score": sl.score,
                "confidence_score": round(sl.confidence_score, 2),
                "notes": sl.notes,
            }
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(LEADS_FINAL_CSV, index=False, encoding="utf-8")

    log.info("stage4_complete", exported=len(top), csv=str(LEADS_FINAL_CSV))
    report.set_stage_count("stage4_csv", len(top))
    return top
