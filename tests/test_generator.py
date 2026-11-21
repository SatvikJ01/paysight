"""Generator tests: determinism, planted-scenario detectability (S1–S8) and data invariants."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from paysight.config import get_settings
from paysight.data import names
from paysight.data.generator import GenerationResult
from paysight.data.ground_truth import iter_checks, load_ground_truth
from tests.conftest import build_dataset


def test_same_seed_gives_identical_content_hash(
    small_dataset: GenerationResult, tmp_path: Path
) -> None:
    again = build_dataset(tmp_path / "again.duckdb")
    assert again.content_hash == small_dataset.content_hash
    assert again.raw_rows == small_dataset.raw_rows


def test_different_seed_gives_different_data(tmp_path: Path) -> None:
    a = build_dataset(tmp_path / "a.duckdb", scale=0.02, seed=1)
    b = build_dataset(tmp_path / "b.duckdb", scale=0.02, seed=2)
    assert a.content_hash != b.content_hash


def test_stored_content_hash_matches_result(
    small_con: duckdb.DuckDBPyConnection, small_dataset: GenerationResult
) -> None:
    row = small_con.execute("SELECT content_hash, seed, scale FROM meta_data_version").fetchone()
    assert row == (small_dataset.content_hash, 42, 0.1)


def _check_ids() -> list[str]:
    # Check names are static, so parametrize from the committed full-scale file.
    truth = load_ground_truth(get_settings().ground_truth_path)
    return [f"{section}:{name}" for section, name, _ in iter_checks(truth)]


@pytest.mark.parametrize("check_id", _check_ids())
def test_scenario_detectable_at_small_scale(small_dataset: GenerationResult, check_id: str) -> None:
    """Every S1–S8 check holds on the scale-0.1 realized ground truth."""
    checks = {f"{s}:{n}": c for s, n, c in iter_checks(small_dataset.ground_truth)}
    check = checks[check_id]
    assert check["passed"], f"{check_id}: realized {check['value']}, expected {check['expected']}"


def test_committed_ground_truth_passes_all_checks() -> None:
    truth = load_ground_truth(get_settings().ground_truth_path)
    failed = [f"{s}:{n}" for s, n, c in iter_checks(truth) if not c["passed"]]
    assert not failed
    assert truth["meta"]["scale"] == 1.0
    assert truth["meta"]["seed"] == 42


@pytest.mark.slow
def test_full_scale_regeneration_matches_committed_hash(full_dataset: GenerationResult) -> None:
    """A fresh full-scale build reproduces the committed ground truth exactly."""
    truth = load_ground_truth(get_settings().ground_truth_path)
    assert full_dataset.content_hash == truth["meta"]["content_hash"]
    assert full_dataset.seconds < 60


def test_clean_view_has_unique_attempts(small_con: duckdb.DuckDBPyConnection) -> None:
    total, distinct = small_con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT attempt_id) FROM v_attempts_clean"
    ).fetchone()
    assert total == distinct


def test_no_orphan_foreign_keys(small_con: duckdb.DuckDBPyConnection) -> None:
    orphans = small_con.execute(
        """
        SELECT COUNT(*) FROM fact_payment_attempts f
        LEFT JOIN dim_merchant m USING (merchant_id)
        LEFT JOIN dim_acquirer a USING (acquirer_id)
        LEFT JOIN dim_payment_method p USING (payment_method)
        WHERE m.merchant_id IS NULL OR a.acquirer_id IS NULL OR p.payment_method IS NULL
        """
    ).fetchone()[0]
    assert orphans == 0


def test_status_and_failure_fields_are_consistent(small_con: duckdb.DuckDBPyConnection) -> None:
    bad = small_con.execute(
        """
        SELECT COUNT(*) FROM fact_payment_attempts
        WHERE (status = 'SUCCESS' AND (failure_code IS NOT NULL OR failure_category IS NOT NULL))
           OR (status = 'FAILED' AND (failure_code IS NULL OR failure_category IS NULL))
           OR status NOT IN ('SUCCESS', 'FAILED')
           OR failure_category NOT IN
              ('customer_dropoff', 'issuer_decline', 'technical', 'risk_decline')
        """
    ).fetchone()[0]
    assert bad == 0


def test_card_bins_only_on_cards_and_fictional(small_con: duckdb.DuckDBPyConnection) -> None:
    rows = small_con.execute(
        """
        SELECT
            SUM(CASE WHEN p.method_family = 'card' AND f.card_bin IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN p.method_family <> 'card' AND f.card_bin IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN f.card_bin IS NOT NULL AND f.card_bin NOT LIKE '9%' THEN 1 ELSE 0 END)
        FROM fact_payment_attempts f JOIN dim_payment_method p USING (payment_method)
        """
    ).fetchone()
    assert rows == (0, 0, 0)


def test_retries_belong_to_orders_and_are_later(small_con: duckdb.DuckDBPyConnection) -> None:
    bad = small_con.execute(
        """
        SELECT COUNT(*) FROM v_attempts_clean r
        JOIN v_attempts_clean f
          ON r.order_id = f.order_id AND f.attempt_number = r.attempt_number - 1
        WHERE r.attempt_number > 1 AND r.created_at <= f.created_at
        """
    ).fetchone()[0]
    max_attempts = small_con.execute("SELECT MAX(attempt_number) FROM v_attempts_clean").fetchone()
    assert bad == 0
    assert max_attempts[0] <= 3


def test_settlements_match_gmv_except_planted_disagreements(
    small_con: duckdb.DuckDBPyConnection,
) -> None:
    rows = small_con.execute(
        """
        WITH g AS (
            SELECT merchant_id, txn_date, SUM(amount_inr) AS gmv, COUNT(*) AS n
            FROM v_attempts_clean WHERE status = 'SUCCESS' GROUP BY 1, 2
        )
        SELECT s.merchant_name, s.covered_from, s.covered_to
        FROM v_settlements s JOIN g
          ON s.merchant_id = g.merchant_id AND s.covered_from = g.txn_date
        WHERE s.gross_amount_inr <> g.gmv OR s.txn_count <> g.n
        """
    ).fetchall()
    # Only Orbit's shorted batch and NovaMart's combined (two-day) batches differ.
    for merchant, covered_from, covered_to in rows:
        if merchant == names.ORBIT_MERCHANT[0]:
            assert str(covered_from) == "2026-10-14"
        else:
            assert merchant.startswith(names.NOVAMART_GROUP)
            assert covered_from != covered_to


def test_net_equals_gross_minus_refunds_and_fees(small_con: duckdb.DuckDBPyConnection) -> None:
    bad = small_con.execute(
        "SELECT COUNT(*) FROM fact_settlements "
        "WHERE net_amount_inr <> gross_amount_inr - refunds_inr - fees_inr "
        "OR settlement_date > DATE '2026-10-31' OR refunds_inr < 0.005 * gross_amount_inr - 0.01"
    ).fetchone()[0]
    assert bad == 0


def test_all_names_are_from_fictional_lists(small_con: duckdb.DuckDBPyConnection) -> None:
    acquirers = set(small_con.execute("SELECT acquirer_name FROM dim_acquirer").fetchall())
    issuers = set(small_con.execute("SELECT issuer_name FROM dim_issuer").fetchall())
    assert acquirers == {(n,) for _, n in names.ACQUIRERS}
    assert issuers == {(n,) for _, n, _ in names.ISSUERS}
    assert small_con.execute("SELECT COUNT(*) FROM dim_merchant").fetchone()[0] == 400
