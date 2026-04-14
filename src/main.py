"""Typer CLI entrypoint for the Author Lead Scraper pipeline."""

from __future__ import annotations

import asyncio
from typing import Optional

import typer
from dotenv import load_dotenv

from src.config import _project_root, clear_settings_cache, get_settings_bundle, load_env_settings, merge_keys_into_bundle
from src.logger import setup_logging
from src.models import AmazonBook, EnrichedLead, VerifiedLead
from src.run_report import get_report, reset_report
from src.stage1_amazon import scrape_all_pools
from src.stage2_linkedin import enrich_authors
from src.stage3_validation import validate_and_dedupe
from src.stage4_scoring import score_and_export
from src.utils.checkpoint import (
    AMAZON_RAW,
    LINKEDIN_MATCHED,
    VERIFIED_LEADS,
    clear_downstream_after,
    load_list,
    save_list,
    should_skip_stage,
)


def _require_keys(stages: list[int]) -> None:
    if not any(s >= 2 for s in stages):
        return
    env = load_env_settings()
    if not (env.linkdapi_key or "").strip():
        typer.echo("Set LINKDAPI_KEY in .env (see .env.example).", err=True)
        raise typer.Exit(code=1)


async def _run_stage1(force: bool) -> list[AmazonBook]:
    if should_skip_stage(AMAZON_RAW, force):
        loaded = load_list(AMAZON_RAW, AmazonBook) or []
        typer.echo(f"Stage 1 skipped (checkpoint). Loaded {len(loaded)} books.")
        return loaded
    books = await scrape_all_pools()
    save_list(AMAZON_RAW, books)
    typer.echo(f"Stage 1 complete: {len(books)} books checkpointed.")
    return books


async def _run_stage2(raw: Optional[list[AmazonBook]], force: bool) -> list[EnrichedLead]:
    if should_skip_stage(LINKEDIN_MATCHED, force):
        loaded = load_list(LINKEDIN_MATCHED, EnrichedLead) or []
        typer.echo(f"Stage 2 skipped (checkpoint). Loaded {len(loaded)} leads.")
        return loaded
    books = raw if raw is not None else load_list(AMAZON_RAW, AmazonBook) or []
    if not books:
        typer.echo("Stage 2: no Amazon books found. Run stage 1 first.", err=True)
        return []
    enriched = await enrich_authors(books)
    save_list(LINKEDIN_MATCHED, enriched)
    typer.echo(f"Stage 2 complete: {len(enriched)} enriched leads.")
    return enriched


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


def _run_stage4(verified: Optional[list[VerifiedLead]]) -> None:
    v = verified if verified is not None else load_list(VERIFIED_LEADS, VerifiedLead) or []
    if not v:
        typer.echo("Stage 4: no verified leads. Run stage 3 first.", err=True)
        return
    top = score_and_export(v)
    typer.echo(f"Stage 4 complete: exported {len(top)} rows to data/leads_final.csv.")


async def _run_stages_async(stages: list[int], force: bool) -> None:
    raw: Optional[list[AmazonBook]] = None
    enriched: Optional[list[EnrichedLead]] = None
    verified: Optional[list[VerifiedLead]] = None

    if 1 in stages:
        raw = await _run_stage1(force)
    if 2 in stages:
        enriched = await _run_stage2(raw, force)
    if 3 in stages:
        verified = _run_stage3(enriched, force)
    if 4 in stages:
        _run_stage4(verified)


def main(
    all_stages: bool = typer.Option(False, "--all", help="Run stages 1 through 4."),
    stage: Optional[int] = typer.Option(None, "--stage", help="Run a single stage (1-4)."),
    force: bool = typer.Option(False, "--force", help="Re-run stage(s) and refresh downstream checkpoints."),
) -> None:
    load_dotenv()
    clear_settings_cache()
    reset_report()
    log_path = setup_logging()

    if not all_stages and stage is None:
        typer.echo("Specify --all or --stage=1|2|3|4.", err=True)
        raise typer.Exit(code=1)
    if stage is not None and stage not in (1, 2, 3, 4):
        typer.echo("--stage must be between 1 and 4.", err=True)
        raise typer.Exit(code=1)

    stages = [1, 2, 3, 4] if all_stages else [int(stage)]
    _require_keys(stages)
    merge_keys_into_bundle(get_settings_bundle(), load_env_settings())

    for s in stages:
        if force:
            clear_downstream_after(s)

    asyncio.run(_run_stages_async(stages, force))

    summary_path = get_report().write_html(_project_root(), log_path=str(log_path))
    typer.echo(f"Summary written to {summary_path}")


if __name__ == "__main__":
    typer.run(main)
