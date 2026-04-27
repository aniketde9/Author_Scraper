"""Stage 2.1: contact email/website from LinkedIn profile context + Truth Reactor.

Runs after Stage 2. Reads/writes ``data/linkedin_matched.json``. Uses position
descriptions (emails), company slug heuristics with MX check, LinkdAPI contact
fields when present, and ``generate_personal_guesses`` + SMTP verification.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import structlog

from src.config import EmailEnrichmentConfig, SettingsBundle, load_env_settings, merge_keys_into_bundle, get_settings_bundle
from src.email_verification.map_status import map_verification_to_scraped
from src.email_verification.truth_reactor import EmailTruthReactor
from src.email_verification.types import VerificationResult
from src.models import EnrichedLead
from src.run_report import get_report
from src.stage2_linkedin import _extract_contact
from src.utils.email_public import generate_personal_guesses, is_generic_localpart, sanitize_author_name_for_guessing
from src.utils.linkedin_profile_domains import blocked_domain, collect_candidate_domains

log = structlog.get_logger(__name__)


def _profile_data(raw: dict[str, Any]) -> dict[str, Any]:
    d = raw.get("data")
    return d if isinstance(d, dict) else {}


def _merge_reactor_into_book_fields(r: VerificationResult) -> dict[str, Any]:
    st, reason = map_verification_to_scraped(r)
    return {
        "scraped_email_status": st,
        "scraped_email_reason": reason,
        "scraped_email_confidence": r.confidence,
        "scraped_email_confidence_range": r.confidence_range,
        "scraped_email_reactor_category": r.category or "",
        "scraped_email_smtp_ok": r.smtp_accepts,
    }


def _blocked_email(email: str) -> bool:
    if not email or "@" not in email:
        return True
    dom = email.split("@", 1)[1].strip().lower()
    return blocked_domain(dom)


def _category_rank(cat: str | None) -> int:
    return {"deliverable": 4, "risky": 3, "unknown": 2, "invalid": 1}.get(str(cat or ""), 0)


def _better_result(a: VerificationResult, b: VerificationResult) -> bool:
    """True if ``a`` should replace ``b`` as the best candidate."""
    ca, cb = _category_rank(a.category), _category_rank(b.category)
    if ca != cb:
        return ca > cb
    return float(a.confidence or 0) > float(b.confidence or 0)


async def _verify_best(
    reactor: EmailTruthReactor | None,
    candidates: list[str],
    cfg: EmailEnrichmentConfig,
) -> tuple[VerificationResult | None, str | None, int]:
    """Pick best verified candidate; cap SMTP probes; early exit on strong deliverable."""
    if reactor is None or not candidates:
        return None, None, 0
    best_r: VerificationResult | None = None
    best_email: str | None = None
    smtp_used = 0
    max_smtp = max(1, cfg.stage21_max_smtp_probes_per_lead)
    do_smtp = bool(cfg.smtp_verification_enabled)

    def probe(email: str) -> VerificationResult:
        return reactor.verify_safe(email, do_smtp=do_smtp)

    for c in candidates:
        if smtp_used >= max_smtp:
            break
        r = await asyncio.to_thread(probe, c)
        smtp_used += 1
        if best_r is None or _better_result(r, best_r):
            best_r, best_email = r, c
        if (
            do_smtp
            and best_r
            and str(best_r.category) == "deliverable"
            and float(best_r.confidence or 0) >= 88.0
        ):
            break
    return best_r, best_email, smtp_used


def _website_hint(email: str | None, api_site: str | None, domains: list[str]) -> str | None:
    if api_site and urlparse(api_site).scheme in ("http", "https"):
        return api_site
    if email and "@" in email:
        dom = email.split("@", 1)[1].strip().lower()
        if dom and not blocked_domain(dom):
            return f"https://{dom}"
    if domains:
        return f"https://{domains[0]}"
    return None


def _append_note(notes: str | None, fragment: str) -> str:
    base = (notes or "").strip()
    frag = fragment.strip()
    if not frag:
        return base
    if not base:
        return frag
    return f"{base}; {frag}"


async def _process_one_lead(
    lead: EnrichedLead,
    reactor: EmailTruthReactor | None,
    cfg: EmailEnrichmentConfig,
    sem: asyncio.Semaphore,
) -> EnrichedLead:
    async with sem:
        raw = lead.raw_profile if isinstance(lead.raw_profile, dict) else {}
        pd = _profile_data(raw)
        profile_obj = pd if pd else raw

        api_email, api_website = (None, None)
        if isinstance(profile_obj, dict):
            api_email, api_website = _extract_contact(profile_obj)

        author_raw = (lead.book.authors[0] if lead.book.authors else "") or (lead.full_name or "")
        author_raw = author_raw.strip()
        clean = sanitize_author_name_for_guessing(author_raw) or author_raw
        full_name = (lead.full_name or author_raw).strip()

        domains, company_used, dom_source = collect_candidate_domains(pd, full_name)

        candidates: list[str] = []
        if api_email:
            e = api_email.strip().lower()
            if e and not is_generic_localpart(e) and not _blocked_email(e):
                candidates.append(e)

        if cfg.use_domain_guess:
            for dom in domains:
                for g in generate_personal_guesses(clean, dom):
                    if g not in candidates:
                        candidates.append(g)

        # Cap list length before SMTP (guesses can explode across TLDs)
        max_list = max(40, cfg.stage21_max_smtp_probes_per_lead * 4)
        candidates = candidates[:max_list]

        prior_email = (lead.contact_email or "").strip()
        if prior_email and prior_email not in candidates and not _blocked_email(prior_email):
            candidates.insert(0, prior_email)

        if not candidates:
            frag = "stage21_email=no_li_domains" + (
                f"({dom_source})" if dom_source else ""
            ) + (f"; company={company_used}" if company_used else "")
            return lead.model_copy(update={"notes": _append_note(lead.notes, frag)})

        best_r, best_email, smtp_n = await _verify_best(reactor, candidates, cfg)

        if best_r is None or not best_email:
            return lead.model_copy(
                update={"notes": _append_note(lead.notes, "stage21_email=reactor_unavailable_or_empty")}
            )

        merged = _merge_reactor_into_book_fields(best_r)
        st = merged["scraped_email_status"]
        book_updates: dict[str, Any] = {
            "scraped_public_email": best_email,
            **merged,
            "scraped_email_sources": [f"stage21:{dom_source}", f"company={company_used}"]
            if company_used
            else [f"stage21:{dom_source}"],
        }
        site = _website_hint(best_email, api_website, domains)
        if site:
            book_updates["scraped_public_website"] = site

        new_book = lead.book.model_copy(update=book_updates)
        method = "linkedin_api_email" if api_email and best_email == api_email.strip().lower() else "linkedin_company_guess"
        frag = (
            f"stage21_email={best_email}({st.value if hasattr(st, 'value') else st}); "
            f"stage21_smtp_probes={smtp_n}; stage21_domain_source={dom_source}; stage21_contact={method}"
        )
        return lead.model_copy(
            update={
                "book": new_book,
                "contact_email": best_email,
                "contact_website": site or lead.contact_website,
                "contact_method": method,
                "notes": _append_note(lead.notes, frag),
            }
        )


async def enrich_leads_from_linkedin(
    leads: list[EnrichedLead],
    bundle: SettingsBundle | None = None,
) -> list[EnrichedLead]:
    bundle = bundle or merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    cfg = bundle.email_enrichment
    report = get_report()

    if not cfg.enabled:
        log.info("stage21_disabled")
        return leads
    if not leads:
        log.info("stage21_no_leads")
        return leads

    reactor: EmailTruthReactor | None = None
    try:
        reactor = EmailTruthReactor.from_enrichment_config(cfg)
    except Exception as e:
        log.warning("stage21_reactor_init_failed", error=str(e))

    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    tasks = [_process_one_lead(lead, reactor, cfg, sem) for lead in leads]
    out = await asyncio.gather(*tasks)

    found = sum(1 for x in out if x.contact_email)
    log.info("stage21_complete", total=len(out), with_contact_email=found)
    report.set_stage_count("stage21_linkedin_email", len(out))
    return list(out)
