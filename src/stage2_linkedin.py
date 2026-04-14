"""Stage 2: LinkdAPI enrichment + paying-capacity analysis (no legacy filters)."""

from __future__ import annotations

import re
from typing import Any

import structlog

from src.config import get_settings_bundle, load_env_settings, merge_keys_into_bundle
from src.models import AmazonBook, EnrichedLead, ProfessionTier
from src.run_report import get_report
from src.utils.fuzzy_match import book_title_in_profile
from src.utils.linkdapi_client import LinkdAPIWrapper
from src.utils.paying_capacity import analyze_paying_capacity

log = structlog.get_logger(__name__)


def _sanitize_author_for_search(raw: str) -> str:
    s = raw.strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return (parts[0] if parts else "", "")


def _classify_profession_tier(headline: str | None) -> ProfessionTier:
    if not headline:
        return ProfessionTier.NONE
    h = headline.lower()
    if any(x in h for x in ("ceo", "founder", "co-founder", "cofounder", "investor", "chief executive")):
        return ProfessionTier.CEO_FOUNDER_INVESTOR
    if any(
        x in h
        for x in (
            "cto",
            "vp ",
            "vice president",
            "doctor",
            "physician",
            "engineer",
            "chief technology",
        )
    ):
        return ProfessionTier.VP_CTO_DOCTOR_ENGINEER
    if "dr." in h or h.startswith("dr "):
        return ProfessionTier.VP_CTO_DOCTOR_ENGINEER
    if any(x in h for x in ("business owner", "president", "owner")):
        return ProfessionTier.OTHER_HIGH_INCOME
    return ProfessionTier.NONE


def _normalize_search_people(resp: Any) -> list[dict[str, Any]]:
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if isinstance(data, dict):
        people = data.get("people") or data.get("elements")
        if isinstance(people, list):
            return [p for p in people if isinstance(p, dict)]
    people = resp.get("people") or resp.get("elements")
    if isinstance(people, list):
        return [p for p in people if isinstance(p, dict)]
    return []


def _username_from_hit(hit: dict[str, Any]) -> str | None:
    for key in ("publicIdentifier", "username", "vanityName", "profileId"):
        v = hit.get(key)
        if isinstance(v, str) and v and " " not in v:
            return v.split("/")[-1]
    nav = hit.get("navigationUrl") or hit.get("link") or ""
    if isinstance(nav, str) and "/in/" in nav:
        m = re.search(r"/in/([^/?#]+)/?", nav)
        if m:
            return m.group(1)
    return None


def _headline_from_hit(hit: dict[str, Any]) -> str | None:
    for key in ("headline", "title", "subtitle", "primarySubtitle"):
        v = hit.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _flatten_profile_for_fuzzy(profile: Any) -> str:
    parts: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, str) and len(x.strip()) > 2:
            parts.append(x.strip())
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(profile)
    return "\n".join(parts)[:12000]


def _extract_contact(profile: Any) -> tuple[str | None, str | None]:
    email: str | None = None
    website: str | None = None
    if isinstance(profile, dict):
        ci = profile.get("contactInfo") or profile.get("contact_info") or {}
        if isinstance(ci, dict):
            email = ci.get("email") or ci.get("primaryEmail")
            website = ci.get("website") or ci.get("url")
        email = email or profile.get("email")
        website = website or profile.get("website")
    return email, website


def _extract_location_company(profile: Any, full: dict[str, Any] | None) -> tuple[str | None, str | None]:
    loc: str | None = None
    company: str | None = None
    src = full if isinstance(full, dict) else profile
    if isinstance(src, dict):
        loc = (
            src.get("location")
            or src.get("locationName")
            or (src.get("geoLocationName") if isinstance(src.get("geoLocationName"), str) else None)
        )
        if isinstance(loc, dict):
            loc = loc.get("name") or loc.get("defaultLocalizedName")
        company = src.get("company") or src.get("currentCompany")
        exp = src.get("experience") or src.get("positions") or []
        if not company and isinstance(exp, list) and exp:
            first = exp[0]
            if isinstance(first, dict):
                company = first.get("companyName") or first.get("title")
    if isinstance(company, dict):
        company = company.get("name")
    return (str(loc) if loc else None, str(company) if company else None)


async def _followers_async(api: LinkdAPIWrapper, username: str) -> int | None:
    try:
        sm = await api.get_social_matrix(username)
        if isinstance(sm, dict):
            data = sm.get("data", sm)
            for k in ("followersCount", "followers", "follower_count"):
                v = data.get(k) if isinstance(data, dict) else None
                if isinstance(v, int):
                    return v
    except Exception as e:
        log.debug("social_matrix_failed", username=username, error=str(e))
    return None


async def enrich_authors(raw_books: list[AmazonBook]) -> list[EnrichedLead]:
    bundle = merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    geo = bundle.linkdapi.geo_urn_usa
    report = get_report()
    api = LinkdAPIWrapper(bundle.linkdapi.api_key)
    out: list[EnrichedLead] = []

    for book in raw_books:
        primary_author_raw = book.authors[0] if book.authors else "Unknown"
        primary_author = _sanitize_author_for_search(primary_author_raw)
        first, last = _split_name(primary_author)
        try:
            resp = await api.search_people(
                first_name=first or None,
                last_name=last or None,
                geo_urn=geo,
                keyword=book.title[:80],
                start=0,
            )
        except Exception as e:
            log.warning("search_people_failed", author=primary_author, error=str(e))
            report.record_discard("stage2", "search_failed", primary_author)
            continue

        hits = _normalize_search_people(resp)
        if not hits:
            report.record_discard("stage2", "no_search_hits", primary_author)
            continue

        best: tuple[float, float, str | None, str | None, dict[str, Any]] | None = None
        for hit in hits[:8]:
            headline = _headline_from_hit(hit)
            username = _username_from_hit(hit)
            if not username:
                continue
            try:
                full = await api.get_full_profile(username=username)
            except Exception as e:
                log.warning("get_full_profile_failed", username=username, error=str(e))
                report.record_discard("stage2", "profile_fetch_failed", username)
                continue

            profile_obj = full
            if isinstance(full, dict) and "data" in full:
                profile_obj = full["data"]

            blob = _flatten_profile_for_fuzzy(profile_obj)
            title_match = book_title_in_profile(book.title, blob)
            loc, company = _extract_location_company(profile_obj, full if isinstance(full, dict) else None)
            followers = await _followers_async(api, username)
            tier = _classify_profession_tier(headline)

            pay = analyze_paying_capacity(
                headline=headline,
                profile_blob=blob,
                company=company,
                follower_count=followers,
                profession_tier=tier,
            )

            email, website = _extract_contact(profile_obj)
            method = "email" if email else ("website" if website else "linkedin_dm")

            meta = {
                "profile": profile_obj,
                "full_response": full,
                "location": loc,
                "company": company,
                "followers": followers,
                "email": email,
                "website": website,
                "method": method,
                "tier": tier,
                "blob": blob,
                "pay": pay,
                "title_match": title_match,
            }
            cand = (pay.score, title_match, username, headline, meta)
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] > best[1]):
                best = cand

        if best is None:
            report.record_discard("stage2", "no_profile_for_hits", primary_author)
            continue

        _pay_score, _title_match, username, headline, meta = best
        pay = meta["pay"]
        notes_parts = pay.factors + [f"title_match={meta['title_match']:.0f}"]
        out.append(
            EnrichedLead(
                book=book,
                linkedin_url=f"https://www.linkedin.com/in/{username}/",
                linkedin_username=username,
                full_name=primary_author_raw,
                headline=headline,
                location=meta.get("location"),
                company=meta.get("company"),
                follower_count=meta.get("followers"),
                profession_tier=meta.get("tier") or ProfessionTier.NONE,
                profile_text_blob=str(meta.get("blob") or ""),
                fuzzy_title_score=float(meta["title_match"]),
                contact_email=meta.get("email"),
                contact_website=meta.get("website"),
                contact_method=str(meta.get("method")),
                raw_profile=meta.get("full_response")
                if isinstance(meta.get("full_response"), dict)
                else {},
                notes="; ".join(notes_parts),
                paying_capacity_score=pay.score,
                paying_capacity_tier=pay.tier,
                paying_capacity_summary=pay.summary,
            )
        )

    log.info("stage2_complete", total=len(out))
    report.set_stage_count("stage2_linkedin", len(out))
    return out
