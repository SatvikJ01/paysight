"""Anomaly detection on rate series (SPEC §10.5).

Robust z-scores (median and MAD) on each period's success rate; anomalous points that are
adjacent in time are merged into windows. Robust statistics matter here: a mean/stddev z-score
is dragged by the very outage it is trying to find.

Periods with too few attempts to judge (``den < min_den``, e.g. night hours) are *unknown*,
not normal: they do not end a window. Two anomalous periods are adjacent when only unknown or
absent periods lie between them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from paysight.analysis.stats import robust_z


class AnomalyWindow(BaseModel):
    """A run of adjacent anomalous periods."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime  # start of the last anomalous period
    end_exclusive: datetime  # end + one step
    periods: int
    direction: Literal["drop", "spike"]
    baseline_rate: float  # median rate over the series
    worst_rate: float
    worst_z: float
    attempts: float
    excess_failures: float  # attempts × (baseline − rate), summed; negative for spikes


def detect_anomalies(
    series: pd.DataFrame,
    *,
    step: timedelta,
    z_threshold: float,
    min_den: float = 1.0,
) -> list[AnomalyWindow]:
    """Find anomaly windows in a ``period``/``num``/``den`` series.

    Args:
        series: one row per period with ``period`` (datetime), ``num`` and ``den``.
        step: the series granularity, used for ``end_exclusive``.
        z_threshold: absolute robust z at or above which a point is anomalous.
        min_den: periods with fewer attempts are ignored (too noisy to judge).
    """
    s = series.loc[series["den"] >= min_den].sort_values("period").reset_index(drop=True)
    if len(s) < 3:
        return []
    rate = (s["num"] / s["den"]).to_numpy(dtype=float)
    z = robust_z(rate)
    baseline = float(np.median(rate))
    flagged = np.abs(z) >= z_threshold

    windows: list[AnomalyWindow] = []
    i = 0
    periods = pd.to_datetime(s["period"]).dt.to_pydatetime()
    while i < len(s):
        if not flagged[i]:
            i += 1
            continue
        direction = "drop" if z[i] < 0 else "spike"
        j = i
        # s holds judgeable periods only, so consecutive rows have only unknown periods between.
        while j + 1 < len(s) and flagged[j + 1] and (z[j + 1] < 0) == (direction == "drop"):
            j += 1
        idx = slice(i, j + 1)
        worst = int(np.argmax(np.abs(z[idx]))) + i
        dens = s["den"].to_numpy(dtype=float)[idx]
        windows.append(
            AnomalyWindow(
                start=periods[i],
                end=periods[j],
                end_exclusive=periods[j] + step,
                periods=j - i + 1,
                direction=direction,
                baseline_rate=baseline,
                worst_rate=float(rate[worst]),
                worst_z=float(z[worst]),
                attempts=float(dens.sum()),
                excess_failures=float((dens * (baseline - rate[idx])).sum()),
            )
        )
        i = j + 1
    return windows
