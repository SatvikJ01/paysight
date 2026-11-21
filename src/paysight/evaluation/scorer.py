"""Scoring: the same rules for Paysight and the baseline wherever a check applies to both.

Answers are scored on their text: the numbers they state, the segments and dates they name, the
behaviour they show. Checks that need structure only Paysight produces (a mix share from a
decomposition, the ad-hoc SQL path) are scored for Paysight and count as a miss or "n/a" for
the baseline, as documented per check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from paysight.evaluation.baseline import BaselineResult
from paysight.evaluation.questions import Check, IntentExpectation
from paysight.orchestrator.grounding import check_draft, extract_claims
from paysight.orchestrator.models import AnswerDraft, AskResult, Evidence, IntentResult
from paysight.semantic.dates import DateResolutionError, resolve

BEHAVIOR_OF_KIND = {
    "answer": "answer",
    "clarification": "clarify",
    "denied": "deny_access",
    "refused": "refuse_unsafe",
    "error": "error",
}
BEHAVIOR_OF_STATUS = {
    "answered": "answer",
    "needs_clarification": "clarify",
    "access_denied": "deny_access",
    "refused": "refuse_unsafe",
    "failed": "error",
}


@dataclass
class SystemOutput:
    """A system's answer, normalised for scoring."""

    system: str
    behavior: str
    core_text: str  # headline + facts (Paysight) or the answer (baseline): numbers are scored here
    full_text: str  # plus hypotheses, recommendations and caveats: mentions are scored here
    evidence: list[Evidence] = field(default_factory=list)
    grounded: bool | None = None
    removed_claims: int = 0
    adhoc: bool = False
    adhoc_repaired: bool = False
    intent: IntentResult | None = None
    latency_ms: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class CheckOutcome:
    """Result of one check: passed is ``None`` when the check does not apply to the system."""

    type: str
    passed: bool | None
    detail: str
    recall: float | None = None


def from_paysight(result: AskResult, *, latency_ms: float, trace: dict[str, Any]) -> SystemOutput:
    """Normalise a Paysight :class:`AskResult`."""
    a = result.answer
    core = [a.headline, *(f.text for f in a.facts)] if a else [result.message or ""]
    extra = []
    if a:
        extra += [h.text for h in a.hypotheses] + [r.text for r in a.recommendations]
    extra += result.caveats + result.assumptions
    grounded = None
    if a is not None:
        from paysight.config import get_settings

        year = get_settings().data.as_of_date.year
        grounded = not check_draft(a, result.evidence, year=year).failures
    steps = trace.get("steps", [])
    return SystemOutput(
        system="paysight",
        behavior=BEHAVIOR_OF_KIND.get(result.kind, "error"),
        core_text=" ".join(core),
        full_text=" ".join(core + extra),
        evidence=result.evidence,
        grounded=grounded,
        removed_claims=len(result.removed_claims),
        adhoc=any(e.ad_hoc for e in result.evidence),
        adhoc_repaired=any(
            e.ad_hoc and e.key_numbers.get("repair_attempts", 0) >= 1 for e in result.evidence
        ),
        intent=result.intent,
        latency_ms=latency_ms,
        llm_calls=sum(1 for s in steps if s.get("llm_source")),
        input_tokens=sum(s.get("input_tokens", 0) for s in steps),
        output_tokens=sum(s.get("output_tokens", 0) for s in steps),
        cost_usd=sum(s.get("cost_usd", 0.0) for s in steps),
    )


def from_baseline(res: BaselineResult) -> SystemOutput:
    """Normalise a baseline run; grounding checks its numbers against its own query rows."""
    from paysight.config import get_settings

    grounded = None
    if res.status == "answered":
        ev = Evidence(
            id="E1",
            title="baseline query",
            kind="query",
            purpose="",
            result=res.rows,
            time_window="",
        )
        draft = AnswerDraft(headline=res.answer)
        year = get_settings().data.as_of_date.year
        grounded = not check_draft(draft, [ev], year=year).failures
    return SystemOutput(
        system="baseline",
        behavior=BEHAVIOR_OF_STATUS.get(res.status, "error"),
        core_text=res.answer,
        full_text=res.answer,
        grounded=grounded,
        latency_ms=res.latency_ms,
        llm_calls=len(res.usages),
        input_tokens=sum(
            u.input_tokens + u.cache_read_tokens + u.cache_write_tokens for u in res.usages
        ),
        output_tokens=sum(u.output_tokens for u in res.usages),
        cost_usd=sum(u.cost_usd for u in res.usages),
    )


def _numbers(text: str) -> list[tuple[float, float, str]]:
    """(value, display tolerance, kind) for every numeric claim in ``text``."""
    from paysight.config import get_settings

    year = get_settings().data.as_of_date.year
    return [
        (c.value, c.tolerance, c.kind)
        for c in extract_claims(text, year=year)
        if c.value is not None
    ]


def score_numeric(check: Check, out: SystemOutput, gold: float) -> CheckOutcome:
    """Pass if the answer states the gold value within tolerance."""
    tol = float(getattr(check, "tolerance", 0.0) or 0.0)
    if getattr(check, "tolerance_pct", None) is not None:
        tol = max(tol, abs(gold) * float(check.tolerance_pct) / 100)
    for value, display_tol, kind in _numbers(out.core_text):
        candidates = [(value, display_tol)]
        if kind == "percent":
            candidates.append((value / 100, display_tol / 100))
        for v, dt in candidates:
            if abs(abs(v) - abs(gold)) <= max(tol, dt):
                return CheckOutcome("numeric", True, f"stated {v:g} vs gold {gold:g}")
    return CheckOutcome("numeric", False, f"no stated number matches gold {gold:g}")


def score_behavior(check: Check, out: SystemOutput) -> CheckOutcome:
    """Pass if the system behaved as expected (answer, clarify, deny, refuse)."""
    want = check.behavior  # type: ignore[attr-defined]
    return CheckOutcome("behavior", out.behavior == want, f"{out.behavior} (expected {want})")


def score_mentions(check: Check, out: SystemOutput) -> CheckOutcome:
    """Pass if all ``all`` phrases and at least one ``any`` phrase appear (case-insensitive)."""
    text = out.full_text.lower()
    required = [str(p) for p in getattr(check, "all", []) or []]
    options = [str(p) for p in getattr(check, "any", []) or []]
    missing = [p for p in required if p.lower() not in text]
    any_ok = not options or any(p.lower() in text for p in options)
    passed = not missing and any_ok
    detail = "ok" if passed else f"missing {missing or ''}{'' if any_ok else f' none of {options}'}"
    return CheckOutcome("mentions", passed, detail)


def _mix_share(out: SystemOutput) -> float | None:
    for ev in out.evidence:
        if ev.kind == "decomposition" and ev.title.endswith("payment_method"):
            pct = ev.key_numbers.get("mix_share_pct")
            return None if pct is None else pct / 100
    return None


def score_drivers(check: Check, out: SystemOutput) -> CheckOutcome:
    """Recall@2 of expected segments in the answer, plus mix share and rate source if given."""
    text = out.full_text.lower()
    segments = [str(s) for s in check.segments][:2]  # type: ignore[attr-defined]
    named = [s for s in segments if s.lower() in text]
    recall = len(named) / len(segments) if segments else 1.0
    problems = [] if recall == 1.0 else [f"segments named {named} of {segments}"]
    mix_range = getattr(check, "mix_share", None)
    if mix_range is not None:
        share = _mix_share(out)
        if share is None or not mix_range[0] <= share <= mix_range[1]:
            problems.append(f"mix share {share} not in {mix_range}")
    rate_words = [str(w) for w in getattr(check, "rate_source", []) or []]
    missing_rate = [w for w in rate_words if w.lower() not in text]
    if missing_rate:
        problems.append(f"rate source missing {missing_rate}")
    return CheckOutcome("drivers", not problems, "; ".join(problems) or "ok", recall=recall)


def score_anomaly(check: Check, out: SystemOutput) -> CheckOutcome:
    """Pass if the answer names a date inside the expected window (± tolerance)."""
    from paysight.config import get_settings

    tol = timedelta(days=int(getattr(check, "tolerance_days", 0)))
    start = date.fromisoformat(str(check.start)) - tol  # type: ignore[attr-defined]
    end = date.fromisoformat(str(check.end)) + tol  # type: ignore[attr-defined]
    year = get_settings().data.as_of_date.year
    dates = [d for c in extract_claims(out.full_text, year=year) for d in c.dates]
    hit = any(start <= d <= end for d in dates)
    return CheckOutcome("anomaly_window", hit, f"dates named: {sorted(set(dates))[:4]}")


def score_adhoc(check: Check, out: SystemOutput) -> CheckOutcome:
    """Paysight only: answered through the ad-hoc SQL fallback (after a repair if required)."""
    if out.system != "paysight":
        return CheckOutcome("adhoc", None, "n/a for the baseline")
    want_repair = bool(getattr(check, "repaired", False))
    passed = out.adhoc and (out.adhoc_repaired or not want_repair)
    return CheckOutcome("adhoc", passed, f"adhoc={out.adhoc} repaired={out.adhoc_repaired}")


def score(check: Check, out: SystemOutput, gold: float | None) -> CheckOutcome:
    """Dispatch one check."""
    if check.type == "numeric":
        if gold is None:
            return CheckOutcome("numeric", False, "gold SQL returned no value")
        return score_numeric(check, out, gold)
    return {
        "behavior": score_behavior,
        "mentions": score_mentions,
        "drivers": score_drivers,
        "anomaly_window": score_anomaly,
        "adhoc": score_adhoc,
    }[check.type](check, out)


def resolved_window(intent: IntentResult) -> str:
    """The intent's current window as ``start..end``, ``none`` or ``unresolvable``."""
    from paysight.config import get_settings

    if not intent.time_expr:
        return "none"
    d = get_settings().data
    try:
        w = resolve(intent.time_expr, as_of=d.as_of_date, data_start=d.start_date).window
    except DateResolutionError:
        return "unresolvable"
    return f"{w.start.isoformat()}..{w.end.isoformat()}"


def score_intent(expected: IntentExpectation, out: SystemOutput) -> dict[str, bool | None]:
    """Intent-stage accuracy per field; ``None`` when not expected or the stage did not run.

    The stage does not run when the deterministic safety guard refuses first, so those cases
    are "n/a" here (the behavior check scores the refusal).
    """
    intent = out.intent
    fields: dict[str, bool | None] = {"question_type": None, "metric": None, "window": None}
    if intent is None:
        return fields
    if expected.question_types:
        fields["question_type"] = intent.question_type in expected.question_types
    if expected.metric is not None:
        fields["metric"] = expected.metric in intent.metrics
    if expected.window is not None:
        fields["window"] = resolved_window(intent) == expected.window
    return fields
