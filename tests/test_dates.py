"""Date resolver tests: every expression resolves against AS_OF_DATE, never the wall clock."""

from __future__ import annotations

from datetime import date

import pytest

from paysight.semantic.dates import DateResolutionError, resolve, resolve_compare

AS_OF = date(2026, 10, 31)
START = date(2026, 8, 1)


def _r(expr: str) -> tuple[date, date]:
    w = resolve(expr, as_of=AS_OF, data_start=START).window
    return w.start, w.end


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("last_n_days:7", (date(2026, 10, 25), date(2026, 10, 31))),
        ("last_n_days:1", (date(2026, 10, 31), date(2026, 10, 31))),
        ("last_n_days:10", (date(2026, 10, 22), date(2026, 10, 31))),
        ("last_n_weeks:1", (date(2026, 10, 25), date(2026, 10, 31))),  # "last week"
        ("last_n_weeks:2", (date(2026, 10, 18), date(2026, 10, 31))),
        ("this_month", (date(2026, 10, 1), date(2026, 10, 31))),
        ("last_month", (date(2026, 9, 1), date(2026, 9, 30))),
        ("month:2026-08", (date(2026, 8, 1), date(2026, 8, 31))),
        ("day:2026-09-17", (date(2026, 9, 17), date(2026, 9, 17))),
        ("explicit:2026-10-18..2026-10-31", (date(2026, 10, 18), date(2026, 10, 31))),
        ("explicit:2026-10-12", (date(2026, 10, 12), date(2026, 10, 12))),
        ("relative_to_named:festive_season", (date(2026, 10, 18), date(2026, 10, 31))),
        ("relative_to_named:festive_season:before", (date(2026, 10, 4), date(2026, 10, 17))),
    ],
)
def test_expressions(expr: str, expected: tuple[date, date]) -> None:
    assert _r(expr) == expected


def test_hero_comparison_windows() -> None:
    current = resolve("last_n_weeks:2", as_of=AS_OF, data_start=START)
    baseline = resolve_compare(current, None, as_of=AS_OF, data_start=START)
    assert (baseline.window.start, baseline.window.end) == (date(2026, 10, 4), date(2026, 10, 17))
    explicit = resolve_compare(current, "previous_period", as_of=AS_OF, data_start=START)
    assert explicit.window == baseline.window


def test_month_over_month_compare() -> None:
    current = resolve("this_month", as_of=AS_OF, data_start=START)
    baseline = resolve_compare(current, "last_month", as_of=AS_OF, data_start=START)
    assert baseline.window.start == date(2026, 9, 1)


def test_clipping_is_reported_not_silent() -> None:
    resolved = resolve("last_n_days:120", as_of=AS_OF, data_start=START)
    assert resolved.window.start == START
    assert any("clipped" in n for n in resolved.notes)
    future = resolve("month:2026-10", as_of=date(2026, 10, 20), data_start=START)
    assert future.window.end == date(2026, 10, 20)
    assert future.notes


@pytest.mark.parametrize(
    "expr",
    [
        "yesterday-ish",
        "last_n_days:0",
        "last_n_days:-3",
        "last_n_days:abc",
        "month:2026-13",
        "explicit:2026-10-31..2026-10-01",
        "explicit:not-a-date",
        "relative_to_named:black_friday",
        "month:2025-01",  # entirely before the data
        "day:2026-11-15",  # after AS_OF_DATE
    ],
)
def test_bad_expressions_raise(expr: str) -> None:
    with pytest.raises(DateResolutionError):
        resolve(expr, as_of=AS_OF, data_start=START)
