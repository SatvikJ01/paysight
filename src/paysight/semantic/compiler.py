"""Compile a :class:`QuerySpec` to DuckDB SQL with sqlglot's expression builder (SPEC §7.6).

Values from the spec (filter values, dates, limits) only ever become typed sqlglot literals; they
are never interpolated into SQL text. The only SQL text parsed here is the governed metric
definitions from ``config/semantic_layer.yaml``.

Output columns:

* ``period`` when a granularity is set (``DATE_TRUNC`` of the view's time column);
* ``comparison_window`` (``'baseline'`` / ``'current'``) when a compare window is set;
* one column per dimension, named after the dimension;
* per metric: ``<name>`` and, for rates and ratios, ``<name>_num`` and ``<name>_den`` too.

``!=`` and ``not_in`` filters are NULL-safe: "everything except StellarFuel" keeps merchants
with no group, instead of silently dropping them through SQL's three-valued logic.

The SQL is pretty-printed and deterministic, because it is shown to users and snapshot-tested.
"""

from __future__ import annotations

from datetime import date

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp

from paysight.semantic.models import Filter, FilterValue, QuerySpec, SemanticLayer, TimeWindow

CURRENT, BASELINE = "current", "baseline"
_TIMESTAMP_TIME_COLUMNS = {"created_at"}


class CompileError(ValueError):
    """The spec cannot be expressed against the semantic layer."""


class CompiledQuery(BaseModel):
    """Compiled SQL plus what the caller needs to interpret its columns."""

    model_config = ConfigDict(frozen=True)

    sql: str
    view: str
    spec: QuerySpec
    dimension_columns: list[str]
    metric_columns: dict[str, list[str]]


def _literal(value: FilterValue) -> exp.Expression:
    if isinstance(value, bool):
        raise CompileError("boolean filter values are not supported")
    if isinstance(value, date):
        return exp.cast(exp.Literal.string(value.isoformat()), exp.DataType.Type.DATE)
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _time_bound(day: date, timestamp: bool) -> exp.Expression:
    target = exp.DataType.Type.TIMESTAMP if timestamp else exp.DataType.Type.DATE
    return exp.cast(exp.Literal.string(day.isoformat()), target)


def _window_condition(column: str, window: TimeWindow, timestamp: bool) -> exp.Expression:
    col = exp.column(column)
    if timestamp:  # half-open on timestamps: [start 00:00, end+1 00:00)
        return exp.and_(
            exp.GTE(this=col, expression=_time_bound(window.start, True)),
            exp.LT(this=col.copy(), expression=_time_bound(window.end_exclusive, True)),
        )
    return exp.and_(
        exp.GTE(this=col, expression=_time_bound(window.start, False)),
        exp.LTE(this=col.copy(), expression=_time_bound(window.end, False)),
    )


def _filter_condition(column: str, f: Filter) -> exp.Expression:
    col = exp.column(column)
    values = [_literal(v) for v in f.values]
    if f.op == "=":
        return exp.EQ(this=col, expression=values[0])
    if f.op == "!=":  # NULL-safe: "not X" keeps rows where the dimension is NULL
        return exp.NullSafeNEQ(this=col, expression=values[0])
    if f.op == ">=":
        return exp.GTE(this=col, expression=values[0])
    if f.op == "<=":
        return exp.LTE(this=col, expression=values[0])
    if f.op == "between":
        return exp.Between(this=col, low=values[0], high=values[1])
    if f.op == "in":
        return exp.In(this=col, expressions=values)
    # not_in, NULL-safe for the same reason as "!=".
    return exp.paren(
        exp.or_(
            exp.Not(this=exp.In(this=col, expressions=values)),
            exp.Is(this=col.copy(), expression=exp.Null()),
        )
    )


def _fragment(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read="duckdb")


def compile_query(spec: QuerySpec, layer: SemanticLayer) -> CompiledQuery:
    """Compile ``spec`` to a single SELECT over one governed view.

    Raises:
        CompileError: metrics span several views, or a dimension/filter is unknown, unavailable
            on the view, or uses an operator the dimension does not allow.
    """
    try:
        metrics = [layer.metric(name) for name in spec.metrics]
    except KeyError as exc:
        raise CompileError(str(exc)) from exc
    views = sorted({m.view for m in metrics})
    if len(views) != 1:
        raise CompileError(f"metrics {spec.metrics} span several views {views}; split the query")
    view = views[0]
    view_def = layer.views[view]
    timestamp = view_def.time_column in _TIMESTAMP_TIME_COLUMNS

    def column_for(dim_name: str) -> str:
        try:
            dim = layer.dimension(dim_name)
        except KeyError as exc:
            raise CompileError(str(exc)) from exc
        if view not in dim.sources:
            raise CompileError(f"dimension {dim_name!r} is not available on {view}")
        return dim.sources[view]

    projections: list[exp.Expression] = []
    group_by: list[exp.Expression] = []
    default_order: list[exp.Expression] = []

    time_col = exp.column(view_def.time_column)
    if spec.granularity:
        period = exp.func("DATE_TRUNC", exp.Literal.string(spec.granularity), time_col)
        projections.append(exp.alias_(period, "period"))
        group_by.append(period.copy())
        default_order.append(exp.Ordered(this=exp.column("period")))
    if spec.compare_window:
        window_case = (
            exp.Case()
            .when(
                _window_condition(view_def.time_column, spec.time_window, timestamp),
                exp.Literal.string(CURRENT),
            )
            .else_(exp.Literal.string(BASELINE))
        )
        projections.append(exp.alias_(window_case, "comparison_window"))
        group_by.append(window_case.copy())
        default_order.append(exp.Ordered(this=exp.column("comparison_window")))

    dimension_columns = []
    for dim_name in spec.dimensions:
        column = column_for(dim_name)
        projections.append(
            exp.column(column) if column == dim_name else exp.alias_(exp.column(column), dim_name)
        )
        group_by.append(exp.column(column))
        default_order.append(exp.Ordered(this=exp.column(dim_name)))
        dimension_columns.append(dim_name)

    metric_columns: dict[str, list[str]] = {}
    for m in metrics:
        num = _fragment(m.numerator_sql)
        if m.denominator_sql is None:
            projections.append(exp.alias_(num, m.name))
            metric_columns[m.name] = [m.name]
            continue
        den = _fragment(m.denominator_sql)
        value = exp.Div(
            this=exp.cast(num.copy(), exp.DataType.Type.DOUBLE),
            expression=exp.func("NULLIF", den.copy(), exp.Literal.number(0)),
        )
        projections += [
            exp.alias_(num, f"{m.name}_num"),
            exp.alias_(den, f"{m.name}_den"),
            exp.alias_(value, m.name),
        ]
        metric_columns[m.name] = [m.name, f"{m.name}_num", f"{m.name}_den"]

    # WHERE: the time window (an envelope over both windows when comparing), then filters.
    windows = [spec.time_window] + ([spec.compare_window] if spec.compare_window else [])
    envelope = TimeWindow(start=min(w.start for w in windows), end=max(w.end for w in windows))
    conditions = [_window_condition(view_def.time_column, envelope, timestamp)]
    if spec.compare_window and envelope.days > spec.time_window.days + spec.compare_window.days:
        conditions.append(
            exp.or_(
                exp.paren(_window_condition(view_def.time_column, spec.time_window, timestamp)),
                exp.paren(_window_condition(view_def.time_column, spec.compare_window, timestamp)),
            )
        )
    for f in [*(df for m in metrics for df in m.default_filters), *spec.filters]:
        column = column_for(f.dimension)
        if f.op not in layer.dimension(f.dimension).allowed_ops:
            raise CompileError(f"operator {f.op!r} is not allowed on {f.dimension!r}")
        conditions.append(_filter_condition(column, f))

    query = exp.select(*projections).from_(exp.to_table(view)).where(exp.and_(*conditions))
    if group_by:
        query = query.group_by(*group_by)

    known_outputs = {c for cols in metric_columns.values() for c in cols} | set(dimension_columns)
    known_outputs |= {"period"} if spec.granularity else set()
    order = []
    for ob in spec.order_by:
        if ob.field not in known_outputs:
            raise CompileError(f"cannot order by {ob.field!r}; not an output column")
        order.append(exp.Ordered(this=exp.column(ob.field), desc=ob.descending))
    order = order or default_order
    if order:
        query = query.order_by(*order)
    if spec.limit is not None:
        query = query.limit(spec.limit)

    return CompiledQuery(
        sql=query.sql(dialect="duckdb", pretty=True),
        view=view,
        spec=spec,
        dimension_columns=dimension_columns,
        metric_columns=metric_columns,
    )
