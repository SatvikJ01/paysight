"""The answer card and the Charts / Evidence / Plan / Data quality / Trace tabs (SPEC §13.2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import streamlit as st

from components.charts import figure_for
from paysight.orchestrator.models import AskResult, Evidence

BADGE = {"High": "#1B6B45", "Medium": "#D68910", "Low": "#C0392B"}
CHECK_ICON = {"pass": "✓", "fail": "✗", "skipped": "–"}


def _chips(ids: list[str]) -> str:
    return " ".join(
        f"<span style='background:#E3EFE7;color:#1B6B45;border-radius:8px;padding:1px 7px;"
        f"font-size:0.8em;font-weight:600'>{i}</span>"
        for i in ids
    )


SOURCE_BADGE = {
    "governed": ("#1f6f8b", "Governed", "Every query came from the semantic layer."),
    "ad_hoc": ("#8b5a1f", "Ad-hoc SQL", "Part of this answer uses validated, AI-written SQL."),
}


def _source_badge(result: AskResult) -> str:
    """HTML badge: Governed (semantic-layer queries only) or Ad-hoc SQL."""
    if result.sql_source is None:
        return ""
    color, label, tip = SOURCE_BADGE[result.sql_source]
    return (
        f"<span title='{tip}' style='border:2px solid {color};color:{color};border-radius:6px;"
        f"padding:1px 8px;font-weight:600'>{label}</span>"
    )


def render_answer(result: AskResult, on_choice: Callable[[str], None]) -> None:
    """Restatement, then the answer card or the clarification / denial / refusal message."""
    if result.restatement:
        st.caption(f"I understood: {result.restatement}")
    if result.kind == "clarification":
        st.info(result.message or "Please clarify.")
        cols = st.columns(max(1, len(result.options)))
        for col, opt in zip(cols, result.options, strict=False):
            if col.button(opt.label, key=f"opt-{opt.id}", use_container_width=True):
                on_choice(opt.id)
        return
    if result.kind == "denied":
        st.error(f"Access denied. {result.message}")
        if result.suggestion:
            st.info(f"Try instead: {result.suggestion}")
        return
    if result.kind == "refused":
        st.warning(result.message)
        return
    if result.kind == "error":
        st.error(result.message)
        if not result.evidence:
            return

    a = result.answer
    if a is not None:
        st.markdown(f"#### {a.headline}")
    if result.confidence is not None:
        color = BADGE[result.confidence.level]
        st.markdown(
            f"<span style='background:{color};color:white;border-radius:6px;padding:3px 10px;"
            f"font-weight:600'>Confidence: {result.confidence.level}</span> "
            f"{_source_badge(result)} "
            f"<span style='color:#555'>{'; '.join(result.confidence.reasons)}</span>",
            unsafe_allow_html=True,
        )
    if a is not None:
        left, right = st.columns([3, 2])
        with left:
            st.markdown("#### Facts")
            for f in a.facts:
                st.markdown(f"- {f.text} {_chips(f.evidence_ids)}", unsafe_allow_html=True)
            if a.hypotheses:
                st.markdown("#### Hypotheses")
                for h in a.hypotheses:
                    st.markdown(f"- **({h.confidence})** {h.text}  \n  _Verify:_ {h.how_to_verify}")
        with right:
            if a.recommendations:
                st.markdown("#### Recommendations")
                for r in a.recommendations:
                    st.markdown(f"- {r.text}")
            st.markdown("#### Caveats & assumptions")
            for c in result.caveats:
                st.markdown(f"- {c}")
            for x in result.assumptions:
                st.markdown(f"- _Assumption:_ {x}")
    if result.removed_claims:
        with st.expander(f"⚠ Unverified claims (removed): {len(result.removed_claims)}"):
            for r in result.removed_claims:
                st.markdown(f"- ~~{r.text}~~  \n  _{r.reason}_")


def _validation_table(ev: Evidence) -> None:
    for i, report in enumerate(ev.validation, 1):
        label = f"Query {i}" + (
            " (failed, then repaired below)" if not report.passed and ev.ad_hoc else ""
        )
        st.markdown(f"**{label}** · role `{report.role}`")
        st.code(report.sql_out or report.sql_in, language="sql")
        st.dataframe(
            pd.DataFrame(
                [
                    {"": CHECK_ICON[c.status], "check": c.name, "detail": c.detail}
                    for c in report.checks
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )


def render_tabs(result: AskResult) -> None:
    """Charts, Evidence, Plan, Data quality and Trace."""
    charts, evidence, plan, dq, trace = st.tabs(
        ["📈 Charts", "🧾 Evidence", "🗺 Plan", "🩺 Data quality", "⏱ Trace"]
    )
    with charts:
        shown = 0
        for ev in result.evidence:
            fig = figure_for(ev)
            if fig is not None:
                st.plotly_chart(fig, use_container_width=True, key=f"chart-{ev.id}")
                st.caption(f"{ev.id} · {ev.time_window} · {', '.join(ev.filters) or 'no filters'}")
                shown += 1
        if not shown:
            st.caption("No charts for this answer.")
    with evidence:
        for ev in result.evidence:
            tags = (" · ⚙ ad-hoc SQL" if ev.ad_hoc else "") + (
                " · 🤖 LLM-suggested" if ev.llm_suggested else ""
            )
            with st.expander(f"{ev.id} · {ev.title}{tags}"):
                st.markdown(f"**Purpose:** {ev.purpose}")
                st.markdown(
                    f"**Datasets:** {', '.join(ev.datasets) or '–'} · "
                    f"**Window:** {ev.time_window} · "
                    f"**Filters:** {', '.join(ev.filters) or 'none'} · **Runtime:** "
                    f"{ev.runtime_ms:.0f} ms · **Cache hits:** {ev.cache_hits}"
                )
                if ev.key_numbers:
                    st.json(ev.key_numbers, expanded=False)
                if ev.result:
                    st.dataframe(pd.DataFrame(ev.result), hide_index=True, use_container_width=True)
                _validation_table(ev)
    with plan:
        for i, step in enumerate(result.plan, 1):
            mark = " 🤖 _LLM-suggested_" if step.llm_suggested else ""
            st.markdown(f"{i}. **{step.name}**: {step.description}{mark}")
        if not result.plan:
            st.caption("No analysis was planned for this request.")
    with dq:
        dq_evidence = [e for e in result.evidence if e.kind == "dq"]
        if dq_evidence and dq_evidence[0].result:
            st.dataframe(
                pd.DataFrame(dq_evidence[0].result), hide_index=True, use_container_width=True
            )
        elif dq_evidence:
            st.success("All data-quality checks passed for this scope.")
        else:
            st.caption("No data-quality scan for this request.")
    with trace:
        if result.trace_path and Path(result.trace_path).exists():
            data = json.loads(Path(result.trace_path).read_text())
            st.markdown(
                f"Run `{data['run_id']}` · mode **{data['llm_mode']}** · total "
                f"{data['total_ms']:.0f} ms · {data['total_tokens']:,} tokens · "
                f"${data['total_cost_usd']:.4f}"
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "step": s["name"],
                            "status": s["status"],
                            "ms": round(s["duration_ms"]),
                            "tokens": s["input_tokens"] + s["output_tokens"],
                            "cost_usd": round(s["cost_usd"], 5),
                            "llm": s.get("llm_source") or "",
                            "summary": s["summary"],
                        }
                        for s in data["steps"]
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
