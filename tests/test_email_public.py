"""Unit tests for Stage 1.5 email helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.utils.email_public import (
    domain_has_mx,
    email_domain_matches_site,
    extract_emails_from_text,
    generate_personal_guesses,
    is_generic_localpart,
    normalize_email_candidate,
    sanitize_author_name_for_guessing,
)


class TestNormalizeAndExtract(unittest.TestCase):
    def test_normalize_obfuscated(self) -> None:
        self.assertEqual(normalize_email_candidate("hello [at] example.com"), "hello@example.com")
        self.assertEqual(normalize_email_candidate("hi [at] example [dot] com"), "hi@example.com")

    def test_extract_skips_amazon_domain(self) -> None:
        text = "Contact support@amazon.com or hello@authorblog.com"
        found = extract_emails_from_text(text)
        self.assertIn("hello@authorblog.com", found)
        self.assertNotIn("support@amazon.com", found)

    def test_extract_deobfuscate(self) -> None:
        text = "Contact jane.doe [at] site [dot] org today"
        found = extract_emails_from_text(text)
        self.assertIn("jane.doe@site.org", found)


class TestSanitizeAndGuess(unittest.TestCase):
    def test_sanitize_pipe_date(self) -> None:
        self.assertEqual(
            sanitize_author_name_for_guessing("John Fareed | May 21"),
            "John Fareed",
        )

    def test_sanitize_last_first(self) -> None:
        self.assertEqual(sanitize_author_name_for_guessing("Smith, John"), "John Smith")

    def test_sanitize_co_author_first_only(self) -> None:
        self.assertEqual(
            sanitize_author_name_for_guessing("Rob Kosberg, R.J. De Rossi"),
            "Rob Kosberg",
        )

    def test_is_generic(self) -> None:
        self.assertTrue(is_generic_localpart("hello@acme.com"))
        self.assertFalse(is_generic_localpart("jane@acme.com"))

    def test_generate_guesses_corey_quinn(self) -> None:
        g = generate_personal_guesses("Corey Quinn", "example.com")
        self.assertIn("corey@example.com", g)
        self.assertIn("corey.quinn@example.com", g)
        self.assertIn("quinn@example.com", g)

    def test_generate_hyphenated(self) -> None:
        g = generate_personal_guesses("Anne-Marie Smith", "blog.org")
        self.assertTrue(any("anne-marie" in x for x in g))
        self.assertTrue(any("smith" in x for x in g))


class TestDomainMatch(unittest.TestCase):
    def test_subdomain(self) -> None:
        self.assertTrue(email_domain_matches_site("a@example.com", "https://www.example.com/about"))

    def test_mismatch(self) -> None:
        self.assertFalse(email_domain_matches_site("a@other.com", "https://example.com"))


class TestMxLookup(unittest.TestCase):
    @patch("src.utils.email_public.dns.resolver.resolve")
    def test_mx_positive(self, mock_resolve: MagicMock) -> None:
        mock_resolve.return_value = [object()]
        self.assertTrue(domain_has_mx("gmail.com"))

    @patch("src.utils.email_public.dns.resolver.resolve")
    def test_mx_negative(self, mock_resolve: MagicMock) -> None:
        import dns.exception

        mock_resolve.side_effect = dns.exception.DNSException()
        self.assertFalse(domain_has_mx("invalid.invalid"))


if __name__ == "__main__":
    unittest.main()
