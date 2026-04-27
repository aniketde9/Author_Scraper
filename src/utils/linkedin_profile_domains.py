"""Derive candidate email domains from LinkdAPI profile `data` (positions, descriptions)."""

from __future__ import annotations

import re

import dns.exception
import dns.resolver

EMAIL_IN_TEXT = re.compile(r"[a-zA-Z0-9._%+-]+@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")
CORP_SUFFIX = re.compile(
    r"\b(llc|l\.l\.c\.?|inc\.?|ltd\.?|plc|corp\.?|corporation|company|co\.|gmbh|s\.a\.|s\.p\.a\.)\b",
    re.IGNORECASE,
)


def blocked_domain(d: str) -> bool:
    d = (d or "").lower().removeprefix("www.")
    if not d:
        return True
    return any(x in d for x in ("amazon.", "amzn.", "qualtrics.", "audible.", "linkedin."))


def _slug_alnum(s: str) -> str:
    s = CORP_SUFFIX.sub("", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def mx_exists(domain: str, *, lifetime: float = 4.0) -> bool:
    domain = domain.strip().lower().rstrip(".")
    if not domain:
        return False
    try:
        return len(dns.resolver.resolve(domain, "MX", lifetime=lifetime)) > 0
    except (dns.exception.DNSException, OSError, ValueError):
        return False


def profile_positions(pd: dict) -> list[dict]:
    for key in ("position", "fullPositions", "experience"):
        v = pd.get(key)
        if isinstance(v, list) and v:
            return [x for x in v if isinstance(x, dict)]
    return []


def sort_positions_recent_first(positions: list[dict]) -> list[dict]:
    def key(p: dict) -> tuple[int, int]:
        end = p.get("end") or {}
        start = p.get("start") or {}
        ey = int(end.get("year") or 0)
        sy = int(start.get("year") or 0)
        current = 1 if ey == 0 else 0
        return (-current, -sy)

    return sorted(positions, key=key)


def domains_from_descriptions(positions: list[dict]) -> list[str]:
    seen: list[str] = []
    for p in positions:
        desc = p.get("description") or ""
        if not isinstance(desc, str):
            continue
        for m in EMAIL_IN_TEXT.finditer(desc):
            dom = m.group(1).lower().strip().rstrip(".")
            if dom and not blocked_domain(dom) and dom not in seen:
                seen.append(dom)
    return seen


def heuristic_domains_from_position(p: dict) -> list[str]:
    out: list[str] = []
    name = (p.get("companyName") or "").strip()
    user = (p.get("companyUsername") or "").strip().lower()
    slugs: list[str] = []
    if user and re.match(r"^[a-z0-9-]+$", user):
        slugs.append(user.replace("-", ""))
        if "-" in user:
            slugs.append(user)
    slug = _slug_alnum(name)
    if slug and len(slug) >= 3:
        slugs.append(slug)
    for cand in dict.fromkeys(slugs):
        for tld in ("com", "io", "co"):
            d = f"{cand}.{tld}"
            if not blocked_domain(d) and d not in out:
                out.append(d)
    return out


def collect_candidate_domains(pd: dict, full_name: str) -> tuple[list[str], str, str]:
    """Return (domains, company_name_used, source_tag)."""
    positions = sort_positions_recent_first(profile_positions(pd))
    from_desc = domains_from_descriptions(positions)
    if from_desc:
        return from_desc, positions[0].get("companyName") or "", "description_email"

    fn_l = (full_name or "").lower()
    for p in positions:
        cname = (p.get("companyName") or "").strip()
        if not cname:
            continue
        if cname.lower() in fn_l or fn_l in cname.lower():
            continue
        cid = p.get("companyId")
        curl = (p.get("companyURL") or "").lower()
        if cid == 0 and "linkedin.com/company/" not in curl:
            continue
        heur = heuristic_domains_from_position(p)
        mx_ok = [d for d in heur if mx_exists(d)]
        if mx_ok:
            return mx_ok, cname, "heuristic_mx"
    return [], "", "no_domain"
