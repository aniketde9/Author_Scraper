"""JSON checkpoint helpers."""

from __future__ import annotations

import hashlib
import json
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
    from datetime import datetime, timezone

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


def clear_downstream_after(stage: int) -> None:
    """Remove checkpoint files for stages after `stage` (1-indexed)."""
    mapping = {
        1: [LINKEDIN_MATCHED, VERIFIED_LEADS, LEADS_FINAL_CSV],
        2: [VERIFIED_LEADS, LEADS_FINAL_CSV],
        3: [LEADS_FINAL_CSV],
        4: [],
    }
    for p in mapping.get(stage, []):
        if p.exists():
            p.unlink()
