"""Period comparison and denominator sensitivity (SPEC §10.1, §10.8)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from paysight.analysis.stats import two_proportion_z_test, wilson_interval
from paysight.config import AnalysisSettings
from paysight.execution.engine import Engine, QueryResult
from paysight.safety.access import RolePolicy
from paysight.semantic.models import Filter, QuerySpec, TimeWindow

SR_METRICS = ("attempt_success_rate", "order_success_rate", "technical_success_rate")


class AnalysisError(RuntimeError):
    """A query behind an analysis step did not run; carries the blocked/failed result."""

    def __init__(self, message: str, result: QueryResult) -> None:
        super().__init__(message)
        self.result = result


def require_ok(result: QueryResult, purpose: str) -> QueryResult:
    """Raise :class:`AnalysisError` unless ``result`` ran successfully."""
    if not result.ok:
        reasons = "; ".join(c.detail for c in result.report.failures) or result.error or "unknown"
        raise AnalysisError(f"{purpose} failed ({result.status}): {reasons}", result)
    return result


class RateComparison(BaseModel):
    """A rate in two windows with uncertainty and significance."""

    model_config = ConfigDict(frozen=True)

    metric: str
    baseline_label: str
    current_label: str
    num0: float
    den0: float
    rate0: float
    ci0: tuple[float, float]
    num1: float
    den1: float
    rate1: float
    ci1: tuple[float, float]
    delta_pp: float
    z: float
    p_value: float
    statistically_significant: bool
    practically_significant: bool


def compare_rates(
    num0: float,
    den0: float,
    num1: float,
    den1: float,
    *,
    settings: AnalysisSettings,
    metric: str = "attempt_success_rate",
    baseline_label: str = "baseline",
    current_label: str = "current",
) -> RateComparison:
    """Compare two proportions: Wilson 95% intervals, pooled z-test, practical significance.

    ``practically_significant`` requires ``|Δ| ≥ practical_significance_pp`` *and* at least
    ``min_sample_attempts`` in both windows.
    """
    rate0 = num0 / den0 if den0 else float("nan")
    rate1 = num1 / den1 if den1 else float("nan")
    z, p = two_proportion_z_test(num0, den0, num1, den1)
    delta_pp = (rate1 - rate0) * 100
    big_enough = min(den0, den1) >= settings.min_sample_attempts
    return RateComparison(
        metric=metric,
        baseline_label=baseline_label,
        current_label=current_label,
        num0=num0,
        den0=den0,
        rate0=rate0,
        ci0=wilson_interval(num0, den0),
        num1=num1,
        den1=den1,
        rate1=rate1,
        ci1=wilson_interval(num1, den1),
        delta_pp=delta_pp,
        z=z,
        p_value=p,
        statistically_significant=p < settings.alpha,
        practically_significant=big_enough and abs(delta_pp) >= settings.practical_significance_pp,
    )


def compare_periods(
    engine: Engine,
    role: RolePolicy,
    *,
    metric: str,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter] | None = None,
    settings: AnalysisSettings,
) -> tuple[RateComparison, QueryResult]:
    """Run one compare query for a rate metric and return the comparison plus its evidence."""
    spec = QuerySpec(
        metrics=[metric], filters=filters or [], time_window=current, compare_window=baseline
    )
    _, result = engine.run_spec(spec, role)
    require_ok(result, f"comparing {metric}")
    rows = {r["comparison_window"]: r for r in result.frame.to_dict("records")}
    b = rows.get("baseline", {})
    c = rows.get("current", {})
    return (
        compare_rates(
            float(b.get(f"{metric}_num", 0) or 0),
            float(b.get(f"{metric}_den", 0) or 0),
            float(c.get(f"{metric}_num", 0) or 0),
            float(c.get(f"{metric}_den", 0) or 0),
            settings=settings,
            metric=metric,
            baseline_label=baseline.label(),
            current_label=current.label(),
        ),
        result,
    )


class DenominatorValue(BaseModel):
    """One success-rate definition for a scope."""

    model_config = ConfigDict(frozen=True)

    metric: str
    numerator: float
    denominator: float
    rate: float


class DenominatorSensitivity(BaseModel):
    """Attempt, order and technical success rate for the same scope (SPEC §10.8)."""

    model_config = ConfigDict(frozen=True)

    window_label: str
    values: list[DenominatorValue]
    max_divergence_pp: float
    note_needed: bool

    def rate(self, metric: str) -> float:
        """The rate for one definition."""
        return next(v.rate for v in self.values if v.metric == metric)


def denominator_sensitivity(
    engine: Engine,
    role: RolePolicy,
    *,
    window: TimeWindow,
    filters: list[Filter] | None = None,
    settings: AnalysisSettings,
) -> tuple[DenominatorSensitivity, list[QueryResult]]:
    """Compute all three success-rate definitions; flag divergence ≥ the settings threshold."""
    values: list[DenominatorValue] = []
    results: list[QueryResult] = []
    # Attempt and technical SR share a view; order SR comes from v_orders.
    for metrics in (["attempt_success_rate", "technical_success_rate"], ["order_success_rate"]):
        _, result = engine.run_spec(
            QuerySpec(metrics=metrics, filters=filters or [], time_window=window), role
        )
        require_ok(result, "denominator sensitivity")
        results.append(result)
        row = result.frame.iloc[0]
        for m in metrics:
            den = float(row[f"{m}_den"] or 0)
            num = float(row[f"{m}_num"] or 0)
            values.append(
                DenominatorValue(
                    metric=m,
                    numerator=num,
                    denominator=den,
                    rate=num / den if den else float("nan"),
                )
            )
    rates = [v.rate for v in values]
    spread = (max(rates) - min(rates)) * 100
    ordered = sorted(values, key=lambda v: SR_METRICS.index(v.metric))
    return (
        DenominatorSensitivity(
            window_label=window.label(),
            values=ordered,
            max_divergence_pp=spread,
            note_needed=spread >= settings.denominator_divergence_pp,
        ),
        results,
    )
