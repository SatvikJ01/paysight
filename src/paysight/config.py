"""Settings loader.

All tunable values live in ``config/settings.yaml``. This module parses them into typed,
immutable pydantic models and resolves repository-relative paths with ``pathlib`` so the same
code works on Linux and Windows.
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DataSettings(_Frozen):
    """Dataset range, determinism and file locations."""

    start_date: date
    end_date: date
    as_of_date: date
    seed: int
    default_scale: float
    duckdb_path: Path
    ground_truth_path: Path

    @model_validator(mode="after")
    def _check_range(self) -> DataSettings:
        if not self.start_date <= self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.as_of_date < self.end_date:
            raise ValueError("as_of_date must not be before end_date")
        return self


class LimitSettings(_Frozen):
    """Query safety and cost limits."""

    query_timeout_s: float
    default_row_limit: int
    max_time_window_days: int
    max_joins: int
    max_estimated_rows: int
    cache_max_entries: int


class AnalysisSettings(_Frozen):
    """Statistical thresholds used by the analysis toolkit."""

    alpha: float
    practical_significance_pp: float
    min_sample_attempts: int
    min_delta_for_shares_pp: float
    denominator_divergence_pp: float
    anomaly_z_threshold: float
    reconcile_unexplained_flag_pct: float


class Settings(_Frozen):
    """Top-level settings object."""

    root: Path
    data: DataSettings
    limits: LimitSettings
    analysis: AnalysisSettings

    def path(self, relative: Path | str) -> Path:
        """Resolve a repository-relative path against the project root."""
        p = Path(relative)
        return p if p.is_absolute() else self.root / p

    @property
    def duckdb_path(self) -> Path:
        """Absolute path of the generated DuckDB database."""
        return self.path(self.data.duckdb_path)

    @property
    def ground_truth_path(self) -> Path:
        """Absolute path of the realized ground-truth YAML."""
        return self.path(self.data.ground_truth_path)


def project_root() -> Path:
    """Return the repository root: ``PAYSIGHT_ROOT`` if set, otherwise the source checkout."""
    override = os.environ.get("PAYSIGHT_ROOT", "").strip()
    return Path(override).resolve() if override else _PACKAGE_ROOT


def load_settings(root: Path | None = None) -> Settings:
    """Parse ``config/settings.yaml`` under ``root`` (default: :func:`project_root`)."""
    base = root or project_root()
    raw = yaml.safe_load((base / "config" / "settings.yaml").read_text(encoding="utf-8"))
    return Settings(root=base, **raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return load_settings()
