"""Paysight: Ask page (SPEC §13.2)."""

from __future__ import annotations

import streamlit as st
from components.answer import render_answer, render_tabs
from components.state import EXAMPLES, get_app

app = get_app()
settings = app.settings
roles = list(app.policies)
ss = st.session_state
ss.setdefault("role", "leadership")
ss.setdefault("question", "")
ss.setdefault("result", None)


def ask(question: str, role: str, choice: str | None = None) -> None:
    """Run the pipeline and keep the result in session state."""
    ss.question, ss.role = question, role
    with st.spinner("Planning, querying and checking…"):
        ss.result = app.ask(question, role, choice=choice)


# Shareable links: ?q=<question>&role=<role>[&choice=<option id>] answers on load.
params = st.query_params
if params.get("q") and ss.result is None:
    ask(params["q"], params.get("role", "leadership"), params.get("choice"))

with st.sidebar:
    st.markdown("## Paysight")
    st.caption("AI payments analyst that shows its work.")
    ss.role = st.selectbox(
        "Role",
        roles,
        index=roles.index(ss.role),
        format_func=lambda r: app.policies[r].label,
    )
    st.caption(app.policies[ss.role].description)
    info = app.llm.info
    color = {"replay": "#1B6B45", "live": "#D68910", "record": "#C0392B"}[info.mode]
    st.markdown(
        f"<span style='background:{color};color:white;border-radius:6px;padding:2px 8px;"
        f"font-weight:600'>{info.mode.upper()}</span> {info.provider} · {info.model}",
        unsafe_allow_html=True,
    )
    st.caption(f"Data as of **{settings.data.as_of_date:%d %b %Y}** (end of day)")
    st.markdown("### Example questions")
    for group, examples in EXAMPLES.items():
        st.markdown(f"**{group}**")
        for ex in examples:
            if st.button(ex.label, key=f"ex-{ex.label}", use_container_width=True):
                ask(ex.question, ex.role)

st.markdown("# Ask Paysight")
st.caption("Every number in an answer is traced to a validated query. All entities are fictional.")
with st.form("ask", clear_on_submit=False):
    question = st.text_input(
        "Your question",
        value=ss.question,
        placeholder="Choose a recorded example, or connect a model to ask a new question."
        if app.llm.info.mode == "replay"
        else "e.g. Why did success rate drop last week?",
    )
    if st.form_submit_button("Ask", type="primary") and question.strip():
        ask(question.strip(), ss.role)

result = ss.result
if result is not None:
    st.markdown(f"**Question** ({app.policies[result.role].label}): {result.question}")
    render_answer(
        result, on_choice=lambda cid: (ask(result.question, result.role, cid), st.rerun())
    )
    if result.evidence:
        render_tabs(result)
else:
    st.markdown(
        "**How it works:** 1. plans the analysis → 2. runs validated SQL on governed data → "
        "3. answers with every number grounded in an executed query."
    )
    hero = EXAMPLES["Root cause"][0]
    if st.button("Try the hero question"):
        ask(hero.question, hero.role)
        st.rerun()
