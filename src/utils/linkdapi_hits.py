"""Normalize LinkdAPI search_people hits (shared by pipeline stage 2 and CSV tools)."""

from __future__ import annotations

import re
from typing import Any


def split_author_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", full.strip()) if p]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return (parts[0] if parts else "", "")


def normalize_search_people(resp: Any) -> list[dict[str, Any]]:
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if isinstance(data, dict):
        people = data.get("people") or data.get("elements")
        if isinstance(people, list):
            return [p for p in people if isinstance(p, dict)]
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    people = resp.get("people") or resp.get("elements")
    if isinstance(people, list):
        return [p for p in people if isinstance(p, dict)]
    return []


def _username_from_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    m = re.search(r"/in/([^/?#]+)", url)
    if m:
        return m.group(1).strip()
    return None


def username_from_hit(hit: dict[str, Any], _depth: int = 0) -> str | None:
    """Extract LinkedIn /in/ slug from search_people hit (incl. 2026 LinkdAPI ``url`` field)."""
    if _depth > 4 or not isinstance(hit, dict):
        return None

    for key in ("publicIdentifier", "publicId", "public_identifier", "vanityName"):
        val = hit.get(key)
        if isinstance(val, str) and val.strip():
            s = val.strip()
            u = _username_from_url(s)
            if u:
                return u
            if " " not in s:
                return s.split("/")[-1]

    for ukey in ("url", "URL"):
        url = hit.get(ukey)
        if isinstance(url, str):
            u = _username_from_url(url)
            if u:
                return u

    for key in ("username", "profileId"):
        v = hit.get(key)
        if isinstance(v, str) and v.strip() and " " not in v:
            return v.strip().split("/")[-1]

    for nav_key in ("navigationUrl", "link", "linkedinUrl", "publicProfileUrl", "profileUrl"):
        nav = hit.get(nav_key)
        if isinstance(nav, str):
            u = _username_from_url(nav)
            if u:
                return u

    for nested_key in ("miniProfile", "profile", "target", "item"):
        nested = hit.get(nested_key)
        if isinstance(nested, dict):
            u = username_from_hit(nested, _depth + 1)
            if u:
                return u
    return None


def headline_from_hit(hit: dict[str, Any]) -> str | None:
    for key in ("headline", "title", "subtitle", "primarySubtitle"):
        v = hit.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def urn_from_hit(hit: dict[str, Any]) -> str | None:
    for key in ("urn", "profileUrn", "entityUrn", "profileID"):
        v = hit.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None
