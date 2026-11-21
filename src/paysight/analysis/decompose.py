"""Kitagawa symmetric decomposition of a rate change into mix and rate effects (SPEC §10.2).

For segment *i* with denominator share ``s`` and rate ``r`` in windows 0 and 1::

    mix_i  = (s_i1 − s_i0) · (r_i0 + r_i1) / 2
    rate_i = (r_i1 − r_i0) · (s_i0 + s_i1) / 2
    Σ mix_i + Σ rate_i = R_1 − R_0          (exact)

Edge cases:

* A segment present in only one window has no rate in the other. Its missing rate is imputed
  from the window where it exists, so its whole contribution is ``mix`` (``s·r``). The identity
  still holds exactly, because ``s = 0`` in the missing window.
* Segments with a zero denominator in both windows are dropped.
* When ``|ΔR|`` is tiny, ``mix_share`` is unstable, so it is reported as ``None`` and only the
  absolute contributions are given.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

SUM_TOLERANCE = 1e-9


class SegmentContribution(BaseModel):
    """One segment's share, rate and contributions in a decomposition."""

    model_config = ConfigDict(frozen=True)

    segment: str
    num0: float
    den0: float
    num1: float
    den1: float
    share0: float
    share1: float
    rate0: float | None
    rate1: float | None
    mix: float
    rate: float

    @property
    def total(self) -> float:
        """Mix plus rate contribution of this segment."""
        return self.mix + self.rate


class Decomposition(BaseModel):
    """Result of :func:`decompose_rate_change`."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    rate0: float
    rate1: float
    delta: float
    mix_effect: float
    rate_effect: float
    mix_share: float | None
    rate_share: float | None
    shares_reliable: bool
    sum_check_error: float
    segments: list[SegmentContribution]


def decompose_rate_change(
    data: pd.DataFrame, *, dimension: str, min_delta_for_shares: float = 0.001
) -> Decomposition:
    """Decompose ``R_1 − R_0`` by the segments in ``data``.

    Args:
        data: one row per segment with columns ``segment``, ``num0``, ``den0``, ``num1``,
            ``den1`` (numerator and denominator of the rate in each window).
        dimension: the name of the segmenting dimension, carried into the result.
        min_delta_for_shares: below this ``|ΔR|`` (as a fraction, 0.001 = 0.1pp) the mix and
            rate *shares* are not reported.

    Raises:
        ValueError: if either window has a zero total denominator, or the parts fail to sum to
            the total within ``1e-9``.
    """
    df = data[["segment", "num0", "den0", "num1", "den1"]].copy()
    df = df.loc[(df["den0"] > 0) | (df["den1"] > 0)].reset_index(drop=True)
    total0, total1 = float(df["den0"].sum()), float(df["den1"].sum())
    if total0 <= 0 or total1 <= 0:
        raise ValueError("both windows need a positive total denominator")

    s0 = df["den0"].to_numpy(dtype=float) / total0
    s1 = df["den1"].to_numpy(dtype=float) / total1
    with np.errstate(invalid="ignore", divide="ignore"):
        r0_raw = df["num0"].to_numpy(dtype=float) / df["den0"].to_numpy(dtype=float)
        r1_raw = df["num1"].to_numpy(dtype=float) / df["den1"].to_numpy(dtype=float)
    r0 = np.where(df["den0"] > 0, r0_raw, r1_raw)
    r1 = np.where(df["den1"] > 0, r1_raw, r0_raw)

    mix = (s1 - s0) * (r0 + r1) / 2.0
    rate = (r1 - r0) * (s0 + s1) / 2.0

    big_r0 = float(df["num0"].sum()) / total0
    big_r1 = float(df["num1"].sum()) / total1
    delta = big_r1 - big_r0
    error = abs(float(mix.sum() + rate.sum()) - delta)
    if error > SUM_TOLERANCE:
        raise ValueError(f"decomposition does not sum to the total (error {error:.3e})")

    reliable = abs(delta) >= min_delta_for_shares
    segments = [
        SegmentContribution(
            segment=str(row.segment),
            num0=float(row.num0),
            den0=float(row.den0),
            num1=float(row.num1),
            den1=float(row.den1),
            share0=float(s0[i]),
            share1=float(s1[i]),
            rate0=float(r0_raw[i]) if row.den0 > 0 else None,
            rate1=float(r1_raw[i]) if row.den1 > 0 else None,
            mix=float(mix[i]),
            rate=float(rate[i]),
        )
        for i, row in enumerate(df.itertuples(index=False))
    ]
    segments.sort(key=lambda s: abs(s.total), reverse=True)
    return Decomposition(
        dimension=dimension,
        rate0=big_r0,
        rate1=big_r1,
        delta=delta,
        mix_effect=float(mix.sum()),
        rate_effect=float(rate.sum()),
        mix_share=float(mix.sum() / delta) if reliable else None,
        rate_share=float(rate.sum() / delta) if reliable else None,
        shares_reliable=reliable,
        sum_check_error=error,
        segments=segments,
    )
