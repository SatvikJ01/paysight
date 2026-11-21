"""Anomaly detection on rate series (SPEC §10.5).

Robust z-scores (median and MAD) on each period's success rate; anomalous points that are
adjacent in time are merged into windows. Robust statistics matter here: a mean/stddev z-score
is dragged by the very outage it is trying to find.

Periods with too few attempts to judge (``den < min_den``, e.g. night hours) are *unknown*,
not normal: they do not end a window. Two anomalous periods are adjacent when only unknown or
absent periods lie between them.

A point must also be practically far from the baseline (``min_delta``), as with
practical significance elsewhere: a −1.3pp day can pass a robust-z test on a very stable
series without being worth calling an anomaly (D37).

``outage_windows`` judges each period against a *reference* rate from outside the window
instead. A long outage inside a short window pulls the window's own median down and inflates
its MAD (S1 covers about a third of the hours around it), so the robust test can miss it.
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
    min_delta: float = 0.0,
) -> list[AnomalyWindow]:
    """Find anomaly windows in a ``period``/``num``/``den`` series.

    Args:
        series: one row per period with ``period`` (datetime), ``num`` and ``den``.
        step: the series granularity, used for ``end_exclusive``.
        z_threshold: absolute robust z at or above which a point is anomalous.
        min_den: periods with fewer attempts are ignored (too noisy to judge).
        min_delta: a point must also differ from the median rate by at least this much
            (as a fraction, e.g. 0.02 for 2pp).
    """
    s = series.loc[series["den"] >= min_den].sort_values("period").reset_index(drop=True)
    if len(s) < 3:
        return []
    rate = (s["num"] / s["den"]).to_numpy(dtype=float)
    z = robust_z(rate)
    baseline = float(np.median(rate))
    flagged = (np.abs(z) >= z_threshold) & (np.abs(rate - baseline) >= min_delta)

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


def outage_windows(
    series: pd.DataFrame,
    *,
    reference_rate: float,
    step: timedelta,
    z_threshold: float,
    min_delta: float,
    min_den: float = 1.0,
    max_gap: int = 1,
) -> list[AnomalyWindow]:
    """Runs of periods significantly and practically below a reference rate.

    A period is below when a one-sided binomial z against ``reference_rate`` is at most
    ``-z_threshold`` and it is at least ``min_delta`` below it. Up to ``max_gap`` judgeable
    periods that are not below may sit inside a run (a single normal hour does not end an
    outage); periods with ``den < min_den`` are unknown and never end a run.
    """
    s = series.loc[series["den"] >= min_den].sort_values("period").reset_index(drop=True)
    if s.empty or not 0 < reference_rate < 1:
        return []
    rate = (s["num"] / s["den"]).to_numpy(dtype=float)
    den = s["den"].to_numpy(dtype=float)
    z = (rate - reference_rate) / np.sqrt(reference_rate * (1 - reference_rate) / den)
    below = (z <= -z_threshold) & (reference_rate - rate >= min_delta)
    periods = pd.to_datetime(s["period"]).dt.to_pydatetime()
    windows: list[AnomalyWindow] = []
    i = 0
    while i < len(s):
        if not below[i]:
            i += 1
            continue
        j, gap = i, 0
        for k in range(i + 1, len(s)):
            if below[k]:
                j, gap = k, 0
            else:
                gap += 1
                if gap > max_gap:
                    break
        idx = slice(i, j + 1)
        worst = int(np.argmin(rate[idx])) + i
        windows.append(
            AnomalyWindow(
                start=periods[i],
                end=periods[j],
                end_exclusive=periods[j] + step,
                periods=j - i + 1,
                direction="drop",
                baseline_rate=reference_rate,
                worst_rate=float(rate[worst]),
                worst_z=float(z[worst]),
                attempts=float(den[idx].sum()),
                excess_failures=float((den[idx] * (reference_rate - rate[idx])).sum()),
            )
        )
        i = j + 1
    return windows
