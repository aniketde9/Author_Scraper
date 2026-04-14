"""rapidfuzz helpers for title matching and deduplication."""

from __future__ import annotations

from rapidfuzz import fuzz

PROFESSION_KEYWORDS = (
    "ceo",
    "founder",
    "co-founder",
    "cofounder",
    "vp",
    "vice president",
    "cto",
    "chief technology",
    "doctor",
    "dr.",
    "physician",
    "engineer",
    "investor",
    "business owner",
    "president",
)

CREATOR_BLOCKLIST = (
    "influencer",
    "content creator",
    "youtuber",
    "tiktok",
    "instagram creator",
    "podcast host",
    "full-time creator",
)


def headline_has_profession_keyword(headline: str | None) -> bool:
    if not headline:
        return False
    h = headline.lower()
    return any(k in h for k in PROFESSION_KEYWORDS)


def headline_looks_creator(headline: str | None) -> bool:
    if not headline:
        return False
    h = headline.lower()
    return any(k in h for k in CREATOR_BLOCKLIST)


def book_title_in_profile(book_title: str, profile_blob: str) -> float:
    if not book_title or not profile_blob:
        return 0.0
    t = book_title.strip().lower()
    b = profile_blob.lower()
    return float(
        max(
            fuzz.token_set_ratio(t, b),
            fuzz.partial_ratio(t, b),
        )
    )


def composite_dedupe_key(name: str, company: str, book_title: str) -> str:
    return f"{name.strip().lower()}|{company.strip().lower()}|{book_title.strip().lower()}"


def dedupe_similarity(a: str, b: str) -> float:
    return float(fuzz.token_sort_ratio(a, b))


def cluster_dedupe_rows(
    rows: list[tuple[str, float]],
    threshold: float = 92.0,
) -> dict[str, str]:
    """
    rows: (composite_key, priority_score) — higher priority_score wins as cluster representative.
    Returns mapping key -> canonical_key (winner id).
    """
    if not rows:
        return {}
    sorted_rows = sorted(rows, key=lambda x: -x[1])
    canonical: list[str] = []
    mapping: dict[str, str] = {}

    for key, _prio in sorted_rows:
        best: str | None = None
        for c in canonical:
            if dedupe_similarity(key, c) >= threshold:
                best = c
                break
        if best is None:
            canonical.append(key)
            mapping[key] = key
        else:
            mapping[key] = best
    return mapping
