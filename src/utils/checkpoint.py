"""JSON checkpoint helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter

from src.config import AmazonScraperConfig, SettingsBundle, _project_root

TModel = TypeVar("TModel", bound=BaseModel)

DATA_DIR = _project_root() / "data"
LINKEDIN_MATCHED = DATA_DIR / "linkedin_matched.json"
VERIFIED_LEADS = DATA_DIR / "verified_leads.json"
LEADS_FINAL_CSV = DATA_DIR / "leads_final.csv"


def amazon_raw_path(amazon: AmazonScraperConfig) -> Path:
    """Resolved path for Stage 1 book list (config `raw_books_json`, basename only)."""
    name = (amazon.raw_books_json or "amazon_raw.json").strip() or "amazon_raw.json"
    return DATA_DIR / Path(name).name


def stage1_meta_path(raw_books: Path) -> Path:
    """Sidecar written next to the Stage 1 JSON (e.g. amazon_raw.meta.json)."""
    return raw_books.parent / f"{raw_books.stem}.meta.json"


def stage15_meta_path(raw_books: Path) -> Path:
    """Legacy sidecar for old Stage 1.5 (e.g. amazon_raw.email_meta.json)."""
    return raw_books.parent / f"{raw_books.stem}.email_meta.json"


def stage21_meta_path(linkedin_json: Path) -> Path:
    """Sidecar for Stage 2.1 (e.g. linkedin_matched.stage21_meta.json)."""
    return linkedin_json.parent / f"{linkedin_json.stem}.stage21_meta.json"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage15_enrichment_fingerprint(bundle: SettingsBundle, raw_path: Path) -> str:
    """Hash of email-enrichment settings + Stage 1 JSON bytes (invalidates on new scrape)."""
    ee = bundle.email_enrichment.model_dump(mode="json")
    stage1_sha = _sha256_file(raw_path) if raw_path.exists() and raw_path.stat().st_size > 0 else ""
    payload = {"email_enrichment": ee, "stage1_sha256": stage1_sha}
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def should_skip_stage15(raw_path: Path, force: bool, bundle: SettingsBundle) -> bool:
    if not bundle.email_enrichment.enabled:
        return True
    if force:
        return False
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        return False
    meta_path = stage15_meta_path(raw_path)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("fingerprint") == stage15_enrichment_fingerprint(bundle, raw_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def write_stage15_checkpoint_meta(raw_path: Path, bundle: SettingsBundle) -> None:
    meta_path = stage15_meta_path(raw_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "fingerprint": stage15_enrichment_fingerprint(bundle, raw_path),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def stage21_enrichment_fingerprint(bundle: SettingsBundle, linkedin_path: Path) -> str:
    """Hash of email_enrichment settings + linkedin_matched.json bytes."""
    ee = bundle.email_enrichment.model_dump(mode="json")
    li_sha = _sha256_file(linkedin_path) if linkedin_path.exists() and linkedin_path.stat().st_size > 0 else ""
    payload = {"email_enrichment": ee, "linkedin_matched_sha256": li_sha}
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def should_skip_stage21(linkedin_path: Path, force: bool, bundle: SettingsBundle) -> bool:
    if not bundle.email_enrichment.enabled:
        return True
    if force:
        return False
    if not linkedin_path.exists() or linkedin_path.stat().st_size == 0:
        return False
    meta_path = stage21_meta_path(linkedin_path)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("fingerprint") == stage21_enrichment_fingerprint(bundle, linkedin_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def write_stage21_checkpoint_meta(linkedin_path: Path, bundle: SettingsBundle) -> None:
    meta_path = stage21_meta_path(linkedin_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "fingerprint": stage21_enrichment_fingerprint(bundle, linkedin_path),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def stage1_search_fingerprint(bundle: SettingsBundle) -> str:
    """Hash of every config knob that changes which books Stage 1 collects."""
    amz = bundle.amazon_scraper
    pipe = bundle.pipeline
    payload = {
        "amazon_scraper": {
            "amazon_base": amz.amazon_base,
            "max_pages_per_pool": amz.max_pages_per_pool,
            "pool_a_keywords": list(amz.pool_a_keywords),
            "pool_b_keywords": list(amz.pool_b_keywords),
            "raw_books_json": amz.raw_books_json,
        },
        "pipeline": {
            "categories": list(pipe.categories),
            "pool_a_date_range_months": list(pipe.pool_a_date_range_months),
            "pool_b_preorder_days": pipe.pool_b_preorder_days,
            "review_threshold_a": list(pipe.review_threshold_a),
            "review_threshold_b": list(pipe.review_threshold_b),
            "non_fiction_gate_enabled": pipe.non_fiction_gate_enabled,
            "non_fiction_include_keywords": list(pipe.non_fiction_include_keywords),
            "non_fiction_exclude_keywords": list(pipe.non_fiction_exclude_keywords),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def should_skip_stage1(raw_path: Path, force: bool, bundle: SettingsBundle) -> bool:
    """Skip Stage 1 only if the JSON exists and was produced with the same search config."""
    if force:
        return False
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        return False
    meta_path = stage1_meta_path(raw_path)
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("fingerprint") == stage1_search_fingerprint(bundle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def write_stage1_checkpoint_meta(raw_path: Path, bundle: SettingsBundle) -> None:
    meta_path = stage1_meta_path(raw_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "fingerprint": stage1_search_fingerprint(bundle),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def load_list(path: Path, item_model: type[TModel]) -> list[TModel] | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return None
    ta: TypeAdapter[list[TModel]] = TypeAdapter(list[item_model])
    return ta.validate_python(raw)


def save_list(path: Path, items: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [m.model_dump(mode="json") for m in items]
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def should_skip_stage(output_path: Path, force: bool) -> bool:
    return (not force) and output_path.exists() and output_path.stat().st_size > 0


def clear_downstream_after(stage: str, *, raw_books_path: Path | None = None) -> None:
    """Remove checkpoint files for stages at or after `stage` (string: 1, 2, 2.1, 3, 4)."""
    if stage == "1":
        if raw_books_path is not None:
            mp = stage15_meta_path(raw_books_path)
            if mp.exists():
                mp.unlink()
        s21 = stage21_meta_path(LINKEDIN_MATCHED)
        if s21.exists():
            s21.unlink()
        paths = [LINKEDIN_MATCHED, VERIFIED_LEADS, LEADS_FINAL_CSV]
    elif stage == "2":
        s21 = stage21_meta_path(LINKEDIN_MATCHED)
        if s21.exists():
            s21.unlink()
        paths = [VERIFIED_LEADS, LEADS_FINAL_CSV]
    elif stage == "2.1":
        paths = [VERIFIED_LEADS, LEADS_FINAL_CSV]
    elif stage == "3":
        paths = [LEADS_FINAL_CSV]
    elif stage == "4":
        paths = []
    else:
        paths = []
    for p in paths:
        if p.exists():
            p.unlink()
