"""Data-quality checks on a hand-built fixture, plus reconciliation and DQ on the small dataset."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from paysight.analysis.quality import data_quality_report
from paysight.analysis.reconcile import reconcile
from paysight.config import get_settings
from paysight.execution.engine import Engine
from paysight.semantic.loader import get_semantic_layer
from paysight.semantic.models import TimeWindow

S = get_settings()
A = S.analysis
AS_OF = S.data.as_of_date
FIX_WINDOW = TimeWindow(start=date(2026, 10, 1), end=date(2026, 10, 7))


def _row(i: int, **over) -> dict:
    created = datetime(2026, 10, 1 + i % 7, 12, i % 60)
    card = i % 2 == 0
    row = {
        "attempt_id": f"A{i:05d}",
        "order_id": f"O{i:05d}",
        "attempt_number": 1,
        "created_at": created,
        "ingested_at": created + timedelta(seconds=60),
        "merchant_id": "M1" if i % 3 else "M2",
        "channel": "online",
        "payment_method": "DEBIT_CARD" if card else "UPI",
        "acquirer_id": "ACQ1",
        "issuer_id": "BNK1" if card else "PSP1",
        "amount_inr": 100.0,
        "status": "SUCCESS",
        "failure_code": None,
        "failure_category": None,
        "latency_ms": 1000,
        "customer_ref_hash": "c",
        "card_bin": "911001" if card else None,
    }
    row.update(over)
    return row


def _build(path: Path, rows: list[dict]) -> Path:
    con = duckdb.connect(str(path))
    con.execute(resources.files("paysight.data").joinpath("schema.sql").read_text())
    con.execute(
        "INSERT INTO dim_merchant VALUES ('M1','North Shop',NULL,'grocery','smb','north','Delhi',"
        "DATE '2020-01-01'), ('M2','South Shop',NULL,'grocery','smb','south','Chennai',"
        "DATE '2020-01-01')"
    )
    con.execute("INSERT INTO dim_acquirer VALUES ('ACQ1', 'Test Acquirer')")
    con.execute(
        "INSERT INTO dim_issuer VALUES ('BNK1','Test Bank','bank'), ('PSP1','Test PSP','upi_psp')"
    )
    con.execute("INSERT INTO dim_payment_method VALUES ('UPI','upi'), ('DEBIT_CARD','card')")
    frame = pd.DataFrame(rows)  # noqa: F841 - referenced by name in SQL
    con.execute("INSERT INTO fact_payment_attempts SELECT * FROM frame")
    con.execute("INSERT INTO meta_data_version VALUES ('test', 0, 0, now(), 'fixture')")
    con.close()
    return path


def _report(path: Path, role):
    engine = Engine(path, layer=get_semantic_layer(), limits=S.limits)
    try:
        report, results = data_quality_report(
            engine, role, window=FIX_WINDOW, as_of=AS_OF, settings=A
        )
    finally:
        engine.close()
    return report, results


CLEAN = [_row(i) for i in range(210)]


def test_clean_fixture_has_no_findings(tmp_path: Path, policies) -> None:
    report, results = _report(_build(tmp_path / "clean.duckdb", CLEAN), policies["leadership"])
    assert report.findings == []
    assert {c.check for c in report.checks} == {
        "duplicates", "null_rate", "orphan_keys", "timestamps", "ingestion_lag", "daily_volume"
    }  # fmt: skip
    assert all(r.report.checks[0].name == "internal_reviewed_query" for r in results)


@pytest.fixture()
def dirty(tmp_path: Path) -> Path:
    rows = list(CLEAN)
    rows.append(
        {**rows[5], "ingested_at": rows[5]["ingested_at"] + timedelta(hours=6)}
    )  # dup on M1 (north)
    for i in range(0, 40, 2):  # NULL issuer on 20 of ~70 M2/M1 card rows
        rows[i] = {**rows[i], "issuer_id": None}
    rows.append(_row(500, merchant_id="M999"))  # orphan merchant
    rows.append(
        _row(501, created_at=datetime(2026, 11, 5), ingested_at=datetime(2026, 11, 5, 0, 1))
    )
    rows.append(_row(502, status="FAILED"))  # failed without a failure code
    late = _row(503)
    rows.append({**late, "ingested_at": late["created_at"] + timedelta(hours=2)})
    return _build(tmp_path / "dirty.duckdb", rows)


def test_each_planted_issue_is_found(dirty: Path, policies) -> None:
    report, _ = _report(dirty, policies["leadership"])
    found = {f.check for f in report.findings}
    assert found == {"duplicates", "null_rate", "orphan_keys", "timestamps", "ingestion_lag"}
    dup = next(f for f in report.findings if f.check == "duplicates")
    assert "1 attempts were ingested twice" in dup.detail and dup.severity == "warning"
    assert "attempts" in dup.affected_metrics
    nulls = [f for f in report.findings if f.check == "null_rate"]
    assert {f.severity for f in nulls} == {"warning", "high"}
    assert report.max_severity == "high"
    assert report.affecting([], ["issuer_name"])


def test_regional_scope_hides_other_regions_findings(dirty: Path, policies) -> None:
    report, _ = _report(dirty, policies["analyst_north"])
    # The orphan merchant is not a north merchant, so a north analyst does not see it.
    assert "orphan_keys" not in {f.check for f in report.findings}
    assert "duplicates" in {f.check for f in report.findings}  # the duplicate is on M1 (north)


def test_small_dataset_dq_finds_s5_s6_and_sale(small_engine, policies) -> None:
    window = TimeWindow(start=S.data.start_date, end=S.data.end_date)
    report, _ = data_quality_report(
        small_engine, policies["leadership"], window=window, as_of=AS_OF, settings=A
    )
    titles = [f.title for f in report.findings]
    assert any(t.startswith("Duplicate records on 12 Oct") for t in titles)
    assert any("Meridian Bank card attempts, 2026-08" in t for t in titles)
    spikes = [f for f in report.findings if f.check == "daily_volume"]
    assert spikes and spikes[0].first_date == date(2026, 10, 18)


def test_reconcile_novamart_last_week(small_engine, policies) -> None:
    rec, results = reconcile(
        small_engine,
        policies["leadership"],
        window=TimeWindow(start=date(2026, 10, 25), end=date(2026, 10, 31)),
        as_of=AS_OF,
        data_start=S.data.start_date,
        merchant_group="NovaMart",
        settings=A,
    )
    labels = {c.label: c for c in rec.components}
    assert labels["Not yet settled (timing)"].kind == "explained"
    assert labels["Not yet settled (timing)"].amount_inr < 0
    assert "25 Oct–26 Oct settled 28 Oct" in labels["Late or combined batch"].detail
    assert "Unexplained difference" not in labels
    assert abs(rec.residual_inr) < 0.01 and not rec.flagged
    assert rec.settled_gross_inr - rec.refunds_inr - rec.fees_inr == pytest.approx(
        rec.settled_net_inr
    )
    assert all(r.report.passed for r in results)


def test_reconcile_flags_orbit_unexplained_shortfall(small_engine, policies) -> None:
    rec, _ = reconcile(
        small_engine,
        policies["leadership"],
        window=TimeWindow(start=date(2026, 10, 12), end=date(2026, 10, 18)),
        as_of=AS_OF,
        data_start=S.data.start_date,
        merchant_names=["Orbit Electronics"],
        settings=A,
    )
    assert rec.flagged
    (batch,) = rec.flagged_batches
    assert batch.covered_from == date(2026, 10, 14)
    assert batch.residual_pct == pytest.approx(-1.8, abs=0.05)
    unexplained = [c for c in rec.components if c.kind == "unexplained"]
    assert len(unexplained) == 1 and unexplained[0].needs_followup


def test_reconcile_respects_row_filters(small_engine, policies) -> None:
    # Orbit Electronics is in the west region, so a north analyst sees no batches or GMV.
    rec, _ = reconcile(
        small_engine,
        policies["analyst_north"],
        window=TimeWindow(start=date(2026, 10, 12), end=date(2026, 10, 18)),
        as_of=AS_OF,
        data_start=S.data.start_date,
        merchant_names=["Orbit Electronics"],
        settings=A,
    )
    assert rec.txn_gmv_inr == 0 and rec.settled_gross_inr == 0 and rec.batches == []
