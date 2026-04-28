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
from src.utils.playwright_amazon import amazon_browser_session, fetch_html_async, fetch_search_html_async

log = structlog.get_logger(__name__)

REVIEW_RE = re.compile(r"([\d,]+)\s*(?:global\s+)?ratings?", re.I)
REVIEW_COUNT_FALLBACK_RE = re.compile(r"\(([\d,]+)\)")
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
    *,
    require_include: bool,
) -> bool:
    low = text.lower()
    has_include = any(_token_match(low, k.lower()) for k in include_keywords if k.strip())
    if require_include and not has_include:
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
    if m:
        return int(m.group(1).replace(",", ""))
    m2 = REVIEW_COUNT_FALLBACK_RE.search(blob)
    if m2:
        return int(m2.group(1).replace(",", ""))
    return None


def _search_url(amazon_base: str, category_keywords: str, page: int) -> str:
    k = quote_plus(category_keywords)
    base = amazon_base.rstrip("/")
    return f"{base}/s?k={k}&i=stripbooks&ref=sr_pg_{page}&page={page}"


def _search_url_variants(amazon_base: str, keywords: str, page: int, *, sort_key: str) -> list[str]:
    k = quote_plus(keywords)
    base = amazon_base.rstrip("/")
    variants = [
        (
            f"{base}/s?k={k}&i=stripbooks&rh=n%3A283155"
            f"&s={sort_key}&ref=sr_pg_{page}&page={page}"
        ),
        f"{base}/s?k={k}&i=stripbooks&s={sort_key}&page={page}",
        _search_url(base, keywords, page),
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for u in variants:
        if u not in seen:
            ordered.append(u)
            seen.add(u)
    return ordered


def parse_search_results(html: str, amazon_base: str) -> list[dict]:
    """Extract minimal fields from a search results page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    cards = soup.select('[data-component-type="s-search-result"]')
    if not cards:
        cards = soup.select(".s-result-item")
    for card in cards:
        asin = card.get("data-asin") or ""
        if not asin or asin == "None":
            continue
        h2 = card.select_one("h2 a span")
        if not h2:
            h2 = card.select_one("a.a-link-normal.a-text-normal span")
        if not h2:
            h2 = card.select_one('a[class*="a-link-normal"] span')
        title = h2.get_text(strip=True) if h2 else ""
        if not title:
            continue
        authors: list[str] = []
        for row in card.select(".a-row.a-size-base.a-color-secondary, .a-row"):
            t = row.get_text(" ", strip=True)
            if t.lower().startswith("by "):
                rest = t[3:].strip()
                for part in re.split(r",| and ", rest):
                    p = part.strip()
                    if p:
                        authors.append(p)
                break
        blob = card.get_text(" ", strip=True)
        rc = None
        review_hint = card.select_one(".a-size-base.s-underline-text")
        if review_hint:
            rc = _extract_review_count_from_text(review_hint.get_text(" ", strip=True))
        if rc is None:
            rc = _extract_review_count_from_text(blob)
        link_el = card.select_one("h2 a") or card.select_one("a.a-link-normal.a-text-normal")
        if link_el and link_el.get("href"):
            href = link_el.get("href") or ""
            url = href if href.startswith("http") else f"{amazon_base.rstrip('/')}{href}"
        else:
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


async def _fetch_search_with_retries(page, url: str, amz_cfg, label: str) -> tuple[str | None, str]:
    last_err: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            html, state = await fetch_search_html_async(page, url, amz_cfg)
            return html, state
        except Exception as e:
            last_err = e
            log.warning("amazon_search_fetch_retry", label=label, attempt=attempt + 1, error=str(e))
            await asyncio.sleep(2**attempt)
    log.error("amazon_search_fetch_failed", label=label, error=str(last_err))
    return None, "fetch_failed"


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
    non_fiction_gate_enabled = p.non_fiction_gate_enabled
    start_page = max(1, int(amz.start_page))
    end_page = int(amz.end_page) if amz.end_page is not None else int(amz.max_pages_per_pool)
    if end_page < start_page:
        log.warning(
            "stage1_invalid_page_window",
            start_page=start_page,
            end_page=end_page,
            note="No pages processed because end_page < start_page",
        )
        report.set_stage_count("stage1_amazon", 0)
        return []

    async with amazon_browser_session(amz) as (_browser, page):
        for category in p.categories:
            kw_pool_a = [f"{category} {k} book author" for k in amz.pool_a_keywords]
            kw_pool_b = [f"{category} {k} book author" for k in amz.pool_b_keywords]
            search_plans = [(kw, "review-rank") for kw in kw_pool_a] + [(kw, "date-desc-rank") for kw in kw_pool_b]
            for page_num in range(start_page, end_page + 1):
                rows: list[dict] = []
                saw_block = False
                for kw, sort_key in search_plans:
                    variants = _search_url_variants(base, kw, page_num, sort_key=sort_key)
                    for variant_idx, url in enumerate(variants, start=1):
                        html, state = await _fetch_search_with_retries(
                            page, url, amz, f"{category}_p{page_num}_v{variant_idx}"
                        )
                        if html is None:
                            continue
                        if state == "blocked_or_captcha":
                            saw_block = True
                        parsed = parse_search_results(html, base)
                        if parsed:
                            rows = parsed
                            break
                    if rows:
                        break
                if not rows:
                    reason = "search_blocked_or_captcha" if saw_block else "search_dom_no_cards"
                    report.record_discard("stage1", reason, f"{category} p{page_num}")
                    break
                for row in rows:
                    asin = row["asin"]
                    if asin in seen_asin:
                        continue
                    preview_text = f"{row.get('title', '')} {row.get('raw_snippet', '')}"
                    if non_fiction_gate_enabled:
                        if not _passes_non_fiction_gate(
                            preview_text,
                            non_fic_include,
                            non_fic_exclude,
                            require_include=False,
                        ):
                            report.record_discard("stage1", "non_fiction_reject_preview", asin)
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
                    if non_fiction_gate_enabled:
                        if not _passes_non_fiction_gate(
                            dhtml,
                            non_fic_include,
                            non_fic_exclude,
                            require_include=True,
                        ):
                            report.record_discard("stage1", "non_fiction_reject_detail", asin)
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
    return candidates
