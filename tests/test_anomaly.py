"""Anomaly detection: window merging on synthetic series, and S1 on the small dataset."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from paysight.analysis.anomaly import detect_anomalies
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
