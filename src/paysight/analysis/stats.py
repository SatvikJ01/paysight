"""Small, dependency-light statistics used across the toolkit."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy import stats as sps

Z_95 = 1.959963984540054
_MAD_TO_SIGMA = 0.6744897501960817  # Φ⁻¹(0.75): makes MAD comparable to a standard deviation


def wilson_interval(successes: float, n: float, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (default 95%).

    Returns ``(nan, nan)`` when ``n`` is 0.
    """
    if n <= 0:
        return math.nan, math.nan
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def two_proportion_z_test(x0: float, n0: float, x1: float, n1: float) -> tuple[float, float]:
    """Pooled two-sided z-test for ``x1/n1`` vs ``x0/n0``. Returns ``(z, p_value)``.

    Degenerate inputs (an empty window, or a pooled rate of exactly 0 or 1) return ``(0.0, 1.0)``.
    """
    if n0 <= 0 or n1 <= 0:
        return 0.0, 1.0
    pooled = (x0 + x1) / (n0 + n1)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n0 + 1 / n1))
    if se == 0:
        return 0.0, 1.0
    z = (x1 / n1 - x0 / n0) / se
    return z, float(2 * sps.norm.sf(abs(z)))


def benjamini_hochberg(p_values: Sequence[float]) -> np.ndarray:
    """Benjamini–Hochberg adjusted p-values (q-values), in the input order."""
    p = np.asarray(p_values, dtype=float)
    m = len(p)
    if m == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(adjusted, 1.0)
    return out


def robust_z(values: Sequence[float]) -> np.ndarray:
    """Robust z-scores ``0.6745 · (x − median) / MAD``.

    If the MAD is 0 (more than half the values identical), the mean absolute deviation is used
    instead (scaled by √(π/2) to estimate σ); if that is also 0, all scores are 0.
    """
    x = np.asarray(values, dtype=float)
    if len(x) == 0:
        return x
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    if mad > 0:
        return _MAD_TO_SIGMA * (x - med) / mad
    mean_ad = float(np.mean(np.abs(x - med)))
    if mean_ad > 0:
        return (x - med) / (mean_ad * math.sqrt(math.pi / 2))
    return np.zeros_like(x)
