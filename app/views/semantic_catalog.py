"""Semantic catalog: governed metrics, dimensions, views and role policies (SPEC §13.4)."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from components.state import get_app

app = get_app()
layer = app.layer

st.markdown("# Semantic catalog")
st.caption("The governed vocabulary the LLM sees. Values never enter SQL as text.")
st.markdown("### Metrics")
st.dataframe(
    pd.DataFrame(
        [
            {
                "metric": m.name,
                "description": m.description,
                "kind": m.kind,
                "view": m.view,
                "numerator": m.numerator_sql,
                "denominator": m.denominator_sql or "",
                "format": m.format,
                "synonyms": ", ".join(m.synonyms),
                "caveats": " ".join(m.caveats),
            }
            for m in layer.metrics
        ]
    ),
    hide_index=True,
    use_container_width=True,
)
st.markdown("### Dimensions")
st.dataframe(
    pd.DataFrame(
        [
            {
                "dimension": d.name,
                "description": d.description,
                "sensitivity": d.sensitivity,
                "views": ", ".join(f"{v}.{c}" for v, c in d.sources.items()),
                "operators": " ".join(d.allowed_ops),
                "synonyms": ", ".join(d.synonyms),
            }
            for d in layer.dimensions
        ]
    ),
    hide_index=True,
    use_container_width=True,
)
st.markdown("### Governed views")
st.dataframe(
    pd.DataFrame(
        [
            {
                "view": name,
                "description": v.description,
                "grain": v.grain,
                "time column": v.time_column,
                "pii columns": ", ".join(v.pii_columns),
            }
            for name, v in layer.views.items()
        ]
    ),
    hide_index=True,
    use_container_width=True,
)
st.markdown("### Role policies")
st.dataframe(
    pd.DataFrame(
        [
            {
                "role": p.label,
                "description": p.description,
                "views": ", ".join(p.allowed_views),
                "denied sensitivities": ", ".join(p.denied_sensitivities) or "none",
                "row filters": "; ".join(f"{k} in {v}" for k, v in p.row_filters.items()) or "none",
            }
            for p in app.policies.values()
        ]
    ),
    hide_index=True,
    use_container_width=True,
)
