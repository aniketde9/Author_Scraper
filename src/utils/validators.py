"""URL and contact validation."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def is_valid_email(email: str | None) -> bool:
    if not email or not isinstance(email, str):
        return False
    return bool(EMAIL_RE.match(email.strip()))


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.hostname or "").lower()
        return host.removeprefix("www.") if host else None
    except Exception:
        return None


def email_domain_matches_website(email: str | None, website: str | None) -> bool:
    if not email or not website:
        return True
    ed = email.split("@")[-1].lower() if "@" in email else ""
    wh = domain_from_url(website)
    if not ed or not wh:
        return True
    return ed == wh or ed.endswith("." + wh) or wh.endswith("." + ed)


def check_url_reachable(url: str, timeout: float = 15.0) -> bool:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.head(url)
            if r.status_code >= 400:
                r = client.get(url)
            return r.status_code < 500
    except httpx.HTTPError:
        return False
