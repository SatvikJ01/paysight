"""SQL validator: every statement, compiled or ad-hoc, passes through here (SPEC §9.4).

Each check produces a :class:`CheckResult`. A query may run only if every check passes. Checks
that depend on an earlier failed check are reported as ``skipped``, so the report always shows
the full checklist.

The engine adds two more checks at run time: the ``EXPLAIN`` cardinality estimate and the
execution timeout (see :mod:`paysight.execution.engine`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from paysight.config import LimitSettings
from paysight.safety.access import RolePolicy
from paysight.safety.rewrite import RowFilterError, apply_row_filters
from paysight.semantic.models import SemanticLayer

CheckStatus = Literal["pass", "fail", "skipped"]
Catalog = dict[str, dict[str, str]]  # view -> column -> DuckDB type

# Node types that must never appear anywhere in a statement.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Pragma,
    exp.Set,
    exp.Command,
    exp.Install,
    exp.Use,
    exp.Describe,
    exp.Summarize,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Into,
    exp.LoadData,
)
_QUERY_ROOTS = (exp.Select, exp.SetOperation)

_DENIED_FUNCTIONS = {
    "glob",
    "sniff_csv",
    "query",
    "query_table",
    "getenv",
    "current_setting",
    "which_secret",
    "load_extension",
}
_DENIED_FUNCTION_PREFIXES = (
    "read_",
    "pragma_",
    "duckdb_",
    "parquet_",
    "iceberg_",
    "delta_",
    "sqlite_",
    "postgres_",
    "mysql_",
    "http",
)
_ALLOWED_TABLE_FUNCTIONS = {"range", "generate_series", "generateseries"}
# Physical tables: only the data-quality tool may read these, never through this validator.
RAW_TABLES = {
    "fact_payment_attempts",
    "fact_settlements",
    "dim_merchant",
    "dim_acquirer",
    "dim_issuer",
    "dim_payment_method",
    "meta_data_version",
}


class CheckResult(BaseModel):
    """One line of the validation checklist."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: CheckStatus
    detail: str

    @property
    def passed(self) -> bool:
        """Whether this check passed."""
        return self.status == "pass"


class ValidationReport(BaseModel):
    """The full checklist for one statement, shown in the UI."""

    role: str
    sql_in: str
    sql_out: str | None = None
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only if every check passed."""
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        """Checks that failed (not skipped)."""
        return [c for c in self.checks if c.status == "fail"]

    def add(self, name: str, status: CheckStatus, detail: str) -> None:
        """Append a check result."""
        self.checks.append(CheckResult(name=name, status=status, detail=detail))


class _CheckFailedError(Exception):
    """Internal: a check failed with a user-facing reason."""


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _is_denied_function(name: str) -> bool:
    return name in _DENIED_FUNCTIONS or name.startswith(_DENIED_FUNCTION_PREFIXES)


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _base_tables(tree: exp.Expression, ctes: set[str]) -> list[exp.Table]:
    """Table references that name a real relation (not a CTE, not a table function)."""
    return [
        t
        for t in tree.find_all(exp.Table)
        if isinstance(t.this, exp.Identifier) and not (t.name.lower() in ctes and not t.db)
    ]


# --------------------------------------------------------------------------------------------
# Individual checks. Each returns a pass detail or raises _CheckFailedError.
# --------------------------------------------------------------------------------------------


def _check_read_only(tree: exp.Expression) -> str:
    if not isinstance(tree, _QUERY_ROOTS):
        raise _CheckFailedError(f"only SELECT queries are allowed, got {tree.key.upper()}")
    bad = sorted({type(n).__name__.upper() for n in tree.walk() if isinstance(n, _FORBIDDEN_NODES)})
    if bad:
        raise _CheckFailedError(f"forbidden operation inside the query: {', '.join(bad)}")
    return "pure SELECT; no data modification, DDL, settings or extensions"


def _check_functions(tree: exp.Expression) -> str:
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            name = _function_name(table.this)
            if name not in _ALLOWED_TABLE_FUNCTIONS:
                raise _CheckFailedError(f"table function {name}() is not allowed")
    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        if _is_denied_function(name):
            raise _CheckFailedError(f"function {name}() is not allowed")
    return "no file, network, settings or catalog functions"


def _check_tables(tree: exp.Expression, role: RolePolicy, catalog: Catalog) -> str:
    ctes = _cte_names(tree)
    shadowing = ctes & (set(catalog) | RAW_TABLES)
    if shadowing:
        raise _CheckFailedError(f"CTE names may not shadow real tables: {sorted(shadowing)}")
    used = set()
    for table in _base_tables(tree, ctes):
        if table.catalog or (table.db and table.db.lower() != "main"):
            raise _CheckFailedError(f"schema-qualified table {table.sql()} is not allowed")
        name = table.name.lower()  # DuckDB identifiers are case-insensitive
        if name in RAW_TABLES:
            raise _CheckFailedError(f"{name} is a raw table; queries must use the governed views")
        if name not in role.allowed_views or name not in catalog:
            raise _CheckFailedError(f"{name} is not a view the {role.label} role may query")
        used.add(name)
    if not used:
        return "no governed views read"
    return f"views: {', '.join(sorted(used))}"


def _scope_table_sources(scope: Scope) -> dict[str, str]:
    """alias -> base view name, for sources in this scope that are real tables."""
    return {
        alias: src.name
        for alias, src in scope.sources.items()
        if isinstance(src, exp.Table) and isinstance(src.this, exp.Identifier)
    }


def _check_columns(
    tree: exp.Expression, role: RolePolicy, layer: SemanticLayer, catalog: Catalog
) -> tuple[str, exp.Expression]:
    facts = {v for v, d in layer.views.items() if d.fact}
    for select in tree.find_all(exp.Select):
        stars = [
            p
            for p in select.expressions
            if isinstance(p, exp.Star)
            or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
        ]
        sources = [select.args.get("from"), *(select.args.get("joins") or [])]
        fact_sources = {
            t.name
            for s in sources
            if s is not None
            for t in s.find_all(exp.Table)
            if t.name in facts
        }
        if stars and fact_sources:
            raise _CheckFailedError(
                f"SELECT * over fact views is not allowed ({', '.join(sorted(fact_sources))})"
            )

    try:
        qualified = qualify(
            tree.copy(),
            schema=catalog,
            dialect="duckdb",
            validate_qualify_columns=True,
            quote_identifiers=False,
        )
    except SqlglotError as exc:
        raise _CheckFailedError(f"could not resolve columns: {exc}") from exc

    denied = set(role.denied_sensitivities)
    used: set[str] = set()
    for scope in traverse_scope(qualified):
        tables = _scope_table_sources(scope)
        for column in scope.columns:
            view = tables.get(column.table)
            if view is None:
                continue
            used.add(f"{view}.{column.name}")
            sensitivity = layer.column_sensitivity(view, column.name)
            if sensitivity in denied:
                raise _CheckFailedError(
                    f"column {column.name} is {sensitivity}; the {role.label} role cannot use it"
                )
    return f"{len(used)} column reference(s) resolved; no restricted columns", qualified


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    node = node.unnest()
    if isinstance(node, exp.And):
        return [c for part in node.flatten() for c in _conjuncts(part)]
    return [node]


def _literal_time(node: exp.Expression) -> datetime | None:
    node = node.unnest()
    if isinstance(node, exp.Cast):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        try:
            return datetime.fromisoformat(node.this)
        except ValueError:
            return None
    return None


def _time_bounds(
    where: exp.Expression | None, alias: str, column: str
) -> tuple[datetime | None, datetime | None]:
    """Tightest literal lower/upper bounds on ``alias.column`` among top-level AND conjuncts."""
    lower: datetime | None = None
    upper: datetime | None = None

    def is_col(e: exp.Expression) -> bool:
        e = e.unnest()
        return isinstance(e, exp.Column) and e.name == column and e.table == alias

    def tighten(lo: datetime | None = None, hi: datetime | None = None) -> None:
        nonlocal lower, upper
        if lo is not None:
            lower = lo if lower is None else max(lower, lo)
        if hi is not None:
            upper = hi if upper is None else min(upper, hi)

    for c in _conjuncts(where):
        if isinstance(c, exp.Between) and is_col(c.this):
            tighten(_literal_time(c.args["low"]), _literal_time(c.args["high"]))
        elif isinstance(c, exp.EQ | exp.GT | exp.GTE | exp.LT | exp.LTE):
            left, right = c.this, c.expression
            if is_col(right):  # normalize "literal op column" to "column op' literal"
                left, right = right, left
                flip = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}
                kind = flip.get(type(c), type(c))
            else:
                kind = type(c)
            if not is_col(left):
                continue
            value = _literal_time(right)
            if kind is exp.EQ:
                tighten(value, value)
            elif kind in (exp.GT, exp.GTE):
                tighten(lo=value)
            else:
                tighten(hi=value)
    return lower, upper


def _check_time_window(qualified: exp.Expression, layer: SemanticLayer, max_days: int) -> str:
    timed = {v: d.time_column for v, d in layer.views.items() if d.fact}
    found = []
    for scope in traverse_scope(qualified):
        if not isinstance(scope.expression, exp.Select):
            continue
        where = scope.expression.args.get("where")
        for alias, view in _scope_table_sources(scope).items():
            if view not in timed:
                continue
            lower, upper = _time_bounds(where.this if where else None, alias, timed[view])
            if lower is None or upper is None:
                raise _CheckFailedError(
                    f"queries over {view} need literal lower and upper bounds on "
                    f"{timed[view]} in the WHERE clause"
                )
            span = (upper - lower).total_seconds() / 86_400
            if span < 0:
                raise _CheckFailedError(f"the {timed[view]} window on {view} is empty")
            if span > max_days:
                raise _CheckFailedError(
                    f"the {timed[view]} window spans {span:.0f} days (max {max_days})"
                )
            found.append(f"{view}: {lower:%Y-%m-%d} to {upper:%Y-%m-%d %H:%M}")
    return "; ".join(found) if found else "no attempt/order views; not required"


def _check_joins(tree: exp.Expression, max_joins: int) -> str:
    joins = list(tree.find_all(exp.Join))
    for join in joins:
        if (join.args.get("kind") or "").upper() == "CROSS":
            raise _CheckFailedError("CROSS JOIN is not allowed")
        if (join.args.get("method") or "").upper() == "NATURAL":
            raise _CheckFailedError("NATURAL JOIN is not allowed; use an explicit condition")
        if not join.args.get("on") and not join.args.get("using"):
            raise _CheckFailedError("every join needs an ON or USING condition")
    if len(joins) > max_joins:
        raise _CheckFailedError(f"{len(joins)} joins (max {max_joins})")
    return f"{len(joins)} join(s), all with conditions"


def _apply_limit(tree: exp.Expression, cap: int) -> tuple[exp.Expression, str]:
    limit = tree.args.get("limit")
    if limit is None:
        return tree.limit(cap), f"no LIMIT; injected LIMIT {cap}"
    value = limit.expression
    if not (isinstance(value, exp.Literal) and value.is_int):
        raise _CheckFailedError("LIMIT must be an integer literal")
    n = int(value.this)
    if n > cap:
        limit.set("expression", exp.Literal.number(cap))
        return tree, f"LIMIT {n} capped to {cap}"
    return tree, f"LIMIT {n}"


def _break_ties(tree: exp.Expression) -> bool:
    """Append every output column (by position) to a top-level ORDER BY.

    ``ORDER BY failures DESC LIMIT 50`` with ties returns a different set and order of rows on
    each run under parallel execution. Extra sort keys only decide between rows the query
    already considers equal, so they never change what it asks for, and they make AI-written
    queries reproducible (replay depends on it).
    """
    order = tree.args.get("order") if isinstance(tree, exp.Select) else None
    if order is None or any(isinstance(p, exp.Star) for p in tree.expressions):
        return False
    for position in range(1, len(tree.expressions) + 1):
        order.append("expressions", exp.Ordered(this=exp.Literal.number(position)))
    return True


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------

CHECK_NAMES = (
    "single_statement",
    "read_only_query",
    "functions_allowed",
    "tables_allowed",
    "columns_allowed",
    "row_level_policy",
    "time_window",
    "row_limit",
    "join_guards",
)


def validate_sql(
    sql: str,
    *,
    role: RolePolicy,
    layer: SemanticLayer,
    catalog: Catalog,
    limits: LimitSettings,
) -> ValidationReport:
    """Validate ``sql`` for ``role`` and return the report (with ``sql_out`` if it passed)."""
    report = ValidationReport(role=role.name, sql_in=sql)

    def skip_rest(start: int) -> ValidationReport:
        for name in CHECK_NAMES[start:]:
            report.add(name, "skipped", "not run because an earlier check failed")
        return report

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except ParseError as exc:
        report.add("single_statement", "fail", f"could not parse SQL: {str(exc).splitlines()[0]}")
        return skip_rest(1)
    if len(statements) != 1:
        report.add(
            "single_statement", "fail", f"expected exactly one statement, got {len(statements)}"
        )
        return skip_rest(1)
    report.add("single_statement", "pass", "one statement")
    tree = statements[0]

    def run(name: str, fn: Callable[[], str]) -> bool:
        try:
            report.add(name, "pass", fn())
            return True
        except _CheckFailedError as exc:
            report.add(name, "fail", str(exc))
            return False

    if not run("read_only_query", lambda: _check_read_only(tree)):
        return skip_rest(2)
    functions_ok = run("functions_allowed", lambda: _check_functions(tree))
    tables_ok = run("tables_allowed", lambda: _check_tables(tree, role, catalog))

    qualified: exp.Expression | None = None
    if tables_ok:
        try:
            detail, qualified = _check_columns(tree, role, layer, catalog)
            report.add("columns_allowed", "pass", detail)
        except _CheckFailedError as exc:
            report.add("columns_allowed", "fail", str(exc))
    else:
        report.add("columns_allowed", "skipped", "needs known tables")

    rewritten: exp.Expression | None = None
    if tables_ok and functions_ok:
        view_columns = {v: set(cols) for v, cols in catalog.items()}
        try:
            rewritten, applied = apply_row_filters(tree, role, view_columns, _cte_names(tree))
            report.add(
                "row_level_policy",
                "pass",
                "; ".join(applied) if applied else "role has no row filters",
            )
        except RowFilterError as exc:
            report.add("row_level_policy", "fail", str(exc))
    else:
        report.add("row_level_policy", "skipped", "needs known tables")

    if qualified is not None:
        run(
            "time_window",
            lambda: _check_time_window(qualified, layer, limits.max_time_window_days),
        )
    else:
        report.add("time_window", "skipped", "needs resolved columns")

    if rewritten is not None:
        try:
            rewritten, detail = _apply_limit(rewritten, limits.default_row_limit)
            if _break_ties(rewritten):
                detail += "; output columns appended to ORDER BY as tie-breakers"
            report.add("row_limit", "pass", detail)
        except _CheckFailedError as exc:
            report.add("row_limit", "fail", str(exc))
    else:
        report.add("row_limit", "skipped", "needs the rewritten query")

    run("join_guards", lambda: _check_joins(tree, limits.max_joins))

    if report.passed and rewritten is not None:
        report.sql_out = rewritten.sql(dialect="duckdb", pretty=True, comments=False)
    return report
