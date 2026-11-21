"""Grounding checker and deterministic confidence (SPEC §11.3, §11.4, §16.1)."""

from __future__ import annotations

from paysight.analysis.quality import Finding, QualityReport
from paysight.config import get_settings
from paysight.orchestrator.confidence import score_confidence
from paysight.orchestrator.grounding import (
    check_draft,
    claim_supported,
    extract_claims,
    strip_unsupported,
)
from paysight.orchestrator.models import AnswerDraft, Evidence, Fact

EV = [
    Evidence(
        id="E1",
        title="SR in both windows",
        kind="test",
        purpose="p",
        time_window="2026-10-18..2026-10-31 vs 2026-10-04..2026-10-17",
        key_numbers={
            "baseline_rate_pct": 90.01,
            "current_rate_pct": 88.54,
            "delta_pp": -1.47,
            "current_attempts": 230100,
            "baseline_attempts": 165297,
            "statistically_significant": True,
            "practically_significant": True,
            "p_value": 1e-48,
        },
    ),
    Evidence(
        id="E2",
        title="Reconciliation",
        kind="reconcile",
        purpose="p",
        time_window="2026-10-25..2026-10-31",
        key_numbers={
            "settled_gross_inr": 366716092.1,
            "unexplained_residual_pct": -1.8,
            "mix_share": 0.68,
        },
        result=[{"day": "2026-10-12", "raw_rows": 11704, "clean_rows": 11363}],
    ),
]
YEAR = 2026


def _draft(headline: str, *facts: tuple[str, list[str]]) -> AnswerDraft:
    return AnswerDraft(headline=headline, facts=[Fact(text=t, evidence_ids=i) for t, i in facts])


def test_extracts_each_claim_kind() -> None:
    claims = extract_claims(
        "SR fell 1.47pp to 88.5% on 18–31 Oct; gross ₹36.67 crore; 11,363 attempts; top 5 at "
        "19:00, 0.32 per 100 attempts",
        year=YEAR,
    )
    kinds = [(c.kind, c.text) for c in claims]
    assert ("pp", "1.47pp") in kinds and ("percent", "88.5%") in kinds
    assert ("inr", "₹36.67 crore") in kinds and ("count", "11,363") in kinds
    assert ("count", "0.32") in kinds
    assert any(k == "date" for k, _ in kinds)
    assert not any(t in ("5", "19", "100") for _, t in kinds)


def test_accepts_correctly_rounded_numbers() -> None:
    draft = _draft(
        "Success rate fell 1.5pp from 90.0% to 88.5%.",
        ("It fell 1.47pp to 88.54% over 230,100 attempts.", ["E1"]),
        ("Settled gross was ₹36.67 crore, 68% mix.", ["E2"]),
        ("Raw rows were 11,704 vs 11,363 clean on 12 October.", ["E2"]),
    )
    report = check_draft(draft, EV, year=YEAR)
    assert report.failures == [] and report.score == 1.0


def test_catches_an_invented_number() -> None:
    draft = _draft("SR fell 1.47pp.", ("SR fell 2.3pp to 88.5%.", ["E1"]))
    report = check_draft(draft, EV, year=YEAR)
    assert len(report.failures) == 1 and "2.3pp" in report.failures[0].reason
    cleaned, removed = strip_unsupported(draft, report)
    assert cleaned.facts == [] and removed[0].text == "SR fell 2.3pp to 88.5%."


def test_rounding_tolerance_follows_displayed_precision() -> None:
    (one_decimal,) = extract_claims("88.5%", year=YEAR)
    (two_decimals,) = extract_claims("88.50%", year=YEAR)
    assert claim_supported(one_decimal, EV[:1])  # 88.54 rounds to 88.5
    assert not claim_supported(two_decimals, EV[:1])  # but not to 88.50


def test_number_must_be_in_the_cited_evidence() -> None:
    draft = _draft("Answer.", ("Settled gross was ₹36.67 crore.", ["E1"]))  # wrong citation
    assert check_draft(draft, EV, year=YEAR).failures


def test_rejects_facts_without_or_with_unknown_evidence() -> None:
    draft = _draft("Answer.", ("No citation here.", []), ("Bad citation.", ["E9"]))
    reasons = [f.reason for f in check_draft(draft, EV, year=YEAR).failures]
    assert reasons == ["cites no evidence", "cites unknown ['E9']"]


def test_dates_must_fall_in_cited_windows() -> None:
    ok = _draft("A.", ("Between 18 Oct and 31 Oct 2026 the rate fell.", ["E1"]))
    bad = _draft("A.", ("On 3 September the rate fell.", ["E1"]))
    assert check_draft(ok, EV, year=YEAR).failures == []
    assert check_draft(bad, EV, year=YEAR).failures


def test_unverifiable_headline_is_replaced() -> None:
    draft = _draft("SR collapsed 9.9pp.", ("SR fell 1.47pp.", ["E1"]))
    report = check_draft(draft, EV, year=YEAR)
    cleaned, _ = strip_unsupported(draft, report)
    assert cleaned.headline == "SR fell 1.47pp."


def test_confidence_medium_for_handled_duplicates() -> None:
    dq = QualityReport(
        window_label="w",
        checks=[],
        findings=[
            Finding(
                check="duplicates",
                severity="warning",
                title="Duplicate records on 12 Oct",
                detail="d",
                affected_metrics=["attempts"],
                handled="removed in the clean view",
            )
        ],
    )
    conf = score_confidence(
        evidence=EV,
        dq=dq,
        relevant_dq=1,
        unresolved_ambiguities=0,
        removed_claims=0,
        settings=get_settings().analysis,
    )
    assert conf.level == "Medium"
    assert (
        "large samples" in conf.summary()
        and "Duplicate records on 12 Oct (removed)" in conf.summary()
    )


def test_confidence_high_and_low() -> None:
    a = get_settings().analysis
    high = score_confidence(
        evidence=EV, dq=None, relevant_dq=0, unresolved_ambiguities=0, removed_claims=0, settings=a
    )
    assert high.level == "High"
    weak = EV[0].model_copy(
        update={
            "key_numbers": {
                **EV[0].key_numbers,
                "current_attempts": 50,
                "statistically_significant": False,
            }
        }
    )
    low = score_confidence(
        evidence=[weak],
        dq=None,
        relevant_dq=0,
        unresolved_ambiguities=0,
        removed_claims=0,
        settings=a,
    )
    assert low.level == "Low"
