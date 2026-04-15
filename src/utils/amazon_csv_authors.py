"""Shared parsing, row keys, and checkpoint helpers for Amazon author CSVs."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import structlog

log = structlog.get_logger(__name__)

REQUIRED_COLUMNS = ("asin", "book_title", "authors")


def checkpoint_path_for_output(output_csv: Path) -> Path:
    return output_csv.with_suffix(output_csv.suffix + ".linkedin_checkpoint.json")


def load_checkpoint(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        done = data.get("done")
        if isinstance(done, list):
            return {str(x) for x in done}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("checkpoint_read_failed", path=str(path), error=str(e))
    return set()


def save_checkpoint(path: Path, done: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"done": sorted(done)}, indent=2),
        encoding="utf-8",
    )


def row_key(asin: str, author_name: str) -> str:
    norm = re.sub(r"\s+", " ", author_name.strip().lower())
    return f"{asin.strip()}::{norm}"


def parse_authors(authors_str: object) -> list[str]:
    if authors_str is None or (isinstance(authors_str, float) and pd.isna(authors_str)):
        return []
    if not str(authors_str).strip():
        return []
    text = re.split(r"\s*\|\s*", str(authors_str), maxsplit=1)[0]
    names = [n.strip() for n in re.split(r"[;,]", text)]
    clean: list[str] = []
    for name in names:
        name = re.sub(r",.*$", "", name).strip()
        if not name:
            continue
        low = name.lower()
        if "et al" in low or low == "unknown":
            continue
        clean.append(name)
    return clean


def explode_author_rows(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["clean_authors"] = df["authors"].apply(parse_authors)
    exploded = df.explode("clean_authors", ignore_index=True)
    exploded = exploded.dropna(subset=["clean_authors"])
    exploded = exploded.rename(columns={"clean_authors": "author_name"})
    return exploded


def normalize_amazon_book_csv_columns(df: pd.DataFrame) -> pd.DataFrame:
    colmap = {str(c).strip().lower(): c for c in df.columns}
    missing = set(REQUIRED_COLUMNS) - set(colmap.keys())
    if missing:
        raise ValueError(f"CSV missing columns: {sorted(missing)}")
    return df.rename(
        columns={
            colmap["asin"]: "asin",
            colmap["book_title"]: "book_title",
            colmap["authors"]: "authors",
        }
    )
