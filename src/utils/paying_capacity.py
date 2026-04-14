"""Heuristic paying-capacity signals from LinkedIn profile text (Stage 2)."""

from __future__ import annotations

from dataclasses import dataclass

from src.models import ProfessionTier


@dataclass(frozen=True)
class PayingCapacityResult:
    score: float  # 0–100
    tier: str  # high | medium | low
    summary: str
    factors: list[str]


_AUTHORITY_HIGH = (
    "chief executive",
    "ceo",
    "founder",
    "co-founder",
    "cofounder",
    "managing partner",
    "general partner",
    "managing director",
    "president",
    "owner",
    "principal",
    "chairman",
    "chairwoman",
    "board member",
    "board director",
)

_AUTHORITY_MID = (
    "cfo",
    "coo",
    "cmo",
    "cro",
    "cpo",
    "chro",
    "chief ",
    "vice president",
    "vp ",
    "v.p.",
    "head of",
    "director",
    "senior director",
    "regional director",
    "partner",
    "investor",
)

_ECON_POSITIVE = (
    "private equity",
    "venture capital",
    "angel investor",
    "board advisor",
    "advisory board",
    "scaled to",
    "revenue",
    "p&l",
    "budget owner",
    "procurement",
    "enterprise sales",
    "b2b",
    "saas",
    "raised ",
    "series a",
    "series b",
    "acquisition",
    "m&a",
)

_NEGATIVE = (
    "intern",
    "internship",
    "student at",
    "university student",
    "open to work",
    "seeking opportunities",
    "job seeker",
    "recent graduate",
    "entry level",
    "volunteer",
    "freelance writer",
    "uber driver",
)


def _low_blob(text: str) -> str:
    return text.lower()


def analyze_paying_capacity(
    *,
    headline: str | None,
    profile_blob: str,
    company: str | None,
    follower_count: int | None,
    profession_tier: ProfessionTier,
) -> PayingCapacityResult:
    """
    Score likelihood the person can authorize or afford typical B2B / consulting spend.
    Heuristic only — not financial advice or a guarantee.
    """
    h = _low_blob(headline or "")
    b = _low_blob(profile_blob or "")
    c = _low_blob(company or "")
    combined = f"{h}\n{b}\n{c}"
    factors: list[str] = []
    score = 0.0

    # Decision authority
    auth = 0.0
    if any(k in h or k in b[:4000] for k in _AUTHORITY_HIGH):
        auth = 35.0
        factors.append("decision_authority_high")
    elif any(k in h or k in b[:4000] for k in _AUTHORITY_MID):
        auth = 22.0
        factors.append("decision_authority_mid")
    elif profession_tier == ProfessionTier.CEO_FOUNDER_INVESTOR:
        auth = 32.0
        factors.append("tier_ceo_founder")
    elif profession_tier == ProfessionTier.VP_CTO_DOCTOR_ENGINEER:
        auth = 20.0
        factors.append("tier_vp_exec")
    elif profession_tier == ProfessionTier.OTHER_HIGH_INCOME:
        auth = 14.0
        factors.append("tier_owner_president")
    score += auth

    # Economic / scale proxies in profile
    econ = 0.0
    for kw in _ECON_POSITIVE:
        if kw in combined:
            econ = min(20.0, econ + 4.0)
    if econ > 0:
        factors.append("economic_signals")
    score += min(20.0, econ)

    # Follower band: very large following often = media brand; tiny = junior
    if follower_count is not None:
        if 500 <= follower_count <= 15_000:
            score += 8.0
            factors.append("followers_sweet_spot")
        elif follower_count > 100_000:
            score -= 5.0
            factors.append("followers_very_high_penalty")
        elif follower_count < 100:
            score += 2.0
            factors.append("followers_low")

    # Penalties
    penalty = 0.0
    for neg in _NEGATIVE:
        if neg in combined:
            penalty += 8.0
    if penalty > 0:
        factors.append("negative_signals")
    score -= min(30.0, penalty)

    if company and len(company.strip()) > 1:
        score += 4.0
        factors.append("company_known")

    score = max(0.0, min(100.0, score))

    if score >= 55:
        tier = "high"
        summary = "Strong signals of budget authority or senior commercial role."
    elif score >= 30:
        tier = "medium"
        summary = "Some commercial or seniority signals; worth qualifying in conversation."
    else:
        tier = "low"
        summary = "Limited signals of purchasing power or decision authority."

    return PayingCapacityResult(
        score=round(score, 1),
        tier=tier,
        summary=summary,
        factors=factors,
    )
