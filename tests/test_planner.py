"""Planner rules: segment comparisons, period options and choice ids (D36)."""

from __future__ import annotations

import pytest

from paysight.config import get_settings
from paysight.orchestrator import planner
from paysight.orchestrator.models import IntentResult
from paysight.semantic.dates import DateResolutionError


def _intent(**kw) -> IntentResult:
    return IntentResult(restatement="r", metrics=["attempt_success_rate"], **kw)


def test_segment_comparison_needs_two_values_and_no_second_period() -> None:
    two = [{"dimension": "method_family", "op": "in", "values": ["upi", "card"]}]
    one = [{"dimension": "method_family", "op": "in", "values": ["upi"]}]
    assert planner.segment_comparison_filter(_intent(question_type="comparison", filters=two))
    assert not planner.segment_comparison_filter(_intent(question_type="comparison", filters=one))
    assert not planner.segment_comparison_filter(
        _intent(question_type="comparison", filters=two, compare_time_expr="month:2026-09")
    )
    assert not planner.segment_comparison_filter(_intent(question_type="ranking", filters=two))
    windows = planner.resolve_windows(
        _intent(question_type="comparison", filters=two, time_expr="month:2026-08"),
        get_settings(),
    )
    assert windows.baseline is None


def test_a_period_outside_the_data_offers_valid_windows_and_says_why() -> None:
    message, options = planner.period_fallback(
        _intent(question_type="comparison", time_expr="month:2026-06"),
        get_settings(),
        DateResolutionError("outside the data"),
    )
    assert "The data covers 1 Aug 2026 to 31 Oct 2026" in message
    assert options == planner.TIME_OPTIONS


def test_root_cause_gets_no_single_period_option() -> None:
    _, options = planner.period_fallback(
        _intent(question_type="root_cause", time_expr="month:2026-08"),
        get_settings(),
        DateResolutionError("outside the data"),
    )
    assert [o.label for o in options] == ["September 2026 vs August 2026"]


@pytest.mark.parametrize(
    "choice",
    [
        None,
        "last_7",
        "period:not-a-window~none~",
        "period:month:2026-08~none~root_cause",  # only metric_lookup may be chosen
        "period:month:2026-08~month:2026-07~",  # July is outside the data
        "period:month:2026-08",
    ],
)
def test_bad_or_foreign_choice_ids_are_ignored(choice) -> None:
    assert planner.option_from_choice(choice, get_settings()) is None


def test_choice_ids_round_trip() -> None:
    _, options = planner.period_fallback(
        _intent(question_type="comparison", time_expr="month:2026-08"),
        get_settings(),
        DateResolutionError("x"),
    )
    for option in options:
        assert planner.option_from_choice(option.id, get_settings()) == option
