"""JSON checkpoint helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter

from src.config import _project_root

TModel = TypeVar("TModel", bound=BaseModel)

DATA_DIR = _project_root() / "data"
AMAZON_RAW = DATA_DIR / "amazon_raw.json"
LINKEDIN_MATCHED = DATA_DIR / "linkedin_matched.json"
VERIFIED_LEADS = DATA_DIR / "verified_leads.json"
LEADS_FINAL_CSV = DATA_DIR / "leads_final.csv"


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
