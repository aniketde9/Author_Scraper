"""Stage 2.5: compile Stage 2 leads into scoring-ready artifacts."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import structlog

from src.models import EnrichedLead, VerifiedLead
from src.utils.checkpoint import DATA_DIR, save_list

log = structlog.get_logger(__name__)

VERIFIED_LEADS_STAGE25 = DATA_DIR / "verified_leads_stage25.json"
SCORING_INPUT_STAGE25_CSV = DATA_DIR / "scoring_input_stage25.csv"


def _to_verified(leads: list[EnrichedLead]) -> list[VerifiedLead]:
    out: list[VerifiedLead] = []
    for e in leads:
        out.append(
            VerifiedLead(
                enriched=e,
                amazon_review_verified=e.book.review_count is not None,
                linkedin_verified=bool((e.linkedin_url or "").strip()),
                contact_verified=True,
            )
        )
    return out


def _write_flat_csv(verified: list[VerifiedLead], output_csv: Path) -> None:
    rows: list[dict[str, object]] = []
    for vl in verified:
        e = vl.enriched
        b = e.book
        rows.append(
            {
                "asin": b.asin,
                "book_title": b.title,
                "authors": "; ".join(b.authors),
                "author_name": e.full_name or "",
                "linkedin_url": e.linkedin_url or "",
                "linkedin_username": e.linkedin_username or "",
                "headline": e.headline or "",
                "location": e.location or "",
                "company": e.company or "",
                "review_count": b.review_count if b.review_count is not None else "",
                "publish_date": b.publish_date.isoformat() if b.publish_date else "",
                "follower_count": e.follower_count if e.follower_count is not None else "",
                "profession_tier": e.profession_tier.value if hasattr(e.profession_tier, "value") else str(e.profession_tier),
                "fuzzy_title_score": e.fuzzy_title_score,
                "paying_capacity_score": e.paying_capacity_score,
                "paying_capacity_tier": e.paying_capacity_tier,
                "paying_capacity_summary": e.paying_capacity_summary,
                "contact_email": e.contact_email or "",
                "contact_website": e.contact_website or "",
                "contact_method": e.contact_method or "",
                "notes": e.notes or "",
                "amazon_review_verified": vl.amazon_review_verified,
                "linkedin_verified": vl.linkedin_verified,
                "contact_verified": vl.contact_verified,
            }
        )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output_csv, index=False, encoding="utf-8")


def compile_stage25_inputs(
    enriched: list[EnrichedLead],
    *,
    verified_output: Path = VERIFIED_LEADS_STAGE25,
    csv_output: Path = SCORING_INPUT_STAGE25_CSV,
) -> list[VerifiedLead]:
    """Compile Stage 2 output into scoring-friendly files."""
    verified = _to_verified(enriched)
    save_list(verified_output, verified)
    _write_flat_csv(verified, csv_output)
    log.info(
        "stage25_complete",
        in_count=len(enriched),
        verified_json=str(verified_output),
        csv=str(csv_output),
    )
    return verified

