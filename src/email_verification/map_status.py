"""Map reactor output to `ScrapedEmailStatus` and reason tags."""

from __future__ import annotations

from src.email_verification.types import VerificationResult
from src.models import ScrapedEmailStatus


def map_verification_to_scraped(
    r: VerificationResult,
) -> tuple[ScrapedEmailStatus, str]:
    if r.skipped:
        return ScrapedEmailStatus.UNVERIFIED, f"reactor:skipped:{r.skip_reason}"
    if r.error and r.final_label == "Reactor error":
        err = (r.error or "")[:120]
        return ScrapedEmailStatus.UNVERIFIED, f"reactor:error:{err}"
    if not r.syntax_valid:
        return ScrapedEmailStatus.INVALID, "reactor:invalid:syntax"
    if r.disposable or not r.mx_valid or r.parked:
        return ScrapedEmailStatus.INVALID, "reactor:invalid:dns_or_list"
    if r.smtp_not_run:
        return ScrapedEmailStatus.UNVERIFIED, f"reactor:dns_only:{r.category}"
    if r.category == "deliverable":
        return ScrapedEmailStatus.VERIFIED, "reactor:deliverable"
    if r.category == "risky":
        return ScrapedEmailStatus.RISKY, "reactor:risky"
    if r.category == "invalid":
        return ScrapedEmailStatus.INVALID, "reactor:invalid:smtp_or_score"
    return ScrapedEmailStatus.UNVERIFIED, f"reactor:unknown:{r.category}"
