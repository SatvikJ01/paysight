"""Pydantic models for the semantic layer and for :class:`QuerySpec` (SPEC §7)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FilterOp = Literal["=", "!=", "in", "not_in", "between", ">=", "<="]
MetricKind = Literal["rate", "ratio", "count", "sum", "quantile"]
MetricFormat = Literal["percent", "integer", "inr", "milliseconds"]
Sensitivity = Literal["public", "internal", "pii"]
Granularity = Literal["hour", "day", "week"]
FilterValue = str | int | float | date


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ViewDef(_Model):
    """A governed view the compiler and validator may target."""

    description: str
    grain: str
    fact: bool
    time_column: str
    pii_columns: list[str] = Field(default_factory=list)


class Metric(_Model):
    """A governed metric. Rates and ratios always expose numerator and denominator."""

    name: str
    description: str
    synonyms: list[str] = Field(default_factory=list)
    kind: MetricKind
    view: str
    numerator_sql: str
    denominator_sql: str | None = None
    format: MetricFormat
    caveats: list[str] = Field(default_factory=list)
    default_filters: list[Filter] = Field(default_factory=list)

    @model_validator(mode="after")
    def _denominator_matches_kind(self) -> Metric:
        needs_den = self.kind in ("rate", "ratio")
        if needs_den != (self.denominator_sql is not None):
            raise ValueError(
                f"metric {self.name}: denominator_sql is required iff kind is rate/ratio"
            )
        return self

    @property
    def has_denominator(self) -> bool:
        """Whether the metric is a quotient (rate or ratio)."""
        return self.denominator_sql is not None


class Dimension(_Model):
    """A governed dimension, available on the views listed in ``sources``."""

    name: str
    description: str
    synonyms: list[str] = Field(default_factory=list)
    sources: dict[str, str]
    sensitivity: Sensitivity
    allowed_ops: list[FilterOp]


class Filter(_Model):
    """A filter on a dimension. Values are always bound as typed literals, never as SQL text."""

    dimension: str
    op: FilterOp
    values: list[FilterValue] = Field(min_length=1)

    @model_validator(mode="after")
    def _arity(self) -> Filter:
        if self.op == "between" and len(self.values) != 2:
            raise ValueError("'between' needs exactly two values")
        if self.op in ("=", "!=", ">=", "<=") and len(self.values) != 1:
            raise ValueError(f"'{self.op}' needs exactly one value")
        return self


class TimeWindow(_Model):
    """An inclusive date window on transaction time."""

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.start > self.end:
            raise ValueError("time window start must not be after end")
        return self

    @property
    def days(self) -> int:
        """Number of calendar days in the window (inclusive)."""
        return (self.end - self.start).days + 1

    @property
    def end_exclusive(self) -> date:
        """The day after ``end``; used for half-open timestamp predicates."""
        return self.end + timedelta(days=1)

    def label(self) -> str:
        """Human-readable ``YYYY-MM-DD..YYYY-MM-DD``."""
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


class OrderBy(_Model):
    """Sort key: a metric or dimension name in the spec's output."""

    field: str
    descending: bool = True


class QuerySpec(_Model):
    """A query in semantic terms (SPEC §7.5). ``time_window`` is required."""

    metrics: list[str] = Field(min_length=1)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time_window: TimeWindow
    compare_window: TimeWindow | None = None
    granularity: Granularity | None = None
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = Field(default=None, gt=0)


class SemanticLayer(_Model):
    """The full governed vocabulary loaded from ``config/semantic_layer.yaml``."""

    views: dict[str, ViewDef]
    metrics: list[Metric]
    dimensions: list[Dimension]

    def metric(self, name: str) -> Metric:
        """Look up a metric by name; raises ``KeyError`` with the known names."""
        for m in self.metrics:
            if m.name == name:
                return m
        raise KeyError(f"unknown metric {name!r}; known: {[m.name for m in self.metrics]}")

    def dimension(self, name: str) -> Dimension:
        """Look up a dimension by name; raises ``KeyError`` with the known names."""
        for d in self.dimensions:
            if d.name == name:
                return d
        raise KeyError(f"unknown dimension {name!r}; known: {[d.name for d in self.dimensions]}")

    def column_sensitivity(self, view: str, column: str) -> Sensitivity:
        """Sensitivity of a physical view column (pii if listed, else from dimensions)."""
        if column in self.views[view].pii_columns:
            return "pii"
        for d in self.dimensions:
            if d.sources.get(view) == column:
                return d.sensitivity
        return "internal"


Metric.model_rebuild()
