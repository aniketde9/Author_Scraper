"""
Stage 1: Amazon search + product pages via Playwright + playwright-stealth.

Search URL template: /s?k=<keywords>&i=stripbooks&page=<n>
Product URL: /dp/<ASIN>
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from urllib.parse import quote_plus

import structlog
from bs4 import BeautifulSoup

from src.config import merge_keys_into_bundle, get_settings_bundle, load_env_settings
from src.models import AmazonBook, Pool
from src.run_report import get_report
from src.utils.playwright_amazon import amazon_browser_session, fetch_html_async

log = structlog.get_logger(__name__)

REVIEW_RE = re.compile(r"([\d,]+)\s*(?:global\s+)?ratings?", re.I)
MONTHS_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}",
    re.I,
)
DATE_NUM_RE = re.compile(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})")

FETCH_RETRIES = 3


def _token_match(text: str, phrase: str) -> bool:
    """Whole-phrase matcher that tolerates spaces/hyphens in text."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", phrase.strip()) if p]
    if not parts:
        return False
    patt = r"\b" + r"[\s\-]*".join(parts) + r"\b"
    return re.search(patt, text, re.I) is not None


def _passes_non_fiction_gate(
    text: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
) -> bool:
    low = text.lower()
    has_include = any(_token_match(low, k.lower()) for k in include_keywords if k.strip())
    if not has_include:
        return False
    # Reject fiction-like terms but allow explicit non-fiction spellings.
    for word in exclude_keywords:
        w = word.strip().lower()
        if not w:
            continue
        if _token_match(low, w):
            if w == "fiction" and re.search(r"\bnon[\s\-]*fiction\b", low):
                continue
            return False
    return True


def _months_since(pub: date, today: date) -> int:
    return (today.year - pub.year) * 12 + (today.month - pub.month)


def _parse_us_date(text: str) -> date | None:
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:50], fmt).date()
        except ValueError:
            continue
    m = MONTHS_RE.search(text)
    if m:
        try:
            return datetime.strptime(m.group(0).replace(",", ""), "%B %d %Y").date()
        except ValueError:
            try:
                return datetime.strptime(m.group(0), "%B %d, %Y").date()
            except ValueError:
                pass
    m2 = DATE_NUM_RE.search(text)
    if m2:
        try:
            return date(int(m2.group(3)), int(m2.group(1)), int(m2.group(2)))
        except ValueError:
            return None
    return None


def _extract_review_count_from_text(blob: str) -> int | None:
    m = REVIEW_RE.search(blob)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def _search_url(amazon_base: str, category_keywords: str, page: int) -> str:
    k = quote_plus(category_keywords)
    base = amazon_base.rstrip("/")
    return f"{base}/s?k={k}&i=stripbooks&ref=sr_pg_{page}&page={page}"


def parse_search_results(html: str, amazon_base: str) -> list[dict]:
    """Extract minimal fields from a search results page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.select('div[data-component-type="s-search-result"]'):
        asin = card.get("data-asin") or ""
        if not asin or asin == "None":
            continue
        h2 = card.select_one("h2 a span")
        title = h2.get_text(strip=True) if h2 else ""
        if not title:
            continue
        authors: list[str] = []
        for row in card.select(".a-row"):
            t = row.get_text(" ", strip=True)
            if t.lower().startswith("by "):
                rest = t[3:].strip()
                for part in re.split(r",| and ", rest):
                    p = part.strip()
                    if p:
                        authors.append(p)
                break
        blob = card.get_text(" ", strip=True)
        rc = _extract_review_count_from_text(blob)
        url = f"{amazon_base.rstrip('/')}/dp/{asin}"
        out.append(
            {
                "asin": asin,
                "title": title,
                "authors": authors or ["Unknown"],
                "amazon_url": url,
                "review_count": rc,
                "raw_snippet": blob[:500],
            }
        )
    return out


def parse_product_detail(html: str) -> tuple[date | None, bool, int | None]:
    """Return (publish_or_release_date, is_preorder, review_count_if_found)."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    is_preorder = bool(
        re.search(r"pre-?order|This title has not yet been released|not yet released", text, re.I)
    )
    review_count: int | None = _extract_review_count_from_text(text)
    pub: date | None = None
    for li in soup.select("#detailBullets_feature_div li, #productDetails_detailBullets_sections1 tr, tr"):
        row = li.get_text(" ", strip=True) if hasattr(li, "get_text") else str(li)
        low = row.lower()
        if "publication date" in low or "release date" in low or "date first published" in low:
            pub = _parse_us_date(row.split(":", 1)[-1]) or pub
        if "release date" in low and is_preorder:
            pub = _parse_us_date(row.split(":", 1)[-1]) or pub
    if pub is None:
        m = MONTHS_RE.search(text)
        if m:
            pub = _parse_us_date(m.group(0))
    return pub, is_preorder, review_count


def _classify_book(
    *,
    review_count: int | None,
    pub: date | None,
    is_preorder: bool,
    pool_a_months: tuple[int, int],
    pool_b_days: int,
    r_a: tuple[int, int],
    r_b: tuple[int, int],
    today: date,
) -> tuple[Pool | None, bool]:
    """Return (pool, preorder_flag) or (None, _) if excluded."""
    if review_count is None:
        return None, is_preorder
    lo_a, hi_a = pool_a_months
    r_lo_a, r_hi_a = r_a
    r_lo_b, r_hi_b = r_b

    if is_preorder and r_lo_b <= review_count <= r_hi_b:
        if pub is None:
            return None, is_preorder
        delta = (pub - today).days
        if 0 <= delta <= pool_b_days:
            return Pool.B, True
        return None, is_preorder

    if r_lo_a <= review_count <= r_hi_a and pub:
        m = _months_since(pub, today)
        if lo_a <= m <= hi_a:
            return Pool.A, False

    return None, is_preorder


async def _fetch_with_retries(page, url: str, amz_cfg, label: str) -> str | None:
    last_err: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            return await fetch_html_async(page, url, amz_cfg)
        except Exception as e:
            last_err = e
            log.warning("amazon_fetch_retry", label=label, attempt=attempt + 1, error=str(e))
            await asyncio.sleep(2**attempt)
    log.error("amazon_fetch_failed", label=label, error=str(last_err))
    return None


async def scrape_all_pools() -> list[AmazonBook]:
    bundle = merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    amz = bundle.amazon_scraper
    p = bundle.pipeline
    base = amz.amazon_base
    report = get_report()
    today = date.today()
    seen_asin: set[str] = set()
    candidates: list[AmazonBook] = []
    non_fic_include = p.non_fiction_include_keywords
    non_fic_exclude = p.non_fiction_exclude_keywords

    async with amazon_browser_session(amz) as (_browser, page):
        for category in p.categories:
            kw = f"{category} non-fiction book author"
            for page_num in range(1, amz.max_pages_per_pool + 1):
                url = _search_url(base, kw, page_num)
                html = await _fetch_with_retries(page, url, amz, f"{category}_p{page_num}")
                if html is None:
                    report.record_discard("stage1", "search_fetch_failed", f"{category} p{page_num}")
                    continue
                rows = parse_search_results(html, base)
                if not rows:
                    report.record_discard("stage1", "no_search_results", f"{category} p{page_num}")
                    break
                for row in rows:
                    asin = row["asin"]
                    if asin in seen_asin:
                        continue
                    preview_text = f"{row.get('title', '')} {row.get('raw_snippet', '')}"
                    if not _passes_non_fiction_gate(preview_text, non_fic_include, non_fic_exclude):
                        report.record_discard("stage1", "not_non_fiction", asin)
                        continue
                    rc = row.get("review_count")
                    if rc is None:
                        continue
                    r_a_lo, r_a_hi = p.review_threshold_a
                    r_b_lo, r_b_hi = p.review_threshold_b
                    if not (
                        (r_a_lo <= rc <= r_a_hi)
                        or (r_b_lo <= rc <= r_b_hi)
                    ):
                        report.record_discard("stage1", "review_band", asin)
                        continue
                    seen_asin.add(asin)
                    dhtml = await _fetch_with_retries(page, row["amazon_url"], amz, f"detail_{asin}")
                    if dhtml is None:
                        report.record_discard("stage1", "detail_fetch_failed", asin)
                        seen_asin.discard(asin)
                        continue
                    if not _passes_non_fiction_gate(dhtml, non_fic_include, non_fic_exclude):
                        report.record_discard("stage1", "not_non_fiction_detail", asin)
                        seen_asin.discard(asin)
                        continue
                    pub, is_pre, detail_rc = parse_product_detail(dhtml)
                    final_rc = detail_rc if detail_rc is not None else rc
                    pool, pre_flag = _classify_book(
                        review_count=final_rc,
                        pub=pub,
                        is_preorder=is_pre,
                        pool_a_months=p.pool_a_date_range_months,
                        pool_b_days=p.pool_b_preorder_days,
                        r_a=p.review_threshold_a,
                        r_b=p.review_threshold_b,
                        today=today,
                    )
                    if pool is None:
                        report.record_discard("stage1", "pool_or_date_mismatch", asin)
                        seen_asin.discard(asin)
                        continue
                    candidates.append(
                        AmazonBook(
                            asin=asin,
                            title=row["title"],
                            authors=row["authors"],
                            amazon_url=row["amazon_url"],
                            review_count=final_rc,
                            pool=pool,
                            publish_date=pub,
                            is_preorder=pre_flag,
                            raw_snippet=row.get("raw_snippet"),
                        )
                    )
                    await asyncio.sleep(amz.delay_between_requests)
                log.info("amazon_page_done", category=category, page=page_num, found=len(rows))
                await asyncio.sleep(amz.delay_between_pages)

    log.info("stage1_complete", total=len(candidates))
    report.set_stage_count("stage1_amazon", len(candidates))
    return candidates
