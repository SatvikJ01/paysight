"""Paysight Streamlit entry point: page navigation (SPEC §13).

Run with ``uv run streamlit run app/streamlit_app.py``. With no API key the app runs in replay
mode from the committed cassettes, so every demo question works offline.
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Paysight", page_icon="📊", layout="wide")
pages = [
    st.Page("views/ask.py", title="Ask", icon="💬", default=True),
    st.Page("views/data_health.py", title="Data health", icon="🩺"),
    st.Page("views/eval_scorecard.py", title="Eval scorecard", icon="🎯"),
    st.Page("views/semantic_catalog.py", title="Semantic catalog", icon="📚"),
]
st.navigation(pages).run()
