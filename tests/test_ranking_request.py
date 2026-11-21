"""Ranking direction and count from the question text (review finding 3)."""

from __future__ import annotations

import pytest

from paysight.orchestrator.planner import ranking_request


@pytest.mark.parametrize(
    ("question", "metric", "descending", "count"),
    [
        ("Which three regions had the highest success rate?", "attempt_success_rate", True, 3),
        ("Which three regions had the lowest success rate?", "attempt_success_rate", False, 3),
        ("Top 5 merchants by GMV in October", "gmv_inr", True, 5),
        ("Show the bottom five issuers by success rate", "attempt_success_rate", False, 5),
        (
            "Which BINs had the most risk declines in the last 10 days of October?",
            "failed_attempts",
            True,
            None,
        ),
        ("Which issuers drove card declines in August?", "failed_attempts", None, None),
        ("Which merchants have the highest failure rate?", "attempt_success_rate", False, None),
        (
            "Which acquirers had the fewest failed attempts last week?",
            "failed_attempts",
            False,
            None,
        ),
        ("Top 3 worst merchants by success rate", "attempt_success_rate", False, 3),
    ],
)
def test_direction_and_count(question, metric, descending, count) -> None:
    request = ranking_request(question, metric)
    assert (request.descending, request.count) == (descending, count)
