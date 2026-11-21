"""Data-quality report (SPEC §10.6).

The only analysis tool allowed to read the raw ``fact_payment_attempts`` table, because
duplicates are invisible in the deduplicated view. All SQL here is fixed module-level text with
bound parameters, run through :meth:`Engine.run_internal`. Role row filters are still applied,
through ``dim_merchant``, so a regional analyst only sees findings for their region.

Each finding has a severity, the metrics and dimensions it affects, and whether Paysight already
corrects for it. Findings propagate into answer caveats.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from paysight.analysis.compare import require_ok
from paysight.analysis.stats import robust_z
from paysight.config import AnalysisSettings
from paysight.execution.engine import Engine, QueryResult
from paysight.safety.access import RolePolicy
from paysight.semantic.models import TimeWindow

Severity = Literal["info", "warning", "high"]
COUNT_METRICS = ["attempts", "successful_attempts", "failed_attempts", "gmv_inr", "orders"]
_MERCHANT_FILTER_COLUMNS = {"region", "city", "category", "tier", "merchant_group"}
NULL_RATE_TOLERANCE = 0.005  # issuer_id should never be NULL; flag scopes above 0.5%
INGEST_LAG_OUTLIER_S = 3_600


class Finding(BaseModel):
    """One data-quality issue in scope."""

    model_config = ConfigDict(frozen=True)

    check: str
    severity: Severity
    title: str
    detail: str
    first_date: date | None = None
    last_date: date | None = None
    affected_metrics: list[str] = Field(default_factory=list)
    affected_dimensions: list[str] = Field(default_factory=list)
    handled: str
    rows: list[dict] = Field(default_factory=list)


class CheckSummary(BaseModel):
    """Whether one check found anything."""

    model_config = ConfigDict(frozen=True)

    check: str
    description: str
    findings: int


class QualityReport(BaseModel):
    """All checks run over a window, with their findings."""

    model_config = ConfigDict(frozen=True)

    window_label: str
    checks: list[CheckSummary]
    findings: list[Finding]

    @property
    def max_severity(self) -> Severity | None:
        """The worst severity among findings, or ``None`` if clean."""
        order = {"info": 0, "warning": 1, "high": 2}
        return max((f.severity for f in self.findings), key=order.__getitem__, default=None)

    def affecting(self, metrics: list[str], dimensions: list[str]) -> list[Finding]:
        """Findings relevant to an analysis using these metrics or dimensions."""
        return [
            f
            for f in self.findings
            if set(f.affected_metrics) & set(metrics)
            or set(f.affected_dimensions) & set(dimensions)
        ]


def _scope(role: RolePolicy, alias: str) -> tuple[str, list[object]]:
    """SQL fragment + params restricting ``alias.merchant_id`` to the role's merchants."""
    if not role.row_filters:
        return "", []
    parts, params = [], []
    for column, values in sorted(role.row_filters.items()):
        if column not in _MERCHANT_FILTER_COLUMNS:  # fail closed on filters we cannot apply
            raise ValueError(f"data-quality checks cannot apply a row filter on {column!r}")
        parts.append(f"{column} IN (SELECT UNNEST(?))")  # column name from a fixed allowlist
        params.append(list(values))
    sql = (
        f" AND {alias}.merchant_id IN (SELECT merchant_id FROM dim_merchant "
        f"WHERE {' AND '.join(parts)})"
    )
    return sql, params


_DUPLICATES = """
SELECT CAST(f.created_at AS DATE) AS day,
       COUNT(*) AS raw_rows,
       COUNT(DISTINCT f.attempt_id) AS clean_rows
FROM fact_payment_attempts f
WHERE f.created_at >= ? AND f.created_at < ? {scope}
GROUP BY 1
HAVING COUNT(*) > COUNT(DISTINCT f.attempt_id)
ORDER BY 1
"""

_NULL_ISSUERS = """
SELECT a.acquirer_name, strftime(f.created_at, '%Y-%m') AS month, p.method_family,
       COUNT(*) AS attempts,
       SUM(CASE WHEN f.issuer_id IS NULL THEN 1 ELSE 0 END) AS null_issuer
FROM fact_payment_attempts f
JOIN dim_acquirer a ON f.acquirer_id = a.acquirer_id
JOIN dim_payment_method p ON f.payment_method = p.payment_method
WHERE f.created_at >= ? AND f.created_at < ? {scope}
GROUP BY 1, 2, 3
HAVING SUM(CASE WHEN f.issuer_id IS NULL THEN 1 ELSE 0 END) > ? * COUNT(*)
ORDER BY 1, 2, 3
"""

_CONSISTENCY = """
SELECT
  SUM(CASE WHEN f.status = 'FAILED' AND f.failure_code IS NULL THEN 1 ELSE 0 END)
    AS failed_without_code,
  SUM(CASE WHEN f.status = 'SUCCESS' AND f.failure_code IS NOT NULL THEN 1 ELSE 0 END)
    AS success_with_code,
  SUM(CASE WHEN p.method_family = 'card' AND f.card_bin IS NULL THEN 1 ELSE 0 END)
    AS card_without_bin,
  SUM(CASE WHEN p.method_family <> 'card' AND f.card_bin IS NOT NULL THEN 1 ELSE 0 END)
    AS non_card_with_bin
FROM fact_payment_attempts f
LEFT JOIN dim_payment_method p ON f.payment_method = p.payment_method
WHERE f.created_at >= ? AND f.created_at < ? {scope}
"""

_ORPHANS = """
SELECT
  SUM(CASE WHEN m.merchant_id IS NULL THEN 1 ELSE 0 END) AS merchant,
  SUM(CASE WHEN a.acquirer_id IS NULL THEN 1 ELSE 0 END) AS acquirer,
  SUM(CASE WHEN f.issuer_id IS NOT NULL AND i.issuer_id IS NULL THEN 1 ELSE 0 END) AS issuer,
  SUM(CASE WHEN p.payment_method IS NULL THEN 1 ELSE 0 END) AS payment_method
FROM fact_payment_attempts f
LEFT JOIN dim_merchant m ON f.merchant_id = m.merchant_id
LEFT JOIN dim_acquirer a ON f.acquirer_id = a.acquirer_id
LEFT JOIN dim_issuer i ON f.issuer_id = i.issuer_id
LEFT JOIN dim_payment_method p ON f.payment_method = p.payment_method
WHERE f.created_at >= ? AND f.created_at < ? {scope}
"""

_TIMESTAMPS = """
SELECT
  SUM(CASE WHEN f.created_at >= ? THEN 1 ELSE 0 END) AS future_created,
  SUM(CASE WHEN f.ingested_at < f.created_at THEN 1 ELSE 0 END) AS ingested_before_created
FROM fact_payment_attempts f
WHERE f.created_at >= ? {scope}
"""

_INGEST_LAG = """
WITH first_ingest AS (
  SELECT f.*, ROW_NUMBER() OVER (PARTITION BY f.attempt_id ORDER BY f.ingested_at) AS rn
  FROM fact_payment_attempts f
  WHERE f.created_at >= ? AND f.created_at < ? {scope}
)
SELECT CAST(created_at AS DATE) AS day, COUNT(*) AS late_rows,
       MAX(date_diff('second', created_at, ingested_at)) AS max_lag_s
FROM first_ingest
WHERE rn = 1 AND date_diff('second', created_at, ingested_at) > ?
GROUP BY 1 ORDER BY 1
"""

_DAILY_VOLUME = """
SELECT txn_date AS day, COUNT(*) AS attempts
FROM v_attempts_clean f
WHERE 1 = 1 {scope}
GROUP BY 1 ORDER BY 1
"""


def _runs(days: list[date]) -> list[tuple[date, date]]:
    """Group sorted dates into runs of consecutive days."""
    runs: list[tuple[date, date]] = []
    for d in days:
        if runs and d - runs[-1][1] == timedelta(days=1):
            runs[-1] = (runs[-1][0], d)
        else:
            runs.append((d, d))
    return runs


def data_quality_report(
    engine: Engine,
    role: RolePolicy,
    *,
    window: TimeWindow,
    as_of: date,
    settings: AnalysisSettings,
) -> tuple[QualityReport, list[QueryResult]]:
    """Run every data-quality check over ``window`` for ``role``'s scope."""
    scope, scope_params = _scope(role, "f")
    lo, hi = window.start, window.end_exclusive
    results: list[QueryResult] = []
    findings: list[Finding] = []
    checks: list[CheckSummary] = []

    def run(sql: str, params: list[object], purpose: str) -> pd.DataFrame:
        result = require_ok(
            engine.run_internal(sql.format(scope=scope), params, purpose=purpose), purpose
        )
        results.append(result)
        return result.frame

    def summarize(check: str, description: str, before: int) -> None:
        checks.append(
            CheckSummary(check=check, description=description, findings=len(findings) - before)
        )

    # 1. Duplicate attempt_ids in the raw table.
    n = len(findings)
    dups = run(_DUPLICATES, [lo, hi, *scope_params], "duplicate attempt_ids")
    if len(dups):
        dups["day"] = pd.to_datetime(dups["day"]).dt.date
        for start, end in _runs(list(dups["day"])):
            part = dups[(dups["day"] >= start) & (dups["day"] <= end)]
            raw, clean = int(part["raw_rows"].sum()), int(part["clean_rows"].sum())
            findings.append(
                Finding(
                    check="duplicates",
                    severity="warning",
                    title=f"Duplicate records on {start:%d %b}"
                    + ("" if start == end else f"–{end:%d %b}"),
                    detail=(
                        f"{raw - clean:,} attempts were ingested twice ({raw:,} raw rows vs "
                        f"{clean:,} unique attempts, +{(raw - clean) / clean:.1%}). "
                        "Paysight counts each attempt once."
                    ),
                    first_date=start,
                    last_date=end,
                    affected_metrics=COUNT_METRICS,
                    handled="removed in the clean view (first ingestion kept)",
                    rows=[
                        {
                            "day": str(r.day),
                            "raw_rows": int(r.raw_rows),
                            "clean_rows": int(r.clean_rows),
                        }
                        for r in part.itertuples()
                    ],
                )
            )
    summarize("duplicates", "Duplicate attempt_id values in the raw attempts table", n)

    # 2. Unexpected NULL issuer IDs (expected rate: 0).
    n = len(findings)
    nulls = run(_NULL_ISSUERS, [lo, hi, *scope_params, NULL_RATE_TOLERANCE], "null issuer ids")
    for r in nulls.itertuples():
        rate = r.null_issuer / r.attempts
        findings.append(
            Finding(
                check="null_rate",
                severity="warning",
                title=(
                    f"Missing issuer IDs: {r.acquirer_name} {r.method_family} attempts, {r.month}"
                ),
                detail=(
                    f"{rate:.1%} of {r.acquirer_name}'s {r.method_family} attempts in {r.month} "
                    f"({int(r.null_issuer):,} of {int(r.attempts):,}) have no issuer_id; "
                    "expected 0%. Issuer-level results for this scope are understated."
                ),
                affected_dimensions=["issuer_name", "issuer_type"],
                handled="not corrected: rows are kept and shown as '(missing)' issuer",
                rows=[{"acquirer": r.acquirer_name, "month": r.month, "null_rate": round(rate, 4)}],
            )
        )
    cons = run(_CONSISTENCY, [lo, hi, *scope_params], "field consistency").iloc[0]
    bad = {k: int(v or 0) for k, v in cons.items() if int(v or 0) > 0}
    if bad:
        findings.append(
            Finding(
                check="null_rate",
                severity="high",
                title="Inconsistent NULLs in status, failure code or card BIN",
                detail=", ".join(f"{k.replace('_', ' ')}: {v:,}" for k, v in bad.items()),
                affected_metrics=["failed_attempts", "attempt_success_rate"],
                handled="not corrected",
            )
        )
    summarize("null_rate", "NULL rates against expected (issuer_id 0%; codes and BINs by rule)", n)

    # 3. Orphan foreign keys.
    n = len(findings)
    orphans = run(_ORPHANS, [lo, hi, *scope_params], "orphan keys").iloc[0]
    bad = {k: int(v or 0) for k, v in orphans.items() if int(v or 0) > 0}
    if bad:
        findings.append(
            Finding(
                check="orphan_keys",
                severity="high",
                title="Attempts referencing unknown dimension keys",
                detail=", ".join(f"{k}: {v:,}" for k, v in bad.items()),
                affected_dimensions=list(bad),
                handled="not corrected: these rows show NULL dimension values",
            )
        )
    summarize("orphan_keys", "Foreign keys without a matching dimension row", n)

    # 4. Future or inconsistent timestamps.
    n = len(findings)
    ts = run(_TIMESTAMPS, [as_of + timedelta(days=1), lo, *scope_params], "timestamps").iloc[0]
    if int(ts["future_created"] or 0) or int(ts["ingested_before_created"] or 0):
        findings.append(
            Finding(
                check="timestamps",
                severity="high",
                title="Future or out-of-order timestamps",
                detail=(
                    f"{int(ts['future_created'] or 0):,} attempts after {as_of}; "
                    f"{int(ts['ingested_before_created'] or 0):,} ingested before creation."
                ),
                affected_metrics=COUNT_METRICS,
                handled="not corrected",
            )
        )
    summarize("timestamps", "created_at after AS_OF_DATE, or ingested before created", n)

    # 5. Ingestion-lag outliers (first ingestion only; re-ingestions are counted as duplicates).
    n = len(findings)
    lag = run(_INGEST_LAG, [lo, hi, *scope_params, INGEST_LAG_OUTLIER_S], "ingestion lag")
    if len(lag):
        findings.append(
            Finding(
                check="ingestion_lag",
                severity="info",
                title="Late-arriving records",
                detail=(
                    f"{int(lag['late_rows'].sum()):,} attempts arrived more than "
                    f"{INGEST_LAG_OUTLIER_S // 60} minutes after they happened."
                ),
                affected_metrics=COUNT_METRICS,
                handled="included; recent figures may still change",
            )
        )
    summarize(
        "ingestion_lag", f"First ingestion more than {INGEST_LAG_OUTLIER_S // 60} min late", n
    )

    # 6. Daily volume anomalies, against the same weekday's median over the whole dataset.
    n = len(findings)
    vol = run(_DAILY_VOLUME, list(scope_params), "daily volume")
    vol["day"] = pd.to_datetime(vol["day"]).dt.date
    vol["weekday"] = [d.weekday() for d in vol["day"]]
    vol["ratio"] = vol["attempts"] / vol.groupby("weekday")["attempts"].transform("median")
    vol["z"] = robust_z(vol["ratio"].to_numpy())
    flagged = vol[(vol["z"].abs() >= settings.anomaly_z_threshold) & (vol["day"] >= window.start)
                  & (vol["day"] <= window.end)]  # fmt: skip
    for direction, part in (
        ("spike", flagged[flagged["z"] > 0]),
        ("drop", flagged[flagged["z"] < 0]),
    ):
        for start, end in _runs(list(part["day"])):
            seg = part[(part["day"] >= start) & (part["day"] <= end)]
            change = float(seg["ratio"].mean()) - 1
            findings.append(
                Finding(
                    check="daily_volume",
                    severity="info" if direction == "spike" else "warning",
                    title=f"Volume {direction} {start:%d %b}–{end:%d %b}",
                    detail=(
                        f"Attempt volume was {change:+.0%} versus the usual level for those "
                        f"weekdays over {len(seg)} day(s). Real business events (sales) and data "
                        "gaps both look like this; check before comparing totals across this "
                        "period."
                    ),
                    first_date=start,
                    last_date=end,
                    affected_metrics=COUNT_METRICS,
                    handled="not a data error by itself; flagged for context",
                )
            )
    summarize("daily_volume", "Daily attempt volume versus same-weekday median (robust z)", n)

    return QualityReport(window_label=window.label(), checks=checks, findings=findings), results
