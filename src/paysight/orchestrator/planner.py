"""Planning (SPEC §9.2–9.3): clarification policy, time resolution and LLM-suggested extra steps.

* A why/compare/trend/anomaly question with no time window is a high-materiality ambiguity with
  no safe default, so Paysight asks, offering fixed options as buttons.
* Other questions with no time window default to the last 7 days, recorded as an assumption.
* The LLM may suggest at most 3 extra steps; each must name a known, non-PII dimension that the
  attempts view supports, or it is dropped.
* A comparison that names two or more values of one dimension and no second period compares
  those segments within one window; it needs no baseline (D36).
* When a stated period cannot be compared because the comparison falls outside the data, the
  options keep the stated period instead of replacing it (D36).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from paysight.config import Settings
from paysight.orchestrator.intent import ParsedIntent
from paysight.orchestrator.models import (
    ClarificationOption,
    ExtraStep,
    IntentFilter,
    IntentResult,
)
from paysight.semantic.dates import DateResolutionError, resolve, resolve_compare
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
    if option.question_type:
        update["question_type"] = option.question_type
    return intent.model_copy(update=update)


def segment_comparison_filter(intent: IntentResult) -> IntentFilter | None:
    """The filter naming the segments to compare, if this compares segments within one period.

    "Compare UPI and card success rates in August" parses as a comparison with
    ``method_family in [upi, card]`` and no second period: it compares the segments, not August
    with July.
    """
    if intent.question_type != "comparison" or intent.compare_time_expr:
        return None
    return next((f for f in intent.filters if f.op == "in" and len(f.values) >= 2), None)


# Choice ids for period options encode the option itself, so the UI and the CLI can send them
# back without the server keeping state: "period:<time_expr>~<compare or none>~<type or ''>".
_PERIOD_PREFIX = "period:"
_CHOICE_TYPES = {"", "metric_lookup"}


def _nice(w: TimeWindow) -> str:
    """'August 2026' for a whole calendar month, otherwise '1–10 Sep 2026'-style ranges."""
    if _is_month(w):
        return f"{w.start:%B %Y}"
    return f"{w.start.day} {w.start:%b %Y} to {w.end.day} {w.end:%b %Y}"


def _is_month(w: TimeWindow) -> bool:
    return w.start.day == 1 and (w.end + timedelta(days=1)).day == 1 and w.days >= 28


def _following(w: TimeWindow) -> TimeWindow:
    """The next calendar month after a whole month, otherwise the next period of equal length."""
    start = w.end + timedelta(days=1)
    if _is_month(w):
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return TimeWindow(start=start, end=end)
    return TimeWindow(start=start, end=w.end + timedelta(days=w.days))


def _explicit(w: TimeWindow) -> str:
    return f"explicit:{w.start.isoformat()}..{w.end.isoformat()}"


def _period_option(
    label: str, time_expr: str, compare: str | None, question_type: str | None = None
) -> ClarificationOption:
    choice_id = f"{_PERIOD_PREFIX}{time_expr}~{compare or 'none'}~{question_type or ''}"
    return ClarificationOption(
        id=choice_id,
        label=label,
        time_expr=time_expr,
        compare_time_expr=compare,
        question_type=question_type,
    )


def option_from_choice(choice: str | None, settings: Settings) -> ClarificationOption | None:
    """Rebuild a period option from its id; anything that does not resolve is ignored."""
    if not choice or not choice.startswith(_PERIOD_PREFIX):
        return None
    parts = choice.removeprefix(_PERIOD_PREFIX).split("~")
    if len(parts) != 3 or parts[2] not in _CHOICE_TYPES:
        return None
    time_expr, compare, qtype = parts[0], None if parts[1] == "none" else parts[1], parts[2]
    d = settings.data
    try:
        cur = resolve(time_expr, as_of=d.as_of_date, data_start=d.start_date).window
        base = (
            resolve(compare, as_of=d.as_of_date, data_start=d.start_date).window
            if compare
            else None
        )
    except DateResolutionError:
        return None
    label = _nice(cur) + (f" vs {_nice(base)}" if base else " on its own")
    return _period_option(label, time_expr, compare, qtype or None)


def period_fallback(
    intent: IntentResult, settings: Settings, error: DateResolutionError
) -> tuple[str, list[ClarificationOption]]:
    """Message and options when a period cannot be used, keeping what the user stated."""
    d = settings.data
    start = f"{d.start_date.day} {d.start_date:%b %Y}"
    end = f"{d.as_of_date.day} {d.as_of_date:%b %Y}"
    try:
        cur = resolve(
            intent.time_expr or DEFAULT_TIME, as_of=d.as_of_date, data_start=d.start_date
        ).window
    except DateResolutionError:
        return (
            f"I couldn't use that period: {error}. The data covers {start} to {end}; "
            "pick a period inside it.",
            TIME_OPTIONS,
        )
    options = []
    nxt = _following(cur)
    if nxt.end <= d.as_of_date:
        options.append(
            _period_option(f"{_nice(nxt)} vs {_nice(cur)}", _explicit(nxt), _explicit(cur))
        )
    if intent.question_type != "root_cause":
        options.append(
            _period_option(f"{_nice(cur)} on its own", _explicit(cur), None, "metric_lookup")
        )
    message = (
        f"The data starts on {start}, so there is nothing before {_nice(cur)} to compare it "
        f"with ({error})."
    )
    return message + (" Choose one of these instead:" if options else ""), options


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
    segments = segment_comparison_filter(intent)
    if segments is not None:
        assumptions.append(
            f"Comparing {', '.join(segments.values)} ({segments.dimension}) within "
            f"{current.window.label()}; no second period was asked for."
        )
    elif intent.question_type in NEEDS_BASELINE or intent.compare_time_expr:
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
    if segments is None and baseline is None:
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
