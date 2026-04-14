"""Stage 3: Re-validate URLs, contacts, dedupe."""

from __future__ import annotations

import structlog

from src.config import load_env_settings, merge_keys_into_bundle, get_settings_bundle
from src.models import EnrichedLead, VerifiedLead
from src.run_report import get_report
from src.stage1_amazon import parse_product_detail
from src.utils.fuzzy_match import cluster_dedupe_rows, composite_dedupe_key
from src.utils.playwright_amazon import fetch_amazon_html_sync
from src.utils.validators import (
    check_url_reachable,
    email_domain_matches_website,
    is_valid_email,
)

log = structlog.get_logger(__name__)

REVIEW_DRIFT = 1


def _review_ok(
    count: int | None,
    pool: str,
    r_a: tuple[int, int],
    r_b: tuple[int, int],
) -> bool:
    if count is None:
        return False
    if pool == "A":
        lo, hi = r_a
        return lo - REVIEW_DRIFT <= count <= hi + REVIEW_DRIFT
    lo, hi = r_b
    return lo - REVIEW_DRIFT <= count <= hi + REVIEW_DRIFT


def validate_and_dedupe(leads: list[EnrichedLead]) -> list[VerifiedLead]:
    bundle = merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    amz_cfg = bundle.amazon_scraper
    r_a = bundle.pipeline.review_threshold_a
    r_b = bundle.pipeline.review_threshold_b
    report = get_report()
    verified: list[VerifiedLead] = []

    for lead in leads:
        book = lead.book
        pool = book.pool.value
        amazon_ok = True
        try:
            html = fetch_amazon_html_sync(amz_cfg, book.amazon_url)
            _pub, _pre, rc = parse_product_detail(html)
            if not _review_ok(rc, pool, r_a, r_b):
                amazon_ok = False
                report.record_discard("stage3", "amazon_review_drift", book.asin)
        except Exception as e:
            log.warning("amazon_revalidate_failed", asin=book.asin, error=str(e))
            amazon_ok = False
            report.record_discard("stage3", "amazon_fetch_failed", book.asin)

        linkedin_ok = True
        if lead.linkedin_url:
            linkedin_ok = check_url_reachable(lead.linkedin_url)
        if not linkedin_ok:
            report.record_discard("stage3", "linkedin_unreachable", lead.linkedin_username or "")

        contact_ok = True
        if lead.contact_email:
            if not is_valid_email(lead.contact_email):
                report.record_discard("stage3", "bad_email", lead.contact_email)
                continue
        if lead.contact_email and lead.contact_website:
            if not email_domain_matches_website(lead.contact_email, lead.contact_website):
                report.record_discard("stage3", "email_domain_mismatch", lead.full_name or "")
                continue

        if not (amazon_ok and linkedin_ok):
            continue

        verified.append(
            VerifiedLead(
                enriched=lead,
                amazon_review_verified=amazon_ok,
                linkedin_verified=linkedin_ok,
                contact_verified=contact_ok,
            )
        )

    rows: list[tuple[str, float]] = []
    for vl in verified:
        e = vl.enriched
        name = e.full_name or ""
        company = e.company or ""
        title = e.book.title
        key = composite_dedupe_key(name, company, title)
        prio = e.fuzzy_title_score + (10.0 if vl.linkedin_verified else 0.0)
        rows.append((key, prio))

    mapping = cluster_dedupe_rows(rows, threshold=92.0)
    best_by_canon: dict[str, VerifiedLead] = {}
    for vl in verified:
        e = vl.enriched
        key = composite_dedupe_key(e.full_name or "", e.company or "", e.book.title)
        canon = mapping.get(key, key)
        prev = best_by_canon.get(canon)
        if prev is None or e.fuzzy_title_score > prev.enriched.fuzzy_title_score:
            best_by_canon[canon] = vl

    deduped = []
    for canon, vl in best_by_canon.items():
        vl.dedupe_cluster_id = canon
        deduped.append(vl)

    log.info("stage3_complete", in_count=len(leads), verified=len(verified), deduped=len(deduped))
    report.set_stage_count("stage3_verified", len(deduped))
    for _ in range(len(verified) - len(deduped)):
        report.record_discard("stage3", "deduped", "")
    return deduped
