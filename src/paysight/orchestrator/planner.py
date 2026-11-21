"""Planning (SPEC §9.2–9.3): clarification policy, time resolution and LLM-suggested extra steps.

* A why/compare/trend/anomaly question with no time window is a high-materiality ambiguity with
  no safe default, so Paysight asks, offering fixed options as buttons.
* Other questions with no time window default to the last 7 days, recorded as an assumption.
* The LLM may suggest at most 3 extra steps; each must name a known, non-PII dimension that the
  attempts view supports, or it is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paysight.config import Settings
from paysight.orchestrator.intent import ParsedIntent
from paysight.orchestrator.models import ClarificationOption, ExtraStep, IntentResult
from paysight.semantic.dates import resolve, resolve_compare
from paysight.semantic.models import Filter, SemanticLayer, TimeWindow

MAX_EXTRA_STEPS = 3
NEEDS_WINDOW = {"root_cause", "comparison", "trend", "anomaly_explain"}
NEEDS_BASELINE = {"root_cause", "comparison"}
DEFAULT_TIME = "last_n_days:7"
SR_METRICS = {"attempt_success_rate", "order_success_rate", "technical_success_rate"}

TIME_OPTIONS = [
    ClarificationOption(
        id="last_7",
        label="Last 7 days vs the 7 days before",
        time_expr="last_n_days:7",
        compare_time_expr="previous_period",
    ),
    ClarificationOption(
        id="last_14",
        label="Last 14 days vs the 14 days before",
        time_expr="last_n_days:14",
        compare_time_expr="previous_period",
    ),
    ClarificationOption(
        id="oct_vs_sep",
        label="October vs September",
        time_expr="this_month",
        compare_time_expr="last_month",
    ),
]


def validate_extras(extras: list[ExtraStep], layer: SemanticLayer) -> list[ExtraStep]:
    """Keep at most 3 valid LLM-suggested steps."""
    kept = []
    for step in extras:
        if step.tool == "breakdown":
            dim = next((d for d in layer.dimensions if d.name == step.dimension), None)
            if dim is None or dim.sensitivity == "pii" or "v_attempts_clean" not in dim.sources:
                continue
        kept.append(step)
    return kept[:MAX_EXTRA_STEPS]


def clarification(parsed: ParsedIntent) -> tuple[str, list[ClarificationOption]] | None:
    """Return (message, options) when the question cannot proceed without the user."""
    intent = parsed.intent
    if parsed.unresolved:
        dim, value, suggestions = parsed.unresolved[0]
        options = [
            ClarificationOption(id=f"entity_{i}", label=s, append_text=f"(I mean {dim} '{s}')")
            for i, s in enumerate(suggestions)
        ]
        hint = " Did you mean one of these?" if options else " Please check the name."
        return f"I couldn't find a {dim.replace('_', ' ')} called '{value}'.{hint}", options
    if intent.question_type in NEEDS_WINDOW and not intent.time_expr:
        return (
            "Which period should I analyse? The answer depends heavily on the window.",
            TIME_OPTIONS,
        )
    for amb in intent.ambiguities:
        if amb.materiality == "high" and not amb.proposed_default and amb.field != "time":
            options = [
                ClarificationOption(id=f"opt_{i}", label=o, append_text=f"({o})")
                for i, o in enumerate(amb.options[:4])
            ]
            if len(options) >= 2:
                return f"Your question is ambiguous about {amb.field}. Which do you mean?", options
    return None


def apply_choice(intent: IntentResult, option: ClarificationOption) -> IntentResult:
    """Apply a time-window clarification choice to the parsed intent."""
    update: dict = {}
    if option.time_expr:
        update["time_expr"] = option.time_expr
        update["compare_time_expr"] = option.compare_time_expr
        update["ambiguities"] = [a for a in intent.ambiguities if a.field != "time"]
    return intent.model_copy(update=update)


@dataclass
class Windows:
    """Resolved windows plus the assumptions they imply."""

    current: TimeWindow
    baseline: TimeWindow | None
    assumptions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def resolve_windows(intent: IntentResult, settings: Settings) -> Windows:
    """Resolve time expressions against AS_OF_DATE (raises DateResolutionError)."""
    d = settings.data
    assumptions: list[str] = []
    expr = intent.time_expr
    if not expr:
        expr = DEFAULT_TIME
        assumptions.append("No period was given; using the last 7 days of data.")
    current = resolve(expr, as_of=d.as_of_date, data_start=d.start_date)
    baseline = None
    if intent.question_type in NEEDS_BASELINE or intent.compare_time_expr:
        baseline = resolve_compare(
            current, intent.compare_time_expr, as_of=d.as_of_date, data_start=d.start_date
        )
        assumptions.append(
            f"Period: {current.window.label()} compared with {baseline.window.label()}"
            + (
                " (the immediately preceding period of equal length)."
                if baseline.expression == "previous_period"
                else "."
            )
        )
    else:
        assumptions.append(f"Period: {current.window.label()}.")
    notes = [*current.notes, *(baseline.notes if baseline else [])]
    return Windows(current.window, baseline.window if baseline else None, assumptions, notes)


def scope_filters(intent: IntentResult) -> list[Filter]:
    """Intent filters as typed semantic-layer filters."""
    return [Filter(dimension=f.dimension, op=f.op, values=list(f.values)) for f in intent.filters]


def standard_assumptions(intent: IntentResult) -> list[str]:
    """Assumptions every answer states: success-rate definition and applied defaults."""
    out = []
    if not intent.metrics or "attempt_success_rate" in intent.metrics:
        out.append(
            "'Success rate' means attempt-level success rate: successful attempts divided by "
            "all attempts, with each retry counted as a separate attempt. Order-level and "
            "technical rates are shown alongside."
        )
    for amb in intent.ambiguities:
        if amb.proposed_default:
            out.append(f"{amb.field.capitalize()}: assumed {amb.proposed_default}.")
    return out
