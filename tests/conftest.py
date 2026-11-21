"""Shared fixtures: a small (scale 0.1) dataset and engine, built once per test session."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from paysight.config import get_settings
from paysight.data.generator import GenerationResult, generate
from paysight.execution.engine import Engine
from paysight.safety.access import RolePolicy, get_policies
from paysight.semantic.loader import get_semantic_layer

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
    """A read-only connection to the small dataset (same config as the engine's, which DuckDB
    requires for two connections to one file in one process)."""
    con = duckdb.connect(
        str(small_dataset.path), read_only=True, config={"enable_external_access": False}
    )
    try:
        yield con
    finally:
        con.close()


@pytest.fixture(scope="session")
def small_engine(small_dataset: GenerationResult) -> Iterator[Engine]:
    """A validated, read-only engine over the small dataset."""
    engine = Engine(small_dataset.path, layer=get_semantic_layer(), limits=get_settings().limits)
    try:
        yield engine
    finally:
        engine.close()


@pytest.fixture(scope="session")
def policies() -> dict[str, RolePolicy]:
    """Role policies keyed by role name."""
    return get_policies()


@pytest.fixture(scope="session")
def full_dataset(tmp_path_factory: pytest.TempPathFactory) -> GenerationResult:
    """The full-scale (1.0) dataset: needed wherever results must match committed artifacts
    (the content hash, and replay cassettes whose prompts contain real evidence numbers)."""
    return build_dataset(tmp_path_factory.mktemp("full") / "paysight.duckdb", scale=1.0)
