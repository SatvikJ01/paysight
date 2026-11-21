"""Statistics helpers against known values (SPEC §16.1)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from paysight.analysis.compare import compare_rates
from paysight.analysis.stats import (
    benjamini_hochberg,
    robust_z,
    two_proportion_z_test,
    wilson_interval,
)
from paysight.config import get_settings


@pytest.mark.parametrize(
    ("x", "n", "lo", "hi"),
    [
        (50, 100, 0.4038, 0.5962),
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
        (81, 263, 0.2553, 0.3662),
    ],
)
def test_wilson_known_values(x: int, n: int, lo: float, hi: float) -> None:
    got = wilson_interval(x, n)
    assert got[0] == pytest.approx(lo, abs=5e-4)
    assert got[1] == pytest.approx(hi, abs=5e-4)


def test_wilson_empty_sample() -> None:
    assert all(math.isnan(v) for v in wilson_interval(0, 0))


def test_two_proportion_z_known_value() -> None:
    z, p = two_proportion_z_test(50, 100, 60, 100)
    assert z == pytest.approx(1.4213, abs=1e-4)
    assert p == pytest.approx(0.1552, abs=1e-4)


def test_two_proportion_degenerate() -> None:
    assert two_proportion_z_test(0, 0, 5, 10) == (0.0, 1.0)
    assert two_proportion_z_test(10, 10, 10, 10) == (0.0, 1.0)


def test_benjamini_hochberg_known_values() -> None:
    q = benjamini_hochberg([0.01, 0.04, 0.03, 0.005])
    assert q == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert benjamini_hochberg([0.9, 0.95]) == pytest.approx([0.95, 0.95])
    assert len(benjamini_hochberg([])) == 0


def test_benjamini_hochberg_is_monotone_and_bounded() -> None:
    rng = np.random.default_rng(0)
    p = rng.random(200)
    q = benjamini_hochberg(p)
    order = np.argsort(p)
    assert np.all(np.diff(q[order]) >= -1e-15)
    assert np.all(q >= p - 1e-12) and np.all(q <= 1)  # p·m/m can differ from p by 1 ulp


def test_robust_z_ignores_the_outlier_it_finds() -> None:
    values = [0.90, 0.91, 0.90, 0.89, 0.90, 0.60]
    z = robust_z(values)
    assert z[-1] < -10
    assert np.all(np.abs(z[:-1]) < 2)


def test_robust_z_with_zero_mad_falls_back() -> None:
    z = robust_z([1.0, 1.0, 1.0, 1.0, 5.0])
    assert z[-1] > 0 and np.all(z[:-1] < 0.0 + 1e-12)
    assert np.all(robust_z([2.0, 2.0, 2.0]) == 0)


def test_practical_significance_needs_size_and_sample() -> None:
    a = get_settings().analysis
    big = compare_rates(9000, 10000, 8900, 10000, settings=a)
    assert big.practically_significant and big.statistically_significant
    assert big.delta_pp == pytest.approx(-1.0)
    small_sample = compare_rates(90, 100, 80, 100, settings=a)
    assert not small_sample.practically_significant
    tiny_delta = compare_rates(90000, 100000, 89800, 100000, settings=a)
    assert not tiny_delta.practically_significant
