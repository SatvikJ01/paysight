"""Data contracts for the orchestrator: LLM schemas, evidence, plans and answers (SPEC §9, §11).

``IntentResult`` and ``AnswerDraft`` are the two schemas the LLM fills. Everything else is
built by deterministic code.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paysight.safety.validator import ValidationReport

QuestionType = Literal[
    "metric_lookup",
    "trend",
    "comparison",
    "root_cause",
    "anomaly_explain",
    "ranking",
    "data_quality",
    "reconciliation",
    "out_of_scope",
    "unsafe_request",
]
ExtraTool = Literal["breakdown", "failure_codes", "daily_series", "hourly_profile", "top_merchants"]


# --------------------------------------------------------------------------------------------
# LLM output: intent
# --------------------------------------------------------------------------------------------


class IntentFilter(BaseModel):
    """A filter in semantic terms. Values are strings; the compiler binds them as literals."""

    dimension: str
    op: Literal["=", "!=", "in", "not_in", "between", ">=", "<="]
    values: list[str]


class Ambiguity(BaseModel):
    """Something the question leaves open."""

    field: str
    options: list[str]
    proposed_default: str | None = None
    materiality: Literal["high", "low"]


class Entity(BaseModel):
    """A named thing in the question (merchant, bank, PSP...) to resolve against data values."""

    text: str
    dimension: str


class ExtraStep(BaseModel):
    """An LLM-suggested analysis step, chosen from a fixed tool list."""

    tool: ExtraTool
    dimension: str | None = None
    reason: str


class IntentResult(BaseModel):
    """Structured reading of the question (filled by the LLM, validated by code)."""

    question_type: QuestionType
    restatement: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[IntentFilter] = Field(default_factory=list)
    time_expr: str | None = None
    compare_time_expr: str | None = None
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    extra_steps: list[ExtraStep] = Field(default_factory=list)
    unsafe_reason: str | None = None
    needs_custom_sql: bool = False
    custom_sql_reason: str | None = None


class AdhocSQL(BaseModel):
    """LLM-written SQL for a question the semantic layer cannot express (SPEC §9.6)."""

    sql: str
    explanation: str


# --------------------------------------------------------------------------------------------
# LLM output: narration
# --------------------------------------------------------------------------------------------


class Fact(BaseModel):
    """An observed fact, citing the evidence it comes from."""

    text: str
    evidence_ids: list[str]


class Hypothesis(BaseModel):
    """A possible explanation. Causal language belongs here, never in facts."""

    text: str
    confidence: Literal["low", "med", "high"]
    rationale: str
    how_to_verify: str


class Recommendation(BaseModel):
    """A suggested next step."""

    text: str
    rationale: str
    linked_fact_or_hypothesis: str


class AnswerDraft(BaseModel):
    """The narrator's structured answer, before grounding."""

    headline: str
    facts: list[Fact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Deterministic records
# --------------------------------------------------------------------------------------------

EvidenceKind = Literal[
    "query",
    "decomposition",
    "drivers",
    "test",
    "dq",
    "reconcile",
    "series",
    "anomaly",
    "sensitivity",
    "ranking",
    "failure_codes",
]
ChartKind = Literal[
    "waterfall",
    "contribution_bar",
    "daily_line",
    "heatmap",
    "failure_codes",
    "reconciliation",
    "bar",
    "table",
]


# Pipeline counters describe how an analysis ran, not the business. They live in
# ``Evidence.meta`` (and the trace) and must never be in ``key_numbers``, which the narrator
# reads and may state as facts.
META_KEYS = frozenset({"repair_attempts", "segments_tested", "cells", "queries", "cache_hits"})


class Evidence(BaseModel):
    """One analysis step's verifiable output (SPEC §11.1)."""

    id: str
    title: str
    kind: EvidenceKind
    purpose: str
    sql: list[str] = Field(default_factory=list)
    validation: list[ValidationReport] = Field(default_factory=list)
    runtime_ms: float = 0.0
    cache_hits: int = 0
    datasets: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    time_window: str = ""
    result: list[dict[str, Any]] = Field(default_factory=list)
    key_numbers: dict[str, Any] = Field(default_factory=dict)
    chart_data: list[dict[str, Any]] = Field(default_factory=list)  # full series for charts only
    meta: dict[str, Any] = Field(default_factory=dict)  # pipeline counters; never narrated
    ad_hoc: bool = False
    llm_suggested: bool = False
    chart: ChartKind | None = None

    @model_validator(mode="after")
    def _no_meta_in_key_numbers(self) -> Evidence:
        leaked = META_KEYS & set(self.key_numbers)
        if leaked:
            raise ValueError(f"pipeline counters belong in meta, not key_numbers: {sorted(leaked)}")
        return self


class PlanStep(BaseModel):
    """One planned analysis step."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    llm_suggested: bool = False


class ClarificationOption(BaseModel):
    """A button the user can press to resolve an ambiguity."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    time_expr: str | None = None
    compare_time_expr: str | None = None
    append_text: str | None = None


class Confidence(BaseModel):
    """Deterministic confidence with its reasons (SPEC §11.4)."""

    model_config = ConfigDict(frozen=True)

    level: Literal["High", "Medium", "Low"]
    reasons: list[str]

    def summary(self) -> str:
        """e.g. 'Medium — large samples and a significant change, but ...'."""
        return f"{self.level} — " + "; ".join(self.reasons)


class RemovedClaim(BaseModel):
    """A claim the grounding checker could not verify and removed."""

    model_config = ConfigDict(frozen=True)

    text: str
    reason: str


AnswerKind = Literal["answer", "clarification", "denied", "refused", "error"]


class AskResult(BaseModel):
    """Everything the CLI and UI need to show one question's outcome."""

    run_id: str
    question: str
    role: str
    kind: AnswerKind
    restatement: str | None = None
    intent: IntentResult | None = None
    message: str | None = None  # denial / refusal / clarification / error text
    suggestion: str | None = None
    options: list[ClarificationOption] = Field(default_factory=list)
    answer: AnswerDraft | None = None
    removed_claims: list[RemovedClaim] = Field(default_factory=list)
    draft_before_grounding: AnswerDraft | None = None  # the narrator's draft, kept for audit
    grounding_score: float | None = None
    confidence: Confidence | None = None
    plan: list[PlanStep] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    queries_executed: int = 0
    llm_mode: str = ""
    trace_path: str | None = None

    @property
    def sql_source(self) -> Literal["governed", "ad_hoc"] | None:
        """Where the answer's SQL came from: governed, ad_hoc, or None when no query ran.

        Governed queries come from the semantic-layer compiler or from reviewed fixed SQL
        (data quality, reconciliation); ad-hoc queries come from the SPEC §9.6 fallback.
        """
        if not self.evidence:
            return None
        return "ad_hoc" if any(e.ad_hoc for e in self.evidence) else "governed"
