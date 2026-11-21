"""The end-to-end question pipeline (SPEC §4).

guard -> intent (LLM) -> unsafe/out-of-scope refusal -> clarification -> access pre-check ->
deterministic playbook (+ validated LLM extras) -> narration (LLM) -> grounding (one regeneration)
-> deterministic confidence -> trace.

Failures never crash the caller: an LLM outage returns the evidence already computed plus a
clear message saying which step failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from paysight.analysis.compare import AnalysisError
from paysight.config import Settings
from paysight.execution.engine import Engine
from paysight.llm.base import LLMError, ReplayMissError
from paysight.llm.replay import LLMClient
from paysight.observability.trace import Trace, new_run_id
from paysight.orchestrator import planner
from paysight.orchestrator.adhoc import run_adhoc
from paysight.orchestrator.confidence import score_confidence
from paysight.orchestrator.grounding import (
    check_draft,
    failure_feedback,
    strip_unsupported,
)
from paysight.orchestrator.intent import (
    catalog_values,
    extract_intent,
    postprocess,
    render_catalog,
    unsafe_reason,
)
from paysight.orchestrator.models import AskResult, ClarificationOption
from paysight.orchestrator.narrator import merge_caveats, narrate
from paysight.orchestrator.playbooks import (
    RUNNERS,
    NotExpressibleError,
    PlanError,
    RunContext,
    Scope,
    dq_step,
    plan_steps,
)
from paysight.safety.access import RolePolicy, precheck
from paysight.semantic.dates import DateResolutionError
from paysight.semantic.models import SemanticLayer

log = logging.getLogger("paysight.pipeline")

REFUSAL = (
    "I can't do that. Paysight is read-only: it can analyse payments data but never modify, "
    "delete or export it, change permissions, or reveal its configuration. Nothing was executed."
)
OUT_OF_SCOPE = (
    "That question is outside what Paysight covers. I can answer questions about payment success "
    "rates, volumes, GMV, failures, data quality and settlements."
)


REPLAY_MISS = (
    "Replay mode can answer the example questions (sidebar). Add an API key to .env to ask "
    "your own questions."
)
REPLAY_MISS_NARRATION = (
    "Replay mode has no recorded written answer for this exact question, so only the computed "
    "evidence is shown below. Add an API key to .env to get the written answer."
)


@dataclass
class Paysight:
    """Everything needed to answer questions; build once and reuse."""

    settings: Settings
    layer: SemanticLayer
    engine: Engine
    llm: LLMClient
    policies: dict[str, RolePolicy]
    runs_dir: Path | None = None

    def __post_init__(self) -> None:
        self._values = catalog_values(self.engine)
        self._catalog = render_catalog(self.layer, self._values)

    def ask(
        self,
        question: str,
        role: str,
        *,
        choice: str | None = None,
        options: list[ClarificationOption] | None = None,
    ) -> AskResult:
        """Answer ``question`` as ``role``. ``choice`` picks a clarification option by id."""
        policy = self.policies[role]
        trace = Trace(
            run_id=new_run_id(), question=question, role=role, llm_mode=self.llm.info.mode
        )
        result = AskResult(
            run_id=trace.run_id,
            question=question,
            role=role,
            kind="answer",
            llm_mode=f"{self.llm.info.mode} · {self.llm.info.provider} · {self.llm.info.model}",
        )
        chosen = next(
            (o for o in (options or planner.TIME_OPTIONS) if o.id == choice), None
        ) or planner.option_from_choice(choice, self.settings)
        if chosen and chosen.append_text:
            question = f"{question} {chosen.append_text}"
        try:
            self._run(question, policy, chosen, trace, result)
        finally:
            if self.runs_dir is not None:
                result.trace_path = str(trace.write(self.runs_dir))
        return result

    # ----------------------------------------------------------------------------------------

    def _adhoc(
        self,
        ctx: RunContext,
        question: str,
        reason: str,
        scope: Scope,
        trace: Trace,
        result: AskResult,
    ) -> None:
        """SPEC §9.6: answer with validated, AI-written SQL, then scan data quality."""
        result.plan = plan_steps("adhoc", scope.extra_steps, [])
        run_adhoc(ctx, self.llm, trace, question=question, reason=reason, filters=scope.filters)
        dq_step(ctx, scope.current, [], [])

    def _run(
        self,
        question: str,
        policy: RolePolicy,
        chosen: ClarificationOption | None,
        trace: Trace,
        result: AskResult,
    ) -> None:
        settings = self.settings
        with trace.step("safety_guard") as step:
            reason = unsafe_reason(question)
            step.summary = reason or "no unsafe pattern"
            if reason:
                step.status = "refused"
                result.kind, result.message = (
                    "refused",
                    f"{REFUSAL} (Reason: the request {reason}.)",
                )
                return

        try:
            with trace.step("intent") as step:
                intent, usage = extract_intent(
                    self.llm,
                    question=question,
                    role_description=policy.description,
                    as_of=settings.data.as_of_date.isoformat(),
                    catalog=self._catalog,
                )
                trace.add_usage(step, usage)
                step.summary = f"{intent.question_type}: {intent.restatement}"
        except ReplayMissError:
            result.kind, result.message = "unavailable", REPLAY_MISS
            return
        except LLMError as exc:
            result.kind, result.message = "error", f"Intent step failed: {exc}"
            return
        result.restatement = intent.restatement
        if intent.question_type == "unsafe_request":
            trace.steps[-1].status = "refused"
            result.kind = "refused"
            result.message = f"{REFUSAL} (Reason: {intent.unsafe_reason or 'unsafe request'}.)"
            result.intent = intent
            return
        if intent.question_type == "out_of_scope":
            result.kind, result.message, result.intent = "refused", OUT_OF_SCOPE, intent
            return

        with trace.step("validate_intent") as step:
            parsed = postprocess(intent, self.layer, self._values)
            if chosen is not None:
                parsed.intent = planner.apply_choice(parsed.intent, chosen)
            intent = parsed.intent
            result.intent = intent
            step.summary = "; ".join(parsed.notes) or "all names valid"
            step.details = {"intent": intent.model_dump(mode="json")}
            clar = planner.clarification(parsed)
            if clar:
                step.status = "skipped"
                result.kind, result.message, result.options = "clarification", clar[0], clar[1]
                return

        with trace.step("access_precheck") as step:
            extras = planner.validate_extras(intent.extra_steps, self.layer)
            filters = planner.scope_filters(intent)
            decision = precheck(
                policy,
                metrics=intent.metrics,
                dimensions=[*intent.dimensions, *(e.dimension for e in extras if e.dimension)],
                filters=filters,
                layer=self.layer,
            )
            step.details = decision.model_dump()
            if not decision.allowed:
                step.status = "denied"
                step.summary = " ".join(decision.reasons)
                result.kind = "denied"
                result.message = " ".join(decision.reasons) + " No query was executed."
                result.suggestion = decision.suggestion
                return
            step.summary = f"allowed for {policy.label}"

        try:
            windows = planner.resolve_windows(intent, settings)
        except DateResolutionError as exc:
            result.kind = "clarification"
            result.message, result.options = planner.period_fallback(intent, settings, exc)
            return

        if chosen is not None and chosen.time_expr:
            # Display only: the narrator still receives the parsed restatement.
            result.restatement = f"{intent.restatement} Period chosen: {chosen.label}."
        qtype = intent.question_type
        if planner.segment_comparison_filter(intent) is not None:
            result.plan = plan_steps("segment_comparison", [], intent.metrics)
        else:
            result.plan = plan_steps(qtype, extras, intent.metrics)
        result.assumptions = [
            *planner.standard_assumptions(intent),
            *windows.assumptions,
            *parsed.notes,
        ]
        scope = Scope(
            question_type=qtype,
            metrics=intent.metrics,
            dimensions=intent.dimensions,
            filters=filters,
            current=windows.current,
            baseline=windows.baseline,
            extra_steps=extras,
        )
        ctx = RunContext(engine=self.engine, role=policy, settings=settings, layer=self.layer)
        ctx.caveats.extend(windows.notes)
        with trace.step(f"playbook:{qtype}") as step:
            try:
                if intent.needs_custom_sql:
                    self._adhoc(ctx, question, intent.custom_sql_reason or "", scope, trace, result)
                else:
                    try:
                        RUNNERS[qtype](ctx, scope)
                    except NotExpressibleError as exc:
                        ctx.evidence.clear()
                        self._adhoc(ctx, question, str(exc), scope, trace, result)
            except ReplayMissError:
                step.status = "skipped"
                step.summary = "no recorded ad-hoc SQL in replay mode"
                result.kind, result.message = "unavailable", REPLAY_MISS
            except (AnalysisError, PlanError, LLMError) as exc:
                step.status = "error"
                step.summary = str(exc)
                result.kind = "error"
                result.message = f"The analysis could not be completed: {exc}"
            finally:
                result.evidence, result.caveats = ctx.evidence, list(dict.fromkeys(ctx.caveats))
                result.queries_executed = ctx.queries
                step.details = {
                    "evidence": [e.id + " " + e.title for e in ctx.evidence],
                    "evidence_meta": {e.id: e.meta for e in ctx.evidence if e.meta},
                    "queries": ctx.queries,
                }
        if result.kind in ("error", "unavailable"):
            return

        year = settings.data.as_of_date.year
        try:
            with trace.step("narrate") as step:
                draft, usage = narrate(
                    self.llm,
                    question=question,
                    restatement=intent.restatement,
                    assumptions=result.assumptions,
                    caveats=result.caveats,
                    evidence=ctx.evidence,
                )
                trace.add_usage(step, usage)
            with trace.step("grounding") as step:
                report = check_draft(draft, ctx.evidence, year=year)
                step.summary = f"{report.claims_supported}/{report.claims_total} claims supported"
                step.details = {"failures": [f.model_dump() for f in report.failures]}
            if report.failures:
                with trace.step("narrate_retry") as step:
                    draft, usage = narrate(
                        self.llm,
                        question=question,
                        restatement=intent.restatement,
                        assumptions=result.assumptions,
                        caveats=result.caveats,
                        evidence=ctx.evidence,
                        failures=failure_feedback(report),
                    )
                    trace.add_usage(step, usage)
                with trace.step("grounding_recheck") as step:
                    report = check_draft(draft, ctx.evidence, year=year)
                    step.summary = (
                        f"{report.claims_supported}/{report.claims_total} claims supported"
                    )
                    step.details = {"failures": [f.model_dump() for f in report.failures]}
        except ReplayMissError:
            result.kind, result.message = "unavailable", REPLAY_MISS_NARRATION
            return
        except LLMError as exc:
            result.kind = "error"
            result.message = (
                f"Narration failed ({exc}). The computed evidence below is complete "
                "and verified; only the written summary is missing."
            )
            return

        result.draft_before_grounding = draft
        shown, removed = strip_unsupported(draft, report)
        result.answer = shown
        result.removed_claims = removed
        result.grounding_score = report.score
        result.caveats = merge_caveats(result.caveats, draft.caveats)
        with trace.step("confidence") as step:
            result.confidence = score_confidence(
                evidence=ctx.evidence,
                dq=ctx.dq,
                relevant_dq=sum(
                    1
                    for c in ctx.caveats
                    if any(c.startswith(f.title) for f in (ctx.dq.findings if ctx.dq else []))
                ),
                unresolved_ambiguities=sum(1 for a in intent.ambiguities if a.proposed_default),
                removed_claims=len(removed),
                settings=settings.analysis,
            )
            step.summary = result.confidence.summary()


def build_paysight(settings: Settings, *, mode: str | None = None) -> Paysight:
    """Wire settings, semantic layer, engine, LLM client and policies together."""
    from paysight.llm.replay import build_client
    from paysight.safety.access import get_policies
    from paysight.semantic.loader import get_semantic_layer

    layer = get_semantic_layer()
    engine = Engine(settings.duckdb_path, layer=layer, limits=settings.limits)
    llm = build_client(settings, mode_override=mode)  # type: ignore[arg-type]
    return Paysight(
        settings=settings,
        layer=layer,
        engine=engine,
        llm=llm,
        policies=get_policies(),
        runs_dir=settings.root / "runs",
    )
