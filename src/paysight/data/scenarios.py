"""Planted scenarios S1–S8: the answer key the evaluation is scored against.

Each scenario is an isolated, documented function. Parameters live in frozen dataclasses at the
top of each section, so tuning is a one-line, reviewable change (and is recorded in
``docs/DECISIONS.md``).

Failure-injecting scenarios (S1, S3, S4) return a :class:`Hazard`: a row mask plus an extra
failure probability. The outcome model applies hazards *before* the baseline success draw, so an
attempt hit by a hazard fails with the scenario's code, and the success probability of an
affected attempt becomes ``p_base × (1 − hazard_prob)``. See DECISIONS.md, "Extra failure
probability".

The frames passed in use the generator's working columns:

* ``created_at``: ``datetime64``
* ``merchant_group``, ``channel``, ``payment_method``, ``acquirer_id``, ``issuer_id``: strings or
  categoricals
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

CARD_METHODS = ("CREDIT_CARD", "DEBIT_CARD", "CARD_EMI")


@dataclass(frozen=True)
class Hazard:
    """An extra, scenario-specific failure probability applied to a subset of attempts."""

    scenario: str
    mask: np.ndarray
    prob: float
    failure_category: str
    failure_code: str


def _between(ts: pd.Series, start: datetime, end: datetime) -> np.ndarray:
    """Return ``start <= ts < end`` as a numpy bool array."""
    return ((ts >= np.datetime64(start)) & (ts < np.datetime64(end))).to_numpy()


# --------------------------------------------------------------------------------------------
# S1: acquirer outage
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S1Params:
    """Kestrel Bank card timeouts, 17 Sep 10:00 to 18 Sep 22:00."""

    acquirer_id: str = "ACQ_KESTREL"
    start: datetime = datetime(2026, 9, 17, 10, 0)
    end: datetime = datetime(2026, 9, 18, 22, 0)  # exclusive
    methods: tuple[str, ...] = CARD_METHODS
    extra_failure_prob: float = 0.35
    failure_category: str = "technical"
    failure_code: str = "ACQ_TIMEOUT"


S1 = S1Params()


def s1_acquirer_outage(frame: pd.DataFrame, params: S1Params = S1) -> Hazard:
    """S1: card attempts routed through Kestrel during the outage window time out."""
    mask = (
        _between(frame["created_at"], params.start, params.end)
        & (frame["acquirer_id"] == params.acquirer_id).to_numpy()
        & frame["payment_method"].isin(params.methods).to_numpy()
    )
    return Hazard(
        "S1", mask, params.extra_failure_prob, params.failure_category, params.failure_code
    )


# --------------------------------------------------------------------------------------------
# S2: festive mix shift (hero)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S2Params:
    """NovaMart's festive EMI/credit-card sale, 18–31 Oct."""

    merchant_group: str = "NovaMart"
    start: date = date(2026, 10, 18)
    end: date = date(2026, 10, 31)  # inclusive
    target_share: float = 0.30  # NovaMart share of first attempts on sale days
    method_mix: dict[str, float] = field(
        default_factory=lambda: {
            "CREDIT_CARD": 0.35,
            "CARD_EMI": 0.30,
            "DEBIT_CARD": 0.15,
            "UPI": 0.15,
            "NETBANKING": 0.03,
            "WALLET": 0.02,
        }
    )


S2 = S2Params()


def s2_in_sale(day: date, params: S2Params = S2) -> bool:
    """Return whether ``day`` falls inside the NovaMart sale."""
    return params.start <= day <= params.end


def s2_group_mask(frame: pd.DataFrame, params: S2Params = S2) -> np.ndarray:
    """Rows belonging to the S2 merchant group."""
    return (frame["merchant_group"] == params.merchant_group).to_numpy()


def s2_channel_blended_bonus(
    payment_method: np.ndarray, pos_bonus_by_method: dict[str, float]
) -> np.ndarray:
    """S2: NovaMart's per-method success rates equal everyone else's.

    NovaMart sells online only, so with the plain online rate its volume would also lower each
    method's blended rate (other merchants' POS attempts get a bonus). Instead each NovaMart
    attempt gets the *channel-blended* POS bonus for its method, so its per-method rates match
    the all-merchant rates and the sale is a pure mix shift. See DECISIONS.md.
    """
    return np.array([pos_bonus_by_method[m] for m in payment_method])


def s2_extra_attempts(base_total: int, base_group: int, params: S2Params = S2) -> int:
    """S2: extra NovaMart first attempts needed so the group reaches its target share of the day.

    Solves ``(base_group + x) / (base_total + x) = target_share`` for ``x``.
    """
    share = params.target_share
    extra = (share * base_total - base_group) / (1.0 - share)
    return max(0, round(extra))


# --------------------------------------------------------------------------------------------
# S3: issuer risk declines
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S3Params:
    """Harbor Bank debit cards hit by a new risk rule from 21 Oct 00:00."""

    issuer_id: str = "BNK_HARBOR"
    method: str = "DEBIT_CARD"
    start: datetime = datetime(2026, 10, 21, 0, 0)
    extra_failure_prob: float = 0.14
    failure_category: str = "risk_decline"
    failure_code: str = "ISSUER_RISK_DECLINE"


S3 = S3Params()


def s3_issuer_risk_declines(frame: pd.DataFrame, params: S3Params = S3) -> Hazard:
    """S3: Harbor Bank debit-card attempts get extra risk declines from the start date onward."""
    mask = (
        (frame["created_at"] >= np.datetime64(params.start)).to_numpy()
        & (frame["issuer_id"] == params.issuer_id).to_numpy()
        & (frame["payment_method"] == params.method).to_numpy()
    )
    return Hazard(
        "S3", mask, params.extra_failure_prob, params.failure_category, params.failure_code
    )


# --------------------------------------------------------------------------------------------
# S4: UPI PSP evening degradation
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S4Params:
    """Zephyr UPI timeouts between 19:00 and 22:59 on 1–10 Sep."""

    issuer_id: str = "PSP_ZEPHYR"
    method: str = "UPI"
    first_day: date = date(2026, 9, 1)
    last_day: date = date(2026, 9, 10)  # inclusive
    first_hour: int = 19
    last_hour: int = 22  # inclusive
    extra_failure_prob: float = 0.12
    failure_category: str = "technical"
    failure_code: str = "UPI_PSP_TIMEOUT"


S4 = S4Params()


def s4_psp_evening_degradation(frame: pd.DataFrame, params: S4Params = S4) -> Hazard:
    """S4: Zephyr UPI attempts in the evening hours of the affected days time out."""
    ts = frame["created_at"]
    start = datetime.combine(params.first_day, datetime.min.time())
    end = datetime.combine(params.last_day + timedelta(days=1), datetime.min.time())
    hours = ts.dt.hour.to_numpy()
    mask = (
        _between(ts, start, end)
        & (hours >= params.first_hour)
        & (hours <= params.last_hour)
        & (frame["issuer_id"] == params.issuer_id).to_numpy()
        & (frame["payment_method"] == params.method).to_numpy()
    )
    return Hazard(
        "S4", mask, params.extra_failure_prob, params.failure_category, params.failure_code
    )


def outcome_hazards(frame: pd.DataFrame) -> list[Hazard]:
    """All failure-injecting scenarios, in the order the outcome model applies them."""
    return [
        s1_acquirer_outage(frame),
        s3_issuer_risk_declines(frame),
        s4_psp_evening_degradation(frame),
    ]


# --------------------------------------------------------------------------------------------
# S5: duplicate ingestion
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S5Params:
    """3% of 12 Oct attempts are re-ingested six hours later."""

    day: date = date(2026, 10, 12)
    duplicate_fraction: float = 0.03
    ingestion_delay: timedelta = timedelta(hours=6)


S5 = S5Params()


def s5_duplicate_ingestion(
    attempts: pd.DataFrame, rng: np.random.Generator, params: S5Params = S5
) -> pd.DataFrame:
    """S5: append re-ingested copies (same ``attempt_id``, later ``ingested_at``) of some rows."""
    on_day = (attempts["created_at"].dt.date == params.day).to_numpy()
    candidates = np.flatnonzero(on_day)
    n_dup = round(len(candidates) * params.duplicate_fraction)
    chosen = np.sort(rng.choice(candidates, size=n_dup, replace=False))
    dups = attempts.iloc[chosen].copy()
    dups["ingested_at"] = dups["ingested_at"] + pd.Timedelta(params.ingestion_delay)
    return pd.concat([attempts, dups], ignore_index=True)


# --------------------------------------------------------------------------------------------
# S6: missing issuer IDs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S6Params:
    """8% of Meridian Bank's August card attempts lose their issuer_id."""

    acquirer_id: str = "ACQ_MERIDIAN"
    month_start: date = date(2026, 8, 1)
    month_end: date = date(2026, 8, 31)  # inclusive
    methods: tuple[str, ...] = CARD_METHODS
    null_fraction: float = 0.08


S6 = S6Params()


def s6_missing_issuer_ids(
    attempts: pd.DataFrame, rng: np.random.Generator, params: S6Params = S6
) -> pd.DataFrame:
    """S6: null out ``issuer_id`` on a random subset of Meridian's August card attempts."""
    days = attempts["created_at"].dt.date
    scope = (
        ((days >= params.month_start) & (days <= params.month_end)).to_numpy()
        & (attempts["acquirer_id"] == params.acquirer_id).to_numpy()
        & attempts["payment_method"].isin(params.methods).to_numpy()
    )
    hit = scope & (rng.random(len(attempts)) < params.null_fraction)
    out = attempts.copy()
    out.loc[hit, "issuer_id"] = None
    return out


# --------------------------------------------------------------------------------------------
# S7: source disagreement (settlements)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S7Params:
    """A missing-then-combined NovaMart batch, and an unexplained Orbit Electronics shortfall."""

    combined_group: str = "NovaMart"
    missing_txn_date: date = date(2026, 10, 25)
    combined_covered_to: date = date(2026, 10, 26)
    combined_settlement_date: date = date(2026, 10, 28)
    shortfall_merchant: str = "Orbit Electronics"
    shortfall_txn_date: date = date(2026, 10, 14)
    shortfall_fraction: float = 0.018


S7 = S7Params()


def s7_settlement_disagreements(
    batches: pd.DataFrame,
    group_merchant_ids: list[str],
    shortfall_merchant_id: str,
    params: S7Params = S7,
) -> pd.DataFrame:
    """S7: merge NovaMart's 25 Oct batch into a late combined batch; short Orbit's 14 Oct gross.

    ``batches`` has one row per (merchant, transaction date) with integer paise columns
    ``gross``, ``refunds``, ``fees`` and ``txn_count`` plus ``covered_from``, ``covered_to`` and
    ``settlement_date``. Fees and refunds of the shorted batch are left as computed: the shortfall
    is deliberately *not* explained by anything in the data.
    """
    out = batches.copy()

    # Missing + combined batch: for each NovaMart merchant, fold the 25 Oct batch into 26 Oct
    # and settle the combined batch on 28 Oct.
    sum_cols = ["gross", "refunds", "fees", "txn_count"]
    for mid in group_merchant_ids:
        first = (out["merchant_id"] == mid) & (out["covered_from"] == params.missing_txn_date)
        second = (out["merchant_id"] == mid) & (out["covered_from"] == params.combined_covered_to)
        if not first.any() or not second.any():
            continue
        out.loc[second, sum_cols] = (
            out.loc[second, sum_cols].to_numpy() + out.loc[first, sum_cols].to_numpy()
        )
        out.loc[second, "covered_from"] = params.missing_txn_date
        out.loc[second, "settlement_date"] = params.combined_settlement_date
        out = out.loc[~first]

    # Unexplained shortfall on one batch.
    short = (out["merchant_id"] == shortfall_merchant_id) & (
        out["covered_from"] == params.shortfall_txn_date
    )
    out.loc[short, "gross"] = np.round(
        out.loc[short, "gross"].to_numpy() * (1.0 - params.shortfall_fraction)
    ).astype(np.int64)
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------------------------
# S8: retries (denominator gap)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class S8Params:
    """Retries after drop-offs and technical failures, which open an attempt/order SR gap."""

    retryable_categories: tuple[str, ...] = ("customer_dropoff", "technical")
    retry_prob: float = 0.45
    min_delay_s: int = 60
    max_delay_s: int = 600
    switch_to_upi_prob: float = 0.60
    max_attempts: int = 3


S8 = S8Params()


def s8_select_retries(
    failure_category: np.ndarray, rng: np.random.Generator, params: S8Params = S8
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """S8: choose which failed attempts are retried, whether they switch to UPI, and the delay.

    Returns ``(retried_mask, switch_to_upi, delay_seconds)``; the last two are aligned with the
    retried rows only.
    """
    retryable = np.isin(failure_category, params.retryable_categories)
    retried = retryable & (rng.random(len(failure_category)) < params.retry_prob)
    n = int(retried.sum())
    switch = rng.random(n) < params.switch_to_upi_prob
    delay = rng.integers(params.min_delay_s, params.max_delay_s + 1, size=n)
    return retried, switch, delay
