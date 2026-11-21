"""Transaction GMV vs settlements reconciliation (SPEC §10.7).

Successful, deduplicated GMV is aligned with settlement batches by **covered transaction date**.
The gap is split into explained components and an unexplained residual::

    transaction GMV (window)
      − not yet settled          (timing: settlement due after AS_OF_DATE, T+1)
      − missing batch            (no batch covers the date and none is due later)
      + combined-batch spill-over (part of a multi-day batch covering dates outside the window)
      + unexplained residual
      = settled gross
      − refunds − fees
      = settled net

Each batch is also checked on its own: ``gross − GMV of its covered dates``. A batch residual at
or above the flag threshold is unexplained and flagged, even if it is small against the whole
window.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from paysight.analysis.compare import require_ok
from paysight.config import AnalysisSettings
from paysight.execution.engine import Engine, QueryResult
from paysight.safety.access import RolePolicy
from paysight.semantic.models import Filter, QuerySpec, TimeWindow

SETTLEMENT_LAG_DAYS = 1


def _as_date(value: object) -> date:
    """Normalize DuckDB/pandas date-like values to ``datetime.date``.

    ``pd.Timestamp`` subclasses ``date``, so an ``isinstance`` check is not enough: a Timestamp
    never compares equal to the matching ``date`` as a dict key.
    """
    return pd.Timestamp(value).date()


_SPILL_MARGIN_DAYS = 7  # look this far outside the window for multi-day batch coverage


class Component(BaseModel):
    """One explained or unexplained part of the GMV-to-settlement gap."""

    model_config = ConfigDict(frozen=True)

    label: str
    kind: Literal["explained", "unexplained"]
    amount_inr: float
    detail: str
    needs_followup: bool = False


class BatchCheck(BaseModel):
    """One settlement batch compared with the GMV of the dates it covers."""

    model_config = ConfigDict(frozen=True)

    settlement_batch_id: str
    merchant_name: str
    covered_from: date
    covered_to: date
    settlement_date: date
    gross_inr: float
    expected_gmv_inr: float
    residual_inr: float
    residual_pct: float
    flagged: bool


class Reconciliation(BaseModel):
    """GMV vs settlement for one merchant or group over a window."""

    model_config = ConfigDict(frozen=True)

    scope: str
    window_label: str
    txn_gmv_inr: float
    settled_gross_inr: float
    refunds_inr: float
    fees_inr: float
    settled_net_inr: float
    components: list[Component]
    residual_inr: float
    residual_pct: float
    flagged: bool
    batches: list[BatchCheck] = Field(default_factory=list)

    @property
    def flagged_batches(self) -> list[BatchCheck]:
        """Batches with an unexplained residual at or above the threshold."""
        return [b for b in self.batches if b.flagged]


def _batches_sql(scope_column: str, scope_values: list[str], lo: date, hi: date) -> str:
    """Batch-level settlement rows, built with sqlglot literals (no string interpolation)."""
    query = (
        exp.select(
            "settlement_batch_id",
            "merchant_name",
            "settlement_date",
            "covered_from",
            "covered_to",
            "gross_amount_inr",
            "refunds_inr",
            "fees_inr",
            "net_amount_inr",
        )
        .from_("v_settlements")
        .where(
            exp.and_(
                exp.In(
                    this=exp.column(scope_column),
                    expressions=[exp.Literal.string(v) for v in scope_values],
                ),
                exp.GTE(
                    this=exp.column("covered_to"),
                    expression=exp.cast(exp.Literal.string(lo.isoformat()), "DATE"),
                ),
                exp.LTE(
                    this=exp.column("covered_from"),
                    expression=exp.cast(exp.Literal.string(hi.isoformat()), "DATE"),
                ),
            )
        )
        .order_by("covered_from", "merchant_name")
    )
    return query.sql(dialect="duckdb", pretty=True)


def reconcile(
    engine: Engine,
    role: RolePolicy,
    *,
    window: TimeWindow,
    as_of: date,
    data_start: date,
    merchant_group: str | None = None,
    merchant_names: list[str] | None = None,
    settings: AnalysisSettings,
) -> tuple[Reconciliation, list[QueryResult]]:
    """Reconcile transaction GMV with settlements for a merchant group or named merchants."""
    if (merchant_group is None) == (not merchant_names):
        raise ValueError("pass exactly one of merchant_group or merchant_names")
    column, values = (
        ("merchant_group", [merchant_group])
        if merchant_group
        else ("merchant_name", merchant_names)
    )
    assert values is not None
    scope_label = merchant_group or ", ".join(values)
    threshold = settings.reconcile_unexplained_flag_pct

    # Batches overlapping the window, and GMV for every date they cover.
    batch_result = engine.run_sql(
        _batches_sql(column, list(values), window.start, window.end), role
    )
    require_ok(batch_result, "settlement batches")
    batches = batch_result.frame
    gmv_window = TimeWindow(
        start=max(data_start, window.start - timedelta(days=_SPILL_MARGIN_DAYS)),
        end=min(as_of, window.end + timedelta(days=_SPILL_MARGIN_DAYS)),
    )
    _, gmv_result = engine.run_spec(
        QuerySpec(
            metrics=["gmv_inr"],
            dimensions=["merchant_name", "date"],
            filters=[Filter(dimension=column, op="in", values=list(values))],
            time_window=gmv_window,
        ),
        role,
    )
    require_ok(gmv_result, "transaction GMV")
    gmv = {
        (str(r.merchant_name), _as_date(r.date)): float(r.gmv_inr)
        for r in gmv_result.frame.itertuples()
    }

    def days(a: date, b: date) -> list[date]:
        return [a + timedelta(days=i) for i in range((b - a).days + 1)]

    in_window = set(days(window.start, window.end))
    txn_gmv = sum(v for (_, d), v in gmv.items() if d in in_window)

    covered: set[tuple[str, date]] = set()
    checks: list[BatchCheck] = []
    spill = 0.0
    combined_notes = []
    for b in batches.itertuples():
        cf, ct, sd = _as_date(b.covered_from), _as_date(b.covered_to), _as_date(b.settlement_date)
        span = days(cf, ct)
        covered.update((b.merchant_name, d) for d in span)
        expected = sum(gmv.get((b.merchant_name, d), 0.0) for d in span)
        residual = float(b.gross_amount_inr) - expected
        pct = 100 * residual / expected if expected else 0.0
        checks.append(
            BatchCheck(
                settlement_batch_id=b.settlement_batch_id,
                merchant_name=b.merchant_name,
                covered_from=cf,
                covered_to=ct,
                settlement_date=sd,
                gross_inr=float(b.gross_amount_inr),
                expected_gmv_inr=expected,
                residual_inr=residual,
                residual_pct=pct,
                flagged=abs(pct) >= threshold,
            )
        )
        if len(span) > 1:
            spill += sum(gmv.get((b.merchant_name, d), 0.0) for d in span if d not in in_window)
            combined_notes.append(f"{b.merchant_name}: {cf:%d %b}–{ct:%d %b} settled {sd:%d %b}")

    unsettled = missing = 0.0
    unsettled_dates: set[date] = set()
    missing_dates: set[date] = set()
    for (merchant, d), value in gmv.items():
        if d not in in_window or (merchant, d) in covered or value == 0:
            continue
        if d + timedelta(days=SETTLEMENT_LAG_DAYS) > as_of:
            unsettled += value
            unsettled_dates.add(d)
        else:
            missing += value
            missing_dates.add(d)

    settled_gross = float(batches["gross_amount_inr"].astype(float).sum()) if len(batches) else 0.0
    refunds = float(batches["refunds_inr"].astype(float).sum()) if len(batches) else 0.0
    fees = float(batches["fees_inr"].astype(float).sum()) if len(batches) else 0.0
    net = float(batches["net_amount_inr"].astype(float).sum()) if len(batches) else 0.0
    residual = settled_gross - (txn_gmv - unsettled - missing + spill)
    residual_pct = 100 * residual / txn_gmv if txn_gmv else 0.0

    components: list[Component] = []
    if unsettled:
        components.append(
            Component(
                label="Not yet settled (timing)",
                kind="explained",
                amount_inr=-unsettled,
                detail=(
                    f"GMV on {', '.join(f'{d:%d %b}' for d in sorted(unsettled_dates))} settles "
                    f"T+{SETTLEMENT_LAG_DAYS}, after the data cut-off ({as_of:%d %b})."
                ),
            )
        )
    if combined_notes:
        components.append(
            Component(
                label="Late or combined batch",
                kind="explained",
                amount_inr=spill,
                detail="Multi-day batch(es): " + "; ".join(combined_notes)
                + (". Part of the batch covers dates outside the window." if spill else
                   ". Fully inside the window, so no net effect on the total."),
            )
        )  # fmt: skip
    if missing:
        components.append(
            Component(
                label="Missing batch",
                kind="explained",
                amount_inr=-missing,
                detail=(
                    "No settlement batch covers "
                    f"{', '.join(f'{d:%d %b}' for d in sorted(missing_dates))}, and none is due "
                    "later. Follow up with the acquirer."
                ),
                needs_followup=True,
            )
        )
    if abs(residual) >= 0.005:
        components.append(
            Component(
                label="Unexplained difference",
                kind="unexplained",
                amount_inr=residual,
                detail="Settled gross differs from the GMV of the dates it covers; nothing in the "
                "data explains this.",
                needs_followup=True,
            )
        )
    components += [
        Component(
            label="Refunds", kind="explained", amount_inr=-refunds, detail="Netted off settlement."
        ),
        Component(
            label="Fees (MDR)", kind="explained", amount_inr=-fees, detail="Gross minus net."
        ),
    ]
    flagged = abs(residual_pct) >= threshold or any(c.flagged for c in checks)
    return (
        Reconciliation(
            scope=scope_label,
            window_label=window.label(),
            txn_gmv_inr=txn_gmv,
            settled_gross_inr=settled_gross,
            refunds_inr=refunds,
            fees_inr=fees,
            settled_net_inr=net,
            components=components,
            residual_inr=residual,
            residual_pct=residual_pct,
            flagged=flagged,
            batches=checks,
        ),
        [batch_result, gmv_result],
    )
