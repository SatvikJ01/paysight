"""Realized ground truth: scenario numbers measured from the generated data (SPEC §6.4).

Nothing here reads scenario *parameters* to produce an answer; every number is computed with SQL
against the built database. Each scenario also carries named checks against the expected ranges
from the spec, which the tests and ``paysight data generate`` report.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

import duckdb
import pandas as pd
import yaml

from paysight.analysis.decompose import decompose_rate_change
from paysight.data import scenarios

CARDS = scenarios.CARD_METHODS
S2_W0 = (date(2026, 10, 4), date(2026, 10, 17))
S2_W1 = (date(2026, 10, 18), date(2026, 10, 31))
S4_REFERENCE = ((date(2026, 8, 22), date(2026, 8, 31)), (date(2026, 9, 11), date(2026, 9, 20)))
RECON_WEEK = (date(2026, 10, 25), date(2026, 10, 31))


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(float(x), nd)


def _check(value: float | bool | None, *, lo: float | None = None, hi: float | None = None) -> dict:
    """A named-check record: the value, its expected range, and whether it passed.

    A missing value (``None``) always fails.
    """
    if value is None:
        return {"value": None, "expected": f"[{lo}, {hi}]", "passed": False}
    if isinstance(value, bool):
        return {"value": value, "expected": True, "passed": value}
    passed = (lo is None or value >= lo) and (hi is None or value <= hi)
    expected = f"[{'' if lo is None else lo}, {'' if hi is None else hi}]"
    return {"value": _r(value, 4), "expected": expected, "passed": bool(passed)}


def _one(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> tuple:
    row = con.execute(sql, params or []).fetchone()
    assert row is not None
    return row


def _sr(
    con: duckdb.DuckDBPyConnection, where: str, params: list[Any], view: str = "v_attempts_clean"
) -> tuple[float, int]:
    """Attempt success rate and attempt count for a filter (``where`` is a constant template)."""
    sr, n = _one(
        con,
        f"SELECT AVG(CASE WHEN status = 'SUCCESS' THEN 1.0 ELSE 0.0 END), COUNT(*) "
        f"FROM {view} WHERE {where}",
        params,
    )
    return float(sr or 0.0), int(n)


def _dataset(con: duckdb.DuckDBPyConnection, scale: float) -> dict:
    raw = _one(con, "SELECT COUNT(*) FROM fact_payment_attempts")[0]
    clean, orders, first_day, last_day, online = _one(
        con,
        "SELECT COUNT(*), COUNT(DISTINCT order_id), MIN(txn_date), MAX(txn_date), "
        "AVG(CASE WHEN channel = 'online' THEN 1.0 ELSE 0.0 END) FROM v_attempts_clean",
    )
    merchants = _one(con, "SELECT COUNT(*) FROM dim_merchant")[0]
    mix = con.execute(
        "SELECT payment_method, COUNT(*) * 1.0 / SUM(COUNT(*)) OVER () FROM v_attempts_clean "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return {
        "raw_rows": int(raw),
        "clean_rows": int(clean),
        "orders": int(orders),
        "merchants": int(merchants),
        "first_date": str(first_day),
        "last_date": str(last_day),
        "online_share": _r(online),
        "method_mix": {m: _r(s) for m, s in mix},
        "checks": {
            "raw_rows_per_unit_scale_in_1.0M_1.3M": _check(raw / scale, lo=1.0e6, hi=1.3e6),
            "online_share_near_65pct": _check(float(online), lo=0.62, hi=0.68),
        },
    }


def _s1(con: duckdb.DuckDBPyConnection) -> dict:
    p = scenarios.S1
    card_filter = "acquirer_id = ? AND payment_method IN (?, ?, ?)"
    sr_win, n_win = _sr(
        con,
        f"{card_filter} AND created_at >= ? AND created_at < ?",
        [p.acquirer_id, *CARDS, p.start, p.end],
    )
    sr_base, _ = _sr(
        con,
        f"{card_filter} AND txn_date BETWEEN ? AND ?",
        [p.acquirer_id, *CARDS, date(2026, 9, 3), date(2026, 9, 16)],
    )
    timeouts = _one(
        con,
        f"SELECT COUNT(*) FROM v_attempts_clean WHERE {card_filter} "
        "AND created_at >= ? AND created_at < ? AND failure_code = 'ACQ_TIMEOUT'",
        [p.acquirer_id, *CARDS, p.start, p.end],
    )[0]
    daily = con.execute(
        "SELECT txn_date, AVG(CASE WHEN status = 'SUCCESS' THEN 1.0 ELSE 0.0 END) "
        "FROM v_attempts_clean WHERE txn_date BETWEEN ? AND ? GROUP BY 1 ORDER BY 1",
        [date(2026, 9, 10), date(2026, 9, 17)],
    ).fetchall()
    trailing_median = median(float(sr) for _, sr in daily[:7])
    sr_0917 = float(daily[7][1])
    gap_pp = (trailing_median - sr_0917) * 100
    return {
        "window": f"{p.start:%Y-%m-%d %H:%M} to {p.end:%Y-%m-%d %H:%M}",
        "kestrel_card_attempts_in_window": n_win,
        "kestrel_card_sr_in_window": _r(sr_win),
        "kestrel_card_sr_baseline_0903_0916": _r(sr_base),
        "acq_timeout_failures_in_window": int(timeouts),
        "excess_failed_attempts": round(n_win * (sr_base - sr_win)),
        "overall_daily_sr_0917": _r(sr_0917),
        "trailing_7d_median_daily_sr": _r(trailing_median),
        "daily_sr_gap_pp": _r(gap_pp, 3),
        "checks": {
            "kestrel_card_sr_below_0.65": _check(sr_win, hi=0.65),
            "daily_sr_0917_at_least_2pp_below_median": _check(gap_pp, lo=2.0),
        },
    }


def _segment_counts(
    con: duckdb.DuckDBPyConnection, segment_sql: str, w0: tuple[date, date], w1: tuple[date, date]
) -> pd.DataFrame:
    """num/den per segment for the two windows (``segment_sql`` is a constant expression)."""
    return con.execute(
        f"""
        SELECT {segment_sql} AS segment,
               SUM(CASE WHEN txn_date BETWEEN ? AND ? AND status = 'SUCCESS' THEN 1 ELSE 0 END)
                   AS num0,
               SUM(CASE WHEN txn_date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS den0,
               SUM(CASE WHEN txn_date BETWEEN ? AND ? AND status = 'SUCCESS' THEN 1 ELSE 0 END)
                   AS num1,
               SUM(CASE WHEN txn_date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS den1
        FROM v_attempts_clean
        WHERE txn_date BETWEEN ? AND ?
        GROUP BY 1 ORDER BY 1
        """,
        [*w0, *w0, *w1, *w1, w0[0], w1[1]],
    ).df()


def _s2_s3(con: duckdb.DuckDBPyConnection) -> tuple[dict, dict]:
    by_method = decompose_rate_change(
        _segment_counts(con, "payment_method", S2_W0, S2_W1), dimension="payment_method"
    )
    by_issuer_method = decompose_rate_change(
        _segment_counts(con, "COALESCE(issuer_name, '(missing)') || ' | ' || payment_method",
                        S2_W0, S2_W1),
        dimension="issuer_name x payment_method",
    )  # fmt: skip
    harbor = next(s for s in by_issuer_method.segments if s.segment == "Harbor Bank | DEBIT_CARD")
    harbor_rate_share = harbor.rate / by_issuer_method.rate_effect

    nova_share = {}
    for label, (lo, hi) in (("w0", S2_W0), ("w1", S2_W1)):
        nova_share[label] = _one(
            con,
            "SELECT AVG(CASE WHEN merchant_group = 'NovaMart' THEN 1.0 ELSE 0.0 END) "
            "FROM v_attempts_clean WHERE txn_date BETWEEN ? AND ?",
            [lo, hi],
        )[0]
    nova_mix = con.execute(
        "SELECT payment_method, COUNT(*) * 1.0 / SUM(COUNT(*)) OVER () FROM v_attempts_clean "
        "WHERE merchant_group = 'NovaMart' AND txn_date BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC",
        list(S2_W1),
    ).fetchall()
    order_sr = {}
    for label, (lo, hi) in (("w0", S2_W0), ("w1", S2_W1)):
        order_sr[label] = _one(
            con,
            "SELECT AVG(CASE WHEN order_status = 'SUCCESS' THEN 1.0 ELSE 0.0 END) FROM v_orders "
            "WHERE txn_date BETWEEN ? AND ?",
            [lo, hi],
        )[0]

    delta_pp = by_method.delta * 100
    s2 = {
        "window_0": f"{S2_W0[0]}..{S2_W0[1]}",
        "window_1": f"{S2_W1[0]}..{S2_W1[1]}",
        "attempt_sr_w0": _r(by_method.rate0),
        "attempt_sr_w1": _r(by_method.rate1),
        "attempt_sr_delta_pp": _r(delta_pp, 3),
        "order_sr_w0": _r(order_sr["w0"]),
        "order_sr_w1": _r(order_sr["w1"]),
        "kitagawa_by_payment_method": {
            "mix_effect_pp": _r(by_method.mix_effect * 100, 3),
            "rate_effect_pp": _r(by_method.rate_effect * 100, 3),
            "mix_share": _r(by_method.mix_share, 4),
            "segments": {
                s.segment: {
                    "share0": _r(s.share0, 4),
                    "share1": _r(s.share1, 4),
                    "rate0": _r(s.rate0, 4),
                    "rate1": _r(s.rate1, 4),
                    "mix_pp": _r(s.mix * 100, 3),
                    "rate_pp": _r(s.rate * 100, 3),
                }
                for s in by_method.segments
            },
        },
        "novamart_share_w0": _r(nova_share["w0"], 4),
        "novamart_share_w1": _r(nova_share["w1"], 4),
        "novamart_method_mix_w1": {m: _r(s, 4) for m, s in nova_mix},
        "checks": {
            "attempt_sr_drop_1.2_to_2.5pp": _check(delta_pp, lo=-2.5, hi=-1.2),
            "mix_share_60_to_85pct": _check(by_method.mix_share or 0.0, lo=0.60, hi=0.85),
            "novamart_share_w1_near_30pct": _check(nova_share["w1"], lo=0.27, hi=0.33),
            "novamart_share_w0_near_6pct": _check(nova_share["w0"], lo=0.04, hi=0.08),
        },
    }

    p = scenarios.S3
    harbor_filter = "issuer_id = ? AND payment_method = ? AND txn_date BETWEEN ? AND ?"
    before, n_before = _sr(
        con, harbor_filter, [p.issuer_id, p.method, S2_W0[0], date(2026, 10, 20)]
    )
    after, n_after = _sr(con, harbor_filter, [p.issuer_id, p.method, p.start.date(), S2_W1[1]])
    drop_pp = (before - after) * 100
    s3 = {
        "harbor_debit_sr_1004_1020": _r(before),
        "harbor_debit_sr_1021_1031": _r(after),
        "harbor_debit_attempts_before": n_before,
        "harbor_debit_attempts_after": n_after,
        "harbor_debit_sr_drop_pp": _r(drop_pp, 3),
        "issuer_x_method_rate_effect_pp": _r(by_issuer_method.rate_effect * 100, 3),
        "harbor_debit_rate_contribution_pp": _r(harbor.rate * 100, 3),
        "harbor_debit_share_of_rate_effect": _r(harbor_rate_share, 4),
        "checks": {
            "harbor_debit_sr_drop_at_least_10pp": _check(drop_pp, lo=10.0),
            "harbor_debit_is_bulk_of_rate_effect": _check(harbor_rate_share, lo=0.5),
        },
    }
    return s2, s3


def _s4(con: duckdb.DuckDBPyConnection) -> dict:
    p = scenarios.S4
    base = "issuer_id = ? AND payment_method = ?"
    evening = "hour_of_day BETWEEN ? AND ?"
    other = "hour_of_day NOT BETWEEN ? AND ?"
    hours = [p.first_hour, p.last_hour]
    win = [p.first_day, p.last_day]
    ref = "(txn_date BETWEEN ? AND ? OR txn_date BETWEEN ? AND ?)"
    ref_days = [*S4_REFERENCE[0], *S4_REFERENCE[1]]
    ids = [p.issuer_id, p.method]
    ev_win, n_ev = _sr(con, f"{base} AND txn_date BETWEEN ? AND ? AND {evening}", ids + win + hours)
    ev_ref, _ = _sr(con, f"{base} AND {ref} AND {evening}", ids + ref_days + hours)
    day_win, _ = _sr(con, f"{base} AND txn_date BETWEEN ? AND ? AND {other}", ids + win + hours)
    day_ref, _ = _sr(con, f"{base} AND {ref} AND {other}", ids + ref_days + hours)
    evening_drop = (ev_ref - ev_win) * 100
    daytime_diff = (day_ref - day_win) * 100
    return {
        "zephyr_upi_evening_sr_in_window": _r(ev_win),
        "zephyr_upi_evening_sr_reference": _r(ev_ref),
        "zephyr_upi_evening_attempts_in_window": n_ev,
        "evening_drop_pp": _r(evening_drop, 3),
        "zephyr_upi_daytime_sr_in_window": _r(day_win),
        "zephyr_upi_daytime_sr_reference": _r(day_ref),
        "daytime_diff_pp": _r(daytime_diff, 3),
        "checks": {
            "evening_drop_at_least_7pp": _check(evening_drop, lo=7.0),
            "daytime_unaffected_within_2pp": _check(abs(daytime_diff), hi=2.0),
        },
    }


def _s5(con: duckdb.DuckDBPyConnection) -> dict:
    day = scenarios.S5.day
    raw, distinct = _one(
        con,
        "SELECT COUNT(*), COUNT(DISTINCT attempt_id) FROM fact_payment_attempts "
        "WHERE CAST(created_at AS DATE) = ?",
        [day],
    )
    clean = _one(con, "SELECT COUNT(*) FROM v_attempts_clean WHERE txn_date = ?", [day])[0]
    dup_total = _one(
        con, "SELECT COUNT(*) - COUNT(DISTINCT attempt_id) FROM fact_payment_attempts"
    )[0]
    inflation = (raw - clean) / clean * 100
    return {
        "date": str(day),
        "raw_attempts": int(raw),
        "clean_attempts": int(clean),
        "duplicate_rows": int(raw - distinct),
        "duplicate_rows_whole_dataset": int(dup_total),
        "raw_inflation_pct": _r(inflation, 3),
        "checks": {
            "raw_inflated_about_3pct": _check(inflation, lo=2.5, hi=3.5),
            "duplicates_only_on_that_day": _check(bool(dup_total == raw - distinct)),
            "clean_view_removes_duplicates": _check(bool(clean == distinct)),
        },
    }


def _s6(con: duckdb.DuckDBPyConnection) -> dict:
    p = scenarios.S6
    rate, n = _one(
        con,
        "SELECT AVG(CASE WHEN issuer_id IS NULL THEN 1.0 ELSE 0.0 END), COUNT(*) "
        "FROM v_attempts_clean WHERE acquirer_id = ? AND payment_method IN (?, ?, ?) "
        "AND txn_date BETWEEN ? AND ?",
        [p.acquirer_id, *CARDS, p.month_start, p.month_end],
    )
    elsewhere = _one(
        con,
        "SELECT COUNT(*) FROM v_attempts_clean WHERE issuer_id IS NULL AND NOT "
        "(acquirer_id = ? AND payment_method IN (?, ?, ?) AND txn_date BETWEEN ? AND ?)",
        [p.acquirer_id, *CARDS, p.month_start, p.month_end],
    )[0]
    return {
        "scope": "Meridian Bank card attempts, August 2026",
        "attempts_in_scope": int(n),
        "null_issuer_rate": _r(rate, 4),
        "null_issuer_rows_outside_scope": int(elsewhere),
        "checks": {
            "null_rate_near_8pct": _check(float(rate), lo=0.06, hi=0.10),
            "no_nulls_outside_scope": _check(bool(elsewhere == 0)),
        },
    }


def _gmv_by_date(con: duckdb.DuckDBPyConnection, where: str, params: list[Any]) -> dict[str, float]:
    rows = con.execute(
        f"SELECT txn_date, SUM(amount_inr) FROM v_attempts_clean WHERE status = 'SUCCESS' "
        f"AND {where} GROUP BY 1 ORDER BY 1",
        params,
    ).fetchall()
    return {str(d): float(v) for d, v in rows}


def _s7(con: duckdb.DuckDBPyConnection) -> dict:
    p = scenarios.S7
    lo, hi = RECON_WEEK
    gmv = _gmv_by_date(
        con, "merchant_group = ? AND txn_date BETWEEN ? AND ?", [p.combined_group, lo, hi]
    )
    batches = con.execute(
        "SELECT covered_from, covered_to, settlement_date, SUM(gross_amount_inr), "
        "SUM(refunds_inr), SUM(fees_inr), SUM(net_amount_inr), COUNT(*) FROM v_settlements "
        "WHERE merchant_group = ? AND covered_to >= ? AND covered_from <= ? "
        "GROUP BY 1, 2, 3 ORDER BY 1",
        [p.combined_group, lo, hi],
    ).fetchall()
    settled_dates = set()
    for cf, ct, *_ in batches:
        settled_dates.update(pd.date_range(cf, ct).date)
    unsettled = [d for d in gmv if date.fromisoformat(d) not in settled_dates]
    combined = [b for b in batches if b[0] != b[1]]
    gross_total = sum(float(b[3]) for b in batches)

    orbit_gmv = _one(
        con,
        "SELECT SUM(amount_inr) FROM v_attempts_clean WHERE status = 'SUCCESS' "
        "AND merchant_name = ? AND txn_date = ?",
        [p.shortfall_merchant, p.shortfall_txn_date],
    )[0]
    orbit_gross = _one(
        con,
        "SELECT SUM(gross_amount_inr) FROM v_settlements WHERE merchant_name = ? "
        "AND covered_from = ?",
        [p.shortfall_merchant, p.shortfall_txn_date],
    )[0]
    # At very small scales a merchant-day can be empty; report "missing", never a made-up value.
    shortfall_pct = (
        (float(orbit_gmv) - float(orbit_gross)) / float(orbit_gmv) * 100
        if orbit_gmv and orbit_gross is not None
        else None
    )
    mismatched = _one(
        con,
        """
        WITH g AS (
            SELECT merchant_id, txn_date, SUM(amount_inr) AS gmv
            FROM v_attempts_clean WHERE status = 'SUCCESS' GROUP BY 1, 2
        )
        SELECT COUNT(*) FROM v_settlements s JOIN g
          ON s.merchant_id = g.merchant_id AND s.covered_from = g.txn_date
        WHERE s.covered_from = s.covered_to AND s.gross_amount_inr <> g.gmv
        """,
    )[0]
    return {
        "novamart_last_week": {
            "window": f"{lo}..{hi}",
            "txn_gmv_inr": round(sum(gmv.values()), 2),
            "settled_gross_inr": round(gross_total, 2),
            "refunds_inr": round(sum(float(b[4]) for b in batches), 2),
            "fees_inr": round(sum(float(b[5]) for b in batches), 2),
            "net_inr": round(sum(float(b[6]) for b in batches), 2),
            "unsettled_txn_dates": unsettled,
            "unsettled_gmv_inr": round(sum(gmv[d] for d in unsettled), 2),
            "combined_batches": [
                {"covered": f"{b[0]}..{b[1]}", "settlement_date": str(b[2])} for b in combined
            ],
        },
        "orbit_shortfall": {
            "merchant": p.shortfall_merchant,
            "txn_date": str(p.shortfall_txn_date),
            "txn_gmv_inr": None if orbit_gmv is None else round(float(orbit_gmv), 2),
            "settled_gross_inr": None if orbit_gross is None else round(float(orbit_gross), 2),
            "shortfall_pct": _r(shortfall_pct, 3),
        },
        "single_day_batches_with_gross_mismatch": int(mismatched),
        "checks": {
            "novamart_combined_batch_present": _check(
                bool(combined)
                and all(str(b[0]) == str(p.missing_txn_date) for b in combined)
                and all(b[2] == p.combined_settlement_date for b in combined)
            ),
            "orbit_shortfall_near_1.8pct": _check(shortfall_pct, lo=1.7, hi=1.9),
            "only_orbit_batch_mismatches": _check(bool(mismatched == 1)),
        },
    }


def _s8(con: duckdb.DuckDBPyConnection) -> dict:
    def rates(where: str, params: list[Any]) -> tuple[float, float, float]:
        attempt, technical = _one(
            con,
            "SELECT AVG(CASE WHEN status = 'SUCCESS' THEN 1.0 ELSE 0.0 END), "
            "SUM(CASE WHEN status = 'SUCCESS' THEN 1.0 ELSE 0.0 END) / "
            "NULLIF(SUM(CASE WHEN failure_category = 'customer_dropoff' THEN 0 ELSE 1 END), 0) "
            f"FROM v_attempts_clean WHERE {where}",
            params,
        )
        order = _one(
            con,
            "SELECT AVG(CASE WHEN order_status = 'SUCCESS' THEN 1.0 ELSE 0.0 END) FROM v_orders "
            f"WHERE {where}",
            params,
        )[0]
        return float(attempt), float(order), float(technical)

    all_a, all_o, all_t = rates("txn_date BETWEEN ? AND ?", [date(2026, 8, 1), date(2026, 10, 31)])
    oct_a, oct_o, oct_t = rates("txn_date BETWEEN ? AND ?", [date(2026, 10, 1), date(2026, 10, 31)])
    multi = _one(con, "SELECT AVG(CASE WHEN attempt_count > 1 THEN 1.0 ELSE 0.0 END) FROM v_orders")
    gap_pp = (all_o - all_a) * 100
    return {
        "full_period": {
            "attempt_sr": _r(all_a),
            "order_sr": _r(all_o),
            "technical_sr": _r(all_t),
            "order_minus_attempt_pp": _r(gap_pp, 3),
        },
        "october": {
            "attempt_sr": _r(oct_a),
            "order_sr": _r(oct_o),
            "technical_sr": _r(oct_t),
            "order_minus_attempt_pp": _r((oct_o - oct_a) * 100, 3),
        },
        "orders_with_retries_share": _r(multi[0], 4),
        "checks": {"order_sr_several_pp_above_attempt_sr": _check(gap_pp, lo=2.0)},
    }


def compute_ground_truth(
    con: duckdb.DuckDBPyConnection, *, seed: int, scale: float, content_hash: str
) -> dict[str, Any]:
    """Measure every scenario's realized numbers from the database behind ``con``."""
    s2, s3 = _s2_s3(con)
    return {
        "meta": {
            "seed": seed,
            "scale": scale,
            "content_hash": content_hash,
            "note": "Realized values measured from the generated data, not scenario parameters.",
        },
        "dataset": _dataset(con, scale),
        "S1_acquirer_outage": _s1(con),
        "S2_mix_shift": s2,
        "S3_issuer_risk_declines": s3,
        "S4_psp_evening_degradation": _s4(con),
        "S5_duplicate_ingestion": _s5(con),
        "S6_missing_issuer_ids": _s6(con),
        "S7_source_disagreement": _s7(con),
        "S8_denominator_gap": _s8(con),
    }


def iter_checks(truth: dict[str, Any]) -> list[tuple[str, str, dict]]:
    """Flatten ``(section, check_name, check)`` triples from a ground-truth dict."""
    return [
        (section, name, check)
        for section, body in truth.items()
        if isinstance(body, dict)
        for name, check in body.get("checks", {}).items()
    ]


def write_ground_truth(truth: dict[str, Any], path: Path) -> None:
    """Write the ground truth as stable, human-readable YAML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Realized ground truth for the planted scenarios (SPEC §6.4).\n"
        "# Written by `paysight data generate` from the generated data. Do not edit by hand.\n"
    )
    body = yaml.safe_dump(truth, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(header + body, encoding="utf-8")


def load_ground_truth(path: Path) -> dict[str, Any]:
    """Read a ground-truth YAML file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))
