"""Evaluation scorer: the same text-based rules for both systems."""

from __future__ import annotations

from pathlib import Path

from paysight.evaluation.questions import Case, Check, IntentExpectation, load_cases
from paysight.evaluation.scorer import SystemOutput, score, score_intent
from paysight.orchestrator.models import Evidence, IntentResult


def _out(text: str, behavior: str = "answer", system: str = "paysight", **kw) -> SystemOutput:
    return SystemOutput(system=system, behavior=behavior, core_text=text, full_text=text, **kw)


def test_numeric_matches_percent_against_fraction_gold() -> None:
    check = Check(type="numeric", gold="g.sql", tolerance=0.001)
    assert score(check, _out("October success rate was 89.2%."), 0.89227).passed
    assert not score(check, _out("October success rate was 88.1%."), 0.89227).passed


def test_numeric_relative_tolerance_and_crore() -> None:
    check = Check(type="numeric", gold="g.sql", tolerance_pct=0.5)
    assert score(check, _out("GMV was ₹126.91 crore."), 1_269_099_576.86).passed
    assert not score(check, _out("GMV was ₹120 crore."), 1_269_099_576.86).passed


def test_behavior_mentions_and_anomaly() -> None:
    assert score(
        Check(type="behavior", behavior="deny_access"), _out("x", "deny_access"), None
    ).passed
    mentions = Check(type="mentions", all=["Kestrel"], any=["timeout", "ACQ_TIMEOUT"])
    assert score(mentions, _out("Kestrel Bank card timeouts"), None).passed
    assert not score(mentions, _out("Kestrel Bank"), None).passed
    window = Check(type="anomaly_window", start="2026-09-17", end="2026-09-18", tolerance_days=0)
    assert score(window, _out("The outage ran from 17 September to 18 September."), None).passed
    assert not score(window, _out("Something happened on 3 September."), None).passed


def test_drivers_recall_mix_share_and_rate_source() -> None:
    check = Check(
        type="drivers",
        segments=["NovaMart", "Harbor"],
        mix_share=[0.6, 0.85],
        rate_source=["Harbor", "debit"],
    )
    decomposition = Evidence(
        id="E4",
        title="Mix vs rate decomposition by payment_method",
        kind="decomposition",
        purpose="",
        key_numbers={"mix_share_pct": 68.0},
    )
    good = _out("NovaMart grew; Harbor Bank debit cards declined.", evidence=[decomposition])
    outcome = score(check, good, None)
    assert outcome.passed and outcome.recall == 1.0
    baseline = _out("NovaMart grew; Harbor Bank debit cards declined.", system="baseline")
    outcome = score(check, baseline, None)
    assert not outcome.passed and outcome.recall == 1.0  # no decomposition: mix share missing


def test_adhoc_is_not_applicable_to_the_baseline() -> None:
    check = Check(type="adhoc", repaired=True)
    assert score(check, _out("x", system="baseline"), None).passed is None
    assert score(check, _out("x", adhoc=True, adhoc_repaired=True), None).passed
    assert not score(check, _out("x", adhoc=True, adhoc_repaired=False), None).passed


def test_intent_stage_scores_type_metric_and_resolved_window() -> None:
    intent = IntentResult(
        question_type="root_cause",
        restatement="r",
        metrics=["attempt_success_rate"],
        time_expr="last_n_weeks:2",
    )
    expected = IntentExpectation(
        question_type=["root_cause", "comparison"],
        metric="attempt_success_rate",
        window="2026-10-18..2026-10-31",
    )
    assert score_intent(expected, _out("x", intent=intent)) == {
        "question_type": True,
        "metric": True,
        "window": True,
    }
    wrong = intent.model_copy(update={"time_expr": None, "metrics": ["gmv_inr"]})
    assert score_intent(expected, _out("x", intent=wrong)) == {
        "question_type": True,
        "metric": False,
        "window": False,
    }
    # The deterministic guard refused before the intent stage ran: not scored.
    assert set(score_intent(expected, _out("x")).values()) == {None}


def test_paraphrase_variants_share_checks_and_group() -> None:
    case = Case(
        id="q",
        question="original",
        role="leadership",
        category="metric_lookup",
        checks=[Check(type="behavior", behavior="answer")],
        paraphrases=["one", "two"],
    )
    variants = case.variants()
    assert [v.id for v in variants] == ["q~p1", "q~p2"]
    assert [v.question for v in variants] == ["one", "two"]
    assert all(v.group == "q" and v.checks == case.checks for v in variants)


def test_question_file_is_valid_and_covers_the_spec_categories() -> None:
    cases = load_cases(Path(__file__).parents[1] / "eval" / "questions.yaml")
    categories = {c.category for c in cases}
    assert {
        "metric_lookup",
        "root_cause",
        "anomaly_explain",
        "ambiguity",
        "access_control",
        "safety",
        "data_quality",
        "reconciliation",
        "adhoc",
    } <= categories
    assert len(cases) >= 30
    groups = [c for c in cases if c.paraphrases]
    assert len(groups) >= 8 and all(len(c.paraphrases) == 2 for c in groups)
    assert sum(c.intent is not None for c in cases) >= 30
    gold_dir = Path(__file__).parents[1] / "eval"
    for case in cases:
        for check in case.checks:
            if check.type == "numeric":
                assert (gold_dir / check.gold).exists(), check.gold
