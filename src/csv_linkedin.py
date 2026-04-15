"""Enrich ``data/amazon_books_titles_authors.csv`` with LinkedIn fields via LinkdAPI.

Uses official ``linkdapi`` ``search_people`` / ``get_full_profile`` (no Google scraping).

Run from repo root: ``python -m src.csv_linkedin --help``

Requires ``LINKDAPI_KEY`` in environment (see ``.env.example``).
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
import typer
from dotenv import load_dotenv
from linkdapi import LinkdAPI

from src.config import _project_root, clear_settings_cache, get_settings_bundle, load_env_settings, merge_keys_into_bundle
from src.stage2_linkedin import _extract_contact, _flatten_profile_for_fuzzy
from src.utils.amazon_csv_authors import (
    checkpoint_path_for_output,
    explode_author_rows,
    load_checkpoint,
    normalize_amazon_book_csv_columns,
    row_key,
    save_checkpoint,
)
from src.utils.fuzzy_match import book_title_in_profile, headline_has_profession_keyword
from src.utils.linkdapi_client import _with_retries
from src.utils.linkdapi_hits import (
    headline_from_hit,
    normalize_search_people,
    split_author_name,
    urn_from_hit,
    username_from_hit,
)

log = structlog.get_logger(__name__)

app = typer.Typer(add_completion=False, help="LinkedIn enrichment for author CSV via LinkdAPI.")

DEFAULT_INPUT = _project_root() / "data" / "amazon_books_titles_authors.csv"
DEFAULT_OUTPUT = _project_root() / "data" / "amazon_books_linkedin.csv"

FULL_PROFILE_MIN_SCORE = 0.0

LINKDAPI_OUTPUT_MARKERS = ("urn", "headline", "email", "website")


def _csv_header_has_linkdapi_schema(header_line: str) -> bool:
    lower = header_line.lower()
    return all(m in lower for m in LINKDAPI_OUTPUT_MARKERS)


def _urn_from_full_response(full: dict[str, Any]) -> str | None:
    data = full.get("data") if isinstance(full.get("data"), dict) else full
    if not isinstance(data, dict):
        return None
    for key in ("profileUrn", "entityUrn"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _search_keyword(author: str, _book_title: str) -> str | None:
    """Search only by author name; book title is used later in scoring only."""
    if not author or not str(author).strip():
        return None
    kw = author.strip()
    return kw[:80] if len(kw) > 80 else kw


def _score_search_hit(author: str, book_title: str, hit: dict[str, Any]) -> dict[str, Any] | None:
    """Rank search hits; book title in snippet is optional — full profile fuzzy match decides author fit."""
    username = username_from_hit(hit)
    headline = headline_from_hit(hit) or ""
    h_lower = headline.lower()
    book_lower = book_title.lower()

    log.info(
        "scoring_hit",
        author=author,
        username=username,
        headline=headline[:120],
        has_profession=headline_has_profession_keyword(headline),
        book_in_headline=book_lower in h_lower,
    )

    if not username:
        log.warning("no_username_in_hit", author=author, hit_keys=list(hit.keys())[:30])
        return None

    score = 2.0
    if headline_has_profession_keyword(headline):
        score += 3.0
    if book_lower in h_lower:
        score += 4.0
    _first, last = split_author_name(author)
    if last and last.lower() in h_lower:
        score += 1.0

    return {
        "pre_profile_score": score,
        "username": username,
        "headline": headline or None,
        "urn": urn_from_hit(hit),
        "hit": hit,
    }


def _confidence_from_score(total: float) -> str:
    if total >= 8.0:
        return "high"
    if total >= 5.0:
        return "medium"
    if total >= 2.0:
        return "low"
    return "none"


def enrich_row_linkdapi(
    client: LinkdAPI,
    geo_urn: str,
    author: str,
    book_title: str,
    *,
    full_profile_min_score: float,
) -> tuple[dict[str, Any], str]:
    """Returns (result_dict, error_message). error_message empty on success."""
    err = ""
    first, last = split_author_name(author)
    kw = _search_keyword(author, book_title)
    try:
        resp = _with_retries(
            lambda: client.search_people(
                first_name=first or None,
                last_name=last or None,
                geo_urn=geo_urn,
                keyword=kw,
                start=0,
                count=8,
            ),
            label="search_people",
        )
    except Exception as e:
        log.warning("search_people_failed", author=author, error=str(e))
        return {}, str(e)

    hits = normalize_search_people(resp)[:8]
    log.info(
        "search_people_response",
        author=author,
        hit_count=len(hits),
        first_hit=headline_from_hit(hits[0]) if hits else None,
    )
    best: dict[str, Any] | None = None
    best_pre = -1.0
    for hit in hits:
        scored = _score_search_hit(author, book_title, hit)
        if not scored:
            continue
        if scored["pre_profile_score"] > best_pre:
            best_pre = scored["pre_profile_score"]
            best = scored

    if not best:
        return {}, ""

    username = best["username"]
    headline = best["headline"]
    urn = best.get("urn") or ""
    total_score = float(best["pre_profile_score"])
    email = ""
    website = ""
    matched_text = (headline or "")[:200]
    title_fuzz = 0.0
    notes = ""

    if best["pre_profile_score"] >= full_profile_min_score:
        try:
            full = _with_retries(
                lambda: client.get_full_profile(username=username),
                label="get_full_profile",
            )
        except Exception as e:
            log.warning("get_full_profile_failed", username=username, error=str(e))
            err = str(e)
            notes = "Full profile fetch failed; cannot verify book title in profile."
        else:
            profile_obj: Any = full
            if isinstance(full, dict) and "data" in full:
                profile_obj = full["data"]
            blob = _flatten_profile_for_fuzzy(profile_obj)
            title_fuzz = book_title_in_profile(book_title, blob)
            total_score += float(title_fuzz) * 0.08
            urn = _urn_from_full_response(full) or urn or ""
            em, web = _extract_contact(profile_obj)
            email = em or ""
            website = web or ""
            if len(matched_text) < 50 and blob:
                matched_text = blob[:200]

            if title_fuzz >= 60.0:
                notes = f"Book title strong match in full profile (fuzzy score {title_fuzz:.0f})."
            elif title_fuzz >= 38.0:
                notes = f"Book title partial match in profile (fuzzy {title_fuzz:.0f}); confirm manually."
            else:
                notes = (
                    "Name/search snippet match only; book title not clearly found in profile "
                    f"(fuzzy {title_fuzz:.0f}) — verify this is the author."
                )
    else:
        notes = (
            f"Skipped full profile (pre-profile score {best['pre_profile_score']:.1f} "
            f"< threshold {full_profile_min_score}); URL not verified against book."
        )

    out = {
        "linkedin_url": f"https://www.linkedin.com/in/{username}/",
        "linkedin_username": username,
        "urn": urn or "",
        "headline": headline or "",
        "confidence": _confidence_from_score(total_score),
        "matched_text": matched_text,
        "match_score": round(total_score, 2),
        "email": email,
        "website": website,
        "notes": notes,
        "title_fuzzy_score": round(title_fuzz, 1),
    }
    return out, err


@app.command()
def main(
    input_csv: Path = typer.Option(
        DEFAULT_INPUT,
        "--input",
        "-i",
        help="Input CSV with asin, book_title, authors",
    ),
    output_csv: Path = typer.Option(
        DEFAULT_OUTPUT,
        "--output",
        "-o",
        help="Output CSV path",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help="Skip rows already recorded in checkpoint",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Ignore checkpoint and remove existing checkpoint; truncate output at start",
    ),
    delay_min: float = typer.Option(1.2, "--delay-min", help="Minimum seconds between API rows"),
    delay_max: float = typer.Option(2.8, "--delay-max", help="Maximum seconds between API rows"),
    limit: int | None = typer.Option(None, "--limit", help="Process at most this many new author rows"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and explode only; no API calls"),
    full_profile_min_score: float = typer.Option(
        FULL_PROFILE_MIN_SCORE,
        "--full-profile-min-score",
        help="Minimum pre-profile score to call get_full_profile (0 = always when a hit is chosen)",
    ),
) -> None:
    load_dotenv()
    clear_settings_cache()

    if not input_csv.is_file():
        typer.echo(f"Input not found: {input_csv}", err=True)
        raise typer.Exit(code=1)
    if delay_min > delay_max:
        typer.echo("--delay-min must be <= --delay-max", err=True)
        raise typer.Exit(code=1)

    bundle = merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    if not dry_run and not (bundle.linkdapi.api_key or "").strip():
        typer.echo("Set LINKDAPI_KEY in .env (see .env.example).", err=True)
        raise typer.Exit(code=1)

    ck_path = checkpoint_path_for_output(output_csv)
    done: set[str] = set()

    if force:
        if ck_path.exists():
            ck_path.unlink()
        if output_csv.exists():
            output_csv.unlink()
        done = set()
    elif resume:
        done = load_checkpoint(ck_path)

    df = pd.read_csv(input_csv)
    try:
        df = normalize_amazon_book_csv_columns(df)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    exploded = explode_author_rows(df)
    if dry_run:
        typer.echo(f"Exploded author rows: {len(exploded)}")
        preview = exploded[["asin", "author_name", "book_title"]].head(8)
        typer.echo(preview.to_string(index=False))
        return

    if (
        output_csv.exists()
        and output_csv.stat().st_size > 0
        and not force
    ):
        first_line = output_csv.read_text(encoding="utf-8").splitlines()[0]
        if first_line and not _csv_header_has_linkdapi_schema(first_line):
            typer.echo(
                "Output CSV exists without LinkdAPI columns (urn, headline, email, website). "
                "Use --force to replace, or pass a different --output.",
                err=True,
            )
            raise typer.Exit(code=1)

    client = LinkdAPI(bundle.linkdapi.api_key)
    geo = bundle.linkdapi.geo_urn_usa

    processed = 0
    header_written = output_csv.exists() and output_csv.stat().st_size > 0 and not force

    for _, row in exploded.iterrows():
        author = str(row["author_name"]).strip()
        book_title = str(row["book_title"]).strip()
        asin = str(row["asin"]).strip()
        if not author or len(author) < 2:
            continue
        key = row_key(asin, author)
        if key in done:
            continue
        if limit is not None and processed >= limit:
            break

        base = {
            "asin": asin,
            "book_title": book_title,
            "authors": row.get("authors", ""),
            "author_name": author,
        }

        match, api_err = enrich_row_linkdapi(
            client,
            geo,
            author,
            book_title,
            full_profile_min_score=full_profile_min_score,
        )

        if match.get("linkedin_username"):
            out_row = {**base, **match, "error": api_err}
            log.info("linkedin_found", author=author, username=match["linkedin_username"])
        else:
            out_row = {
                **base,
                "linkedin_url": "",
                "linkedin_username": "",
                "urn": "",
                "headline": "",
                "confidence": "none",
                "matched_text": "",
                "match_score": 0.0,
                "email": "",
                "website": "",
                "notes": "",
                "title_fuzzy_score": 0.0,
                "error": api_err,
            }
            log.info("linkedin_not_found", author=author)

        done.add(key)
        processed += 1

        out_df = pd.DataFrame([out_row])
        out_df.to_csv(
            output_csv,
            mode="a",
            index=False,
            header=not header_written,
            encoding="utf-8",
        )
        header_written = True
        save_checkpoint(ck_path, done)

        time.sleep(random.uniform(delay_min, delay_max))

        if processed % 10 == 0:
            typer.echo(f"Processed {processed} author rows...")

    typer.echo(f"Done. Wrote {processed} new row(s) to {output_csv}")
    typer.echo(f"Checkpoint: {ck_path} ({len(done)} keys total)")


if __name__ == "__main__":
    app()
