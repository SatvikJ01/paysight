"""Anomaly detection: window merging on synthetic series, and S1 on the small dataset."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from paysight.analysis.anomaly import detect_anomalies, outage_windows
from paysight.analysis.drilldown import rate_series
from paysight.config import get_settings
from paysight.semantic.models import Filter, TimeWindow

DAY = timedelta(days=1)
Z = 3.5


def _series(rates: list[float], den: list[float] | None = None) -> pd.DataFrame:
    den = den or [1000.0] * len(rates)
    return pd.DataFrame(
        {
            "period": [datetime(2026, 9, 1) + i * DAY for i in range(len(rates))],
            "num": [r * d for r, d in zip(rates, den, strict=True)],
            "den": den,
        }
    )


def test_adjacent_points_merge_into_one_window() -> None:
    rates = [0.90, 0.91, 0.90, 0.89, 0.90, 0.70, 0.72, 0.90, 0.91, 0.90]
    windows = detect_anomalies(_series(rates), step=DAY, z_threshold=Z)
    assert len(windows) == 1
    w = windows[0]
    assert (w.start.day, w.end.day, w.periods) == (6, 7, 2)
    assert w.end_exclusive == w.end + DAY
    assert w.direction == "drop"
    assert w.excess_failures == pytest_approx((0.90 - 0.70) * 1000 + (0.90 - 0.72) * 1000)


def pytest_approx(x: float) -> object:
    import pytest

    return pytest.approx(x, rel=1e-9)


def test_separated_anomalies_stay_separate() -> None:
    rates = [0.90, 0.70, 0.90, 0.91, 0.70, 0.90, 0.89, 0.90]
    windows = detect_anomalies(_series(rates), step=DAY, z_threshold=Z)
    assert [w.start.day for w in windows] == [2, 5]


def test_low_volume_gap_does_not_split_a_window() -> None:
    rates = [0.90, 0.91, 0.70, 0.95, 0.71, 0.90, 0.90, 0.89]
    den = [1000, 1000, 1000, 3, 1000, 1000, 1000, 1000]  # day 4 is too small to judge
    windows = detect_anomalies(_series(rates, den), step=DAY, z_threshold=Z, min_den=10)
    assert len(windows) == 1 and windows[0].periods == 2
    assert (windows[0].start.day, windows[0].end.day) == (3, 5)


def test_drop_and_spike_are_not_merged() -> None:
    rates = [0.90, 0.90, 0.70, 0.99, 0.90, 0.90, 0.91, 0.89]
    windows = detect_anomalies(_series(rates), step=DAY, z_threshold=Z)
    assert [w.direction for w in windows] == ["drop", "spike"]


def test_flat_or_short_series_has_no_anomalies() -> None:
    assert detect_anomalies(_series([0.9] * 10), step=DAY, z_threshold=Z) == []
    assert detect_anomalies(_series([0.9, 0.1]), step=DAY, z_threshold=Z) == []


def test_s1_outage_window_found_hourly(small_engine, policies) -> None:
    settings = get_settings()
    series, _ = rate_series(
        small_engine,
        policies["leadership"],
        window=TimeWindow(start=date(2026, 9, 10), end=date(2026, 9, 24)),
        filters=[
            Filter(dimension="acquirer_name", op="=", values=["Kestrel Bank"]),
            Filter(dimension="method_family", op="=", values=["card"]),
        ],
        granularity="day",
    )
    windows = detect_anomalies(
        series, step=DAY, z_threshold=settings.analysis.anomaly_z_threshold, min_den=50
    )
    biggest = max(windows, key=lambda w: w.excess_failures)
    assert biggest.direction == "drop"
    assert biggest.start.date() == date(2026, 9, 17)
    assert biggest.end.date() == date(2026, 9, 18)


def test_small_deviation_on_a_stable_series_is_not_an_anomaly() -> None:
    """Kestrel on 4 Sep: about -1.3pp on a very stable series passes robust z, not 2pp (D37)."""
    rates = [0.898, 0.899, 0.897, 0.885, 0.898, 0.899, 0.897, 0.898, 0.899, 0.770, 0.780, 0.898]
    loose = detect_anomalies(_series(rates), step=DAY, z_threshold=Z)
    strict = detect_anomalies(_series(rates), step=DAY, z_threshold=Z, min_delta=0.02)
    assert [w.start.day for w in loose] == [4, 10]
    assert [(w.start.day, w.end.day) for w in strict] == [(10, 11)]


def test_long_outage_is_found_against_a_reference_rate() -> None:
    """A third of the window is down: the window's own median is pulled down, the reference
    rate is not. One normal hour inside the outage does not split it."""
    hour = timedelta(hours=1)
    rates = [0.87] * 10 + [0.55] * 8 + [0.86] + [0.55] * 8 + [0.87] * 10
    series = _series(rates, [60.0] * len(rates)).assign(
        period=[datetime(2026, 9, 17) + i * hour for i in range(len(rates))]
    )
    robust = detect_anomalies(series, step=hour, z_threshold=Z, min_delta=0.02)
    found = outage_windows(series, reference_rate=0.87, step=hour, z_threshold=Z, min_delta=0.02)
    assert len(found) == 1
    w = found[0]
    assert (w.start.hour, w.end_exclusive.hour, w.periods) == (10, 3, 17)
    assert w.worst_rate == 0.55 and w.baseline_rate == 0.87
    assert sum(x.periods for x in robust) < 16  # the robust test alone misses part of it


def test_two_normal_hours_end_an_outage() -> None:
    hour = timedelta(hours=1)
    rates = [0.87] * 5 + [0.5] * 3 + [0.87, 0.87] + [0.5] * 3 + [0.87] * 5
    series = _series(rates, [60.0] * len(rates)).assign(
        period=[datetime(2026, 9, 17) + i * hour for i in range(len(rates))]
    )
    found = outage_windows(series, reference_rate=0.87, step=hour, z_threshold=Z, min_delta=0.02)
    assert [w.periods for w in found] == [3, 3]
