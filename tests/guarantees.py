"""Shared assertion: the grounding guarantee the system makes, independent of model output."""

from __future__ import annotations

from paysight.config import get_settings
from paysight.orchestrator.grounding import check_draft
from paysight.orchestrator.models import AskResult


def assert_grounding_guarantee(result: AskResult) -> None:
    """(a) Every claim still shown is grounded in the cited evidence; (b) every claim the checker
    removed from the narrator's draft is listed under "Unverified claims (removed)" with a reason.
    """
    if result.answer is None:
        return
    year = get_settings().data.as_of_date.year
    recheck = check_draft(result.answer, result.evidence, year=year)
    assert recheck.failures == [], f"shown answer has unsupported claims: {recheck.failures}"

    draft = result.draft_before_grounding
    assert draft is not None, "the pre-grounding draft must be kept for audit"
    removed = {r.text for r in result.removed_claims}
    assert all(r.reason for r in result.removed_claims), "every removed claim needs a reason"
    shown = {f.text for f in result.answer.facts}
    for fact in draft.facts:
        if fact.text not in shown:
            assert fact.text in removed, f"fact dropped without being listed: {fact.text!r}"
    assert not removed & shown, "a removed claim is still shown"
    if result.answer.headline != draft.headline:
        assert draft.headline in removed, "replaced headline must be listed as removed"
