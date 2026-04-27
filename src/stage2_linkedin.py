"""Stage 2: Chromium search + LinkdAPI username confirmation."""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import Any

import structlog
from playwright.async_api import Browser, Page, async_playwright

from src.config import get_settings_bundle, load_env_settings, merge_keys_into_bundle
from src.models import AmazonBook, EnrichedLead, ProfessionTier
from src.run_report import get_report
from src.utils.fuzzy_match import book_title_in_profile
from src.utils.linkdapi_client import LinkdAPIWrapper
from src.utils.paying_capacity import analyze_paying_capacity
from src.utils.validators import email_domain_matches_website

log = structlog.get_logger(__name__)
LINKEDIN_IN_RE = re.compile(r"https?://(?:[\w.-]+\.)?linkedin\.com/in/([^/?#]+)", re.IGNORECASE)
GOOGLE_SEARCH_URL = "https://www.google.com/search?q={q}&hl=en&gl=us"


def _sanitize_author_for_search(raw: str) -> str:
    s = raw.strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    return re.sub(r"\s+", " ", s)


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 2]


def _has_name_overlap(author: str, candidate_text: str) -> bool:
    author_tokens = _name_tokens(author)
    if not author_tokens:
        return False
    text = candidate_text.lower()
    matched = sum(1 for t in author_tokens if t in text)
    return matched >= 2 or (len(author_tokens) == 1 and matched == 1)


def _is_captcha_or_block(page_text: str, page_url: str, page_title: str = "") -> bool:
    url_l = (page_url or "").lower()
    title_l = (page_title or "").lower()
    text_l = (page_text or "").lower()
    return (
        "/sorry/" in url_l
        or "google.com/sorry" in url_l
        or "unusual traffic" in title_l
        or "unusual traffic" in text_l
        or "automated queries" in text_l
        or "our systems have detected unusual traffic" in text_l
        or "recaptcha" in text_l
    )


async def _manual_captcha_wait(page: Page) -> bool:
    print(
        "\nGoogle CAPTCHA/verification detected.\n"
        "Solve it in the current Chromium window, then press ENTER here.\n"
        "This keeps the same tab/session alive.\n",
        flush=True,
    )
    await asyncio.to_thread(input, "Press ENTER to continue... ")
    await asyncio.sleep(1.0)
    html = await page.content()
    title = await page.title()
    return not _is_captcha_or_block(html, page.url, title)


def _resolve_google_result_href(href: str) -> str:
    if href.startswith("/url?"):
        parsed = urllib.parse.urlparse(href)
        q = urllib.parse.parse_qs(parsed.query).get("q", [None])[0]
        if isinstance(q, str) and q.strip():
            return urllib.parse.unquote(q)
    return href


async def _extract_google_linkedin_candidates(page: Page, max_results: int = 8) -> list[tuple[str, str]]:
    """Return (username, url) from SERP top results."""
    anchors = page.locator("a:has(h3)")
    count = await anchors.count()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for i in range(min(count, 20)):
        a = anchors.nth(i)
        href = (await a.get_attribute("href")) or ""
        resolved = _resolve_google_result_href(href.strip())
        m = LINKEDIN_IN_RE.search(resolved)
        if not m:
            continue
        username = m.group(1).strip().lower()
        if not username or username in seen:
            continue
        seen.add(username)
        out.append((username, resolved))
        if len(out) >= max_results:
            break
    return out


def _extract_profile_blob(profile_obj: Any) -> str:
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

    walk(profile_obj)
    return "\n".join(parts)[:12000]


def _extract_contact(profile_obj: Any) -> tuple[str | None, str | None]:
    if not isinstance(profile_obj, dict):
        return None, None
    ci = profile_obj.get("contactInfo") or profile_obj.get("contact_info") or {}
    email = None
    website = None
    if isinstance(ci, dict):
        email = ci.get("email") or ci.get("primaryEmail")
        website = ci.get("website") or ci.get("url")
    email = email or profile_obj.get("email")
    website = website or profile_obj.get("website")
    return email, website


def _extract_headline(profile_obj: Any) -> str | None:
    if not isinstance(profile_obj, dict):
        return None
    h = profile_obj.get("headline")
    return str(h).strip() if isinstance(h, str) and h.strip() else None


def _extract_location(profile_obj: Any) -> str | None:
    if not isinstance(profile_obj, dict):
        return None
    geo = profile_obj.get("geo")
    if isinstance(geo, dict):
        for key in ("full", "city", "country"):
            val = geo.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    loc = profile_obj.get("location") or profile_obj.get("locationName")
    return str(loc).strip() if isinstance(loc, str) and loc.strip() else None


def _extract_company(profile_obj: Any) -> str | None:
    if not isinstance(profile_obj, dict):
        return None
    cur = profile_obj.get("currentPositions")
    if isinstance(cur, list) and cur:
        first = cur[0]
        if isinstance(first, dict):
            name = first.get("companyName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    exp = profile_obj.get("experience")
    if isinstance(exp, list) and exp:
        first = exp[0]
        if isinstance(first, dict):
            name = first.get("companyName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


async def _open_browser() -> tuple[Browser, Page]:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = await browser.new_page(viewport={"width": 1600, "height": 1000})
    page.set_default_timeout(30000)
    await page.goto("https://www.google.com", wait_until="domcontentloaded")
    await asyncio.sleep(1.0)
    setattr(browser, "_pw_handle", pw)
    return browser, page


async def _close_browser(browser: Browser) -> None:
    pw = getattr(browser, "_pw_handle", None)
    await browser.close()
    if pw is not None:
        await pw.stop()


async def enrich_authors(raw_books: list[AmazonBook]) -> list[EnrichedLead]:
    bundle = merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    report = get_report()
    api = LinkdAPIWrapper(bundle.linkdapi.api_key)
    out: list[EnrichedLead] = []
    browser, page = await _open_browser()
    try:
        for book in raw_books:
            primary_author_raw = book.authors[0] if book.authors else "Unknown"
            primary_author = _sanitize_author_for_search(primary_author_raw)
            queries = [
                f"{primary_author} {book.title} linkedin",
                f"{primary_author} linkedin",
            ]
            chosen: EnrichedLead | None = None

            for query in queries:
                encoded = urllib.parse.quote_plus(query)
                await page.goto(GOOGLE_SEARCH_URL.format(q=encoded), wait_until="domcontentloaded")
                await asyncio.sleep(1.0)
                html = await page.content()
                title = await page.title()
                if _is_captcha_or_block(html, page.url, title):
                    cleared = await _manual_captcha_wait(page)
                    if not cleared:
                        report.record_discard("stage2", "captcha_not_resolved", primary_author)
                        break

                candidates = await _extract_google_linkedin_candidates(page, max_results=8)
                if not candidates:
                    continue

                for username, _url in candidates:
                    try:
                        full = await api.get_full_profile(username=username)
                    except Exception as e:
                        log.warning("profile_confirm_failed", username=username, error=str(e))
                        report.record_discard("stage2", "profile_confirm_failed", username)
                        continue

                    profile_obj = full["data"] if isinstance(full, dict) and isinstance(full.get("data"), dict) else full
                    if not isinstance(profile_obj, dict):
                        continue
                    profile_name = f"{profile_obj.get('firstName', '')} {profile_obj.get('lastName', '')}".strip()
                    headline = _extract_headline(profile_obj)
                    verification_text = f"{profile_name} {headline or ''} {username}"
                    if not _has_name_overlap(primary_author, verification_text):
                        continue

                    blob = _extract_profile_blob(profile_obj)
                    title_match = book_title_in_profile(book.title, blob)
                    email, website = _extract_contact(profile_obj)
                    contact_email = email or book.scraped_public_email
                    if email:
                        contact_website = website
                    else:
                        contact_website = book.scraped_public_website or website
                    if contact_email and contact_website and not email_domain_matches_website(
                        contact_email, contact_website
                    ):
                        contact_website = None
                    if email:
                        method = "email"
                    elif book.scraped_public_email:
                        method = "scraped_public_email"
                    elif website or book.scraped_public_website:
                        method = "website"
                    else:
                        method = "linkedin_dm"
                    company = _extract_company(profile_obj)
                    location = _extract_location(profile_obj)
                    follower_count = profile_obj.get("followerCount") if isinstance(profile_obj.get("followerCount"), int) else None

                    pay = analyze_paying_capacity(
                        headline=headline,
                        profile_blob=blob,
                        company=company,
                        follower_count=follower_count,
                        profession_tier=ProfessionTier.NONE,
                    )
                    notes_parts = pay.factors + [
                        f"title_match={title_match:.0f}",
                        "source=chromium_google_search+linkdapi_confirm",
                        f"query={query[:80]}",
                    ]
                    if book.scraped_public_email:
                        em_line = f"{book.scraped_public_email}({book.scraped_email_status})"
                        if book.scraped_email_confidence is not None:
                            em_line += f" conf={book.scraped_email_confidence}"
                        if book.scraped_email_reactor_category:
                            em_line += f" cat={book.scraped_email_reactor_category}"
                        notes_parts.append(f"book_scraped_email={em_line}")
                    chosen = EnrichedLead(
                        book=book,
                        linkedin_url=f"https://www.linkedin.com/in/{username}/",
                        linkedin_username=username,
                        full_name=primary_author_raw,
                        headline=headline,
                        location=location,
                        company=company,
                        follower_count=follower_count,
                        profession_tier=ProfessionTier.NONE,
                        profile_text_blob=blob,
                        fuzzy_title_score=float(title_match),
                        contact_email=contact_email,
                        contact_website=contact_website,
                        contact_method=method,
                        raw_profile=full if isinstance(full, dict) else {},
                        notes="; ".join(notes_parts),
                        paying_capacity_score=pay.score,
                        paying_capacity_tier=pay.tier,
                        paying_capacity_summary=pay.summary,
                    )
                    break
                if chosen is not None:
                    break

            if chosen is None:
                report.record_discard("stage2", "no_verified_linkedin_hit", primary_author)
                continue
            out.append(chosen)
            await asyncio.sleep(0.7)
    finally:
        await _close_browser(browser)

    log.info("stage2_complete", total=len(out))
    report.set_stage_count("stage2_linkedin", len(out))
    return out
