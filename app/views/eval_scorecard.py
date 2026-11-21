"""Eval scorecard: renders eval/reports/latest.json, Paysight vs the naive baseline (SPEC §13.4)."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from paysight.config import get_settings

st.markdown("# Evaluation scorecard")
path = get_settings().root / "eval" / "reports" / "latest.json"
if not path.exists():
    st.info("No evaluation report yet. Generate one with `uv run paysight eval run --mode replay`.")
    st.stop()

report = json.loads(path.read_text(encoding="utf-8"))
st.caption(
    f"Generated {report.get('generated_at', '')} · mode {report.get('mode', '')} · "
    f"{report.get('n_questions', '')} questions"
)
st.markdown("### Paysight vs naive text-to-SQL baseline")
st.dataframe(pd.DataFrame(report["summary"]), hide_index=True, use_container_width=True)
rob = report.get("robustness") or {}
if rob.get("groups"):
    st.markdown("### Phrasing robustness (Paysight)")
    st.metric(
        "Robustness score",
        f"{rob['score_pct']}%",
        help="Share of question groups where the original and every paraphrase pass.",
    )
    st.dataframe(
        pd.DataFrame(rob["groups"])[["id", "phrasings", "passed", "all_passed", "same_parse"]],
        hide_index=True,
        use_container_width=True,
    )
st.markdown("### Per question")
st.dataframe(pd.DataFrame(report["questions"]), hide_index=True, use_container_width=True)
if report.get("failures"):
    st.markdown("### Failures")
    for f in report["failures"]:
        st.markdown(f"- **{f['id']}** ({f['system']}): {f['reason']}")
