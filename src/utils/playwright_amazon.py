"""Playwright + playwright-stealth helpers for Amazon HTML fetch."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Browser, Page, async_playwright
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from src.config import AmazonScraperConfig

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

EXTRA_HEADERS = {
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="133", "Not;A=Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _stealth() -> Stealth:
    return Stealth(
        navigator_user_agent_override=CHROME_UA,
        sec_ch_ua_override='"Chromium";v="133", "Not;A=Brand";v="99"',
    )


async def humanize_page_async(page: Page, cfg: AmazonScraperConfig) -> None:
    await asyncio.sleep(random.uniform(cfg.delay_between_requests, cfg.delay_between_requests + 1.5))
    for _ in range(cfg.human_scroll_steps):
        await page.evaluate("window.scrollBy(0, 400)")
        await asyncio.sleep(random.uniform(0.8, 1.6))


def humanize_page_sync(page, cfg: AmazonScraperConfig) -> None:
    time.sleep(random.uniform(cfg.delay_between_requests, cfg.delay_between_requests + 1.5))
    for _ in range(cfg.human_scroll_steps):
        page.evaluate("window.scrollBy(0, 400)")
        time.sleep(random.uniform(0.8, 1.6))


async def fetch_html_async(page: Page, url: str, cfg: AmazonScraperConfig) -> str:
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(2.5, 4.0))
    await humanize_page_async(page, cfg)
    return await page.content()


@asynccontextmanager
async def amazon_browser_session(cfg: AmazonScraperConfig) -> AsyncIterator[tuple[Browser, Page]]:
    stealth = _stealth()
    async with stealth.use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=cfg.use_headless, args=LAUNCH_ARGS)
        w = random.randint(cfg.viewport_width_range[0], cfg.viewport_width_range[1])
        h = random.randint(cfg.viewport_height_range[0], cfg.viewport_height_range[1])
        locale = random.choice(["en-US", "en-GB"])
        context = await browser.new_context(
            viewport={"width": w, "height": h},
            locale=locale,
            timezone_id="America/New_York",
        )
        page = await context.new_page()
        await page.set_extra_http_headers(EXTRA_HEADERS)
        try:
            yield browser, page
        finally:
            await context.close()
            await browser.close()


def fetch_amazon_html_sync(cfg: AmazonScraperConfig, url: str) -> str:
    """One-shot sync fetch (Stage 3 validation)."""
    stealth = _stealth()
    with stealth.use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=cfg.use_headless, args=LAUNCH_ARGS)
        w = random.randint(cfg.viewport_width_range[0], cfg.viewport_width_range[1])
        h = random.randint(cfg.viewport_height_range[0], cfg.viewport_height_range[1])
        locale = random.choice(["en-US", "en-GB"])
        context = browser.new_context(
            viewport={"width": w, "height": h},
            locale=locale,
            timezone_id="America/New_York",
        )
        page = context.new_page()
        page.set_extra_http_headers(EXTRA_HEADERS)
        try:
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(random.uniform(2.5, 4.0))
            humanize_page_sync(page, cfg)
            return page.content()
        finally:
            context.close()
            browser.close()
