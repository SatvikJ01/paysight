"""Charts get explicit category and value fields (review finding 5)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))

from components.charts import bar

from paysight.orchestrator.models import Evidence


def test_segment_comparison_renders_two_labelled_bars_on_a_percentage_axis() -> None:
    """August UPI vs card (93.11% vs 86.59%): pandas 3 stores text as a string dtype, which
    the old object-dtype detection missed, so no chart was drawn."""
    ev = Evidence(
        id="E1",
        title="attempt_success_rate by method_family: upi vs card",
        kind="test",
        purpose="",
        chart="bar",
        chart_data=[
            {"method_family": "upi", "rate_pct": 93.11},
            {"method_family": "card", "rate_pct": 86.59},
        ],
        chart_x="method_family",
        chart_y="rate_pct",
    )
    fig = bar(ev)
    assert fig is not None
    (trace,) = fig.data
    assert list(trace.x) == ["upi", "card"]
    assert list(trace.y) == [93.11, 86.59]
    assert fig.layout.yaxis.ticksuffix == "%"


def test_generic_bar_finds_text_columns_of_any_string_dtype() -> None:
    ev = Evidence(
        id="E2",
        title="Top 2 by failed_attempts",
        kind="ranking",
        purpose="",
        chart="bar",
        result=[
            {"acquirer_name": "Kestrel Bank", "failed_attempts": 2776},
            {"acquirer_name": "Meridian Bank", "failed_attempts": 1965},
        ],
    )
    fig = bar(ev)
    assert fig is not None and list(fig.data[0].x) == ["Kestrel Bank", "Meridian Bank"]


def test_root_cause_answer_leads_with_the_waterfall() -> None:
    from components.charts import main_chart_evidence

    from paysight.orchestrator.models import AskResult, IntentResult

    waterfall = Evidence(
        id="E4",
        title="Mix vs rate decomposition by payment_method",
        kind="decomposition",
        purpose="",
        chart="waterfall",
        key_numbers={"rate0_pct": 90.0, "rate1_pct": 88.5, "delta_pp": -1.5},
        result=[{"segment": "UPI", "mix_pp": -0.4, "rate_pp": 0.0}],
    )
    other = Evidence(id="E1", title="t", kind="test", purpose="", chart="bar")
    result = AskResult(
        run_id="r",
        question="q",
        role="leadership",
        kind="answer",
        intent=IntentResult(question_type="root_cause", restatement="r"),
        evidence=[other, waterfall],
    )
    assert main_chart_evidence(result) is waterfall
    lookup = result.model_copy(
        update={"intent": IntentResult(question_type="metric_lookup", restatement="r")}
    )
    assert main_chart_evidence(lookup) is None
