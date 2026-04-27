"""Unit tests for Email Truth Reactor (mocked network)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.email_verification.map_status import map_verification_to_scraped
from src.email_verification.truth_reactor import EmailTruthReactor
from src.email_verification.types import VerificationResult
from src.models import ScrapedEmailStatus


def _make_reactor_no_network(root: Path) -> EmailTruthReactor:
    """Pre-seed list files so __init__ does not call httpx (fresh files skip download)."""
    lists = root / "lists"
    lists.mkdir(parents=True, exist_ok=True)
    (lists / "disposable.txt").write_text("throwaway.example\n", encoding="utf-8")
    (lists / "role.txt").write_text("info\n", encoding="utf-8")
    time.sleep(0.01)
    return EmailTruthReactor(
        cache_db=root / "cache.db",
        list_dir=lists,
        list_update_interval_seconds=86_400 * 365 * 10,
        smtp_timeout=1.0,
        smtp_catchall_timeout=1.0,
    )


class TestTruthReactorSyntax(unittest.TestCase):
    def test_invalid_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r = _make_reactor_no_network(Path(td))
            out = r.verify("not-an-email", do_smtp=False)
            self.assertFalse(out.syntax_valid)
            self.assertEqual(out.category, "invalid")

    def test_smtp_off_dns_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reactor = _make_reactor_no_network(root)
            mx = MagicMock()
            mx.preference = 10
            mx.exchange = "mail.example.com."
            with patch.object(reactor, "_get_mx_records", return_value=[mx]):
                out = reactor.verify("jane@example.com", do_smtp=False)
            self.assertTrue(out.syntax_valid)
            self.assertTrue(out.mx_valid)
            self.assertTrue(out.smtp_not_run)
            st, _ = map_verification_to_scraped(out)
            self.assertEqual(st, ScrapedEmailStatus.UNVERIFIED)


class TestMapStatus(unittest.TestCase):
    def test_map_deliverable(self) -> None:
        v = VerificationResult(
            input="a@b.com",
            syntax_valid=True,
            mx_valid=True,
            parked=False,
            disposable=False,
            category="deliverable",
        )
        st, reason = map_verification_to_scraped(v)
        self.assertEqual(st, ScrapedEmailStatus.VERIFIED)
        self.assertIn("deliverable", reason)

    def test_map_risky(self) -> None:
        v = VerificationResult(
            input="a@b.com",
            syntax_valid=True,
            mx_valid=True,
            parked=False,
            disposable=False,
            category="risky",
        )
        st, _ = map_verification_to_scraped(v)
        self.assertEqual(st, ScrapedEmailStatus.RISKY)

    def test_map_skipped(self) -> None:
        v = VerificationResult(
            input="a@b.com",
            skipped=True,
            skip_reason="x",
        )
        st, reason = map_verification_to_scraped(v)
        self.assertEqual(st, ScrapedEmailStatus.UNVERIFIED)
        self.assertIn("skipped", reason)

    def test_map_smtp_not_run(self) -> None:
        v = VerificationResult(
            input="a@b.com",
            syntax_valid=True,
            mx_valid=True,
            smtp_not_run=True,
            category="unknown",
        )
        st, _ = map_verification_to_scraped(v)
        self.assertEqual(st, ScrapedEmailStatus.UNVERIFIED)

    def test_reactor_error(self) -> None:
        v = VerificationResult(
            input="a@b.com",
            syntax_valid=False,
            error="boom",
            final_label="Reactor error",
        )
        st, reason = map_verification_to_scraped(v)
        self.assertEqual(st, ScrapedEmailStatus.UNVERIFIED)
        self.assertIn("reactor:error", reason)


class TestTruthReactorDnsSmtpMocked(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self._root = Path(self._td.name)
        self._reactor = _make_reactor_no_network(self._root)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_mx_and_smtp_happy_path(self) -> None:
        mx = MagicMock()
        mx.preference = 10
        mx.exchange = "mail.example.com."

        with (
            patch.object(self._reactor, "_get_mx_records", return_value=[mx]),
            patch.object(
                self._reactor,
                "_smtp_probe",
                return_value=(True, 250, "ok", 0.05),
            ),
            patch.object(
                self._reactor,
                "_probe_catch_all_and_seg",
                return_value={
                    "catch_all_rate": 0.0,
                    "is_catch_all": False,
                    "is_seg_like": False,
                    "timing_variance": 0.2,
                    "results": [],
                },
            ),
        ):
            out = self._reactor.verify("jane@example.com", do_smtp=True)
        self.assertTrue(out.syntax_valid)
        self.assertTrue(out.mx_valid)
        self.assertTrue(out.smtp_accepts)
        st, _ = map_verification_to_scraped(out)
        self.assertEqual(st, ScrapedEmailStatus.VERIFIED)

    def test_smtp_hard_reject_is_invalid(self) -> None:
        mx = MagicMock()
        mx.preference = 10
        mx.exchange = "mail.example.com."

        with (
            patch.object(self._reactor, "_get_mx_records", return_value=[mx]),
            patch.object(
                self._reactor,
                "_smtp_probe",
                return_value=(False, 550, "mailbox unavailable", 0.05),
            ),
            patch.object(
                self._reactor,
                "_probe_catch_all_and_seg",
                return_value={
                    "catch_all_rate": 0.0,
                    "is_catch_all": False,
                    "is_seg_like": False,
                    "timing_variance": 0.2,
                    "results": [],
                },
            ),
        ):
            out = self._reactor.verify("jane@example.com", do_smtp=True)
        self.assertEqual(out.category, "invalid")
        self.assertIn("hard reject", out.final_label.lower())
        st, _ = map_verification_to_scraped(out)
        self.assertEqual(st, ScrapedEmailStatus.INVALID)
