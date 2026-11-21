"""Deterministic playbooks (SPEC §9.3): one analysis sequence per question type.

Each step runs analysis-toolkit functions (every query through the validator) and records an
:class:`Evidence` item. Numbers shown to the narrator are rounded for display: rates as
percentages with two decimals, contributions in pp. The grounding checker verifies narrated
numbers against these same values.

Steps per playbook (shown in the UI's Plan tab):
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from paysight.analysis.anomaly import detect_anomalies, outage_windows
from paysight.analysis.compare import RateComparison, compare_periods, denominator_sensitivity
from paysight.analysis.decompose import Decomposition
from paysight.analysis.drilldown import (
    breakdown,
    failure_code_shift,
    hour_by_date,
    rate_series,
    top_merchants,
)
from paysight.analysis.drivers import ROOT_CAUSE_DIMENSIONS, localize_rate_effect, scan_drivers
from paysight.analysis.quality import QualityReport, data_quality_report
from paysight.analysis.reconcile import reconcile
from paysight.analysis.stats import two_proportion_z_test, wilson_interval
from paysight.config import Settings
from paysight.execution.engine import Engine, QueryResult
from paysight.orchestrator.models import ChartKind, Evidence, EvidenceKind, ExtraStep, PlanStep
from paysight.safety.access import RolePolicy
from paysight.semantic.compiler import CompileError
from paysight.semantic.models import Filter, OrderBy, QuerySpec, SemanticLayer, TimeWindow

SR = "attempt_success_rate"
RATE_METRICS = {"attempt_success_rate", "order_success_rate", "technical_success_rate"}
ANOMALY_DIMENSIONS = (
    "acquirer_name",
    "payment_method",
    "issuer_name",
    "merchant_group",
    "channel",
    "region",
    "category",
)
TREND_DIMENSIONS = (
    "issuer_name",
    "acquirer_name",
    "channel",
    "merchant_group",
    "region",
    "category",
)

PLAYBOOK_STEPS: dict[str, list[tuple[str, str]]] = {
    "root_cause": [
        ("headline", "Headline metric in both windows, with Wilson intervals and a z-test"),
        ("data_quality", "Data-quality scan over both windows; findings become caveats"),
        ("drivers", "Kitagawa decomposition across 8 dimensions; rank drivers (BH-corrected)"),
        ("decomposition", "Mix vs rate decomposition for the top drivers (waterfall)"),
        ("rate_localization", "Localize the rate effect within payment methods"),
        ("drilldown", "Failure-code shift and daily series for the top rate segment"),
        ("denominators", "Denominator sensitivity: attempt, order and technical SR"),
    ],
    "anomaly_explain": [
        ("daily_series", "Daily success rate around the date; robust-z anomaly windows"),
        ("headline", "Success rate in the anomaly window vs the week before"),
        ("segment_scan", "Which segments explain the drop (decomposition + ranking)"),
        ("drilldown", "Methods affected, failure codes and hourly timeline of the top segment"),
        ("data_quality", "Data-quality scan over the window"),
    ],
    "trend": [
        ("series", "Daily success rate over the period"),
        ("headline", "Period vs the preceding period of equal length"),
        ("segment_scan", "Which segments explain the change"),
        (
            "drilldown",
            "Hour-of-day profile, hour x date heatmap and failure codes for the top segment",
        ),
        ("data_quality", "Data-quality scan over the period"),
    ],
    "comparison": [
        ("headline", "Metric in both windows with significance"),
        ("decomposition", "Decomposition across the requested or default dimensions"),
        ("data_quality", "Data-quality scan"),
        ("denominators", "Denominator sensitivity (success-rate questions)"),
    ],
    "metric_lookup": [
        ("lookup", "Compile the metric query from the semantic layer and run it"),
        ("data_quality", "Data-quality scan over the period; raw vs clean counts"),
        ("denominators", "Alternative success-rate definitions (success-rate questions)"),
    ],
    "segment_comparison": [
        ("segments", "Metric per segment in one window, with intervals and a significance test"),
        ("data_quality", "Data-quality scan"),
    ],
    "ranking": [
        ("ranking", "Rank segments by the metric (top 10)"),
        ("data_quality", "Data-quality scan; missing values that affect the ranking"),
    ],
    "reconciliation": [
        ("reconcile", "Align transaction GMV with settlements by covered date"),
        ("data_quality", "Data-quality scan over the window"),
    ],
    "data_quality": [("data_quality", "All data-quality checks over the period")],
    "adhoc": [
        ("adhoc_sql", "AI-written SQL over the governed views; validated, one repair, flagged"),
        ("data_quality", "Data-quality scan over the period"),
    ],
}
__doc__ = (__doc__ or "") + "\n".join(
    f"* ``{k}``: " + "; ".join(name for name, _ in v) for k, v in PLAYBOOK_STEPS.items()
)


class PlanError(RuntimeError):
    """The question cannot be analysed as planned (shown to the user, nothing fabricated)."""


class NotExpressibleError(PlanError):
    """The question cannot be expressed as a QuerySpec; the ad-hoc SQL fallback may answer it."""


@dataclass
class Scope:
    """Everything a playbook needs to know about the question."""

    question_type: str
    metrics: list[str]
    dimensions: list[str]
    filters: list[Filter]
    current: TimeWindow
    baseline: TimeWindow | None
    extra_steps: list[ExtraStep] = field(default_factory=list)


@dataclass
class RunContext:
    """Shared state while running a playbook."""

    engine: Engine
    role: RolePolicy
    settings: Settings
    layer: SemanticLayer
    evidence: list[Evidence] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    dq: QualityReport | None = None
    queries: int = 0

    def add(
        self,
        *,
        title: str,
        kind: EvidenceKind,
        purpose: str,
        results: list[QueryResult],
        filters: list[Filter],
        window: str,
        rows: list[dict[str, Any]] | None = None,
        key_numbers: dict[str, Any] | None = None,
        chart: ChartKind | None = None,
        llm_suggested: bool = False,
        ad_hoc: bool = False,
        chart_data: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Evidence:
        """Record one evidence item with the SQL and validation of every query behind it."""
        self.queries += sum(1 for r in results if r.status != "blocked" and not r.cache_hit)
        datasets = sorted(
            {m for r in results for m in re.findall(r"\b(v_\w+|fact_\w+|dim_\w+)\b", r.sql or "")}
        )
        ev = Evidence(
            id=f"E{len(self.evidence) + 1}",
            title=title,
            kind=kind,
            purpose=purpose,
            sql=[r.sql for r in results if r.sql],
            validation=[r.report for r in results],
            runtime_ms=sum(r.runtime_ms for r in results),
            cache_hits=sum(r.cache_hit for r in results),
            datasets=datasets,
            filters=[_fmt_filter(f) for f in filters],
            time_window=window,
            result=[_clean(row) for row in (rows or [])[:50]],
            key_numbers={k: _clean_value(v) for k, v in (key_numbers or {}).items()},
            chart=chart,
            llm_suggested=llm_suggested,
            ad_hoc=ad_hoc,
            chart_data=[_clean(row) for row in (chart_data or [])],
            meta=meta or {},
        )
        self.evidence.append(ev)
        return ev


def _fmt_filter(f: Filter) -> str:
    return f"{f.dimension} {f.op} {', '.join(str(v) for v in f.values)}"


def _clean_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, pd.Timestamp | datetime):
        return (
            v.isoformat(sep=" ", timespec="minutes")
            if (v.hour or v.minute)
            else v.date().isoformat()
        )
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        return _clean_value(v.item())
    return v


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _clean_value(v) for k, v in row.items()}


def pct(x: float | None) -> float | None:
    """Fraction to a percentage with two decimals."""
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(100 * x, 2)


def _window_label(a: TimeWindow, b: TimeWindow | None = None) -> str:
    return a.label() if b is None else f"{a.label()} vs {b.label()}"


# --------------------------------------------------------------------------------------------
# Shared steps
# --------------------------------------------------------------------------------------------


def headline_step(
    ctx: RunContext, metric: str, current: TimeWindow, baseline: TimeWindow, filters: list[Filter]
) -> RateComparison:
    """Rate in both windows with intervals and a z-test."""
    comp, result = compare_periods(
        ctx.engine,
        ctx.role,
        metric=metric,
        current=current,
        baseline=baseline,
        filters=filters,
        settings=ctx.settings.analysis,
    )
    ctx.add(
        title=f"{metric} in both windows",
        kind="test",
        purpose="Size and significance of the change",
        results=[result],
        filters=filters,
        window=_window_label(current, baseline),
        rows=[
            {
                "window": "baseline",
                "period": baseline.label(),
                "successes": comp.num0,
                "attempts": comp.den0,
                "rate_pct": pct(comp.rate0),
            },
            {
                "window": "current",
                "period": current.label(),
                "successes": comp.num1,
                "attempts": comp.den1,
                "rate_pct": pct(comp.rate1),
            },
        ],
        key_numbers={
            "baseline_rate_pct": pct(comp.rate0),
            "current_rate_pct": pct(comp.rate1),
            "delta_pp": round(comp.delta_pp, 2),
            "baseline_ci_low_pct": pct(comp.ci0[0]),
            "baseline_ci_high_pct": pct(comp.ci0[1]),
            "current_ci_low_pct": pct(comp.ci1[0]),
            "current_ci_high_pct": pct(comp.ci1[1]),
            "baseline_attempts": int(comp.den0),
            "current_attempts": int(comp.den1),
            "p_value": float(f"{comp.p_value:.3g}"),
            "statistically_significant": comp.statistically_significant,
            "practically_significant": comp.practically_significant,
        },
        chart="table",
    )
    return comp


def dq_step(ctx: RunContext, window: TimeWindow, metrics: list[str], dimensions: list[str]) -> None:
    """Data-quality scan; relevant findings become caveats."""
    report, results = data_quality_report(
        ctx.engine,
        ctx.role,
        window=window,
        as_of=ctx.settings.data.as_of_date,
        settings=ctx.settings.analysis,
    )
    ctx.dq = report
    relevant = report.affecting(metrics, dimensions) if metrics or dimensions else report.findings
    for f in relevant:
        ctx.caveats.append(f"{f.title}: {f.detail} Handling: {f.handled}.")
    rows = [
        {
            "check": f.check,
            "severity": f.severity,
            "title": f.title,
            "detail": f.detail,
            "handled": f.handled,
        }
        for f in report.findings
    ]
    key: dict[str, Any] = {"findings": len(report.findings)}
    for f in report.findings:
        for r in f.rows:
            if "raw_rows" in r:
                key[f"raw_rows_{r['day']}"] = r["raw_rows"]
                key[f"clean_rows_{r['day']}"] = r["clean_rows"]
                key[f"duplicates_{r['day']}"] = r["raw_rows"] - r["clean_rows"]
            if "null_rate" in r:
                key[f"null_issuer_pct_{r['acquirer']}_{r['month']}"] = pct(r["null_rate"])
    ctx.add(
        title="Data-quality scan",
        kind="dq",
        purpose="Duplicates, missing values, orphan keys, timestamps, ingestion lag, volume",
        results=results,
        filters=[],
        window=window.label(),
        rows=rows,
        key_numbers=key,
        chart="table",
    )


def sensitivity_step(ctx: RunContext, window: TimeWindow, filters: list[Filter]) -> None:
    """Attempt, order and technical SR side by side."""
    sens, results = denominator_sensitivity(
        ctx.engine, ctx.role, window=window, filters=filters, settings=ctx.settings.analysis
    )
    if sens.note_needed:
        ctx.caveats.append(
            "Success rate depends on the denominator: attempt-level "
            f"{pct(sens.rate('attempt_success_rate'))}%, order-level "
            f"{pct(sens.rate('order_success_rate'))}%, technical "
            f"{pct(sens.rate('technical_success_rate'))}% (retries and drop-offs explain the gap)."
        )
    ctx.add(
        title="Success rate under three definitions",
        kind="sensitivity",
        purpose="Show how the denominator changes the success rate",
        results=results,
        filters=filters,
        window=window.label(),
        rows=[
            {
                "definition": v.metric,
                "numerator": v.numerator,
                "denominator": v.denominator,
                "rate_pct": pct(v.rate),
            }
            for v in sens.values
        ],
        key_numbers={
            "attempt_sr_pct": pct(sens.rate("attempt_success_rate")),
            "order_sr_pct": pct(sens.rate("order_success_rate")),
            "technical_sr_pct": pct(sens.rate("technical_success_rate")),
            "max_divergence_pp": round(sens.max_divergence_pp, 2),
        },
        chart="bar",
    )


def _decomposition_evidence(
    ctx: RunContext,
    d: Decomposition,
    filters: list[Filter],
    window: str,
    results: list[QueryResult],
) -> None:
    rows = [
        {
            "segment": s.segment,
            "share0_pct": pct(s.share0),
            "share1_pct": pct(s.share1),
            "rate0_pct": pct(s.rate0),
            "rate1_pct": pct(s.rate1),
            "mix_pp": round(s.mix_centered * 100, 2),
            "rate_pp": round(s.rate * 100, 2),
            "total_pp": round(s.attributed * 100, 2),
        }
        for s in d.segments
    ]
    ctx.add(
        title=f"Mix vs rate decomposition by {d.dimension}",
        kind="decomposition",
        purpose="Split the change into mix (share shifts) and rate (within-segment) effects",
        results=results,
        filters=filters,
        window=window,
        rows=rows,
        key_numbers={
            "rate0_pct": pct(d.rate0),
            "rate1_pct": pct(d.rate1),
            "delta_pp": round(d.delta * 100, 2),
            "mix_effect_pp": round(d.mix_effect * 100, 2),
            "rate_effect_pp": round(d.rate_effect * 100, 2),
            "mix_share_pct": pct(d.mix_share),
        },
        chart="waterfall",
    )


def _extras(
    ctx: RunContext,
    scope: Scope,
    filters: list[Filter],
    current: TimeWindow,
    baseline: TimeWindow | None,
) -> None:
    """Run validated LLM-suggested steps (at most 3), marked as such."""
    a = ctx.settings.analysis
    for step in scope.extra_steps:
        if step.tool == "breakdown" and step.dimension and baseline is not None:
            rows, result = breakdown(
                ctx.engine,
                ctx.role,
                dimension=step.dimension,
                current=current,
                baseline=baseline,
                filters=filters,
                settings=a,
            )
            ctx.add(
                title=f"Breakdown by {step.dimension}",
                kind="query",
                purpose=step.reason,
                results=[result],
                filters=filters,
                window=_window_label(current, baseline),
                rows=[
                    {
                        "segment": r.segment,
                        "rate0_pct": pct(r.rate0),
                        "rate1_pct": pct(r.rate1),
                        "delta_pp": r.delta_pp and round(r.delta_pp, 2),
                        "significant": r.significant,
                    }
                    for r in rows
                ],
                chart="bar",
                llm_suggested=True,
            )
        elif step.tool == "failure_codes" and baseline is not None:
            _failure_codes(ctx, current, baseline, filters, llm_suggested=True, purpose=step.reason)
        elif step.tool == "daily_series":
            _series(
                ctx,
                current if baseline is None else TimeWindow(start=baseline.start, end=current.end),
                filters,
                llm_suggested=True,
                purpose=step.reason,
            )
        elif step.tool == "hourly_profile" and baseline is not None:
            _hour_profile(ctx, current, baseline, filters, llm_suggested=True, purpose=step.reason)
        elif step.tool == "top_merchants" and baseline is not None:
            merchants, result = top_merchants(
                ctx.engine, ctx.role, current=current, baseline=baseline, filters=filters
            )
            ctx.add(
                title="Merchants with the most excess failures",
                kind="ranking",
                purpose=step.reason,
                results=[result],
                filters=filters,
                window=_window_label(current, baseline),
                rows=[
                    {
                        "merchant": m.merchant_name,
                        "attempts": m.attempts,
                        "rate0_pct": pct(m.rate0),
                        "rate1_pct": pct(m.rate1),
                        "excess_failures": round(m.excess_failures),
                    }
                    for m in merchants
                ],
                chart="bar",
                llm_suggested=True,
            )


def _failure_codes(
    ctx: RunContext,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter],
    *,
    llm_suggested: bool = False,
    purpose: str = "Which failure codes changed",
) -> None:
    codes, results = failure_code_shift(
        ctx.engine, ctx.role, current=current, baseline=baseline, filters=filters
    )
    top = codes[0] if codes else None
    ctx.add(
        title="Failure-code shift (per 100 attempts)",
        kind="failure_codes",
        purpose=purpose,
        results=results,
        filters=filters,
        window=_window_label(current, baseline),
        rows=[
            {
                "failure_code": c.failure_code,
                "category": c.failure_category,
                "failures_baseline": c.count0,
                "failures_current": c.count1,
                "per_100_attempts_baseline": round(c.per100_0, 2),
                "per_100_attempts_current": round(c.per100_1, 2),
                "change_per_100_attempts": round(c.delta_per100, 2),
            }
            for c in codes[:12]
        ],
        key_numbers={}
        if top is None
        else {
            "top_code": top.failure_code,
            "top_code_per_100_attempts_baseline": round(top.per100_0, 2),
            "top_code_per_100_attempts_current": round(top.per100_1, 2),
            "top_code_failures_current": top.count1,
        },
        chart="failure_codes",
        llm_suggested=llm_suggested,
    )


def _series(
    ctx: RunContext,
    window: TimeWindow,
    filters: list[Filter],
    *,
    llm_suggested: bool = False,
    purpose: str = "Daily success rate",
    summary: bool = False,
) -> pd.DataFrame:
    series, result = rate_series(ctx.engine, ctx.role, window=window, filters=filters)
    windows = _daily_anomalies(ctx, series)
    series_rows = [
        {"date": r.period.date().isoformat(), "attempts": int(r.den), "rate_pct": pct(r.rate)}
        for r in series.itertuples()
    ]
    ctx.add(
        title="Daily success rate",
        kind="series",
        purpose=purpose,
        results=[result],
        filters=filters,
        window=window.label(),
        rows=series_rows,
        chart_data=series_rows,
        # The narrator sees only the first rows, so a series that answers "when" is summarised
        # in full here; context series keep the short form (D37).
        key_numbers=_daily_summary(series, windows)
        if summary
        else {"anomaly_windows": _window_list(windows)},
        chart="daily_line",
        llm_suggested=llm_suggested,
    )
    return series


def _daily_anomalies(ctx: RunContext, series: pd.DataFrame) -> list:
    """Daily anomaly windows: robust z plus the practical-magnitude threshold (D37)."""
    a = ctx.settings.analysis
    return detect_anomalies(
        series,
        step=timedelta(days=1),
        min_den=50,
        z_threshold=a.anomaly_z_threshold,
        min_delta=a.anomaly_min_delta_pp / 100,
    )


def _daily_summary(series: pd.DataFrame, windows: list) -> dict[str, Any]:
    """Days, median, lowest and highest day, and anomaly windows with their size."""
    judged = series.loc[series["den"] > 0]
    if judged.empty:
        return {"anomaly_windows": "none"}
    low, high = judged.loc[judged["rate"].idxmin()], judged.loc[judged["rate"].idxmax()]
    return {
        "days": len(judged),
        "median_daily_rate_pct": pct(float(judged["rate"].median())),
        "lowest_day": low.period.date().isoformat(),
        "lowest_day_rate_pct": pct(float(low.rate)),
        "highest_day": high.period.date().isoformat(),
        "highest_day_rate_pct": pct(float(high.rate)),
        "anomaly_windows": "; ".join(
            f"{w.start:%Y-%m-%d}..{w.end:%Y-%m-%d} ({w.direction}; worst day "
            f"{pct(w.worst_rate)}% vs median {pct(w.baseline_rate)}%)"
            for w in windows
        )
        or "none",
    }


def _window_list(windows: list) -> str:
    """The short form: 'start..end (drop)' per window."""
    return (
        "; ".join(f"{w.start:%Y-%m-%d}..{w.end:%Y-%m-%d} ({w.direction})" for w in windows)
        or "none"
    )


def _hour_profile(
    ctx: RunContext,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter],
    *,
    llm_suggested: bool = False,
    purpose: str = "Success rate by hour of day",
) -> list:
    rows, result = breakdown(
        ctx.engine,
        ctx.role,
        dimension="hour_of_day",
        current=current,
        baseline=baseline,
        filters=filters,
        settings=ctx.settings.analysis,
    )
    rows = sorted(rows, key=lambda r: int(float(r.segment)))
    sig = [r for r in rows if r.significant and (r.delta_pp or 0) < 0]
    ctx.add(
        title="Success rate by hour of day",
        kind="query",
        purpose=purpose,
        results=[result],
        filters=filters,
        window=_window_label(current, baseline),
        rows=[
            {
                "hour": int(float(r.segment)),
                "rate0_pct": pct(r.rate0),
                "rate1_pct": pct(r.rate1),
                "delta_pp": r.delta_pp and round(r.delta_pp, 2),
                "significant": r.significant,
            }
            for r in rows
        ],
        key_numbers={
            "hours_with_significant_drop": ", ".join(str(int(float(r.segment))) for r in sig)
            or "none",
            "worst_hour": int(float(min(rows, key=lambda r: r.delta_pp or 0).segment))
            if rows
            else None,
            "worst_hour_delta_pp": round(min((r.delta_pp or 0) for r in rows), 2) if rows else None,
        },
        chart="bar",
        llm_suggested=llm_suggested,
    )
    return rows


def _hourly_timeline(
    ctx: RunContext, window: TimeWindow, filters: list[Filter], *, title: str, reference: TimeWindow
) -> None:
    """Hourly success rate and the outage: hours significantly and practically below the
    rate in ``reference`` (merged, one normal hour allowed inside), with start, end, duration
    and worst hour computed here, because the narrator sees only the first rows."""
    a = ctx.settings.analysis
    hours, hresult = rate_series(
        ctx.engine, ctx.role, window=window, filters=filters, granularity="hour"
    )
    ref, rresult = rate_series(ctx.engine, ctx.role, window=reference, filters=filters)
    ref_den = float(ref["den"].sum())
    ref_rate = float(ref["num"].sum()) / ref_den if ref_den else 0.0
    found = outage_windows(
        hours,
        reference_rate=ref_rate,
        step=timedelta(hours=1),
        z_threshold=a.anomaly_z_threshold,
        min_delta=a.anomaly_min_delta_pp / 100,
        min_den=10,
    )
    main = max(found, key=lambda x: x.excess_failures) if found else None
    hour_rows = [
        {
            "hour": r.period.isoformat(sep=" ", timespec="minutes"),
            "attempts": int(r.den),
            "rate_pct": pct(r.rate),
        }
        for r in hours.itertuples()
    ]
    key: dict[str, Any] = {
        "reference_window": reference.label(),
        "reference_rate_pct": pct(ref_rate),
    }
    if main is not None:
        inside = hours.loc[(hours["period"] >= main.start) & (hours["period"] <= main.end)]
        worst = inside.loc[(inside["num"] / inside["den"]).idxmin()] if len(inside) else None
        key |= {
            "outage_start": main.start.isoformat(sep=" ", timespec="minutes"),
            "outage_end": main.end_exclusive.isoformat(sep=" ", timespec="minutes"),
            "outage_hours": round((main.end_exclusive - main.start).total_seconds() / 3600),
            "worst_hour": None
            if worst is None
            else pd.Timestamp(worst.period).isoformat(sep=" ", timespec="minutes"),
            "worst_hour_rate_pct": pct(main.worst_rate),
            "excess_failed_attempts": round(main.excess_failures),
            "affected_attempts": int(main.attempts),
        }
    else:
        key["outage"] = "none found"
    ctx.add(
        title=title,
        kind="anomaly",
        purpose="When the problem started and ended, and its impact",
        results=[hresult, rresult],
        filters=filters,
        window=window.label(),
        rows=hour_rows,
        chart_data=hour_rows,
        key_numbers=key,
        chart="daily_line",
    )


def _explain_drop(
    ctx: RunContext, seg_filter: list[Filter], seg: str, win: TimeWindow, base: TimeWindow
) -> None:
    """Inside a drop window: which payment methods fell, their failure codes, and an hourly
    timeline of the affected traffic (when it started and ended)."""
    d = ctx.settings.data
    methods, mresult = breakdown(
        ctx.engine,
        ctx.role,
        dimension="payment_method",
        current=win,
        baseline=base,
        filters=seg_filter,
        settings=ctx.settings.analysis,
    )
    affected = [m.segment for m in methods if m.significant and (m.delta_pp or 0) < -2]
    ctx.add(
        title=f"Payment methods within {seg}",
        kind="query",
        purpose="Which methods were affected",
        results=[mresult],
        filters=seg_filter,
        window=_window_label(win, base),
        rows=[
            {
                "payment_method": m.segment,
                "attempts": m.den1,
                "rate0_pct": pct(m.rate0),
                "rate1_pct": pct(m.rate1),
                "delta_pp": m.delta_pp and round(m.delta_pp, 2),
                "significant": m.significant,
            }
            for m in methods
        ],
        key_numbers={"affected_methods": ", ".join(affected) or "none"},
        chart="bar",
    )
    focus = seg_filter + (
        [Filter(dimension="payment_method", op="in", values=affected)] if affected else []
    )
    _failure_codes(ctx, win, base, focus, purpose=f"Failure codes for {seg}")
    hourly_window = TimeWindow(
        start=max(d.start_date, win.start - timedelta(days=1)),
        end=min(d.as_of_date, win.end + timedelta(days=1)),
    )
    _hourly_timeline(
        ctx,
        hourly_window,
        focus,
        title=f"Hourly timeline for {seg}" + (f" ({', '.join(affected)})" if affected else ""),
        reference=base,
    )


# --------------------------------------------------------------------------------------------
# Playbooks
# --------------------------------------------------------------------------------------------


def run_root_cause(ctx: RunContext, scope: Scope) -> None:
    """Why did the success rate change between two windows?"""
    assert scope.baseline is not None
    cur, base, flt = scope.current, scope.baseline, scope.filters
    a = ctx.settings.analysis
    label = _window_label(cur, base)
    metric = next((m for m in scope.metrics if m in RATE_METRICS), SR)
    headline_step(ctx, metric, cur, base, flt)
    dq_step(
        ctx,
        TimeWindow(start=base.start, end=cur.end),
        [metric, "attempts"],
        ["issuer_name", *scope.dimensions],
    )

    fixed = {f.dimension for f in flt if f.op == "=" or (f.op == "in" and len(f.values) == 1)}
    dims = tuple(d for d in ROOT_CAUSE_DIMENSIONS if d not in fixed)
    ranking, decomps, results = scan_drivers(
        ctx.engine, ctx.role, current=cur, baseline=base, filters=flt, dimensions=dims, settings=a
    )
    ctx.add(
        title="Driver ranking across dimensions",
        kind="drivers",
        purpose="Which dimensions explain the change beyond their size (BH-corrected tests)",
        results=results,
        filters=flt,
        window=label,
        rows=[
            {
                "rank": d.rank,
                "dimension": d.dimension,
                "strength": round(d.strength, 2),
                "mix_pp": round(d.mix_pp, 2),
                "rate_pp": round(d.rate_pp, 2),
                "top_segment": d.top_segments[0].segment if d.top_segments else None,
                "top_segment_pp": round(d.top_segments[0].attributed_pp, 2)
                if d.top_segments
                else None,
                "top_segment_share_of_change_pct": pct(d.top_segments[0].share_of_change)
                if d.top_segments
                else None,
                "top_segment_volume_pct": pct(
                    (d.top_segments[0].share0 + d.top_segments[0].share1) / 2
                )
                if d.top_segments
                else None,
                "reason": d.reasons[0] if d.reasons else "",
            }
            for d in ranking.dimensions
        ],
        key_numbers={
            "delta_pp": round(ranking.delta_pp, 2),
            "top_dimension": ranking.dimensions[0].dimension,
        },
        chart="contribution_bar",
        meta={"segments_tested": ranking.segments_tested},
    )
    by_dim = {d.dimension: (d, r) for d, r in zip(decomps, results, strict=True)}
    # The mix/rate split is reported by payment method only. Splitting by merchant group would
    # call a group's internal method-mix change a "rate" effect (NovaMart's rate falls because
    # its sale is EMI-heavy). Group-level drivers stay visible in the ranking. DECISIONS D32.
    shown = ["payment_method"] if "payment_method" in by_dim else [ranking.dimensions[0].dimension]
    for dim in shown:
        decomposition, result = by_dim[dim]
        _decomposition_evidence(ctx, decomposition, flt, label, [result])

    local, local_results = localize_rate_effect(
        ctx.engine, ctx.role, current=cur, baseline=base, filters=flt, settings=a
    )
    top = local.top_rate_segments[0] if local.top_rate_segments else None
    ctx.add(
        title="Where the rate effect comes from (within payment method)",
        kind="drivers",
        purpose="Separate genuine rate drops from mix changes inside a segment",
        results=local_results,
        filters=flt,
        window=label,
        rows=[
            {
                "segment": s.segment,
                "rate0_pct": pct(s.rate0),
                "rate1_pct": pct(s.rate1),
                "rate_pp": round(s.rate_pp, 2),
                "q_value": float(f"{s.q_value:.2g}"),
            }
            for s in local.top_rate_segments
        ],
        key_numbers={}
        if top is None
        else {
            "top_rate_segment": top.segment,
            "top_rate_segment_rate0_pct": pct(top.rate0),
            "top_rate_segment_rate1_pct": pct(top.rate1),
            "top_rate_segment_drop_pp": round(((top.rate1 or 0) - (top.rate0 or 0)) * 100, 2),
            "top_rate_segment_contribution_pp": round(top.rate_pp, 2),
        },
        chart="contribution_bar",
    )
    if top is not None:
        value, method = top.segment.split(" | ")
        dim = top.dimension.split(" x ")[0]
        seg_filters = [
            *flt,
            Filter(dimension=dim, op="=", values=[value]),
            Filter(dimension="payment_method", op="=", values=[method]),
        ]
        _failure_codes(ctx, cur, base, seg_filters, purpose=f"Failure codes for {top.segment}")
        _series(
            ctx,
            TimeWindow(start=base.start, end=cur.end),
            seg_filters,
            purpose=f"Daily success rate for {top.segment}",
        )
    if metric in RATE_METRICS:
        sensitivity_step(ctx, cur, flt)
    _extras(ctx, scope, flt, cur, base)


def run_anomaly(ctx: RunContext, scope: Scope) -> None:
    """What happened on a day (or short period)?"""
    a = ctx.settings.analysis
    d = ctx.settings.data
    target, flt = scope.current, scope.filters
    context = TimeWindow(
        start=max(d.start_date, target.start - timedelta(days=14)),
        end=min(d.as_of_date, target.end + timedelta(days=7)),
    )
    series = _series(ctx, context, flt, purpose="Daily success rate around the date", summary=True)
    windows = _daily_anomalies(ctx, series)
    hit = [
        w
        for w in windows
        if w.direction == "drop" and w.start.date() <= target.end and w.end.date() >= target.start
    ]
    if hit:
        w = max(hit, key=lambda x: x.excess_failures)
        win = TimeWindow(start=w.start.date(), end=w.end.date())
    else:
        win = target
        ctx.caveats.append(f"No statistically unusual drop was detected around {target.label()}.")
    base = TimeWindow(start=win.start - timedelta(days=7), end=win.start - timedelta(days=1))
    headline_step(ctx, SR, win, base, flt)

    ranking, _, results = scan_drivers(
        ctx.engine,
        ctx.role,
        current=win,
        baseline=base,
        filters=flt,
        dimensions=ANOMALY_DIMENSIONS,
        settings=a,
    )
    top_dim = ranking.dimensions[0]
    seg = top_dim.top_segments[0].segment
    ctx.add(
        title="Segment scan inside the anomaly window",
        kind="drivers",
        purpose="Which segment concentrates the drop",
        results=results,
        filters=flt,
        window=_window_label(win, base),
        rows=[
            {
                "rank": x.rank,
                "dimension": x.dimension,
                "strength": round(x.strength, 2),
                "top_segment": x.top_segments[0].segment,
                "rate0_pct": pct(x.top_segments[0].rate0),
                "rate1_pct": pct(x.top_segments[0].rate1),
                "top_segment_pp": round(x.top_segments[0].attributed_pp, 2),
            }
            for x in ranking.dimensions
        ],
        key_numbers={"top_dimension": top_dim.dimension, "top_segment": seg},
        chart="contribution_bar",
    )
    seg_filter = [*flt, Filter(dimension=top_dim.dimension, op="=", values=[seg])]
    _explain_drop(ctx, seg_filter, seg, win, base)
    dq_step(ctx, win, [SR, "attempts"], [top_dim.dimension])
    _extras(ctx, scope, flt, win, base)


def run_trend(ctx: RunContext, scope: Scope) -> None:
    """How did a metric behave over a period (vs the preceding period)?"""
    a = ctx.settings.analysis
    cur, flt = scope.current, scope.filters
    base = scope.baseline or TimeWindow(
        start=cur.start - timedelta(days=cur.days), end=cur.start - timedelta(days=1)
    )
    _series(ctx, TimeWindow(start=base.start, end=cur.end), flt, summary=True)
    headline_step(ctx, SR, cur, base, flt)
    fixed = {f.dimension for f in flt}
    dims = tuple(d for d in TREND_DIMENSIONS if d not in fixed)
    ranking, _, results = scan_drivers(
        ctx.engine, ctx.role, current=cur, baseline=base, filters=flt, dimensions=dims, settings=a
    )
    top_dim = ranking.dimensions[0]
    top = top_dim.top_segments[0]
    ctx.add(
        title="Segment scan",
        kind="drivers",
        purpose="Which segment concentrates the change",
        results=results,
        filters=flt,
        window=_window_label(cur, base),
        rows=[
            {
                "rank": x.rank,
                "dimension": x.dimension,
                "strength": round(x.strength, 2),
                "top_segment": x.top_segments[0].segment,
                "rate0_pct": pct(x.top_segments[0].rate0),
                "rate1_pct": pct(x.top_segments[0].rate1),
                "top_segment_pp": round(x.top_segments[0].attributed_pp, 2),
            }
            for x in ranking.dimensions
        ],
        key_numbers={
            "top_dimension": top_dim.dimension,
            "top_segment": top.segment,
            "top_segment_rate0_pct": pct(top.rate0),
            "top_segment_rate1_pct": pct(top.rate1),
        },
        chart="contribution_bar",
    )
    seg_filter = [*flt, Filter(dimension=top_dim.dimension, op="=", values=[top.segment])]
    _hour_profile(ctx, cur, base, seg_filter, purpose=f"Hour-of-day profile for {top.segment}")
    grid, gresult = hour_by_date(
        ctx.engine, ctx.role, window=TimeWindow(start=base.start, end=cur.end), filters=seg_filter
    )
    grid_rows = [
        {
            "date": r.date.isoformat(),
            "hour": r.hour_of_day,
            "attempts": int(r.den),
            "rate_pct": pct(r.rate),
        }
        for r in grid.itertuples()
    ]
    ctx.add(
        title=f"Hour x date success rate for {top.segment}",
        kind="series",
        purpose="Heatmap: is the problem tied to specific hours and dates?",
        results=[gresult],
        filters=seg_filter,
        window=f"{base.start}..{cur.end}",
        rows=grid_rows[:50],
        chart_data=grid_rows,
        key_numbers={},
        chart="heatmap",
        meta={"cells": len(grid)},
    )
    _failure_codes(ctx, cur, base, seg_filter, purpose=f"Failure codes for {top.segment}")
    dq_step(ctx, cur, [SR, "attempts"], [top_dim.dimension])
    _extras(ctx, scope, flt, cur, base)


def run_comparison(ctx: RunContext, scope: Scope) -> None:
    """Compare two windows, decomposing by the requested (or default) dimensions.

    With no baseline, the planner decided this compares segments within one window.
    """
    if scope.baseline is None:
        run_segment_comparison(ctx, scope)
        return
    cur, base, flt = scope.current, scope.baseline, scope.filters
    metric = next((m for m in scope.metrics if m in RATE_METRICS), None)
    if metric is None:
        run_lookup(ctx, scope, compare=True)
        return
    headline_step(ctx, metric, cur, base, flt)
    dims = tuple(scope.dimensions) or ROOT_CAUSE_DIMENSIONS
    _, decomps, results = scan_drivers(
        ctx.engine,
        ctx.role,
        current=cur,
        baseline=base,
        filters=flt,
        dimensions=dims,
        settings=ctx.settings.analysis,
    )
    for d, result in list(zip(decomps, results, strict=True))[:3]:
        _decomposition_evidence(ctx, d, flt, _window_label(cur, base), [result])
    dq_step(ctx, TimeWindow(start=base.start, end=cur.end), [metric], list(dims))
    sensitivity_step(ctx, cur, flt)
    _extras(ctx, scope, flt, cur, base)


def _pct2(x: float) -> str:
    """A percentage with exactly two decimals, so close values read consistently (89.40 vs
    89.45, not 89.4 vs 89.45)."""
    return f"{x * 100:.2f}"


def run_segment_comparison(ctx: RunContext, scope: Scope) -> None:
    """Compare segments of one dimension within one window (e.g. UPI vs card in August)."""
    seg = next((f for f in scope.filters if f.op == "in" and len(f.values) >= 2), None)
    if seg is None:
        raise PlanError("name two or more segments to compare, or a second period")
    metric = next((m for m in scope.metrics if m in RATE_METRICS), None)
    if metric is None:
        run_lookup(ctx, replace(scope, dimensions=[seg.dimension]))
        return
    dim, cur = seg.dimension, scope.current
    spec = QuerySpec(metrics=[metric], dimensions=[dim], filters=scope.filters, time_window=cur)
    _, result = ctx.engine.run_spec(spec, ctx.role)
    if not result.ok:
        raise PlanError("; ".join(c.detail for c in result.report.failures) or str(result.error))
    stats = []
    for row in result.frame.to_dict("records"):
        num, den = float(row[f"{metric}_num"] or 0), float(row[f"{metric}_den"] or 0)
        if den > 0:
            stats.append((str(row[dim]), num, den, num / den, *wilson_interval(num, den)))
    if len(stats) < 2:
        names = ", ".join(seg.values)
        raise PlanError(f"found data for fewer than two of {names} in {cur.label()}")
    stats.sort(key=lambda s: -s[3])
    hi, lo = stats[0], stats[-1]
    z, p = two_proportion_z_test(lo[1], lo[2], hi[1], hi[2])
    a = ctx.settings.analysis
    gap_pp = (hi[3] - lo[3]) * 100
    ctx.add(
        title=f"{metric} by {dim}: {' vs '.join(s[0] for s in stats)}",
        kind="test",
        purpose="Rate per segment in the same window, with 95% Wilson intervals and a "
        "two-proportion z-test between the highest and the lowest",
        results=[result],
        filters=scope.filters,
        window=cur.label(),
        rows=[
            {
                dim: s[0],
                "successes": int(s[1]),
                "attempts": int(s[2]),
                "rate_pct": _pct2(s[3]),
                "ci_low_pct": _pct2(s[4]),
                "ci_high_pct": _pct2(s[5]),
            }
            for s in stats
        ],
        chart_data=[{dim: s[0], "rate_pct": round(s[3] * 100, 2)} for s in stats],
        key_numbers={
            "highest_segment": hi[0],
            "lowest_segment": lo[0],
            "gap_pp": f"{gap_pp:.2f}",
            "z": round(z, 2),
            "p_value": float(f"{p:.3g}"),
            "statistically_significant": p < a.alpha,
            "practically_significant": p < a.alpha and gap_pp >= a.practical_significance_pp,
        },
        chart="bar",
    )
    dq_step(ctx, cur, [metric], [dim])


def run_lookup(ctx: RunContext, scope: Scope, *, compare: bool = False) -> None:
    """Compile the requested metrics and run them; add DQ and denominator context."""
    metrics = scope.metrics or [SR]
    views = {ctx.layer.metric(m).view for m in metrics}
    for view in sorted(views):
        group = [m for m in metrics if ctx.layer.metric(m).view == view]
        spec = QuerySpec(
            metrics=group,
            dimensions=scope.dimensions,
            filters=scope.filters,
            time_window=scope.current,
            compare_window=scope.baseline if compare else None,
            order_by=[OrderBy(field=group[0], descending=True)] if scope.dimensions else [],
            limit=25 if scope.dimensions else None,
        )
        try:
            _, result = ctx.engine.run_spec(spec, ctx.role)
        except CompileError as exc:
            raise NotExpressibleError(
                f"cannot express this with the semantic layer: {exc}"
            ) from exc
        if not result.ok:
            raise PlanError(
                "; ".join(c.detail for c in result.report.failures) or str(result.error)
            )
        rows = result.frame.to_dict("records")
        for row in rows:
            for m in group:
                if m in RATE_METRICS and row.get(m) is not None:
                    row[f"{m}_pct"] = pct(row.pop(m))
        key = {}
        if len(rows) == 1:
            key = {k: v for k, v in rows[0].items() if isinstance(v, int | float | Decimal)}
        ctx.add(
            title=", ".join(group)
            + (f" by {', '.join(scope.dimensions)}" if scope.dimensions else ""),
            kind="query",
            purpose="The requested metric(s), computed from the governed semantic layer",
            results=[result],
            filters=scope.filters,
            window=_window_label(scope.current, scope.baseline if compare else None),
            rows=rows,
            key_numbers=key,
            chart="bar" if scope.dimensions else "table",
        )
    dq_step(ctx, scope.current, metrics, scope.dimensions)
    if any(m in RATE_METRICS for m in metrics):
        sensitivity_step(ctx, scope.current, scope.filters)


def run_ranking(ctx: RunContext, scope: Scope) -> None:
    """Rank segments by a metric (top 10), with share of total."""
    metrics = scope.metrics or ["failed_attempts"]
    if not scope.dimensions:
        raise PlanError("a ranking needs something to rank (e.g. merchants, issuers, BINs)")
    # Context dimensions are functionally dependent on the ranked ones (a BIN has one issuer),
    # so adding them to GROUP BY labels the rows without changing the ranking.
    denied = set(ctx.role.denied_sensitivities)
    context = [
        c
        for d in scope.dimensions
        for c in ctx.layer.dimension(d).context
        if c not in scope.dimensions and ctx.layer.dimension(c).sensitivity not in denied
    ]
    spec = QuerySpec(
        metrics=metrics[:2],
        dimensions=[*scope.dimensions, *dict.fromkeys(context)],
        filters=scope.filters,
        time_window=scope.current,
        order_by=[OrderBy(field=metrics[0], descending=metrics[0] not in RATE_METRICS)],
        limit=10,
    )
    _, top = ctx.engine.run_spec(spec, ctx.role)
    total_spec = spec.model_copy(update={"dimensions": [], "order_by": [], "limit": None})
    _, total = ctx.engine.run_spec(total_spec, ctx.role)
    for r in (top, total):
        if not r.ok:
            raise PlanError("; ".join(c.detail for c in r.report.failures) or str(r.error))
    m0 = metrics[0]
    grand = float(total.frame[m0].iloc[0] or 0) if m0 not in RATE_METRICS else None
    rows = top.frame.to_dict("records")
    for row in rows:
        if grand:
            row["share_of_total_pct"] = pct(float(row[m0] or 0) / grand)
        for m in metrics:
            if m in RATE_METRICS and row.get(m) is not None:
                row[f"{m}_pct"] = pct(row.pop(m))
    results = [top, total]
    purpose = "Rank segments by the requested metric"
    if m0 == "failed_attempts" and len(scope.dimensions) == 1 and rows:
        results.append(_add_failure_rates(ctx, scope, rows))
        purpose += "; failure_rate_pct is each segment's failures as a share of its own attempts"
    key: dict[str, Any] = {f"total_{m0}": grand} if grand is not None else {}
    if rows:
        key["top_segment"] = " | ".join(str(rows[0][d]) for d in scope.dimensions)
        key["top_share_of_total_pct"] = rows[0].get("share_of_total_pct")
        key["top3_share_of_total_pct"] = round(
            sum(r.get("share_of_total_pct") or 0 for r in rows[:3]), 2
        )
        if "failure_rate_pct" in rows[0]:
            key["top_segment_failure_rate_pct"] = rows[0]["failure_rate_pct"]
    ctx.add(
        title=f"Top {len(rows)} by {m0}",
        kind="ranking",
        purpose=purpose,
        results=results,
        filters=scope.filters,
        window=scope.current.label(),
        rows=rows,
        key_numbers=key,
        chart="bar",
    )
    focus = next((d for d in scope.dimensions if d not in TIME_DIMENSIONS), None)
    if rows and focus and pd.notna(rows[0][focus]) and _timing_requested(scope):
        _timing_drilldown(ctx, scope, focus, rows[0][focus])
    dq_step(ctx, scope.current, metrics, scope.dimensions)


TIME_DIMENSIONS = {"date", "hour_of_day", "day_of_week"}
FAILURE_DIMENSIONS = {"failure_category", "failure_code"}


def _add_failure_rates(ctx: RunContext, scope: Scope, rows: list[dict[str, Any]]) -> QueryResult:
    """Add each ranked segment's failure rate: a count alone favours the biggest segments.

    The denominator is the segment's attempts under the same filters minus the failure-type
    ones (for "technical failures by acquirer": technical failures / all attempts).
    """
    dim = scope.dimensions[0]
    values = [r[dim] for r in rows if pd.notna(r[dim])]
    base = [f for f in scope.filters if f.dimension not in FAILURE_DIMENSIONS]
    spec = QuerySpec(
        metrics=["attempts"],
        dimensions=[dim],
        filters=[*base, Filter(dimension=dim, op="in", values=values)],
        time_window=scope.current,
    )
    _, result = ctx.engine.run_spec(spec, ctx.role)
    if not result.ok:
        raise PlanError("; ".join(c.detail for c in result.report.failures) or str(result.error))
    attempts = {r[dim]: float(r["attempts"] or 0) for r in result.frame.to_dict("records")}
    for row in rows:
        den = attempts.get(row[dim]) if pd.notna(row[dim]) else None
        row["segment_attempts"] = int(den) if den else None
        row["failure_rate_pct"] = pct(float(row["failed_attempts"] or 0) / den) if den else None
    return result


def _timing_requested(scope: Scope) -> bool:
    """The intent asked when it happened: a daily or hourly step, or a time dimension."""
    return any(e.tool in ("daily_series", "hourly_profile") for e in scope.extra_steps) or any(
        d in TIME_DIMENSIONS for d in scope.dimensions
    )


def _timing_drilldown(ctx: RunContext, scope: Scope, dim: str, value: Any) -> None:
    """When did it happen for the top segment? Find its main daily drop, then explain that
    drop the way the anomaly playbook does (methods, failure codes, hourly timeline)."""
    seg = [*scope.filters, Filter(dimension=dim, op="=", values=[value])]
    # Success rate is meaningless inside "technical failures only", so the timing view uses
    # the segment's full traffic.
    traffic = [f for f in seg if f.dimension not in FAILURE_DIMENSIONS]
    win, d = scope.current, ctx.settings.data
    series = _series(
        ctx, win, traffic, purpose=f"When it happened: daily success rate for {value}", summary=True
    )
    drops = [w for w in _daily_anomalies(ctx, series) if w.direction == "drop"]
    main = max(drops, key=lambda w: w.excess_failures) if drops else None
    if main is not None:
        drop = TimeWindow(start=main.start.date(), end=main.end.date())
        base = TimeWindow(start=drop.start - timedelta(days=7), end=drop.start - timedelta(days=1))
        if base.start >= d.start_date:
            _explain_drop(ctx, traffic, str(value), drop, base)
            return
    ctx.caveats.append(f"No unusual daily drop was found for {value} in {win.label()}.")
    spec = QuerySpec(
        metrics=["failed_attempts"],
        dimensions=["failure_code"],
        filters=seg,
        time_window=win,
        order_by=[OrderBy(field="failed_attempts", descending=True)],
        limit=10,
    )
    _, result = ctx.engine.run_spec(spec, ctx.role)
    if not result.ok:
        raise PlanError("; ".join(c.detail for c in result.report.failures) or str(result.error))
    codes = result.frame.to_dict("records")
    total = sum(float(r["failed_attempts"] or 0) for r in codes)
    for r in codes:
        r["share_of_failures_pct"] = (
            pct(float(r["failed_attempts"] or 0) / total) if total else None
        )
    ctx.add(
        title=f"Failure codes for {value}",
        kind="failure_codes",
        purpose=f"Which failure codes make up {value}'s failures in the window",
        results=[result],
        filters=seg,
        window=win.label(),
        rows=codes,
        key_numbers={"top_code": codes[0]["failure_code"]} if codes else {},
        chart="bar",
    )


def run_reconciliation(ctx: RunContext, scope: Scope) -> None:
    """Transaction GMV vs settlements for a merchant group or merchants."""
    group = next((f.values for f in scope.filters if f.dimension == "merchant_group"), None)
    names = next((f.values for f in scope.filters if f.dimension == "merchant_name"), None)
    if not group and not names:
        raise PlanError("name a merchant or merchant group to reconcile (e.g. NovaMart)")
    d = ctx.settings.data
    rec, results = reconcile(
        ctx.engine,
        ctx.role,
        window=scope.current,
        as_of=d.as_of_date,
        data_start=d.start_date,
        merchant_group=str(group[0]) if group else None,
        merchant_names=None if group else [str(n) for n in names or []],
        settings=ctx.settings.analysis,
    )
    if rec.flagged:
        ctx.caveats.append(
            f"Unexplained settlement difference for {rec.scope}: flagged for investigation."
        )
    rows = [
        {
            "component": c.label,
            "kind": c.kind,
            "amount_inr": round(c.amount_inr, 2),
            "detail": c.detail,
        }
        for c in rec.components
    ]
    rows += [
        {
            "component": f"Batch {b.settlement_batch_id}",
            "kind": "unexplained",
            "amount_inr": round(b.residual_inr, 2),
            "detail": f"{b.merchant_name} {b.covered_from}: gross {b.gross_inr:,.2f} vs GMV "
            f"{b.expected_gmv_inr:,.2f} ({b.residual_pct:+.2f}%)",
        }
        for b in rec.flagged_batches
    ]
    ctx.add(
        title=f"Reconciliation: {rec.scope}",
        kind="reconcile",
        purpose="Explain the gap between transaction GMV and settlements",
        results=results,
        filters=scope.filters,
        window=scope.current.label(),
        rows=rows,
        key_numbers={
            "txn_gmv_inr": round(rec.txn_gmv_inr, 2),
            "settled_gross_inr": round(rec.settled_gross_inr, 2),
            "refunds_inr": round(rec.refunds_inr, 2),
            "fees_inr": round(rec.fees_inr, 2),
            "settled_net_inr": round(rec.settled_net_inr, 2),
            "unexplained_residual_inr": round(rec.residual_inr, 2),
            "unexplained_residual_pct": round(rec.residual_pct, 3),
            "flagged": rec.flagged,
            "flagged_batches": len(rec.flagged_batches),
        },
        chart="reconciliation",
    )
    dq_step(ctx, scope.current, ["gmv_inr", "settled_gross_inr"], [])


def run_data_quality(ctx: RunContext, scope: Scope) -> None:
    """All data-quality checks over the period."""
    dq_step(ctx, scope.current, [], [])


RUNNERS = {
    "root_cause": run_root_cause,
    "anomaly_explain": run_anomaly,
    "trend": run_trend,
    "comparison": run_comparison,
    "metric_lookup": run_lookup,
    "ranking": run_ranking,
    "reconciliation": run_reconciliation,
    "data_quality": run_data_quality,
}


def plan_steps(question_type: str, extras: list[ExtraStep], metrics: list[str]) -> list[PlanStep]:
    """The deterministic plan for a question type plus validated LLM-suggested steps."""
    rate_question = not metrics or bool(set(metrics) & RATE_METRICS)
    steps = [
        PlanStep(name=n, description=d)
        for n, d in PLAYBOOK_STEPS[question_type]
        if n != "denominators" or rate_question
    ]
    steps += [
        PlanStep(
            name=e.tool,
            description=f"{e.tool}"
            + (f" by {e.dimension}" if e.dimension else "")
            + f": {e.reason}",
            llm_suggested=True,
        )
        for e in extras
    ]
    return steps
