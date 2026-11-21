"""Drill-down on a segment (SPEC §10.4): failure codes, series, merchants and breakdowns."""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel, ConfigDict

from paysight.analysis.compare import require_ok
from paysight.analysis.drivers import segment_counts
from paysight.analysis.stats import benjamini_hochberg, two_proportion_z_test
from paysight.config import AnalysisSettings
from paysight.execution.engine import Engine, QueryResult
from paysight.safety.access import RolePolicy
from paysight.semantic.models import Filter, Granularity, QuerySpec, TimeWindow

SR = "attempt_success_rate"


class FailureCodeShift(BaseModel):
    """Failures with one code per 100 attempts, in each window."""

    model_config = ConfigDict(frozen=True)

    failure_code: str
    failure_category: str
    count0: int
    count1: int
    per100_0: float
    per100_1: float
    delta_per100: float


def failure_code_shift(
    engine: Engine,
    role: RolePolicy,
    *,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter] | None = None,
) -> tuple[list[FailureCodeShift], list[QueryResult]]:
    """How each failure code's incidence (per 100 attempts) changed between the windows."""
    filters = filters or []
    _, totals = engine.run_spec(
        QuerySpec(
            metrics=["attempts"], filters=filters, time_window=current, compare_window=baseline
        ),
        role,
    )
    require_ok(totals, "attempt totals")
    attempts = dict(zip(totals.frame["comparison_window"], totals.frame["attempts"], strict=True))
    _, codes = engine.run_spec(
        QuerySpec(
            metrics=["failed_attempts"],
            dimensions=["failure_code", "failure_category"],
            filters=filters,
            time_window=current,
            compare_window=baseline,
        ),
        role,
    )
    require_ok(codes, "failure codes")
    frame = codes.frame.dropna(subset=["failure_code"])  # successes have no code
    n0 = float(attempts.get("baseline", 0) or 0)
    n1 = float(attempts.get("current", 0) or 0)
    out = []
    for (code, cat), g in frame.groupby(["failure_code", "failure_category"], sort=True):
        c0 = int(g.loc[g["comparison_window"] == "baseline", "failed_attempts"].sum())
        c1 = int(g.loc[g["comparison_window"] == "current", "failed_attempts"].sum())
        p0 = 100 * c0 / n0 if n0 else 0.0
        p1 = 100 * c1 / n1 if n1 else 0.0
        out.append(
            FailureCodeShift(
                failure_code=str(code),
                failure_category=str(cat),
                count0=c0,
                count1=c1,
                per100_0=p0,
                per100_1=p1,
                delta_per100=p1 - p0,
            )
        )
    out.sort(key=lambda s: abs(s.delta_per100), reverse=True)
    return out, [totals, codes]


def rate_series(
    engine: Engine,
    role: RolePolicy,
    *,
    window: TimeWindow,
    filters: list[Filter] | None = None,
    granularity: Granularity = "day",
) -> tuple[pd.DataFrame, QueryResult]:
    """Attempt SR per period: columns ``period``, ``num``, ``den``, ``rate``."""
    _, result = engine.run_spec(
        QuerySpec(metrics=[SR], filters=filters or [], time_window=window, granularity=granularity),
        role,
    )
    require_ok(result, f"{granularity} series")
    f = result.frame
    series = pd.DataFrame(
        {
            "period": pd.to_datetime(f["period"]),
            "num": f[f"{SR}_num"].astype(float),
            "den": f[f"{SR}_den"].astype(float),
            "rate": f[SR].astype(float),
        }
    )
    return series, result


def hour_by_date(
    engine: Engine, role: RolePolicy, *, window: TimeWindow, filters: list[Filter] | None = None
) -> tuple[pd.DataFrame, QueryResult]:
    """Attempt SR for every (date, hour_of_day) cell: the heatmap behind S4-style questions."""
    _, result = engine.run_spec(
        QuerySpec(
            metrics=[SR],
            dimensions=["date", "hour_of_day"],
            filters=filters or [],
            time_window=window,
        ),
        role,
    )
    require_ok(result, "hour x date matrix")
    f = result.frame
    return (
        pd.DataFrame(
            {
                "date": pd.to_datetime(f["date"]).dt.date,
                "hour_of_day": f["hour_of_day"].astype(int),
                "num": f[f"{SR}_num"].astype(float),
                "den": f[f"{SR}_den"].astype(float),
                "rate": f[SR].astype(float),
            }
        ),
        result,
    )


class SegmentChange(BaseModel):
    """A segment's rate in both windows with a (BH-corrected) significance flag."""

    model_config = ConfigDict(frozen=True)

    segment: str
    den0: float
    den1: float
    rate0: float | None
    rate1: float | None
    delta_pp: float | None
    p_value: float
    q_value: float
    significant: bool


def breakdown(
    engine: Engine,
    role: RolePolicy,
    *,
    dimension: str,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter] | None = None,
    settings: AnalysisSettings,
) -> tuple[list[SegmentChange], QueryResult]:
    """Per-segment rate change within a scope, largest absolute change first."""
    counts, result = segment_counts(
        engine, role, dimension=dimension, current=current, baseline=baseline, filters=filters
    )
    p_values = [
        two_proportion_z_test(r.num0, r.den0, r.num1, r.den1)[1] for r in counts.itertuples()
    ]
    q_values = benjamini_hochberg(p_values)
    out = []
    for r, p, q in zip(counts.itertuples(), p_values, q_values, strict=True):
        rate0 = r.num0 / r.den0 if r.den0 else None
        rate1 = r.num1 / r.den1 if r.den1 else None
        out.append(
            SegmentChange(
                segment=str(r.segment),
                den0=r.den0,
                den1=r.den1,
                rate0=rate0,
                rate1=rate1,
                delta_pp=(rate1 - rate0) * 100 if rate0 is not None and rate1 is not None else None,
                p_value=float(p),
                q_value=float(q),
                significant=bool(q < settings.alpha),
            )
        )
    out.sort(key=lambda s: abs(s.delta_pp or 0.0), reverse=True)
    return out, result


class MerchantImpact(BaseModel):
    """Failures above what the merchant's baseline rate would predict."""

    model_config = ConfigDict(frozen=True)

    merchant_name: str
    attempts: float
    rate0: float
    rate1: float
    excess_failures: float


def top_merchants(
    engine: Engine,
    role: RolePolicy,
    *,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter] | None = None,
    n: int = 5,
    min_baseline_attempts: int = 30,
) -> tuple[list[MerchantImpact], QueryResult]:
    """Merchants with the most excess failures: ``attempts₁ × (rate₀ − rate₁)``."""
    counts, result = segment_counts(
        engine, role, dimension="merchant_name", current=current, baseline=baseline, filters=filters
    )
    counts = counts[(counts["den0"] >= min_baseline_attempts) & (counts["den1"] > 0)]
    impacts = [
        MerchantImpact(
            merchant_name=str(r.segment),
            attempts=r.den1,
            rate0=r.num0 / r.den0,
            rate1=r.num1 / r.den1,
            excess_failures=r.den1 * (r.num0 / r.den0 - r.num1 / r.den1),
        )
        for r in counts.itertuples()
    ]
    impacts.sort(key=lambda m: m.excess_failures, reverse=True)
    return impacts[:n], result
