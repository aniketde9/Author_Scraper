"""Re-guess author emails from LinkedIn company context + verify with Truth Reactor.

Run from repo root:
  PYTHONPATH=. python scripts/linkedin_company_email_reguess.py
  PYTHONPATH=. python scripts/linkedin_company_email_reguess.py --max-leads 3

Reads data/linkedin_matched.json, selects leads with bad/invalid Stage-1 contact hints,
extracts domains from profile positions (description emails + MX-checked slug guesses),
then scores all personal local-part combos and writes data/linkedin_company_email_reguess.json.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import dns.exception
import dns.resolver

from src.config import get_settings_bundle
from src.email_verification.truth_reactor import EmailTruthReactor
from src.utils.email_public import generate_personal_guesses, sanitize_author_name_for_guessing

ROOT = Path(__file__).resolve().parent.parent
LINKEDIN_JSON = ROOT / "data" / "linkedin_matched.json"
OUT_JSON = ROOT / "data" / "linkedin_company_email_reguess.json"

EMAIL_IN_TEXT = re.compile(r"[a-zA-Z0-9._%+-]+@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")
CORP_SUFFIX = re.compile(
    r"\b(llc|l\.l\.c\.?|inc\.?|ltd\.?|plc|corp\.?|corporation|company|co\.|gmbh|s\.a\.|s\.p\.a\.)\b",
    re.IGNORECASE,
)


def _blocked_domain(d: str) -> bool:
    d = (d or "").lower().removeprefix("www.")
    if not d:
        return True
    return any(x in d for x in ("amazon.", "amzn.", "qualtrics.", "audible.", "linkedin."))


def _slug_alnum(s: str) -> str:
    s = CORP_SUFFIX.sub("", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def _mx_exists(domain: str, *, lifetime: float = 4.0) -> bool:
    domain = domain.strip().lower().rstrip(".")
    if not domain:
        return False
    try:
        return len(dns.resolver.resolve(domain, "MX", lifetime=lifetime)) > 0
    except (dns.exception.DNSException, OSError, ValueError):
        return False


def _profile_data(raw: dict) -> dict:
    d = raw.get("data")
    return d if isinstance(d, dict) else {}


def _positions(pd: dict) -> list[dict]:
    for key in ("position", "fullPositions", "experience"):
        v = pd.get(key)
        if isinstance(v, list) and v:
            return [x for x in v if isinstance(x, dict)]
    return []


def _sort_positions_recent_first(positions: list[dict]) -> list[dict]:
    def key(p: dict) -> tuple[int, int]:
        end = p.get("end") or {}
        start = p.get("start") or {}
        ey = int(end.get("year") or 0)
        sy = int(start.get("year") or 0)
        current = 1 if ey == 0 else 0
        return (-current, -sy)

    return sorted(positions, key=key)


def _domains_from_descriptions(positions: list[dict]) -> list[str]:
    seen: list[str] = []
    for p in positions:
        desc = p.get("description") or ""
        if not isinstance(desc, str):
            continue
        for m in EMAIL_IN_TEXT.finditer(desc):
            dom = m.group(1).lower().strip().rstrip(".")
            if dom and not _blocked_domain(dom) and dom not in seen:
                seen.append(dom)
    return seen


def _heuristic_domains_from_position(p: dict) -> list[str]:
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
            if not _blocked_domain(d) and d not in out:
                out.append(d)
    return out


def _collect_candidate_domains(pd: dict, full_name: str) -> tuple[list[str], str, str]:
    """Return (domains, company_name_used, source_note)."""
    positions = _sort_positions_recent_first(_positions(pd))
    # Domains explicitly mentioned in job descriptions (highest signal)
    from_desc = _domains_from_descriptions(positions)
    if from_desc:
        return from_desc, positions[0].get("companyName") or "", "description_email"

    fn_l = (full_name or "").lower()
    for p in positions:
        cname = (p.get("companyName") or "").strip()
        if not cname:
            continue
        # Skip obvious personal-brand rows
        if cname.lower() in fn_l or fn_l in cname.lower():
            continue
        cid = p.get("companyId")
        curl = (p.get("companyURL") or "").lower()
        if cid == 0 and "linkedin.com/company/" not in curl:
            continue
        heur = _heuristic_domains_from_position(p)
        mx_ok = [d for d in heur if _mx_exists(d)]
        if mx_ok:
            return mx_ok, cname, "heuristic_mx"
    return [], "", "no_domain"


def _needs_reguess(lead: dict) -> bool:
    """True when Stage-1 contact is missing, disposable, or used a bad guessed domain (no MX, etc.).

    Avoids running SMTP here so the script stays fast; verification happens only on generated guesses.
    """
    book = lead.get("book") or {}
    email = (lead.get("contact_email") or book.get("scraped_public_email") or "").strip()
    web = (lead.get("contact_website") or book.get("scraped_public_website") or "").lower()
    status = (book.get("scraped_email_status") or "").lower()
    reason = (book.get("scraped_email_reason") or "").lower()

    if status in ("invalid", "not_found"):
        return True
    if "no_email" in reason or "no_mx" in reason:
        return True
    el = email.lower()
    if "qualtrics" in el or "qualtrics" in web:
        return True
    if "amazonexteu" in el:
        return True
    return False


def _verify_guesses(
    reactor: EmailTruthReactor,
    candidates: list[str],
    *,
    max_smtp: int,
) -> tuple[dict | None, int]:
    """Pick best candidate; cap SMTP probes and stop early on strong deliverable."""
    best: dict | None = None
    smtp_used = 0
    for c in candidates:
        if smtp_used >= max_smtp:
            break
        r = reactor.verify_safe(c, do_smtp=True)
        smtp_used += 1
        rec = {
            "email": c,
            "confidence": r.confidence,
            "category": r.category,
            "final": r.final_label,
            "smtp_accepts": r.smtp_accepts,
        }
        if best is None or rec["confidence"] > best["confidence"]:
            best = rec
        elif rec["confidence"] == best["confidence"]:
            rank = {"deliverable": 3, "risky": 2, "unknown": 1, "invalid": 0}
            if rank.get(str(rec["category"]), 0) > rank.get(str(best["category"]), 0):
                best = rec
        if (
            best
            and str(best["category"]) == "deliverable"
            and float(best["confidence"] or 0) >= 88.0
        ):
            break
    return best, smtp_used


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-guess emails from LinkedIn company context.")
    ap.add_argument(
        "--max-leads",
        type=int,
        default=0,
        metavar="N",
        help="Process at most N reguess-eligible leads (0 = all).",
    )
    args = ap.parse_args()

    cfg = get_settings_bundle().email_enrichment
    reactor = EmailTruthReactor(
        cache_db=ROOT / "data" / "email_reactor_company_guess.db",
        list_dir=ROOT / "data" / "lists",
        list_update_interval_seconds=cfg.reactor_list_update_interval_seconds,
        smtp_timeout=2.5,
        smtp_catchall_timeout=2.0,
        smtp_greylist_wait=False,
        smtp_catchall_probe=False,
    )
    max_smtp_per_author = 12

    leads = json.loads(LINKEDIN_JSON.read_text(encoding="utf-8"))
    results: list[dict] = []
    processed = 0

    for lead in leads:
        if not isinstance(lead, dict):
            continue
        if not _needs_reguess(lead):
            continue
        if args.max_leads and processed >= args.max_leads:
            break
        processed += 1

        raw = lead.get("raw_profile") or {}
        pd = _profile_data(raw if isinstance(raw, dict) else {})
        full_name = (lead.get("full_name") or "").strip()
        authors = (lead.get("book") or {}).get("authors") or []
        author_raw = (authors[0] if authors else full_name or "").strip()
        clean = sanitize_author_name_for_guessing(author_raw) or author_raw

        domains, company_used, dom_source = _collect_candidate_domains(pd, full_name)

        asin = (lead.get("book") or {}).get("asin")
        if not domains:
            print(f"[reguess] asin={asin} no_candidate_domains", flush=True)
            results.append(
                {
                    "asin": asin,
                    "author": author_raw,
                    "full_name": full_name,
                    "linkedin_url": lead.get("linkedin_url"),
                    "prior_contact_email": lead.get("contact_email"),
                    "company_used": company_used,
                    "domain_source": dom_source,
                    "candidate_domains": [],
                    "best_email": None,
                    "confidence": None,
                    "category": None,
                    "final": None,
                }
            )
            continue

        all_candidates: list[str] = []
        for dom in domains:
            for c in generate_personal_guesses(clean, dom):
                if c not in all_candidates:
                    all_candidates.append(c)

        print(
            f"[reguess] asin={asin} company={company_used!r} "
            f"domains={domains} guesses={len(all_candidates)} smtp_cap={max_smtp_per_author}",
            flush=True,
        )
        best, smtp_n = _verify_guesses(reactor, all_candidates, max_smtp=max_smtp_per_author)
        assert best is not None
        results.append(
            {
                "asin": asin,
                "author": author_raw,
                "full_name": full_name,
                "linkedin_url": lead.get("linkedin_url"),
                "prior_contact_email": lead.get("contact_email"),
                "company_used": company_used,
                "domain_source": dom_source,
                "candidate_domains": domains,
                "candidates_checked": len(all_candidates),
                "smtp_probes_used": smtp_n,
                "best_email": best["email"],
                "confidence": best["confidence"],
                "category": best["category"],
                "final": best["final"],
                "smtp_accepts": best["smtp_accepts"],
            }
        )
        print(
            f"[reguess] asin={asin} best={best['email']} "
            f"cat={best['category']} conf={best['confidence']}",
            flush=True,
        )

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(results)} rows to {OUT_JSON}")


if __name__ == "__main__":
    main()
