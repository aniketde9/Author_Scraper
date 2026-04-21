"""Typer CLI entrypoint for the Author Lead Scraper pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from src.config import (
    SettingsBundle,
    _project_root,
    clear_settings_cache,
    get_settings_bundle,
    load_env_settings,
    merge_keys_into_bundle,
)
from src.logger import setup_logging
from src.models import AmazonBook, EnrichedLead, VerifiedLead
from src.run_report import get_report, reset_report
from src.stage1_amazon import scrape_all_pools
from src.stage2_linkedin import enrich_authors
from src.stage25_compile import SCORING_INPUT_STAGE25_CSV, VERIFIED_LEADS_STAGE25, compile_stage25_inputs
from src.stage3_validation import validate_and_dedupe
from src.stage4_scoring import score_and_export
from src.utils.checkpoint import (
    LINKEDIN_MATCHED,
    VERIFIED_LEADS,
    amazon_raw_path,
    clear_downstream_after,
    load_list,
    save_list,
    should_skip_stage,
    should_skip_stage1,
    stage1_meta_path,
    stage1_search_fingerprint,
    write_stage1_checkpoint_meta,
)


def _sha256_prefix(path: Path, n: int = 16) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest[:n]


def _sync_stage1_report(
    raw_books_file: Path,
    *,
    from_checkpoint: bool,
    in_memory_count: int | None = None,
) -> None:
    """Set stage1_amazon count and notes from what is actually on disk."""
    report = get_report()
    if not raw_books_file.exists():
        report.set_stage_count("stage1_amazon", 0)
        report.extra_notes.append(
            f"Stage 1: expected {raw_books_file} missing on disk (0 books). "
            f"{'Loaded from checkpoint was skipped — file absent.' if from_checkpoint else 'Save may have failed.'}"
        )
        return
    on_disk = load_list(raw_books_file, AmazonBook) or []
    report.set_stage_count("stage1_amazon", len(on_disk))
    prefix = _sha256_prefix(raw_books_file)
    head = ", ".join(b.asin for b in on_disk[:5])
    if from_checkpoint:
        report.extra_notes.append(
            f"Stage 1: checkpoint only (no scrape) — {raw_books_file.name}, "
            f"{len(on_disk)} books on disk, sha256[:16]={prefix}. "
            f"First ASINs: {head or '—'}. "
            f"Sidecar {raw_books_file.stem}.meta.json matches current search config. "
            "Discard table for this run is empty."
        )
        return
    if in_memory_count is not None and len(on_disk) != in_memory_count:
        report.extra_notes.append(
            f"WARNING: Stage 1 in-memory books ({in_memory_count}) != books re-read from disk ({len(on_disk)})."
        )
    report.extra_notes.append(
        f"Stage 1: scrape complete — wrote {raw_books_file.name}, "
        f"{len(on_disk)} books on disk, sha256[:16]={prefix}. "
        f"First ASINs: {head or '—'}. "
        "Discards below are rejected candidates only, not the JSON contents."
    )


def _require_keys(stages: list[str]) -> None:
    if not any(s in {"2", "2.5", "3", "4"} for s in stages):
        return
    env = load_env_settings()
    if not (env.linkdapi_key or "").strip():
        typer.echo("Set LINKDAPI_KEY in .env (see .env.example).", err=True)
        raise typer.Exit(code=1)


async def _run_stage1(force: bool, raw_books_file: Path, bundle: SettingsBundle) -> list[AmazonBook]:
    skip = should_skip_stage1(raw_books_file, force, bundle)
    if skip:
        loaded = load_list(raw_books_file, AmazonBook) or []
        _sync_stage1_report(raw_books_file, from_checkpoint=True)
        typer.echo(
            f"Stage 1 skipped — {raw_books_file.name} matches current search config "
            f"({raw_books_file.stem}.meta.json). Loaded {len(loaded)} books. "
            "Use --stage 1 --force to re-scrape anyway."
        )
        return loaded

    if force:
        typer.echo("Stage 1: --force — running fresh Amazon scrape.")
    elif raw_books_file.exists() and raw_books_file.stat().st_size > 0:
        mp = stage1_meta_path(raw_books_file)
        want = stage1_search_fingerprint(bundle)
        if not mp.exists():
            typer.echo(
                "Stage 1: re-scraping — no .meta.json sidecar (first run after this update or meta removed).",
            )
        else:
            try:
                old = json.loads(mp.read_text(encoding="utf-8")).get("fingerprint")
                if old != want:
                    typer.echo(
                        "Stage 1: re-scraping — config.yaml search/filter settings changed since last scrape.",
                    )
            except (OSError, json.JSONDecodeError):
                typer.echo("Stage 1: re-scraping — could not read .meta.json.")

    books = await scrape_all_pools()
    save_list(raw_books_file, books)
    write_stage1_checkpoint_meta(raw_books_file, bundle)
    _sync_stage1_report(raw_books_file, from_checkpoint=False, in_memory_count=len(books))
    typer.echo(f"Stage 1 complete: {len(books)} books checkpointed to {raw_books_file}.")
    return books


async def _run_stage2(
    raw: Optional[list[AmazonBook]], force: bool, raw_books_file: Path
) -> list[EnrichedLead]:
    if should_skip_stage(LINKEDIN_MATCHED, force):
        loaded = load_list(LINKEDIN_MATCHED, EnrichedLead) or []
        typer.echo(f"Stage 2 skipped (checkpoint). Loaded {len(loaded)} leads.")
        return loaded
    books = raw if raw is not None else load_list(raw_books_file, AmazonBook) or []
    if not books:
        typer.echo("Stage 2: no Amazon books found. Run stage 1 first.", err=True)
        return []
    enriched = await enrich_authors(books)
    save_list(LINKEDIN_MATCHED, enriched)
    typer.echo(f"Stage 2 complete: {len(enriched)} enriched leads.")
    return enriched


def _run_stage25(enriched: Optional[list[EnrichedLead]], force: bool) -> list[VerifiedLead]:
    if should_skip_stage(VERIFIED_LEADS_STAGE25, force):
        loaded = load_list(VERIFIED_LEADS_STAGE25, VerifiedLead) or []
        typer.echo(f"Stage 2.5 skipped (checkpoint). Loaded {len(loaded)} rows.")
        return loaded
    leads = enriched if enriched is not None else load_list(LINKEDIN_MATCHED, EnrichedLead) or []
    if not leads:
        typer.echo("Stage 2.5: no enriched leads. Run stage 2 first.", err=True)
        return []
    compiled = compile_stage25_inputs(leads)
    typer.echo(
        f"Stage 2.5 complete: compiled {len(compiled)} rows to "
        f"{VERIFIED_LEADS_STAGE25.name} and {SCORING_INPUT_STAGE25_CSV.name}."
    )
    return compiled


def _run_stage3(enriched: Optional[list[EnrichedLead]], force: bool) -> list[VerifiedLead]:
    if should_skip_stage(VERIFIED_LEADS, force):
        loaded = load_list(VERIFIED_LEADS, VerifiedLead) or []
        typer.echo(f"Stage 3 skipped (checkpoint). Loaded {len(loaded)} verified leads.")
        return loaded
    leads = enriched if enriched is not None else load_list(LINKEDIN_MATCHED, EnrichedLead) or []
    if not leads:
        typer.echo("Stage 3: no enriched leads. Run stage 2 first.", err=True)
        return []
    verified = validate_and_dedupe(leads)
    save_list(VERIFIED_LEADS, verified)
    typer.echo(f"Stage 3 complete: {len(verified)} verified leads.")
    return verified


def _run_stage4(verified: Optional[list[VerifiedLead]], use_stage25_input: bool = False) -> None:
    if verified is not None:
        v = verified
    else:
        source = VERIFIED_LEADS_STAGE25 if use_stage25_input else VERIFIED_LEADS
        v = load_list(source, VerifiedLead) or []
    if not v:
        if use_stage25_input:
            typer.echo("Stage 4: no stage 2.5 input found. Run stage 2.5 first.", err=True)
        else:
            typer.echo("Stage 4: no verified leads. Run stage 3 first.", err=True)
        return
    top = score_and_export(v)
    origin = "stage 2.5 input" if use_stage25_input and verified is None else "verified leads"
    typer.echo(f"Stage 4 complete: exported {len(top)} rows to data/leads_final.csv (from {origin}).")


async def _run_stages_async(
    stages: list[str],
    force: bool,
    raw_books_file: Path,
    bundle: SettingsBundle,
    *,
    stage4_use_stage25_input: bool = False,
) -> None:
    raw: Optional[list[AmazonBook]] = None
    enriched: Optional[list[EnrichedLead]] = None
    verified: Optional[list[VerifiedLead]] = None

    if "1" in stages:
        raw = await _run_stage1(force, raw_books_file, bundle)
    if "2" in stages:
        enriched = await _run_stage2(raw, force, raw_books_file)
    if "2.5" in stages:
        _run_stage25(enriched, force)
    if "3" in stages:
        verified = _run_stage3(enriched, force)
    if "4" in stages:
        _run_stage4(verified, use_stage25_input=stage4_use_stage25_input)


def main(
    all_stages: bool = typer.Option(False, "--all", help="Run stages 1 through 4."),
    stage: Optional[str] = typer.Option(None, "--stage", help="Run a single stage (1, 2, 2.5, 3, 4)."),
    force: bool = typer.Option(False, "--force", help="Re-run stage(s) and refresh downstream checkpoints."),
    stage4_use_stage25_input: bool = typer.Option(
        False,
        "--stage4-use-stage25-input",
        help="For stage 4, read from data/verified_leads_stage25.json instead of data/verified_leads.json.",
    ),
) -> None:
    load_dotenv()
    clear_settings_cache()
    reset_report()
    log_path = setup_logging()

    if not all_stages and stage is None:
        typer.echo("Specify --all or --stage=1|2|2.5|3|4.", err=True)
        raise typer.Exit(code=1)
    if stage is not None and stage not in ("1", "2", "2.5", "3", "4"):
        typer.echo("--stage must be one of: 1, 2, 2.5, 3, 4.", err=True)
        raise typer.Exit(code=1)

    stages = ["1", "2", "2.5", "3", "4"] if all_stages else [stage]
    _require_keys(stages)
    bundle = merge_keys_into_bundle(get_settings_bundle(), load_env_settings())
    raw_books_file = amazon_raw_path(bundle.amazon_scraper)

    for s in stages:
        if force:
            if s == "1":
                clear_downstream_after(1)
            elif s == "2":
                clear_downstream_after(2)
            elif s == "2.5":
                # stage 2.5 only compiles from stage 2; do not delete stage 3/4 checkpoints.
                for p in (VERIFIED_LEADS_STAGE25, SCORING_INPUT_STAGE25_CSV):
                    if p.exists():
                        p.unlink()
            elif s == "3":
                clear_downstream_after(3)
            elif s == "4":
                clear_downstream_after(4)

    asyncio.run(
        _run_stages_async(
            stages,
            force,
            raw_books_file,
            bundle,
            stage4_use_stage25_input=stage4_use_stage25_input,
        )
    )

    summary_path = get_report().write_html(_project_root(), log_path=str(log_path))
    typer.echo(f"Summary written to {summary_path}")


if __name__ == "__main__":
    typer.run(main)
