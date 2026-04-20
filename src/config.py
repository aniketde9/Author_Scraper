"""Load merged YAML + environment configuration."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _substitute_env(obj: Any) -> Any:
    if isinstance(obj, str):
        def repl(m: re.Match[str]) -> str:
            key = m.group(1).strip()
            val = os.environ.get(key)
            if val is None:
                raise ValueError(f"Missing environment variable for config placeholder: {key}")
            return val

        return _ENV_PATTERN.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _substitute_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_env(x) for x in obj]
    return obj


def load_yaml_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or _project_root() / "config" / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return _substitute_env(raw)


class AmazonScraperConfig(BaseModel):
    mode: str = "playwright"
    amazon_base: str = "https://www.amazon.com"
    max_pages_per_pool: int = 12
    delay_between_pages: float = 8.0
    delay_between_requests: float = 2.5
    use_headless: bool = True
    viewport_width_range: tuple[int, int] = (1280, 1440)
    viewport_height_range: tuple[int, int] = (820, 980)
    human_scroll_steps: int = 4
    pool_a_keywords: list[str] = Field(
        default_factory=lambda: [
            "entrepreneurship",
            "startup",
            "founder",
            "small business",
            "scaling a business",
        ]
    )
    pool_b_keywords: list[str] = Field(
        default_factory=lambda: ["pre-order", "coming soon", "launching soon"]
    )
    # Basename only; file is written under data/. JSON list of AmazonBook objects.
    raw_books_json: str = "amazon_raw.json"


class LinkdAPIConfig(BaseModel):
    api_key: str
    geo_urn_usa: str = "103644278"


class PipelineConfig(BaseModel):
    pool_a_date_range_months: tuple[int, int] = (15, 24)
    pool_b_preorder_days: int = 30
    review_threshold_a: tuple[int, int] = (10, 40)
    review_threshold_b: tuple[int, int] = (0, 5)
    max_followers: int = 5000
    min_score_to_keep: int = 5
    categories: list[str] = Field(default_factory=list)
    non_fiction_gate_enabled: bool = True
    non_fiction_include_keywords: list[str] = Field(
        default_factory=lambda: ["non fiction", "non-fiction", "nonfiction"]
    )
    non_fiction_exclude_keywords: list[str] = Field(
        default_factory=lambda: [
            "fiction",
            "novel",
            "romance",
            "fantasy",
            "science fiction",
            "sci-fi",
            "thriller",
            "mystery",
            "young adult",
            "ya",
            "paranormal",
            "dystopian",
        ]
    )


class ScoringProfessionWeights(BaseModel):
    ceo_founder_investor: int = 3
    vp_cto_doctor_engineer: int = 2
    other_high_income: int = 1


class ScoringReviewWeights(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    band_10_20: int = Field(2, alias="10_20")
    band_21_40: int = Field(1, alias="21_40")


class ScoringVisibilityWeights(BaseModel):
    low: int = 2
    medium: int = 1
    high: int = 0


class ScoringConfig(BaseModel):
    profession: ScoringProfessionWeights = Field(default_factory=ScoringProfessionWeights)
    reviews: ScoringReviewWeights = Field(default_factory=ScoringReviewWeights)
    location: int = 1
    visibility: ScoringVisibilityWeights = Field(default_factory=ScoringVisibilityWeights)


class AppSettings(BaseSettings):
    """Secrets from environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    linkdapi_key: str = Field(default="", validation_alias="LINKDAPI_KEY")


class SettingsBundle(BaseModel):
    amazon_scraper: AmazonScraperConfig
    linkdapi: LinkdAPIConfig
    pipeline: PipelineConfig
    scoring: ScoringConfig


def _normalize_pipeline(raw: dict[str, Any]) -> dict[str, Any]:
    p = dict(raw)
    if "pool_a_date_range_months" in p and isinstance(p["pool_a_date_range_months"], list):
        lo, hi = p["pool_a_date_range_months"]
        p["pool_a_date_range_months"] = (int(lo), int(hi))
    for key in ("review_threshold_a", "review_threshold_b"):
        if key in p and isinstance(p[key], list):
            a, b = p[key]
            p[key] = (int(a), int(b))
    return p


def _normalize_amazon_scraper(raw: dict[str, Any]) -> dict[str, Any]:
    a = dict(raw)
    for key in ("viewport_width_range", "viewport_height_range"):
        if key in a and isinstance(a[key], list):
            lo, hi = a[key]
            a[key] = (int(lo), int(hi))
    return a


def clear_settings_cache() -> None:
    get_settings_bundle.cache_clear()


@lru_cache(maxsize=1)
def get_settings_bundle(config_path: str | None = None) -> SettingsBundle:
    path = Path(config_path) if config_path else _project_root() / "config" / "config.yaml"
    data = load_yaml_config(path)
    pipeline_raw = _normalize_pipeline(data.get("pipeline", {}))
    amazon_raw = _normalize_amazon_scraper(data.get("amazon_scraper", {}))
    return SettingsBundle(
        amazon_scraper=AmazonScraperConfig(**amazon_raw),
        linkdapi=LinkdAPIConfig(**data.get("linkdapi", {})),
        pipeline=PipelineConfig(**pipeline_raw),
        scoring=ScoringConfig(**data.get("scoring", {})),
    )


def load_env_settings() -> AppSettings:
    return AppSettings()


def merge_keys_into_bundle(bundle: SettingsBundle, env: AppSettings) -> SettingsBundle:
    """Overlay LinkdAPI key from .env onto YAML bundle."""
    l = bundle.linkdapi.model_copy(update={"api_key": env.linkdapi_key})
    return bundle.model_copy(update={"linkdapi": l})
