"""Compiler snapshot tests and round trips through the validator (SPEC §7.6).

Snapshots live in ``tests/snapshots/compiler``. To update them after an intended change, run
``UPDATE_SNAPSHOTS=1 uv run pytest tests/test_compiler.py`` and review the diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from paysight.execution.engine import Engine
from paysight.semantic.compiler import CompileError, compile_query
from paysight.semantic.loader import get_semantic_layer
from paysight.semantic.models import QuerySpec

SNAPSHOTS = Path(__file__).parent / "snapshots" / "compiler"
HERO = {"start": "2026-10-18", "end": "2026-10-31"}
HERO_BASE = {"start": "2026-10-04", "end": "2026-10-17"}

SPECS: dict[str, dict] = {
    "hero_compare_by_method": {
        "metrics": ["attempt_success_rate"],
        "dimensions": ["payment_method"],
        "time_window": HERO,
        "compare_window": HERO_BASE,
    },
    "kestrel_cards_daily": {
        "metrics": ["attempt_success_rate", "failed_attempts"],
        "filters": [
            {"dimension": "acquirer_name", "op": "=", "values": ["Kestrel Bank"]},
            {"dimension": "method_family", "op": "in", "values": ["card"]},
        ],
        "time_window": {"start": "2026-09-10", "end": "2026-09-20"},
        "granularity": "day",
    },
    "order_sr_by_channel_evening": {
        "metrics": ["order_success_rate", "orders"],
        "dimensions": ["channel"],
        "filters": [{"dimension": "hour_of_day", "op": "between", "values": [19, 22]}],
        "time_window": {"start": "2026-09-01", "end": "2026-09-10"},
    },
    "settlements_by_group": {
        "metrics": ["settled_gross_inr", "settled_net_inr"],
        "dimensions": ["merchant_group"],
        "filters": [{"dimension": "merchant_group", "op": "!=", "values": ["StellarFuel"]}],
        "time_window": {"start": "2026-10-25", "end": "2026-10-31"},
    },
    "top_merchants_failed": {
        "metrics": ["failed_attempts", "p95_latency_ms"],
        "dimensions": ["merchant_name"],
        "filters": [{"dimension": "payment_method", "op": "not_in", "values": ["UPI", "WALLET"]}],
        "time_window": {"start": "2026-10-25", "end": "2026-10-31"},
        "order_by": [{"field": "failed_attempts", "descending": True}],
        "limit": 10,
    },
    "non_adjacent_compare": {
        "metrics": ["gmv_inr", "avg_ticket_inr"],
        "time_window": {"start": "2026-10-01", "end": "2026-10-31"},
        "compare_window": {"start": "2026-08-01", "end": "2026-08-31"},
    },
    "injection_looking_value": {
        "metrics": ["attempts"],
        "filters": [
            {"dimension": "merchant_name", "op": "=", "values": ["x' OR 1=1; DROP TABLE t --"]}
        ],
        "time_window": {"start": "2026-10-01", "end": "2026-10-07"},
    },
}


@pytest.mark.parametrize("name", list(SPECS))
def test_compiler_snapshot(name: str) -> None:
    sql = compile_query(QuerySpec(**SPECS[name]), get_semantic_layer()).sql + "\n"
    path = SNAPSHOTS / f"{name}.sql"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sql, encoding="utf-8")
    if not path.exists():
        pytest.fail(f"missing snapshot {path.name}; run with UPDATE_SNAPSHOTS=1 and review it")
    assert sql == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", list(SPECS))
def test_compiled_sql_passes_validator_and_runs(
    small_engine: Engine, policies: dict, name: str
) -> None:
    _, result = small_engine.run_spec(QuerySpec(**SPECS[name]), policies["leadership"])
    assert result.ok, [c.detail for c in result.report.failures]


def test_compilation_is_deterministic() -> None:
    layer = get_semantic_layer()
    spec = QuerySpec(**SPECS["hero_compare_by_method"])
    assert compile_query(spec, layer).sql == compile_query(spec, layer).sql


def test_injection_value_stays_a_literal(small_engine: Engine, policies: dict) -> None:
    compiled, result = small_engine.run_spec(
        QuerySpec(**SPECS["injection_looking_value"]), policies["leadership"]
    )
    assert "'x'' OR 1=1; DROP TABLE t --'" in compiled.sql
    assert result.ok and int(result.frame["attempts"].iloc[0]) == 0


def test_rates_expose_numerator_and_denominator(small_engine: Engine, policies: dict) -> None:
    compiled, result = small_engine.run_spec(
        QuerySpec(**SPECS["hero_compare_by_method"]), policies["leadership"]
    )
    cols = compiled.metric_columns["attempt_success_rate"]
    assert set(cols) <= set(result.frame.columns)
    frame = result.frame
    assert (frame["attempt_success_rate"] - frame["attempt_success_rate_num"]
            / frame["attempt_success_rate_den"]).abs().max() < 1e-12  # fmt: skip
    assert set(frame["comparison_window"]) == {"baseline", "current"}


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"metrics": ["attempts", "orders"]}, "span several views"),
        ({"metrics": ["no_such_metric"]}, "unknown metric"),
        ({"metrics": ["orders"], "dimensions": ["issuer_name"]}, "not available on v_orders"),
        ({"metrics": ["attempts"], "dimensions": ["bogus"]}, "unknown dimension"),
        (
            {
                "metrics": ["attempts"],
                "filters": [{"dimension": "channel", "op": "between", "values": ["a", "b"]}],
            },
            "not allowed",
        ),
        ({"metrics": ["attempts"], "order_by": [{"field": "gmv_inr"}]}, "cannot order by"),
    ],
)
def test_compile_errors(spec: dict, message: str) -> None:
    spec = {"time_window": {"start": "2026-10-01", "end": "2026-10-07"}, **spec}
    with pytest.raises(CompileError, match=message):
        compile_query(QuerySpec(**spec), get_semantic_layer())


def test_time_window_is_required() -> None:
    with pytest.raises(ValueError, match="time_window"):
        QuerySpec(metrics=["attempts"])  # type: ignore[call-arg]


def test_negative_filters_keep_null_dimension_values(small_engine: Engine, policies: dict) -> None:
    """'Everything except StellarFuel' must keep merchants with no group (NULL)."""
    window = {"start": "2026-08-01", "end": "2026-10-31"}
    role = policies["leadership"]
    _, everything = small_engine.run_spec(
        QuerySpec(metrics=["settled_gross_inr"], time_window=window), role
    )
    _, stellar = small_engine.run_spec(
        QuerySpec(
            metrics=["settled_gross_inr"],
            filters=[{"dimension": "merchant_group", "op": "=", "values": ["StellarFuel"]}],
            time_window=window,
        ),
        role,
    )
    for op, values in (("!=", ["StellarFuel"]), ("not_in", ["StellarFuel"])):
        _, rest = small_engine.run_spec(
            QuerySpec(
                metrics=["settled_gross_inr"],
                filters=[{"dimension": "merchant_group", "op": op, "values": values}],
                time_window=window,
            ),
            role,
        )
        total = float(everything.frame["settled_gross_inr"].iloc[0])
        part = float(stellar.frame["settled_gross_inr"].iloc[0])
        assert float(rest.frame["settled_gross_inr"].iloc[0]) == pytest.approx(total - part)


def test_explicit_order_gets_deterministic_tie_breakers() -> None:
    sql = compile_query(QuerySpec(**SPECS["top_merchants_failed"]), get_semantic_layer()).sql
    order_clause = sql.split("ORDER BY", 1)[1]
    assert order_clause.index("failed_attempts DESC") < order_clause.index("merchant_name")
