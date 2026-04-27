"""Email Truth Reactor — local DNS/SMTP verification for Stage 1.5."""

from src.email_verification.map_status import map_verification_to_scraped
from src.email_verification.truth_reactor import EmailTruthReactor, default_reactor_paths
from src.email_verification.types import VerificationResult

__all__ = [
    "EmailTruthReactor",
    "VerificationResult",
    "default_reactor_paths",
    "map_verification_to_scraped",
]
