"""Deterministic confidence (SPEC §11.4). The LLM never sets it.

Start at High and cap the level for each concern found; every cap adds a reason. Positive
reasons (large samples, significant change) are listed first so the summary reads like
"Medium — large samples and a significant change, but duplicate records ... (removed)".
"""

from __future__ import annotations

from typing import Literal

from paysight.analysis.quality import QualityReport
from paysight.config import AnalysisSettings
from paysight.orchestrator.models import Confidence, Evidence

Level = Literal["High", "Medium", "Low"]
_ORDER = {"High": 2, "Medium": 1, "Low": 0}


def format_p(p: float | None) -> str:
    """'p < 0.001' for tiny values (a z of 60 underflows to 0.0), otherwise 'p=0.0123'."""
    if p is None:
        return "p not computed"
    return "p < 0.001" if p < 0.001 else f"p={p:.3g}"


def score_confidence(
    *,
    evidence: list[Evidence],
    dq: QualityReport | None,
    relevant_dq: int,
    unresolved_ambiguities: int,
    removed_claims: int,
    settings: AnalysisSettings,
) -> Confidence:
    """Combine sample size, significance, decomposition stability, DQ, ad-hoc SQL and ambiguity."""
    level: Level = "High"
    good: list[str] = []
    bad: list[str] = []

    def cap(to: Level, reason: str) -> None:
        nonlocal level
        if _ORDER[to] < _ORDER[level]:
            level = to
        bad.append(reason)

    tests = [e for e in evidence if e.kind == "test"]
    if tests:
        k = tests[0].key_numbers
        # Period tests report both windows; segment comparisons have one row per segment.
        segments = "highest_segment" in k
        unit, what = ("segment", "difference") if segments else ("window", "change")
        if segments:
            sizes = [int(r.get("attempts") or 0) for r in tests[0].result]
        else:
            sizes = [k.get("baseline_attempts", 0), k.get("current_attempts", 0)]
        n = min(sizes) if sizes else 0
        if n < settings.min_sample_attempts:
            cap("Low", f"small samples ({n:,} attempts in the smaller {unit})")
        else:
            good.append(f"large samples ({n:,}+ attempts per {unit})")
        if k.get("statistically_significant") and k.get("practically_significant"):
            good.append(
                f"a statistically and practically significant {what} ({format_p(k.get('p_value'))})"
            )
        elif k.get("statistically_significant"):
            cap("Medium", f"the {what} is statistically significant but small in practice")
        else:
            cap("Low", f"the {what} is not statistically significant")
    else:
        sizes = [
            int(r.get("attempts") or 0) for e in evidence if e.kind == "query" for r in e.result
        ]
        if sizes and max(sizes) and max(sizes) < settings.min_sample_attempts:
            cap("Medium", "small sample behind the figure")
        elif evidence:
            good.append("computed directly from validated queries on the governed views")

    for e in evidence:
        if e.kind == "decomposition" and e.key_numbers.get("mix_share_pct") is None:
            cap("Medium", "the change is too small for a stable mix/rate split")
            break

    if dq is not None and relevant_dq:
        worst = dq.max_severity
        titles = "; ".join(f.title for f in dq.findings if f.severity == worst)
        if worst == "high":
            cap("Low", f"serious data-quality issues in scope ({titles})")
        elif worst == "warning":
            handled = all("removed" in f.handled for f in dq.findings if f.severity == worst)
            cap(
                "Medium",
                f"data-quality warnings in scope: {titles}" + (" (removed)" if handled else ""),
            )
    if any(e.ad_hoc for e in evidence):
        cap("Medium", "part of the analysis used ad-hoc SQL outside the semantic layer")
    if unresolved_ambiguities:
        cap("Medium", "the question was ambiguous; defaults were assumed")
    if removed_claims:
        cap("Medium", f"{removed_claims} unverifiable claim(s) were removed from the answer")

    reasons = good + ([f"but {', '.join(bad)}"] if good and bad else bad)
    return Confidence(level=level, reasons=reasons or ["no concerns found"])
