"""Deterministic synthetic payments data generator.

Builds ``data/paysight.duckdb``: dimension tables, attempt-grain facts with the planted
scenarios (see :mod:`paysight.data.scenarios`), settlements, governed views and a
``meta_data_version`` row carrying a content hash.

Determinism: every random component draws from its own named stream derived from the seed
(:func:`_rng`), so the same ``(seed, scale)`` always yields identical tables. The content hash
covers every data table (not ``generated_at``, which records the wall-clock build time).

Performance: all row-level work is vectorised with numpy; the only Python loops run over days,
payment methods or merchants.
"""

from __future__ import annotations

import hashlib
import os
import time
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from paysight.data import names, scenarios

GENERATOR_VERSION = "1.0.0"

# --------------------------------------------------------------------------------------------
# Baseline behaviour (SPEC §6.1, §6.3)
# --------------------------------------------------------------------------------------------

BASE_FIRST_ATTEMPTS_PER_DAY = 11_000  # first attempts; S8 retries come on top
WEEKEND_FACTOR = 1.15
FESTIVE_START = date(2026, 10, 18)
FESTIVE_UPLIFT = 1.04  # mild general uplift; most of the festive volume comes from S2

# Relative attempt volume by hour of day (IST): lunch and evening peaks.
HOUR_WEIGHTS = np.array(
    [0.8, 0.5, 0.3, 0.25, 0.25, 0.4, 0.8, 1.5, 2.5, 3.5, 4.5, 5.5,
     6.5, 6.8, 6.0, 5.0, 4.8, 5.2, 6.0, 7.0, 7.6, 7.2, 5.5, 3.0]
)  # fmt: skip

METHODS = tuple(m for m, _ in names.PAYMENT_METHODS)
METHOD_FAMILY = dict(names.PAYMENT_METHODS)
CHANNELS = ("online", "pos")

# Method mix by channel, in METHODS order:
# UPI, CREDIT_CARD, DEBIT_CARD, CARD_EMI, NETBANKING, WALLET
METHOD_MIX = {
    "online": (0.55, 0.12, 0.13, 0.03, 0.09, 0.08),
    "pos": (0.50, 0.20, 0.24, 0.03, 0.00, 0.03),
}

BASE_SR = {
    "UPI": 0.92,
    "CREDIT_CARD": 0.87,
    "DEBIT_CARD": 0.84,
    "CARD_EMI": 0.80,
    "NETBANKING": 0.79,
    "WALLET": 0.91,
}
POS_SR_BONUS = 0.03
SR_CAP = 0.98
ONLINE_SHARE = 0.65  # target channel mix (realized via per-category online propensity)


def _blended_pos_bonus() -> dict[str, float]:
    """Expected POS bonus per method across both channels (used for NovaMart, see S2)."""
    out = {}
    for i, m in enumerate(METHODS):
        online = ONLINE_SHARE * METHOD_MIX["online"][i]
        pos = (1.0 - ONLINE_SHARE) * METHOD_MIX["pos"][i]
        out[m] = POS_SR_BONUS * pos / (online + pos)
    return out


# Small, stable per-entity offsets so segments are not identical (±1pp).
ACQUIRER_SR_OFFSET = {
    "ACQ_KESTREL": 0.004,
    "ACQ_MERIDIAN": -0.003,
    "ACQ_SAFFRON": 0.002,
    "ACQ_NORTHSTAR": -0.005,
}
ISSUER_SR_OFFSET = {
    "BNK_HARBOR": 0.003,
    "BNK_LOTUS": 0.006,
    "BNK_RIDGE": -0.004,
    "BNK_INDIGO": 0.000,
    "BNK_CORAL": -0.007,
    "BNK_BANYAN": 0.002,
    "PSP_ZEPHYR": 0.002,
    "PSP_PAYNEST": 0.004,
    "PSP_TEZRA": -0.003,
    "PSP_ORCHID": -0.006,
    "WAL_POCKETLY": 0.003,
    "WAL_WALLETRA": -0.004,
}

ACQUIRER_IDS = tuple(a for a, _ in names.ACQUIRERS)
ISSUER_IDS = tuple(i for i, _, _ in names.ISSUERS)
BANK_IDS = tuple(i for i, _, t in names.ISSUERS if t == "bank")

# Acquirer routing weights (ACQUIRER_IDS order). Kestrel carries ~30% of card volume (S1).
CARD_ACQUIRER_WEIGHTS = (0.30, 0.28, 0.22, 0.20)
OTHER_ACQUIRER_WEIGHTS = (0.25, 0.25, 0.25, 0.25)

_BANK_DEBIT = {
    "BNK_HARBOR": 0.25,  # ~25% of debit volume (S3)
    "BNK_LOTUS": 0.18,
    "BNK_RIDGE": 0.16,
    "BNK_INDIGO": 0.15,
    "BNK_CORAL": 0.14,
    "BNK_BANYAN": 0.12,
}
_BANK_CREDIT = {
    "BNK_HARBOR": 0.18,
    "BNK_LOTUS": 0.22,
    "BNK_RIDGE": 0.17,
    "BNK_INDIGO": 0.16,
    "BNK_CORAL": 0.15,
    "BNK_BANYAN": 0.12,
}
ISSUER_WEIGHTS_BY_METHOD: dict[str, dict[str, float]] = {
    "UPI": {"PSP_ZEPHYR": 0.30, "PSP_PAYNEST": 0.28, "PSP_TEZRA": 0.24, "PSP_ORCHID": 0.18},
    "CREDIT_CARD": _BANK_CREDIT,
    "DEBIT_CARD": _BANK_DEBIT,
    "CARD_EMI": _BANK_CREDIT,
    "NETBANKING": _BANK_DEBIT,
    "WALLET": {"WAL_POCKETLY": 0.55, "WAL_WALLETRA": 0.45},
}

# Baseline failure mix per method: (failure_category, failure_code, weight).
# Customer drop-off dominates UPI, netbanking and wallet; issuer declines dominate cards.
FAILURE_MIX: dict[str, list[tuple[str, str, float]]] = {
    "UPI": [
        ("customer_dropoff", "UPI_COLLECT_EXPIRED", 0.35),
        ("customer_dropoff", "USER_CANCELLED", 0.20),
        ("technical", "UPI_BANK_UNAVAILABLE", 0.15),
        ("technical", "UPI_PSP_TIMEOUT", 0.04),
        ("technical", "GATEWAY_TIMEOUT", 0.06),
        ("issuer_decline", "UPI_INSUFFICIENT_FUNDS", 0.08),
        ("issuer_decline", "UPI_PIN_INCORRECT", 0.05),
        ("issuer_decline", "UPI_LIMIT_EXCEEDED", 0.02),
        ("risk_decline", "RISK_RULE_DECLINE", 0.05),
    ],
    "CREDIT_CARD": [
        ("issuer_decline", "DO_NOT_HONOR", 0.20),
        ("issuer_decline", "INSUFFICIENT_FUNDS", 0.15),
        ("issuer_decline", "INVALID_CVV", 0.08),
        ("issuer_decline", "CARD_EXPIRED", 0.07),
        ("customer_dropoff", "AUTH_3DS_ABANDONED", 0.15),
        ("customer_dropoff", "USER_CANCELLED", 0.05),
        ("technical", "GATEWAY_TIMEOUT", 0.08),
        ("technical", "ACQ_TIMEOUT", 0.02),
        ("technical", "ISSUER_UNAVAILABLE", 0.05),
        ("risk_decline", "RISK_RULE_DECLINE", 0.08),
        ("risk_decline", "ISSUER_RISK_DECLINE", 0.04),
        ("risk_decline", "SUSPECTED_FRAUD", 0.03),
    ],
    "DEBIT_CARD": [
        ("issuer_decline", "INSUFFICIENT_FUNDS", 0.28),
        ("issuer_decline", "DO_NOT_HONOR", 0.15),
        ("issuer_decline", "INVALID_CVV", 0.07),
        ("issuer_decline", "CARD_EXPIRED", 0.05),
        ("customer_dropoff", "AUTH_3DS_ABANDONED", 0.15),
        ("customer_dropoff", "USER_CANCELLED", 0.05),
        ("technical", "GATEWAY_TIMEOUT", 0.07),
        ("technical", "ACQ_TIMEOUT", 0.02),
        ("technical", "ISSUER_UNAVAILABLE", 0.04),
        ("risk_decline", "RISK_RULE_DECLINE", 0.07),
        ("risk_decline", "ISSUER_RISK_DECLINE", 0.03),
        ("risk_decline", "SUSPECTED_FRAUD", 0.02),
    ],
    "CARD_EMI": [
        ("issuer_decline", "EMI_NOT_ELIGIBLE", 0.25),
        ("issuer_decline", "DO_NOT_HONOR", 0.12),
        ("issuer_decline", "INSUFFICIENT_FUNDS", 0.08),
        ("customer_dropoff", "AUTH_3DS_ABANDONED", 0.18),
        ("customer_dropoff", "USER_CANCELLED", 0.07),
        ("technical", "GATEWAY_TIMEOUT", 0.06),
        ("technical", "ACQ_TIMEOUT", 0.02),
        ("technical", "ISSUER_UNAVAILABLE", 0.04),
        ("risk_decline", "RISK_RULE_DECLINE", 0.10),
        ("risk_decline", "ISSUER_RISK_DECLINE", 0.05),
        ("risk_decline", "SUSPECTED_FRAUD", 0.03),
    ],
    "NETBANKING": [
        ("customer_dropoff", "NB_SESSION_ABANDONED", 0.45),
        ("customer_dropoff", "USER_CANCELLED", 0.15),
        ("technical", "NB_BANK_UNAVAILABLE", 0.17),
        ("technical", "GATEWAY_TIMEOUT", 0.08),
        ("issuer_decline", "NB_INSUFFICIENT_FUNDS", 0.07),
        ("issuer_decline", "NB_BANK_DECLINED", 0.03),
        ("risk_decline", "RISK_RULE_DECLINE", 0.05),
    ],
    "WALLET": [
        ("customer_dropoff", "USER_CANCELLED", 0.35),
        ("customer_dropoff", "WALLET_OTP_TIMEOUT", 0.20),
        ("technical", "WALLET_UNAVAILABLE", 0.12),
        ("technical", "GATEWAY_TIMEOUT", 0.08),
        ("issuer_decline", "WALLET_INSUFFICIENT_BALANCE", 0.15),
        ("risk_decline", "RISK_RULE_DECLINE", 0.10),
    ],
}

# Latency (ms): log-normal around a per-method median, scaled by outcome.
LATENCY_MEDIAN_MS = {
    "UPI": 1800,
    "CREDIT_CARD": 2400,
    "DEBIT_CARD": 2600,
    "CARD_EMI": 3200,
    "NETBANKING": 6000,
    "WALLET": 1200,
}
LATENCY_FACTOR = {
    "SUCCESS": 1.0,
    "customer_dropoff": 4.0,
    "technical": 6.0,
    "issuer_decline": 0.9,
    "risk_decline": 1.1,
}
LATENCY_SIGMA = 0.45
LATENCY_MAX_MS = 90_000

# Merchants.
TIER_COUNTS = {"enterprise": 19, "mid_market": 100, "smb": 266}  # plus groups and Orbit = 400
TIER_VOLUME_MEDIAN = {"enterprise": 25.0, "mid_market": 4.5, "smb": 1.0}
TIER_VOLUME_SIGMA = {"enterprise": 0.4, "mid_market": 0.5, "smb": 0.6}
CATEGORY_WEIGHTS = {
    "grocery": 0.18,
    "electronics": 0.12,
    "fashion": 0.15,
    "travel": 0.07,
    "fuel": 0.10,
    "quick_commerce": 0.08,
    "pharmacy": 0.12,
    "dining": 0.18,
}
CATEGORY_ONLINE_PROPENSITY = {
    "grocery": 0.65,
    "electronics": 0.78,
    "fashion": 0.80,
    "travel": 0.97,
    "fuel": 0.08,
    "quick_commerce": 1.00,
    "pharmacy": 0.60,
    "dining": 0.40,
}
REGION_WEIGHTS = {"north": 0.28, "south": 0.27, "east": 0.18, "west": 0.27}
NOVAMART_BASE_SHARE = 0.06  # of first attempts, outside the S2 sale
NOVAMART_SPLIT = (0.5, 0.3, 0.2)  # electronics, fashion, grocery

# Amounts (INR): log-normal per category (median, sigma). EMI tickets are larger.
CATEGORY_AMOUNT = {
    "grocery": (900.0, 0.8),
    "electronics": (12_000.0, 0.9),
    "fashion": (2_200.0, 0.8),
    "travel": (8_500.0, 0.8),
    "fuel": (1_500.0, 0.6),
    "quick_commerce": (450.0, 0.7),
    "pharmacy": (600.0, 0.8),
    "dining": (800.0, 0.7),
}
EMI_AMOUNT_MULTIPLIER = 2.5
EMI_MIN_AMOUNT = 3_000.0

CUSTOMER_POOL = 400_000
INGEST_LAG_MEDIAN_S = 90.0
INGEST_LAG_SIGMA = 0.7
INGEST_LAG_MAX_S = 3_600

# Settlements: MDR-like fee rates per method family, and refunds as a share of gross.
MDR_BY_FAMILY = {"upi": 0.0, "card": 0.016, "netbanking": 0.012, "wallet": 0.015}
REFUND_SHARE_RANGE = (0.005, 0.02)


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _rng(seed: int, stream: str) -> np.random.Generator:
    """Independent, named random stream: changing one component never shifts another."""
    return np.random.default_rng(np.random.SeedSequence([seed, zlib.crc32(stream.encode())]))


def _draw_rows(rng: np.random.Generator, group: np.ndarray, table: np.ndarray) -> np.ndarray:
    """Draw one category per row, where row ``i`` uses probability row ``table[group[i]]``."""
    cum = np.cumsum(table / table.sum(axis=1, keepdims=True), axis=1)
    u = rng.random(len(group))
    idx = (u[:, None] >= cum[group]).sum(axis=1)
    return np.minimum(idx, table.shape[1] - 1)


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


@dataclass(frozen=True)
class GenerationResult:
    """Summary of one generator run."""

    path: Path
    seed: int
    scale: float
    content_hash: str
    raw_rows: int
    settlement_rows: int
    seconds: float
    ground_truth: dict[str, Any]


# --------------------------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------------------------


def build_merchants(rng: np.random.Generator) -> pd.DataFrame:
    """Build ~400 fictional merchants with volume weights and online propensity.

    Returns ``dim_merchant`` columns plus working columns ``weight`` and ``online_prob``.
    """
    rows: list[dict[str, Any]] = []
    used_names: set[str] = set()
    cats = list(CATEGORY_WEIGHTS)
    cat_p = np.array(list(CATEGORY_WEIGHTS.values()))
    regions = list(REGION_WEIGHTS)
    region_p = np.array(list(REGION_WEIGHTS.values()))

    def onboarded() -> date:
        return date(2019, 1, 1) + timedelta(days=int(rng.integers(0, 2_737)))  # to mid-2026

    def unique_name(category: str) -> str:
        while True:
            prefix = names.MERCHANT_PREFIXES[int(rng.integers(len(names.MERCHANT_PREFIXES)))]
            suffixes = names.MERCHANT_SUFFIXES[category]
            name = f"{prefix} {suffixes[int(rng.integers(len(suffixes)))]}"
            if name not in used_names:
                used_names.add(name)
                return name

    def city_in(region: str) -> str:
        options = names.CITIES[region]
        return options[int(rng.integers(len(options)))]

    def add(name: str, group: str | None, category: str, tier: str, region: str, city: str) -> None:
        weight = float(rng.lognormal(np.log(TIER_VOLUME_MEDIAN[tier]), TIER_VOLUME_SIGMA[tier]))
        online = CATEGORY_ONLINE_PROPENSITY[category] + rng.normal(0.0, 0.05)
        rows.append(
            {
                "merchant_name": name,
                "merchant_group": group,
                "category": category,
                "tier": tier,
                "region": region,
                "city": city,
                "onboarded_on": onboarded(),
                "weight": weight,
                "online_prob": float(np.clip(online, 0.0, 1.0)),
            }
        )

    for tier, count in TIER_COUNTS.items():
        for _ in range(count):
            category = cats[int(rng.choice(len(cats), p=cat_p))]
            region = regions[int(rng.choice(len(regions), p=region_p))]
            add(unique_name(category), None, category, tier, region, city_in(region))

    for group, category, count in names.BACKGROUND_GROUPS:
        for _ in range(count):
            region = regions[int(rng.choice(len(regions), p=region_p))]
            city = city_in(region)
            tier = "mid_market" if category == "fuel" else "smb"
            name, k = f"{group} {city}", 2
            while name in used_names:
                name, k = f"{group} {city} {k}", k + 1
            used_names.add(name)
            add(name, group, category, tier, region, city)

    orbit_name, orbit_cat, orbit_region, orbit_city = names.ORBIT_MERCHANT
    add(orbit_name, None, orbit_cat, "mid_market", orbit_region, orbit_city)

    others_total = sum(r["weight"] for r in rows)
    nova_total = others_total * NOVAMART_BASE_SHARE / (1.0 - NOVAMART_BASE_SHARE)
    for (name, category, region, city), split in zip(
        names.NOVAMART_MERCHANTS, NOVAMART_SPLIT, strict=True
    ):
        add(name, names.NOVAMART_GROUP, category, "enterprise", region, city)
        rows[-1]["weight"] = nova_total * split
        rows[-1]["online_prob"] = 1.0  # NovaMart sells online only

    df = pd.DataFrame(rows)
    df.insert(0, "merchant_id", [f"MER{i:04d}" for i in range(1, len(df) + 1)])
    return df


def dimension_frames(merchants: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Return the four dimension tables as DataFrames, in schema column order."""
    return {
        "dim_merchant": merchants[
            [
                "merchant_id",
                "merchant_name",
                "merchant_group",
                "category",
                "tier",
                "region",
                "city",
                "onboarded_on",
            ]
        ].copy(),
        "dim_acquirer": pd.DataFrame(names.ACQUIRERS, columns=["acquirer_id", "acquirer_name"]),
        "dim_issuer": pd.DataFrame(
            names.ISSUERS, columns=["issuer_id", "issuer_name", "issuer_type"]
        ),
        "dim_payment_method": pd.DataFrame(
            names.PAYMENT_METHODS, columns=["payment_method", "method_family"]
        ),
    }


# --------------------------------------------------------------------------------------------
# Attempts
# --------------------------------------------------------------------------------------------


def _card_bins() -> np.ndarray:
    """Fictional BIN table indexed ``[bank, kind, k]``; kind 0 = debit, 1 = credit/EMI."""
    table = np.empty((len(BANK_IDS), 2, 3), dtype=object)
    for b in range(len(BANK_IDS)):
        for kind in range(2):
            for k in range(3):
                table[b, kind, k] = f"9{b + 1}{kind + 1}{k + 1:03d}"
    return table


def _route(frame: pd.DataFrame, rng: np.random.Generator) -> None:
    """Assign acquirer, issuer and card BIN in place, based on ``method``."""
    method = frame["method"].to_numpy()
    n = len(frame)
    is_card = np.isin(method, [METHODS.index(m) for m in scenarios.CARD_METHODS])

    acq_table = np.array([OTHER_ACQUIRER_WEIGHTS, CARD_ACQUIRER_WEIGHTS])
    frame["acquirer"] = _draw_rows(rng, is_card.astype(int), acq_table)

    iss_table = np.array(
        [[ISSUER_WEIGHTS_BY_METHOD[m].get(i, 0.0) for i in ISSUER_IDS] for m in METHODS]
    )
    issuer = _draw_rows(rng, method, iss_table)
    frame["issuer"] = issuer

    bins = np.full(n, None, dtype=object)
    if is_card.any():
        bank = issuer[is_card]  # bank issuers are the first entries of ISSUER_IDS
        kind = (method[is_card] != METHODS.index("DEBIT_CARD")).astype(int)
        k = rng.integers(0, 3, size=int(is_card.sum()))
        bins[is_card] = _card_bins()[bank, kind, k]
    frame["card_bin"] = bins


def _first_attempts(
    merchants: pd.DataFrame, days: list[date], scale: float, rng: np.random.Generator
) -> pd.DataFrame:
    """Generate first attempts for every day, including S2's extra NovaMart volume."""
    lam = np.array(
        [
            BASE_FIRST_ATTEMPTS_PER_DAY
            * scale
            * (WEEKEND_FACTOR if d.weekday() >= 5 else 1.0)
            * (FESTIVE_UPLIFT if d >= FESTIVE_START else 1.0)
            for d in days
        ]
    )
    counts = rng.poisson(lam)
    day_idx = np.repeat(np.arange(len(days)), counts)
    weights = merchants["weight"].to_numpy()
    merchant = rng.choice(len(merchants), size=len(day_idx), p=weights / weights.sum())

    # S2: extra NovaMart volume on sale days.
    nova = (merchants["merchant_group"] == names.NOVAMART_GROUP).to_numpy()
    nova_ids = np.flatnonzero(nova)
    nova_p = weights[nova_ids] / weights[nova_ids].sum()
    base_nova_per_day = np.bincount(day_idx[nova[merchant]], minlength=len(days))
    extra = np.array(
        [
            scenarios.s2_extra_attempts(int(counts[i]), int(base_nova_per_day[i]))
            if scenarios.s2_in_sale(d)
            else 0
            for i, d in enumerate(days)
        ]
    )
    day_idx = np.concatenate([day_idx, np.repeat(np.arange(len(days)), extra)])
    merchant = np.concatenate([merchant, rng.choice(nova_ids, size=int(extra.sum()), p=nova_p)])
    n = len(day_idx)

    # Timestamps.
    hour = rng.choice(24, size=n, p=HOUR_WEIGHTS / HOUR_WEIGHTS.sum())
    second = rng.integers(0, 3_600, size=n)
    day0 = np.array([np.datetime64(d, "s") for d in days])
    created_at = day0[day_idx] + (hour * 3_600 + second).astype("timedelta64[s]")

    # Channel and method. Mix row 0 = online, 1 = pos, 2 = NovaMart sale mix.
    online = rng.random(n) < merchants["online_prob"].to_numpy()[merchant]
    in_sale = np.array([scenarios.s2_in_sale(d) for d in days])[day_idx]
    mix_row = np.where(online, 0, 1)
    mix_row[nova[merchant] & in_sale] = 2
    mix_table = np.array(
        [
            METHOD_MIX["online"],
            METHOD_MIX["pos"],
            [scenarios.S2.method_mix[m] for m in METHODS],
        ]
    )
    method = _draw_rows(rng, mix_row, mix_table)

    # Amounts in integer paise.
    category = merchants["category"].to_numpy()[merchant]
    median = np.array([CATEGORY_AMOUNT[c][0] for c in category])
    sigma = np.array([CATEGORY_AMOUNT[c][1] for c in category])
    amount = np.exp(np.log(median) + sigma * rng.standard_normal(n))
    emi = method == METHODS.index("CARD_EMI")
    amount[emi] = np.maximum(amount[emi] * EMI_AMOUNT_MULTIPLIER, EMI_MIN_AMOUNT)
    amount_paise = np.round(amount * 100).astype(np.int64)

    frame = pd.DataFrame(
        {
            "created_at": created_at,
            "merchant": merchant,
            "channel": np.where(online, 0, 1),
            "method": method,
            "amount_paise": amount_paise,
            "customer": rng.integers(0, CUSTOMER_POOL, size=n),
            "attempt_number": 1,
        }
    )
    _route(frame, rng)
    frame = frame.sort_values("created_at", kind="stable").reset_index(drop=True)
    frame["order_seq"] = np.arange(len(frame))
    return frame


def _scenario_view(frame: pd.DataFrame, merchants: pd.DataFrame) -> pd.DataFrame:
    """Readable columns that scenario functions match on."""
    return pd.DataFrame(
        {
            "created_at": frame["created_at"],
            "merchant_group": merchants["merchant_group"].to_numpy()[frame["merchant"].to_numpy()],
            "channel": np.array(CHANNELS)[frame["channel"].to_numpy()],
            "payment_method": np.array(METHODS)[frame["method"].to_numpy()],
            "acquirer_id": np.array(ACQUIRER_IDS)[frame["acquirer"].to_numpy()],
            "issuer_id": np.array(ISSUER_IDS)[frame["issuer"].to_numpy()],
        }
    )


def simulate_outcomes(
    frame: pd.DataFrame, merchants: pd.DataFrame, rng: np.random.Generator
) -> None:
    """Decide status, failure category/code and latency for each attempt, in place.

    Scenario hazards are applied first (see :mod:`paysight.data.scenarios`), then the baseline
    success draw for everything a hazard did not hit.
    """
    n = len(frame)
    method = frame["method"].to_numpy()
    view = _scenario_view(frame, merchants)

    p = np.array([BASE_SR[m] for m in METHODS])[method]
    bonus = POS_SR_BONUS * (frame["channel"].to_numpy() == 1)
    group = scenarios.s2_group_mask(view)
    bonus[group] = scenarios.s2_channel_blended_bonus(
        view["payment_method"].to_numpy()[group], _blended_pos_bonus()
    )
    p = np.minimum(p + bonus, SR_CAP)
    p = p + np.array([ACQUIRER_SR_OFFSET[a] for a in ACQUIRER_IDS])[frame["acquirer"].to_numpy()]
    p = p + np.array([ISSUER_SR_OFFSET[i] for i in ISSUER_IDS])[frame["issuer"].to_numpy()]

    category = np.full(n, None, dtype=object)
    code = np.full(n, None, dtype=object)
    hit_any = np.zeros(n, dtype=bool)
    for hazard in scenarios.outcome_hazards(view):
        hit = hazard.mask & ~hit_any & (rng.random(n) < hazard.prob)
        category[hit] = hazard.failure_category
        code[hit] = hazard.failure_code
        hit_any |= hit

    success = ~hit_any & (rng.random(n) < p)
    base_fail = ~hit_any & ~success
    for m_idx, m in enumerate(METHODS):
        rows = np.flatnonzero(base_fail & (method == m_idx))
        if len(rows) == 0:
            continue
        mix = FAILURE_MIX[m]
        weights = np.array([w for _, _, w in mix])
        pick = rng.choice(len(mix), size=len(rows), p=weights / weights.sum())
        category[rows] = np.array([c for c, _, _ in mix], dtype=object)[pick]
        code[rows] = np.array([c for _, c, _ in mix], dtype=object)[pick]

    outcome = np.where(success, "SUCCESS", category).astype(object)
    median = np.array([LATENCY_MEDIAN_MS[m] for m in METHODS])[method]
    factor = np.array([LATENCY_FACTOR[o] for o in outcome])
    latency = median * factor * np.exp(LATENCY_SIGMA * rng.standard_normal(n))

    frame["success"] = success
    frame["failure_category"] = category
    frame["failure_code"] = code
    frame["latency_ms"] = np.minimum(np.round(latency), LATENCY_MAX_MS).astype(np.int64)


def _retries(
    batch: pd.DataFrame,
    merchants: pd.DataFrame,
    end_exclusive: np.datetime64,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """S8: build the next round of retry attempts from a batch's failures."""
    failed = batch.loc[~batch["success"]]
    retried, switch, delay = scenarios.s8_select_retries(failed["failure_category"].to_numpy(), rng)
    nxt = failed.loc[retried, ["created_at", "merchant", "channel", "method", "amount_paise",
                               "customer", "attempt_number", "order_seq", "acquirer", "issuer",
                               "card_bin"]].copy()  # fmt: skip
    nxt["created_at"] = nxt["created_at"].to_numpy() + delay.astype("timedelta64[s]")
    nxt["attempt_number"] = nxt["attempt_number"] + 1

    # Switching to UPI re-routes the attempt (new acquirer and PSP, no card).
    if switch.any():
        switched = nxt.loc[switch].copy()
        switched["method"] = METHODS.index("UPI")
        _route(switched, rng)
        nxt.loc[switch, ["method", "acquirer", "issuer", "card_bin"]] = switched[
            ["method", "acquirer", "issuer", "card_bin"]
        ].to_numpy()
        for col in ("method", "acquirer", "issuer"):
            nxt[col] = nxt[col].astype(np.int64)

    nxt = nxt.loc[nxt["created_at"] < end_exclusive].reset_index(drop=True)
    simulate_outcomes(nxt, merchants, rng)
    return nxt


def _customer_hashes(customer: np.ndarray) -> np.ndarray:
    """Pseudonymous customer reference: 16 hex chars of sha256 over a synthetic id."""
    unique, inverse = np.unique(customer, return_inverse=True)
    hashes = np.array(
        [hashlib.sha256(f"cust-{c}".encode()).hexdigest()[:16] for c in unique], dtype=object
    )
    return hashes[inverse]


def build_attempts(
    merchants: pd.DataFrame, start: date, end: date, scale: float, seed: int
) -> pd.DataFrame:
    """Generate the full ``fact_payment_attempts`` table, scenarios included.

    Also returns working column ``amount_paise`` (exact integer amounts) for settlement
    computation; it is dropped before loading.
    """
    days = _date_range(start, end)
    first = _first_attempts(merchants, days, scale, _rng(seed, "first_attempts"))
    outcome_rng = _rng(seed, "outcomes")
    simulate_outcomes(first, merchants, outcome_rng)

    end_exclusive = np.datetime64(end + timedelta(days=1), "s")
    retry_rng = _rng(seed, "retries")
    batches = [first]
    while len(batches[-1]) and batches[-1]["attempt_number"].iloc[0] < scenarios.S8.max_attempts:
        batches.append(_retries(batches[-1], merchants, end_exclusive, retry_rng))
    allr = pd.concat(batches, ignore_index=True)

    keys = (allr["attempt_number"], allr["order_seq"], allr["created_at"])
    order = np.lexsort(tuple(k.to_numpy() for k in keys))
    allr = allr.iloc[order].reset_index(drop=True)

    misc = _rng(seed, "ingestion")
    lag = np.exp(np.log(INGEST_LAG_MEDIAN_S) + INGEST_LAG_SIGMA * misc.standard_normal(len(allr)))
    lag_s = np.clip(np.round(lag), 5, INGEST_LAG_MAX_S).astype(np.int64)

    m_idx = allr["merchant"].to_numpy()
    attempts = pd.DataFrame(
        {
            "attempt_id": [f"ATT{i:08d}" for i in range(1, len(allr) + 1)],
            "order_id": [f"ORD{i:08d}" for i in allr["order_seq"].to_numpy() + 1],
            "attempt_number": allr["attempt_number"].to_numpy().astype(np.int32),
            "created_at": allr["created_at"].to_numpy(),
            "ingested_at": allr["created_at"].to_numpy() + lag_s.astype("timedelta64[s]"),
            "merchant_id": merchants["merchant_id"].to_numpy()[m_idx],
            "channel": np.array(CHANNELS, dtype=object)[allr["channel"].to_numpy()],
            "payment_method": np.array(METHODS, dtype=object)[allr["method"].to_numpy()],
            "acquirer_id": np.array(ACQUIRER_IDS, dtype=object)[allr["acquirer"].to_numpy()],
            "issuer_id": np.array(ISSUER_IDS, dtype=object)[allr["issuer"].to_numpy()],
            "amount_paise": allr["amount_paise"].to_numpy(),
            "status": np.where(allr["success"].to_numpy(), "SUCCESS", "FAILED").astype(object),
            "failure_code": allr["failure_code"].to_numpy(),
            "failure_category": allr["failure_category"].to_numpy(),
            "latency_ms": allr["latency_ms"].to_numpy().astype(np.int32),
            "customer_ref_hash": _customer_hashes(allr["customer"].to_numpy()),
            "card_bin": allr["card_bin"].to_numpy(),
        }
    )
    attempts["created_at"] = attempts["created_at"].astype("datetime64[us]")
    attempts["ingested_at"] = attempts["ingested_at"].astype("datetime64[us]")

    attempts = scenarios.s6_missing_issuer_ids(attempts, _rng(seed, "s6"))
    return attempts


def build_settlements(
    clean_attempts: pd.DataFrame,
    merchants: pd.DataFrame,
    as_of: date,
    seed: int,
) -> pd.DataFrame:
    """Settlement batches from clean successful attempts (T+1, one date per batch), plus S7."""
    ok = clean_attempts.loc[clean_attempts["status"] == "SUCCESS"].copy()
    ok["txn_date"] = ok["created_at"].dt.date
    ok["family"] = ok["payment_method"].map(METHOD_FAMILY)
    ok["fee"] = ok["amount_paise"] * ok["family"].map(MDR_BY_FAMILY)

    per_family = ok.groupby(["merchant_id", "txn_date", "family"], sort=True).agg(
        gross=("amount_paise", "sum"), fee=("fee", "sum")
    )
    per_family["fee"] = np.round(per_family["fee"]).astype(np.int64)
    batches = per_family.groupby(level=["merchant_id", "txn_date"], sort=True).sum()
    batches["txn_count"] = ok.groupby(["merchant_id", "txn_date"], sort=True).size()
    batches = batches.reset_index().rename(columns={"fee": "fees"})

    rng = _rng(seed, "settlements")
    lo, hi = REFUND_SHARE_RANGE
    batches["refunds"] = np.round(batches["gross"] * rng.uniform(lo, hi, len(batches))).astype(
        np.int64
    )
    batches["covered_from"] = batches["txn_date"]
    batches["covered_to"] = batches["txn_date"]
    batches["settlement_date"] = batches["txn_date"] + timedelta(days=1)
    batches = batches.loc[batches["settlement_date"] <= as_of].drop(columns="txn_date")

    nova_ids = merchants.loc[merchants["merchant_group"] == names.NOVAMART_GROUP, "merchant_id"]
    orbit_id = merchants.loc[merchants["merchant_name"] == names.ORBIT_MERCHANT[0], "merchant_id"]
    batches = scenarios.s7_settlement_disagreements(batches, list(nova_ids), str(orbit_id.iloc[0]))
    batches["net"] = batches["gross"] - batches["refunds"] - batches["fees"]
    pairs = zip(batches["merchant_id"], batches["covered_from"], strict=True)
    batches["settlement_batch_id"] = [f"STL-{m}-{d:%Y%m%d}" for m, d in pairs]
    batches = batches.sort_values(["settlement_date", "merchant_id"], kind="stable")
    return batches[
        [
            "settlement_batch_id",
            "merchant_id",
            "settlement_date",
            "covered_from",
            "covered_to",
            "gross",
            "refunds",
            "fees",
            "net",
            "txn_count",
        ]
    ].reset_index(drop=True)


def content_hash(frames: dict[str, pd.DataFrame]) -> str:
    """sha256 over every table's row hashes, in a fixed table order."""
    digest = hashlib.sha256()
    for name in sorted(frames):
        df = frames[name]
        digest.update(name.encode())
        digest.update(",".join(df.columns).encode())
        digest.update(pd.util.hash_pandas_object(df, index=False).to_numpy().tobytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------------


def _schema_sql() -> str:
    return resources.files("paysight.data").joinpath("schema.sql").read_text(encoding="utf-8")


def _load(
    con: duckdb.DuckDBPyConnection,
    dims: dict[str, pd.DataFrame],
    attempts: pd.DataFrame,
    settlements: pd.DataFrame,
) -> None:
    con.execute(_schema_sql())
    for table, df in dims.items():
        con.register("src", df)
        con.execute(f"INSERT INTO {table} SELECT * FROM src")  # table names are constants
        con.unregister("src")

    con.register("src", attempts)
    con.execute(
        """
        INSERT INTO fact_payment_attempts
        SELECT attempt_id, order_id, attempt_number, created_at, ingested_at, merchant_id,
               channel, payment_method, acquirer_id, issuer_id,
               CAST(amount_paise / 100 AS DECIMAL(12, 2)), status, failure_code,
               failure_category, latency_ms, customer_ref_hash, card_bin
        FROM src
        """
    )
    con.unregister("src")

    con.register("src", settlements)
    con.execute(
        """
        INSERT INTO fact_settlements
        SELECT settlement_batch_id, merchant_id, settlement_date, covered_from, covered_to,
               CAST(gross / 100 AS DECIMAL(14, 2)), CAST(refunds / 100 AS DECIMAL(14, 2)),
               CAST(fees / 100 AS DECIMAL(14, 2)), CAST(net / 100 AS DECIMAL(14, 2)), txn_count
        FROM src
        """
    )
    con.unregister("src")


def generate(
    out_path: Path,
    *,
    start: date,
    end: date,
    as_of: date,
    seed: int,
    scale: float,
) -> GenerationResult:
    """Generate the full dataset into ``out_path`` and compute the realized ground truth.

    The database is built in a temporary file and moved into place only once complete.
    """
    from paysight.data.ground_truth import compute_ground_truth

    t0 = time.perf_counter()
    merchants = build_merchants(_rng(seed, "merchants"))
    dims = dimension_frames(merchants)
    clean = build_attempts(merchants, start, end, scale, seed)
    settlements = build_settlements(clean, merchants, as_of, seed)
    raw = scenarios.s5_duplicate_ingestion(clean, _rng(seed, "s5"))
    raw = raw.sort_values(["created_at", "attempt_id", "ingested_at"], kind="stable")
    raw = raw.reset_index(drop=True)

    digest = content_hash({**dims, "fact_payment_attempts": raw, "fact_settlements": settlements})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        _load(con, dims, raw, settlements)
        con.execute(
            "INSERT INTO meta_data_version VALUES (?, ?, ?, ?, ?)",
            [GENERATOR_VERSION, seed, scale, datetime.now().replace(microsecond=0), digest],
        )
        truth = compute_ground_truth(con, seed=seed, scale=scale, content_hash=digest)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    os.replace(tmp, out_path)

    return GenerationResult(
        path=out_path,
        seed=seed,
        scale=scale,
        content_hash=digest,
        raw_rows=len(raw),
        settlement_rows=len(settlements),
        seconds=time.perf_counter() - t0,
        ground_truth=truth,
    )
