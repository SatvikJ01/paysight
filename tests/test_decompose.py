"""Kitagawa decomposition tests (SPEC §10.2), including the exact toy case from the spec."""

from __future__ import annotations

import pandas as pd
import pytest

from paysight.analysis.decompose import decompose_rate_change


def _frame(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["segment", "num0", "den0", "num1", "den1"])


def test_spec_toy_case_is_pure_mix() -> None:
    # UPI 0.95 and Card 0.85 in both windows; shares 0.8/0.2 -> 0.6/0.4.
    result = decompose_rate_change(
        _frame([("UPI", 760, 800, 570, 600), ("Card", 170, 200, 340, 400)]),
        dimension="payment_method",
    )
    assert result.rate0 == pytest.approx(0.93)
    assert result.rate1 == pytest.approx(0.91)
    assert result.mix_effect == pytest.approx(-0.02, abs=1e-12)
    assert result.rate_effect == pytest.approx(0.0, abs=1e-12)
    assert result.mix_share == pytest.approx(1.0)
    by_seg = {s.segment: s for s in result.segments}
    assert by_seg["UPI"].mix == pytest.approx(-0.19)
    assert by_seg["Card"].mix == pytest.approx(0.17)


def test_mixed_case_sums_exactly() -> None:
    rows = [("A", 900, 1000, 1700, 2000), ("B", 400, 500, 280, 400), ("C", 150, 200, 90, 100)]
    result = decompose_rate_change(_frame(rows), dimension="d")
    r0 = (900 + 400 + 150) / 1700
    r1 = (1700 + 280 + 90) / 2500
    assert result.delta == pytest.approx(r1 - r0)
    assert result.mix_effect + result.rate_effect == pytest.approx(result.delta, abs=1e-12)
    assert result.sum_check_error <= 1e-9
    # Hand-computed contributions for segment B.
    s0, s1, rb0, rb1 = 500 / 1700, 400 / 2500, 0.8, 0.7
    b = next(s for s in result.segments if s.segment == "B")
    assert b.mix == pytest.approx((s1 - s0) * (rb0 + rb1) / 2)
    assert b.rate == pytest.approx((rb1 - rb0) * (s0 + s1) / 2)


def test_rate_only_change() -> None:
    result = decompose_rate_change(
        _frame([("A", 90, 100, 80, 100), ("B", 50, 100, 50, 100)]), dimension="d"
    )
    assert result.mix_effect == pytest.approx(0.0, abs=1e-12)
    assert result.rate_effect == pytest.approx(-0.05)
    assert result.rate_share == pytest.approx(1.0)


def test_segment_present_in_one_window_only() -> None:
    result = decompose_rate_change(
        _frame([("old", 90, 100, 90, 100), ("new", 0, 0, 30, 50)]), dimension="d"
    )
    new = next(s for s in result.segments if s.segment == "new")
    assert new.rate0 is None
    assert new.rate == 0.0  # all of a new segment's contribution is mix
    assert new.mix == pytest.approx(50 / 150 * 0.6)
    assert result.mix_effect + result.rate_effect == pytest.approx(result.delta, abs=1e-12)


def test_zero_denominator_segment_is_dropped() -> None:
    result = decompose_rate_change(
        _frame([("A", 90, 100, 85, 100), ("ghost", 0, 0, 0, 0)]), dimension="d"
    )
    assert [s.segment for s in result.segments] == ["A"]


def test_tiny_delta_reports_absolute_contributions_only() -> None:
    result = decompose_rate_change(
        _frame([("A", 9000, 10000, 9001, 10000), ("B", 5000, 10000, 5000, 10000)]),
        dimension="d",
        min_delta_for_shares=0.001,
    )
    assert not result.shares_reliable
    assert result.mix_share is None and result.rate_share is None
    assert result.rate_effect == pytest.approx(0.00005)


def test_empty_window_raises() -> None:
    with pytest.raises(ValueError, match="positive total denominator"):
        decompose_rate_change(_frame([("A", 0, 0, 5, 10)]), dimension="d")
