"""Plotly charts chosen by evidence type (SPEC §13.3). Every chart has a title and axis units."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from paysight.orchestrator.models import AskResult, Evidence

GREEN, RED, GREY, AMBER = "#1B6B45", "#C0392B", "#95A5A6", "#D68910"
LAYOUT: dict[str, Any] = {
    "template": "plotly_white",
    "margin": {"l": 60, "r": 30, "t": 60, "b": 50},
    "height": 420,
    "font": {"size": 13},
}


def _rows(ev: Evidence) -> pd.DataFrame:
    return pd.DataFrame(ev.chart_data or ev.result)


def waterfall(ev: Evidence) -> go.Figure | None:
    """Start SR -> mix effect by top segments -> rate effect by top segments -> end SR."""
    rows, k = _rows(ev), ev.key_numbers
    if rows.empty or "mix_pp" not in rows or k.get("rate0_pct") is None:
        return None

    def top(col: str) -> list[tuple[str, float]]:
        ordered = rows.reindex(rows[col].abs().sort_values(ascending=False).index)
        picked = [
            (f"{r.segment} ({'mix' if col == 'mix_pp' else 'rate'})", float(r[col]))
            for _, r in ordered.head(3).iterrows()
        ]
        rest = float(rows[col].sum()) - sum(v for _, v in picked)
        if abs(rest) >= 0.005:
            picked.append((f"other ({'mix' if col == 'mix_pp' else 'rate'})", rest))
        return picked

    steps = top("mix_pp") + top("rate_pp")
    labels = ["Start", *(s for s, _ in steps), "End"]
    values = [k["rate0_pct"], *(v for _, v in steps), k["rate1_pct"]]
    measures = ["absolute", *(["relative"] * len(steps)), "total"]
    fig = go.Figure(
        go.Waterfall(
            x=labels,
            y=values,
            measure=measures,
            text=[
                f"{v:+.2f}" if m == "relative" else f"{v:.2f}%"
                for v, m in zip(values, measures, strict=True)
            ],
            textposition="outside",
            increasing={"marker": {"color": GREEN}},
            decreasing={"marker": {"color": RED}},
            totals={"marker": {"color": GREY}},
            connector={"line": {"color": "#BBBBBB"}},
        )
    )
    low = min(k["rate0_pct"], k["rate1_pct"]) - abs(k.get("delta_pp") or 1) * 1.5
    high = max(k["rate0_pct"], k["rate1_pct"]) + abs(k.get("delta_pp") or 1) * 0.8
    fig.update_layout(
        **LAYOUT,
        title=f"Mix vs rate: {ev.title.split(' by ')[-1]} "
        f"({k.get('mix_share_pct') or '–'}% of the change is mix)",
        yaxis={"title": "Attempt success rate (%)", "range": [low, high]},
        xaxis={"title": "Contribution (pp)"},
        showlegend=False,
    )
    return fig


def contribution_bar(ev: Evidence) -> go.Figure | None:
    """Driver strength by dimension, or rate-effect contribution by segment."""
    rows = _rows(ev)
    if rows.empty:
        return None
    if "strength" in rows:
        rows = rows.sort_values("strength")
        fig = go.Figure(
            go.Bar(
                x=rows["strength"],
                y=rows["dimension"],
                orientation="h",
                marker_color=GREEN,
                text=rows.get("top_segment"),
                textposition="auto",
            )
        )
        fig.update_layout(
            **LAYOUT,
            title=ev.title,
            xaxis={"title": "Explanatory strength (0–1)"},
            yaxis={"title": "Dimension"},
        )
        return fig
    if "rate_pp" in rows:
        rows = rows.sort_values("rate_pp", ascending=False)
        fig = go.Figure(
            go.Bar(x=rows["rate_pp"], y=rows["segment"], orientation="h", marker_color=RED)
        )
        fig.update_layout(
            **LAYOUT,
            title=ev.title,
            xaxis={"title": "Rate-effect contribution (pp)"},
            yaxis={"title": "Segment"},
        )
        return fig
    return None


def _windows(text: str) -> list[tuple[str, str]]:
    return re.findall(r"(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d) \(drop", text or "")


def daily_line(ev: Evidence) -> go.Figure | None:
    """Success rate over time with anomaly windows shaded."""
    rows, k = _rows(ev), ev.key_numbers
    if rows.empty or "rate_pct" not in rows:
        return None
    xcol = "date" if "date" in rows else "hour"
    fig = go.Figure(
        go.Scatter(
            x=rows[xcol],
            y=rows["rate_pct"],
            mode="lines+markers",
            line={"color": GREEN},
            name="Success rate",
        )
    )
    shaded = [(a, f"{b} 23:59") for a, b in _windows(k.get("anomaly_windows", ""))]
    if k.get("outage_start"):
        shaded.append((k["outage_start"], k["outage_end"]))
    for start, end in shaded:
        fig.add_vrect(
            x0=start,
            x1=end,
            fillcolor=RED,
            opacity=0.12,
            line_width=0,
            annotation_text="anomaly",
            annotation_position="top left",
        )
    fig.update_layout(
        **LAYOUT,
        title=ev.title,
        yaxis={"title": "Attempt success rate (%)"},
        xaxis={"title": "Date" if xcol == "date" else "Hour (IST)"},
    )
    return fig


def heatmap(ev: Evidence) -> go.Figure | None:
    """Hour x date success-rate heatmap."""
    rows = _rows(ev)
    if rows.empty or "hour" not in rows:
        return None
    grid = rows.pivot_table(index="hour", columns="date", values="rate_pct")
    fig = go.Figure(
        go.Heatmap(
            z=grid.values,
            x=grid.columns,
            y=grid.index,
            colorscale="RdYlGn",
            colorbar={"title": "SR (%)"},
        )
    )
    fig.update_layout(
        **LAYOUT,
        title=ev.title,
        xaxis={"title": "Date"},
        yaxis={"title": "Hour of day (IST)", "dtick": 2},
    )
    return fig


def failure_codes(ev: Evidence) -> go.Figure | None:
    """Failure incidence per 100 attempts, baseline vs current, for the codes that moved most."""
    rows = _rows(ev).head(8)
    if rows.empty or "per_100_attempts_current" not in rows:
        return None
    fig = go.Figure(
        [
            go.Bar(
                name="Baseline",
                x=rows["failure_code"],
                y=rows["per_100_attempts_baseline"],
                marker_color=GREY,
            ),
            go.Bar(
                name="Current",
                x=rows["failure_code"],
                y=rows["per_100_attempts_current"],
                marker_color=RED,
            ),
        ]
    )
    fig.update_layout(
        **LAYOUT,
        title=ev.title,
        barmode="group",
        yaxis={"title": "Failures per 100 attempts"},
        xaxis={"title": "Failure code"},
    )
    return fig


def reconciliation(ev: Evidence) -> go.Figure | None:
    """Explained vs unexplained components of the GMV-to-settlement gap (INR)."""
    k = ev.key_numbers
    rows = _rows(ev)
    if rows.empty or "txn_gmv_inr" not in k:
        return None
    comps = rows[~rows["component"].str.startswith("Batch ")]
    labels = ["Transaction GMV", *comps["component"], "Settled net"]
    values = [k["txn_gmv_inr"], *comps["amount_inr"], k["settled_net_inr"]]
    colors = [GREY, *[(AMBER if kind == "unexplained" else GREEN) for kind in comps["kind"]], GREY]
    fig = go.Figure(
        go.Bar(
            x=labels,
            y=values,
            marker_color=colors,
            text=[f"₹{v:,.0f}" for v in values],
            textposition="outside",
        )
    )
    fig.update_layout(
        **LAYOUT,
        title=f"{ev.title} (amber = unexplained)",
        yaxis={"title": "Amount (INR)"},
        xaxis={"title": "Component"},
    )
    return fig


def bar(ev: Evidence) -> go.Figure | None:
    """Bar chart of ``chart_x`` against ``chart_y``; without them, the first text column
    against the first numeric one. Text may be object or string dtype (pandas 3)."""
    rows = _rows(ev)
    if rows.empty:
        return None
    numeric = {c for c in rows.columns if pd.api.types.is_numeric_dtype(rows[c])}
    text = [c for c in rows.columns if c not in numeric and not pd.api.types.is_bool_dtype(rows[c])]
    nums = [c for c in rows.columns if c in numeric and c != "rank"]
    x = ev.chart_x if ev.chart_x in rows else (text[0] if text else None)
    y = ev.chart_y if ev.chart_y in rows else (nums[0] if nums else None)
    if x is None or y is None:
        return None
    data = rows.head(15)
    fig = go.Figure(go.Bar(x=data[x].astype(str).tolist(), y=data[y], marker_color=GREEN))
    yaxis: dict[str, Any] = {"title": y.replace("_", " ")}
    if y.endswith("_pct"):
        yaxis |= {"title": y.removesuffix("_pct").replace("_", " ") + " (%)", "ticksuffix": "%"}
    fig.update_layout(**LAYOUT, title=ev.title, xaxis={"title": x.replace("_", " ")}, yaxis=yaxis)
    return fig


CHARTS = {
    "waterfall": waterfall,
    "contribution_bar": contribution_bar,
    "daily_line": daily_line,
    "heatmap": heatmap,
    "failure_codes": failure_codes,
    "reconciliation": reconciliation,
    "bar": bar,
}


def figure_for(ev: Evidence) -> go.Figure | None:
    """The chart for one evidence item, or ``None`` if it has nothing to plot."""
    fn = CHARTS.get(ev.chart or "")
    return fn(ev) if fn else None


def main_chart_evidence(result: AskResult) -> Evidence | None:
    """The one chart worth showing under the headline of a root-cause or comparison answer:
    the mix-vs-rate waterfall if there is one, otherwise the first chartable evidence."""
    if result.intent is None or result.intent.question_type not in ("root_cause", "comparison"):
        return None
    waterfalls = [e for e in result.evidence if e.chart == "waterfall"]
    candidates = waterfalls or [e for e in result.evidence if e.chart and e.chart != "table"]
    return next((e for e in candidates if figure_for(e) is not None), None)
