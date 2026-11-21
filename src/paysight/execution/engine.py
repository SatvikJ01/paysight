"""Read-only DuckDB executor (SPEC §9.5).

* The database is opened ``read_only=True`` with ``enable_external_access = false``, and then
  ``lock_configuration = true``, so no query can read files, reach the network or change a
  setting, even if it got past the validator.
* Every statement is validated for the caller's role first, then checked against the
  ``EXPLAIN`` cardinality estimate, then run under a timer that calls ``interrupt()``.
* Results are cached by ``sha256(validated SQL + data content hash)``.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import duckdb
import pandas as pd

from paysight.config import LimitSettings
from paysight.execution.cache import QueryCache, cache_key
from paysight.safety.access import RolePolicy
from paysight.safety.validator import Catalog, ValidationReport, validate_sql
from paysight.semantic.compiler import CompiledQuery, compile_query
from paysight.semantic.models import QuerySpec, SemanticLayer

RunStatus = Literal["ok", "blocked", "timeout", "error"]


@dataclass(frozen=True)
class QueryResult:
    """Rows plus everything needed to show and audit how they were produced."""

    status: RunStatus
    report: ValidationReport
    frame: pd.DataFrame | None
    sql: str | None
    runtime_ms: float
    cache_hit: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the query ran (or was served from cache) successfully."""
        return self.status == "ok"

    @property
    def row_count(self) -> int:
        """Number of rows returned (0 if none)."""
        return 0 if self.frame is None else len(self.frame)


class Engine:
    """Validated, read-only query execution against the Paysight database."""

    def __init__(
        self,
        db_path: Path,
        *,
        layer: SemanticLayer,
        limits: LimitSettings,
    ) -> None:
        if not db_path.exists():
            raise FileNotFoundError(
                f"{db_path} not found. Run `uv run paysight data generate` first."
            )
        self.layer = layer
        self.limits = limits
        self._con = duckdb.connect(
            str(db_path), read_only=True, config={"enable_external_access": False}
        )
        self._harden()
        self._lock = threading.Lock()
        self.cache = QueryCache(limits.cache_max_entries)
        self.catalog: Catalog = self._load_catalog()
        row = self._con.execute("SELECT content_hash FROM meta_data_version").fetchone()
        self.content_hash: str = row[0] if row else ""

    def _harden(self) -> None:
        """Lock the configuration, then verify the hardening holds (fail closed if not).

        Connections to one file within a process share a database instance, so a second engine
        may find the configuration already locked; that is fine as long as it is locked with
        external access disabled.
        """
        locked = self._setting("lock_configuration")
        if not locked:
            self._con.execute("SET lock_configuration = true")
        if self._setting("enable_external_access") or not self._setting("lock_configuration"):
            self._con.close()
            raise RuntimeError("DuckDB hardening failed: external access must be off and locked")

    def _setting(self, name: str) -> bool:
        row = self._con.execute("SELECT current_setting(?)", [name]).fetchone()
        return str(row[0]).lower() == "true" if row else False

    def _load_catalog(self) -> Catalog:
        catalog: Catalog = {}
        rows = self._con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_name IN (SELECT UNNEST(?)) ORDER BY table_name, ordinal_position",
            [list(self.layer.views)],
        ).fetchall()
        for table, column, dtype in rows:
            catalog.setdefault(table, {})[column] = dtype
        missing = set(self.layer.views) - set(catalog)
        if missing:
            raise RuntimeError(f"governed views missing from the database: {sorted(missing)}")
        return catalog

    def close(self) -> None:
        """Close the underlying connection."""
        self._con.close()

    # ----------------------------------------------------------------------------------------

    def _estimated_rows(self, sql: str) -> int:
        plan = json.loads(self._con.execute(f"EXPLAIN (FORMAT json) {sql}").fetchall()[0][1])
        best = 0
        stack = list(plan)
        while stack:
            node = stack.pop()
            value = str(node.get("extra_info", {}).get("Estimated Cardinality", "0"))
            if value.isdigit():
                best = max(best, int(value))
            stack.extend(node.get("children", []))
        return best

    def run_sql(self, sql: str, role: RolePolicy) -> QueryResult:
        """Validate and execute ``sql`` as ``role``. Nothing runs unless every check passes."""
        t0 = time.perf_counter()
        report = validate_sql(
            sql, role=role, layer=self.layer, catalog=self.catalog, limits=self.limits
        )

        def done(status: RunStatus, frame: pd.DataFrame | None = None, *, hit: bool = False,
                 error: str | None = None) -> QueryResult:  # fmt: skip
            return QueryResult(
                status=status,
                report=report,
                frame=frame,
                sql=report.sql_out,
                runtime_ms=(time.perf_counter() - t0) * 1000,
                cache_hit=hit,
                error=error,
            )

        if not report.passed or report.sql_out is None:
            report.add("cardinality_estimate", "skipped", "not run because validation failed")
            report.add("execution_guard", "skipped", "nothing executed")
            return done("blocked")

        sql_out = report.sql_out
        key = cache_key(sql_out, self.content_hash)
        cached = self.cache.get(key)
        if cached is not None:
            report.add("cardinality_estimate", "pass", "served from cache")
            report.add("execution_guard", "pass", "served from cache; not re-executed")
            return done("ok", cached, hit=True)

        with self._lock:
            try:
                estimate = self._estimated_rows(sql_out)
            except duckdb.Error as exc:
                report.add("cardinality_estimate", "fail", f"EXPLAIN failed: {exc}")
                report.add("execution_guard", "skipped", "nothing executed")
                return done("error", error=str(exc))
            if estimate > self.limits.max_estimated_rows:
                report.add(
                    "cardinality_estimate",
                    "fail",
                    f"estimated {estimate:,} rows exceeds {self.limits.max_estimated_rows:,}",
                )
                report.add("execution_guard", "skipped", "nothing executed")
                return done("blocked")
            report.add("cardinality_estimate", "pass", f"estimated max {estimate:,} rows")

            timer = threading.Timer(self.limits.query_timeout_s, self._con.interrupt)
            timer.start()
            try:
                frame = self._con.execute(sql_out).df()
            except duckdb.InterruptException:
                report.add(
                    "execution_guard",
                    "fail",
                    f"interrupted after the {self.limits.query_timeout_s:g}s timeout",
                )
                return done("timeout", error="query timed out")
            except duckdb.Error as exc:
                report.add("execution_guard", "fail", f"execution error: {exc}")
                return done("error", error=str(exc))
            finally:
                timer.cancel()
        report.add("execution_guard", "pass", f"completed within {self.limits.query_timeout_s:g}s")
        self.cache.put(key, frame)
        return done("ok", frame)

    def run_spec(self, spec: QuerySpec, role: RolePolicy) -> tuple[CompiledQuery, QueryResult]:
        """Compile a :class:`QuerySpec` and run it through the same validator as ad-hoc SQL."""
        compiled = compile_query(spec, self.layer)
        return compiled, self.run_sql(compiled.sql, role)
