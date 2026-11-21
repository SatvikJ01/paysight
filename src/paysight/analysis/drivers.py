"""Driver scanning and ranking (SPEC §10.3).

For each candidate dimension the rate change is decomposed (Kitagawa) and every segment's own
rate change is tested with a two-proportion z-test. Benjamini–Hochberg correction is applied
across *all* segments scanned, because scanning many segments makes some look significant by
chance.

**Explanatory strength is disproportionality, not size.** If a change were uniform, every
segment would contribute in proportion to its volume share, and a big segment would "explain"
most of it just by being big. A dimension's strength is therefore the total-variation distance
between its attribution and its volume shares::

    strength = ½ · Σ_i |a_i − s̄_i · ΔR| / |ΔR|

where ``a_i`` is segment *i*'s attributed contribution (centred mix + rate) and ``s̄_i`` its
average share. The top segment is the most disproportionate one, e.g. "18% of volume but 65% of
the change". See DECISIONS.md D14.

**Rate-effect localization.** A segment's rate can fall only because its own mix changed. For
example, NovaMart's rate falls because its sale is EMI-heavy. :func:`localize_rate_effect`
decomposes by ``payment_method × dimension``, so method-mix changes inside a segment count as
mix, and what remains as rate is a genuine within-method rate change.

Several dimensions can legitimately explain the same change: a merchant group's sale shows up in
``merchant_group``, ``payment_method`` and ``channel``. The narrative layer tells that as one
story.
"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel, ConfigDict

from paysight.analysis.compare import require_ok
from paysight.analysis.decompose import Decomposition, decompose_rate_change
from paysight.analysis.stats import benjamini_hochberg, two_proportion_z_test
from paysight.config import AnalysisSettings
from paysight.execution.engine import Engine, QueryResult
from paysight.safety.access import RolePolicy
from paysight.semantic.models import Filter, QuerySpec, TimeWindow

ROOT_CAUSE_DIMENSIONS = (
    "payment_method",
    "channel",
    "acquirer_name",
    "issuer_name",
    "merchant_tier",
    "merchant_group",
    "region",
    "category",
)
MISSING = "(missing)"
NO_GROUP = "(no group)"


def segment_counts(
    engine: Engine,
    role: RolePolicy,
    *,
    dimension: str,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter] | None = None,
    metric: str = "attempt_success_rate",
) -> tuple[pd.DataFrame, QueryResult]:
    """Numerator/denominator per segment of ``dimension`` in both windows.

    NULL segment values are labelled ``(missing)`` (or ``(no group)`` for merchant groups),
    never dropped.
    """
    spec = QuerySpec(
        metrics=[metric],
        dimensions=[dimension],
        filters=filters or [],
        time_window=current,
        compare_window=baseline,
    )
    _, result = engine.run_spec(spec, role)
    require_ok(result, f"segment counts by {dimension}")
    frame = result.frame
    label = NO_GROUP if dimension == "merchant_group" else MISSING
    frame = frame.assign(
        segment=frame[dimension].astype(object).where(frame[dimension].notna(), label)
    )
    frame["segment"] = frame["segment"].astype(str)
    wide = frame.pivot_table(
        index="segment",
        columns="comparison_window",
        values=[f"{metric}_num", f"{metric}_den"],
        aggfunc="sum",
        fill_value=0,
    )
    out = pd.DataFrame(
        {
            "segment": wide.index,
            "num0": wide.get((f"{metric}_num", "baseline"), 0),
            "den0": wide.get((f"{metric}_den", "baseline"), 0),
            "num1": wide.get((f"{metric}_num", "current"), 0),
            "den1": wide.get((f"{metric}_den", "current"), 0),
        }
    ).reset_index(drop=True)
    return out.astype({c: float for c in ("num0", "den0", "num1", "den1")}), result


class SegmentDriver(BaseModel):
    """One segment's contribution and significance."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    segment: str
    share0: float
    share1: float
    rate0: float | None
    rate1: float | None
    den0: float
    den1: float
    mix_pp: float  # centred mix contribution
    rate_pp: float
    attributed_pp: float
    share_of_change: float | None
    p_value: float
    q_value: float
    significant: bool


class DimensionDriver(BaseModel):
    """A candidate dimension's explanatory power for the change."""

    model_config = ConfigDict(frozen=True)

    rank: int
    dimension: str
    strength: float
    concentration: float
    mix_pp: float
    rate_pp: float
    mix_share: float | None
    top_segments: list[SegmentDriver]
    reasons: list[str]


class DriverRanking(BaseModel):
    """Ranked dimensions plus the strongest significant rate-effect segments."""

    model_config = ConfigDict(frozen=True)

    delta_pp: float
    alpha: float
    segments_tested: int
    dimensions: list[DimensionDriver]
    top_rate_segments: list[SegmentDriver]

    def dimension(self, name: str) -> DimensionDriver:
        """Look up one dimension's result."""
        return next(d for d in self.dimensions if d.dimension == name)


def _fmt_rate(r: float | None) -> str:
    return "n/a" if r is None else f"{r * 100:.1f}%"


def rank_drivers(
    decompositions: list[Decomposition], *, settings: AnalysisSettings
) -> DriverRanking:
    """Rank dimensions by explanatory strength, with BH-corrected segment tests.

    All decompositions must describe the same overall change (same windows and scope).
    """
    if not decompositions:
        raise ValueError("need at least one decomposition")
    delta = decompositions[0].delta
    min_delta = settings.min_delta_for_shares_pp / 100

    flat = [(d, s) for d in decompositions for s in d.segments]
    p_values = [two_proportion_z_test(s.num0, s.den0, s.num1, s.den1)[1] for _, s in flat]
    q_values = benjamini_hochberg(p_values)
    by_dim: dict[str, list[SegmentDriver]] = {}
    for (d, s), p, q in zip(flat, p_values, q_values, strict=True):
        by_dim.setdefault(d.dimension, []).append(
            SegmentDriver(
                dimension=d.dimension,
                segment=s.segment,
                share0=s.share0,
                share1=s.share1,
                rate0=s.rate0,
                rate1=s.rate1,
                den0=s.den0,
                den1=s.den1,
                mix_pp=s.mix_centered * 100,
                rate_pp=s.rate * 100,
                attributed_pp=s.attributed * 100,
                share_of_change=s.attributed / delta if abs(delta) >= min_delta else None,
                p_value=float(p),
                q_value=float(q),
                significant=bool(q < settings.alpha),
            )
        )

    scored = []
    for d in decompositions:
        segs = by_dim.get(d.dimension, [])
        if abs(delta) < min_delta or not segs:
            excess = {s.segment: 0.0 for s in segs}
        else:
            excess = {
                s.segment: s.attributed_pp - (s.share0 + s.share1) / 2 * delta * 100 for s in segs
            }
        strength = 0.5 * sum(abs(v) for v in excess.values()) / abs(delta * 100) if delta else 0.0
        segs = sorted(segs, key=lambda s: abs(excess[s.segment]), reverse=True)
        abs_excess = sum(abs(v) for v in excess.values()) or 1.0
        top = segs[0] if segs else None
        concentration = abs(excess[top.segment]) / abs_excess if top else 0.0
        reasons = []
        if top is not None and abs(delta) >= min_delta:
            volume = (top.share0 + top.share1) / 2
            kind = "mix (share change)" if abs(top.mix_pp) >= abs(top.rate_pp) else "rate change"
            reasons.append(
                f"'{top.segment}' is {volume:.0%} of volume but {top.attributed_pp:+.2f}pp "
                f"({top.attributed_pp / (delta * 100):.0%}) of the {delta * 100:+.2f}pp change, "
                f"mostly {kind}: share {top.share0:.1%} → {top.share1:.1%}, rate "
                f"{_fmt_rate(top.rate0)} → {_fmt_rate(top.rate1)}."
            )
        if d.mix_share is not None:
            reasons.append(
                f"Across {d.dimension}: mix {d.mix_effect * 100:+.2f}pp, rate "
                f"{d.rate_effect * 100:+.2f}pp ({d.mix_share:.0%} mix)."
            )
        n_sig = sum(s.significant for s in segs)
        reasons.append(f"{n_sig} of {len(segs)} segments changed rate significantly after BH.")
        scored.append((strength, concentration, d, segs[:3], reasons))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    dimensions = [
        DimensionDriver(
            rank=i + 1,
            dimension=d.dimension,
            strength=strength,
            concentration=concentration,
            mix_pp=d.mix_effect * 100,
            rate_pp=d.rate_effect * 100,
            mix_share=d.mix_share,
            top_segments=top,
            reasons=reasons,
        )
        for i, (strength, concentration, d, top, reasons) in enumerate(scored)
    ]

    return DriverRanking(
        delta_pp=delta * 100,
        alpha=settings.alpha,
        segments_tested=len(flat),
        dimensions=dimensions,
        top_rate_segments=_top_rate_segments(by_dim, delta),
    )


def _top_rate_segments(by_dim: dict[str, list[SegmentDriver]], delta: float) -> list[SegmentDriver]:
    """Significant segments whose rate moved in the same direction as the total, largest first."""
    same = [
        s
        for segs in by_dim.values()
        for s in segs
        if s.significant and s.rate_pp * delta > 0 and MISSING not in s.segment
    ]
    return sorted(same, key=lambda s: abs(s.rate_pp), reverse=True)[:5]


def scan_drivers(
    engine: Engine,
    role: RolePolicy,
    *,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter] | None = None,
    dimensions: tuple[str, ...] = ROOT_CAUSE_DIMENSIONS,
    settings: AnalysisSettings,
    metric: str = "attempt_success_rate",
) -> tuple[DriverRanking, list[Decomposition], list[QueryResult]]:
    """Decompose the change in ``metric`` across ``dimensions`` and rank them."""
    decompositions: list[Decomposition] = []
    results: list[QueryResult] = []
    for dim in dimensions:
        counts, result = segment_counts(
            engine,
            role,
            dimension=dim,
            current=current,
            baseline=baseline,
            filters=filters,
            metric=metric,
        )
        results.append(result)
        decompositions.append(
            decompose_rate_change(
                counts, dimension=dim, min_delta_for_shares=settings.min_delta_for_shares_pp / 100
            )
        )
    return rank_drivers(decompositions, settings=settings), decompositions, results


def localize_rate_effect(
    engine: Engine,
    role: RolePolicy,
    *,
    current: TimeWindow,
    baseline: TimeWindow,
    filters: list[Filter] | None = None,
    base_dimension: str = "payment_method",
    dimensions: tuple[str, ...] = ("issuer_name", "acquirer_name", "merchant_group", "region"),
    settings: AnalysisSettings,
) -> tuple[DriverRanking, list[QueryResult]]:
    """Find genuine rate changes by decomposing over ``base_dimension × dimension`` segments.

    Segments are labelled ``"<dimension value> | <base value>"`` (e.g. ``Harbor Bank |
    DEBIT_CARD``). The returned ranking's ``top_rate_segments`` are the localized sources.
    """
    decompositions: list[Decomposition] = []
    results: list[QueryResult] = []
    for dim in dimensions:
        spec = QuerySpec(
            metrics=["attempt_success_rate"],
            dimensions=[dim, base_dimension],
            filters=filters or [],
            time_window=current,
            compare_window=baseline,
        )
        _, result = engine.run_spec(spec, role)
        require_ok(result, f"rate localization by {dim} x {base_dimension}")
        results.append(result)
        frame = result.frame.copy()
        label = NO_GROUP if dim == "merchant_group" else MISSING
        frame[dim] = frame[dim].astype(object).where(frame[dim].notna(), label)
        frame["segment"] = frame[dim].astype(str) + " | " + frame[base_dimension].astype(str)
        rows = []
        for seg, g in frame.groupby("segment", sort=True):
            b = g[g["comparison_window"] == "baseline"]
            c = g[g["comparison_window"] == "current"]
            rows.append(
                {
                    "segment": seg,
                    "num0": float(b["attempt_success_rate_num"].sum()),
                    "den0": float(b["attempt_success_rate_den"].sum()),
                    "num1": float(c["attempt_success_rate_num"].sum()),
                    "den1": float(c["attempt_success_rate_den"].sum()),
                }
            )
        decompositions.append(
            decompose_rate_change(
                pd.DataFrame(rows),
                dimension=f"{dim} x {base_dimension}",
                min_delta_for_shares=settings.min_delta_for_shares_pp / 100,
            )
        )
    return rank_drivers(decompositions, settings=settings), results
