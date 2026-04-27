"""Legacy Stage 1.5: Amazon/DDG email crawl (unused by CLI).

Contact enrichment runs as **Stage 2.1** after LinkedIn enrichment; see ``stage21_linkedin_email.py``.
This module is kept for reference or ad-hoc imports.
"""

from __future__ import annotations

import asyncio
import random
import re
from urllib.parse import urljoin, urlparse

import structlog
from bs4 import BeautifulSoup
from playwright.async_api import Browser, Page, async_playwright

from src.config import EmailEnrichmentConfig, SettingsBundle, load_env_settings, merge_keys_into_bundle, get_settings_bundle
from src.email_verification.map_status import map_verification_to_scraped
from src.email_verification.truth_reactor import EmailTruthReactor
from src.email_verification.types import VerificationResult
from src.models import AmazonBook, ScrapedEmailStatus
from src.run_report import get_report
from src.utils.email_public import (
    email_domain_matches_site,
    extract_emails_from_text,
    generate_personal_guesses,
    hostname_from_url,
    is_generic_localpart,
    sanitize_author_name_for_guessing,
)

log = structlog.get_logger(__name__)

AMAZON_HOST_FRAGMENTS = ("amazon.", "amzn.")
SKIP_CRAWL_HOST_FRAGMENTS = (
    "amazon.",
    "amzn.",
    "qualtrics.",
    "audible.",
    "twitter.",
    "x.com",
    "linkedin.",
    "instagram.",
    "youtube.",
    "youtu.be",
    "pinterest.",
    "reddit.",
    "wikipedia.org",
    "goodreads.com",
    "bloomberg.",
    "nytimes.",
)


def _should_skip_crawl_host(url: str) -> bool:
    h = (hostname_from_url(url) or "").lower()
    if not h:
        return True
    return any(x in h for x in SKIP_CRAWL_HOST_FRAGMENTS)


def _is_amazon_url(url: str) -> bool:
    h = (hostname_from_url(url) or "").lower()
    return any(x in h for x in AMAZON_HOST_FRAGMENTS)


def _is_blocked_guess_domain(domain: str | None) -> bool:
    d = (domain or "").strip().lower().removeprefix("www.")
    if not d:
        return True
    blocked = ("amazon.", "amzn.", "qualtrics.", "audible.")
    return any(x in d for x in blocked)


def _jitter(cfg: EmailEnrichmentConfig) -> float:
    return random.uniform(cfg.delay_seconds_min, cfg.delay_seconds_max)


def _gather_urls_from_html(html: str, base_url: str, max_links: int) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(base_url, href)
        if full not in seen:
            seen.add(full)
            urls.append(full)
        if len(urls) >= max_links:
            break
    return urls


def _find_author_page_url(html: str, base_book_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        low = href.lower()
        if "/author/" in low or ("/e/" in low and "amazon" in low):
            full = urljoin(base_book_url, href)
            if _is_amazon_url(full):
                return full
        if "author.amazon" in low:
            return href if href.startswith("http") else urljoin(base_book_url, href)
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        full = urljoin(base_book_url, href)
        if _is_amazon_url(full) and re.search(r"/e/[A-Z0-9]", href, re.I):
            return full
    return None


def _find_external_website_url(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = (a.get_text() or "").strip().lower()
        if not href or href.startswith("mailto:"):
            continue
        full = urljoin(page_url, href)
        if _is_amazon_url(full) or _should_skip_crawl_host(full):
            continue
        score = 0
        for kw in ("website", "official", "homepage", "blog", "visit", "my site"):
            if kw in text:
                score += 3
        if urlparse(full).scheme in ("http", "https"):
            score += 1
        if score > 0:
            candidates.append((score, full))
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1] if candidates else None


async def _fetch_html(page: Page, url: str, cfg: EmailEnrichmentConfig) -> str:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.navigation_timeout_ms)
        await asyncio.sleep(_jitter(cfg) * 0.25)
        return await page.content()
    except Exception as e:
        log.warning("stage15_fetch_failed", url=url, error=str(e))
        return ""


def _emails_from_pages(url_html_pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    by_email: dict[str, list[str]] = {}
    for url, html in url_html_pairs:
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=False)
        for e in extract_emails_from_text(text):
            by_email.setdefault(e, []).append(url)
    return by_email


def _pick_best_non_generic_email(
    by_email: dict[str, list[str]],
    official_site: str | None,
) -> tuple[str | None, list[str]]:
    filtered = {e: v for e, v in by_email.items() if not is_generic_localpart(e)}
    return _pick_best_email(filtered, official_site)


def _pick_best_email(
    by_email: dict[str, list[str]],
    official_site: str | None,
) -> tuple[str | None, list[str]]:
    if not by_email:
        return None, []
    scored: list[tuple[tuple[int, int, str], str, list[str]]] = []
    for e, srcs in by_email.items():
        uniq = list(dict.fromkeys(srcs))
        site_bonus = (
            100
            if official_site
            and (email_domain_matches_site(e, official_site) or any(email_domain_matches_site(e, s) for s in uniq))
            else 0
        )
        scored.append(((site_bonus, len(uniq), e), e, uniq))
    scored.sort(key=lambda x: x[0], reverse=True)
    _, best, uniq = scored[0]
    return best, uniq


def _reactor_field_defaults() -> dict:
    return {
        "scraped_email_confidence": None,
        "scraped_email_confidence_range": "",
        "scraped_email_reactor_category": "",
        "scraped_email_smtp_ok": None,
    }


def _merge_reactor_result(r: VerificationResult) -> dict:
    st, reason = map_verification_to_scraped(r)
    return {
        "scraped_email_status": st,
        "scraped_email_reason": reason,
        "scraped_email_confidence": r.confidence,
        "scraped_email_confidence_range": r.confidence_range,
        "scraped_email_reactor_category": r.category,
        "scraped_email_smtp_ok": r.smtp_accepts,
    }


async def _run_reactor(
    reactor: EmailTruthReactor | None,
    email: str,
    cfg: EmailEnrichmentConfig,
) -> VerificationResult:
    if reactor is None:
        return VerificationResult(
            input=email,
            skipped=True,
            skip_reason="reactor_unavailable",
            category="unknown",
            final_label="Reactor not initialized",
        )
    do_smtp = bool(cfg.smtp_verification_enabled)

    def _go() -> VerificationResult:
        return reactor.verify_safe(email, do_smtp=do_smtp)

    return await asyncio.to_thread(_go)


def _registrable_domain_from_site(site_url: str | None) -> str | None:
    h = hostname_from_url(site_url or "")
    if not h:
        return None
    d = h.removeprefix("www.")
    return None if _is_blocked_guess_domain(d) else d


async def _try_guessed_emails(
    author_name: str,
    domain: str,
    cfg: EmailEnrichmentConfig,
) -> str | None:
    """Return first pattern where a DDG text search returns any hit (conservative confirmation)."""
    try:
        from ddgs import DDGS
    except ImportError:
        log.warning("stage15_ddgs_missing", hint="pip install ddgs")
        return None
    guesses = generate_personal_guesses(author_name, domain)[: cfg.guess_max_patterns]
    if not guesses:
        return None
    display_name = (author_name or "").strip()
    for guess in guesses:
        q = f'"{guess}" {display_name}'
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(q, max_results=cfg.guess_max_ddg_results))
            if results:
                return guess
        except Exception as e:
            log.warning("stage15_guess_ddg_failed", error=str(e))
        await asyncio.sleep(random.uniform(1.0, 2.5))
    return None


def _ddg_search_urls(author_name: str, cfg: EmailEnrichmentConfig) -> list[str]:
    try:
        from ddgs import DDGS
    except ImportError:
        log.warning("stage15_ddgs_missing", hint="pip install ddgs")
        return []
    q = f'"{author_name}" (contact OR email OR website OR about)'
    out: list[str] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(q, max_results=cfg.max_ddg_results):
                href = (r.get("href") or r.get("url") or "").strip()
                if href and not _should_skip_crawl_host(href):
                    out.append(href)
    except Exception as e:
        log.warning("stage15_ddg_failed", error=str(e))
    return out[: cfg.max_ddg_results]


async def _process_one_book(
    book: AmazonBook,
    browser: Browser,
    cfg: EmailEnrichmentConfig,
    sem: asyncio.Semaphore,
    reactor: EmailTruthReactor | None,
) -> AmazonBook:
    report = get_report()
    primary = book.authors[0] if book.authors else ""
    if not primary.strip() or not (book.amazon_url or "").strip():
        return book.model_copy(
            update={
                "scraped_public_email": None,
                "scraped_email_status": ScrapedEmailStatus.NOT_FOUND,
                "scraped_email_reason": "missing_author_or_amazon_url",
                "scraped_email_sources": [],
                **_reactor_field_defaults(),
                "scraped_public_website": None,
            }
        )

    async with sem:
        page = await browser.new_page()
        try:
            url_html: list[tuple[str, str]] = []
            official_site: str | None = None
            on_amazon_author = False

            book_html = await _fetch_html(page, book.amazon_url, cfg)
            if not book_html:
                report.record_discard("stage15", "book_page_fetch_failed", book.asin)
            else:
                author_url = _find_author_page_url(book_html, book.amazon_url)
                if cfg.use_author_central and author_url:
                    ah = await _fetch_html(page, author_url, cfg)
                    if ah:
                        on_amazon_author = True
                        url_html.append((author_url, ah))
                        official_site = _find_external_website_url(ah, author_url)
                        if official_site:
                            for path in ("", "contact", "contact-me", "about", "about-me"):
                                if len(url_html) >= cfg.max_urls_per_author:
                                    break
                                u = urljoin(official_site.rstrip("/") + "/", path) if path else official_site
                                h = await _fetch_html(page, u, cfg)
                                if h:
                                    url_html.append((u, h))

            if cfg.use_ddg_fallback and len(url_html) < 2:
                for ddg_url in _ddg_search_urls(primary, cfg):
                    if len(url_html) >= cfg.max_urls_per_author:
                        break
                    if _should_skip_crawl_host(ddg_url):
                        continue
                    h = await _fetch_html(page, ddg_url, cfg)
                    if h:
                        url_html.append((ddg_url, h))
                        if official_site is None and not _is_amazon_url(ddg_url):
                            official_site = ddg_url

            by_email = _emails_from_pages(url_html)
            best_personal, src_personal = _pick_best_non_generic_email(by_email, official_site)

            if best_personal:
                vr = await _run_reactor(reactor, best_personal, cfg)
                return book.model_copy(
                    update={
                        "scraped_public_email": best_personal,
                        **_merge_reactor_result(vr),
                        "scraped_email_sources": src_personal[:10],
                        "scraped_public_website": official_site,
                    }
                )

            domain = _registrable_domain_from_site(official_site)
            guessed: str | None = None
            if cfg.use_domain_guess and domain:
                name_for_guess = sanitize_author_name_for_guessing(primary) or primary
                guessed = await _try_guessed_emails(name_for_guess, domain, cfg)

            if guessed:
                vr = await _run_reactor(reactor, guessed, cfg)
                return book.model_copy(
                    update={
                        "scraped_public_email": guessed,
                        **_merge_reactor_result(vr),
                        "scraped_email_sources": ["ddg:guess"],
                        "scraped_public_website": official_site,
                    }
                )

            best, uniq_sources = _pick_best_email(by_email, official_site)
            if not best:
                return book.model_copy(
                    update={
                        "scraped_public_email": None,
                        "scraped_email_status": ScrapedEmailStatus.NOT_FOUND,
                        "scraped_email_reason": "no_email_in_crawled_pages",
                        "scraped_email_sources": uniq_sources,
                        **_reactor_field_defaults(),
                        "scraped_public_website": official_site,
                    }
                )

            vr = await _run_reactor(reactor, best, cfg)
            return book.model_copy(
                update={
                    "scraped_public_email": best,
                    **_merge_reactor_result(vr),
                    "scraped_email_sources": uniq_sources[:10],
                    "scraped_public_website": official_site,
                }
            )
        except Exception as e:
            log.exception("stage15_book_error", asin=book.asin, error=str(e))
            report.record_discard("stage15", "exception", book.asin)
            return book.model_copy(
                update={
                    "scraped_public_email": None,
                    "scraped_email_status": ScrapedEmailStatus.NOT_FOUND,
                    "scraped_email_reason": f"error:{e!s:.200}",
                    "scraped_email_sources": [],
                    **_reactor_field_defaults(),
                }
            )
        finally:
            try:
                await page.close()
            except Exception:
                pass


async def enrich_books_with_public_email(
    books: list[AmazonBook],
    bundle: SettingsBundle | None = None,
) -> list[AmazonBook]:
    bundle = bundle or merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    cfg = bundle.email_enrichment
    report = get_report()

    if not cfg.enabled:
        log.info("stage15_disabled")
        return books
    if not books:
        log.info("stage15_no_books")
        return books

    reactor: EmailTruthReactor | None = None
    try:
        reactor = EmailTruthReactor.from_enrichment_config(cfg)
    except Exception as e:
        log.warning("reactor_init_failed", error=str(e))

    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=cfg.use_headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            tasks = [_process_one_book(b, browser, cfg, sem, reactor) for b in books]
            out = await asyncio.gather(*tasks)
        finally:
            await browser.close()

    found = sum(1 for b in out if b.scraped_public_email)
    verified = sum(1 for b in out if b.scraped_email_status == ScrapedEmailStatus.VERIFIED)
    risky = sum(1 for b in out if b.scraped_email_status == ScrapedEmailStatus.RISKY)
    log.info("stage15_complete", total=len(out), with_email=found, verified=verified, risky=risky)
    report.set_stage_count("stage15_email", len(out))
    return list(out)
