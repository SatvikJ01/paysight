"""Shared fixtures: a small (scale 0.1) dataset built once per test session."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from paysight.config import get_settings
from paysight.data.generator import GenerationResult, generate

TEST_SCALE = 0.1


def build_dataset(
    path: Path, *, scale: float = TEST_SCALE, seed: int | None = None
) -> GenerationResult:
    """Generate a dataset with the project's date range into ``path``."""
    d = get_settings().data
    return generate(
        path,
        start=d.start_date,
        end=d.end_date,
        as_of=d.as_of_date,
        seed=d.seed if seed is None else seed,
        scale=scale,
    )


@pytest.fixture(scope="session")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> GenerationResult:
    """The scale-0.1, seed-42 dataset, generated once for the whole session."""
    return build_dataset(tmp_path_factory.mktemp("data") / "paysight.duckdb")


@pytest.fixture()
def small_con(small_dataset: GenerationResult) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only connection to the small dataset."""
    con = duckdb.connect(str(small_dataset.path), read_only=True)
    try:
        yield con
    finally:
        con.close()
