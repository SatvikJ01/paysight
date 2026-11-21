"""Data health: the data-quality report over the full dataset (SPEC §13.4)."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from components.charts import GREEN, LAYOUT
from components.state import get_app

from paysight.analysis.drilldown import rate_series
from paysight.analysis.quality import data_quality_report
from paysight.semantic.models import TimeWindow

app = get_app()
d = app.settings.data
role = st.session_state.get("role", "leadership")
policy = app.policies[role]
window = TimeWindow(start=d.start_date, end=d.end_date)

st.markdown("# Data health")
st.caption(f"Full dataset {window.label()} · role: {policy.label} (row filters apply)")


@st.cache_data(show_spinner="Running data-quality checks…")
def _report(role: str):
    report, _ = data_quality_report(
        app.engine,
        app.policies[role],
        window=window,
        as_of=d.as_of_date,
        settings=app.settings.analysis,
    )
    series, _ = rate_series(app.engine, app.policies[role], window=window)
    return report, series


report, series = _report(role)
st.dataframe(
    pd.DataFrame(
        [
            {
                "check": c.check,
                "description": c.description,
                "result": "✓ clean" if c.findings == 0 else f"⚠ {c.findings} finding(s)",
            }
            for c in report.checks
        ]
    ),
    hide_index=True,
    use_container_width=True,
)
for f in report.findings:
    icon = {"high": "🔴", "warning": "🟠", "info": "🔵"}[f.severity]
    with st.expander(f"{icon} {f.title}"):
        st.markdown(f"{f.detail}\n\n**Handling:** {f.handled}")
        if f.affected_metrics or f.affected_dimensions:
            st.caption("Affects: " + ", ".join(f.affected_metrics + f.affected_dimensions))
        if f.rows:
            st.dataframe(pd.DataFrame(f.rows), hide_index=True)

fig = go.Figure(go.Bar(x=series["period"], y=series["den"], marker_color=GREEN))
fig.update_layout(
    **LAYOUT,
    title="Daily attempts (deduplicated)",
    yaxis={"title": "Attempts per day"},
    xaxis={"title": "Date"},
)
st.plotly_chart(fig, use_container_width=True)
