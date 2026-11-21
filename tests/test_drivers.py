"""Driver ranking, rate-effect localization, drill-down and comparison on the hero question."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from paysight.analysis.compare import compare_periods, denominator_sensitivity
from paysight.analysis.decompose import decompose_rate_change
from paysight.analysis.drilldown import breakdown, failure_code_shift, top_merchants
from paysight.analysis.drivers import localize_rate_effect, rank_drivers, scan_drivers
from paysight.config import get_settings
from paysight.semantic.models import Filter, TimeWindow

A = get_settings().analysis
CURRENT = TimeWindow(start=date(2026, 10, 18), end=date(2026, 10, 31))
BASELINE = TimeWindow(start=date(2026, 10, 4), end=date(2026, 10, 17))


def _decomp(name: str, rows: list[tuple[str, float, float, float, float]]):
    frame = pd.DataFrame(rows, columns=["segment", "num0", "den0", "num1", "den1"])
    return decompose_rate_change(frame, dimension=name)


def test_size_alone_does_not_make_a_driver() -> None:
    # 'big' holds 90% of volume and falls exactly like everything else: uniform, uninformative.
    uniform = _decomp("tier", [("big", 9000, 10000, 8800, 10000), ("small", 900, 1000, 880, 1000)])
    # 'emi' is small but its share jumps and its rate is low: a disproportionate mix driver.
    concentrated = _decomp(
        "method", [("upi", 9500, 10000, 6650, 7000), ("emi", 400, 500, 3600 * 0.6, 3600)]
    )
    ranking = rank_drivers([uniform, concentrated], settings=A)
    assert ranking.dimensions[0].dimension == "method"
    assert ranking.dimension("tier").strength < 0.05
    assert ranking.dimension("method").top_segments[0].segment == "emi"


def test_bh_correction_spans_all_dimensions() -> None:
    a = _decomp("a", [("x", 900, 1000, 880, 1000), ("y", 900, 1000, 899, 1000)])
    b = _decomp("b", [("p", 900, 1000, 880, 1000), ("q", 900, 1000, 899, 1000)])
    ranking = rank_drivers([a, b], settings=A)
    assert ranking.segments_tested == 4
    for dim in ranking.dimensions:
        for seg in dim.top_segments:
            assert seg.q_value >= seg.p_value


@pytest.fixture(scope="module")
def hero(small_engine, policies):
    role = policies["leadership"]
    ranking, decomps, results = scan_drivers(
        small_engine, role, current=CURRENT, baseline=BASELINE, settings=A
    )
    local, _ = localize_rate_effect(
        small_engine, role, current=CURRENT, baseline=BASELINE, settings=A
    )
    return ranking, decomps, local, results


def test_hero_top_drivers_include_method_and_group(hero) -> None:
    ranking, _, _, _ = hero
    top3 = [d.dimension for d in ranking.dimensions[:3]]
    assert "payment_method" in top3 and "merchant_group" in top3
    assert ranking.dimensions[0].dimension in ("payment_method", "merchant_group")


def test_hero_mix_share_by_method_is_majority(hero) -> None:
    _, decomps, _, _ = hero
    method = next(d for d in decomps if d.dimension == "payment_method")
    assert method.delta < 0
    assert 0.5 <= method.mix_share <= 0.95


def test_hero_rate_effect_localized_to_harbor_debit(hero) -> None:
    _, _, local, _ = hero
    top = local.top_rate_segments[0]
    assert top.segment == "Harbor Bank | DEBIT_CARD"
    assert top.significant and top.rate1 < top.rate0 - 0.05


def test_every_driver_query_was_validated(hero) -> None:
    _, _, _, results = hero
    assert results and all(r.ok and r.report.passed for r in results)


def test_harbor_failure_code_shift_is_risk_decline(small_engine, policies) -> None:
    codes, _ = failure_code_shift(
        small_engine,
        policies["leadership"],
        current=TimeWindow(start=date(2026, 10, 21), end=date(2026, 10, 31)),
        baseline=BASELINE,
        filters=[
            Filter(dimension="issuer_name", op="=", values=["Harbor Bank"]),
            Filter(dimension="payment_method", op="=", values=["DEBIT_CARD"]),
        ],
    )
    assert codes[0].failure_code == "ISSUER_RISK_DECLINE"
    assert codes[0].delta_per100 > 8


def test_breakdown_and_top_merchants(small_engine, policies) -> None:
    role = policies["leadership"]
    rows, _ = breakdown(
        small_engine,
        role,
        dimension="payment_method",
        current=CURRENT,
        baseline=BASELINE,
        filters=[Filter(dimension="issuer_name", op="=", values=["Harbor Bank"])],
        settings=A,
    )
    assert rows[0].segment == "DEBIT_CARD" and rows[0].significant
    merchants, _ = top_merchants(small_engine, role, current=CURRENT, baseline=BASELINE)
    assert len(merchants) == 5
    assert merchants == sorted(merchants, key=lambda m: m.excess_failures, reverse=True)


def test_compare_periods_headline(small_engine, policies) -> None:
    comparison, result = compare_periods(
        small_engine,
        policies["leadership"],
        metric="attempt_success_rate",
        current=CURRENT,
        baseline=BASELINE,
        settings=A,
    )
    assert comparison.delta_pp < -1.0
    assert comparison.ci1[0] < comparison.rate1 < comparison.ci1[1]
    assert comparison.statistically_significant and comparison.practically_significant
    assert "comparison_window" in result.sql


def test_denominator_sensitivity_flags_gap(small_engine, policies) -> None:
    sens, results = denominator_sensitivity(
        small_engine, policies["leadership"], window=CURRENT, settings=A
    )
    assert [v.metric for v in sens.values] == [
        "attempt_success_rate",
        "order_success_rate",
        "technical_success_rate",
    ]
    assert sens.rate("attempt_success_rate") < sens.rate("order_success_rate")
    assert sens.rate("attempt_success_rate") < sens.rate("technical_success_rate")
    assert sens.note_needed and len(results) == 2


def test_regional_analyst_scan_sees_only_north(small_engine, policies) -> None:
    ranking, decomps, _ = scan_drivers(
        small_engine,
        policies["analyst_north"],
        current=CURRENT,
        baseline=BASELINE,
        dimensions=("region",),
        settings=A,
    )
    assert [s.segment for s in decomps[0].segments] == ["north"]
    assert ranking.dimensions[0].dimension == "region"
