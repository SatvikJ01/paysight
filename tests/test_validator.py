"""SQL validator attack corpus and executor hardening (SPEC §9.4, §9.5, §16.1)."""

from __future__ import annotations

import duckdb
import pytest

from paysight.config import get_settings
from paysight.execution.engine import Engine
from paysight.safety.access import RolePolicy
from paysight.safety.validator import CHECK_NAMES, validate_sql
from paysight.semantic.loader import get_semantic_layer

TW = "created_at >= TIMESTAMP '2026-10-01' AND created_at < TIMESTAMP '2026-10-08'"
TW_O = TW.replace("created_at", "o.created_at")

# (case id, sql, check that must fail). Role: leadership unless the id says otherwise.
ATTACKS: list[tuple[str, str, str]] = [
    # DDL / DML and other non-query statements
    ("drop", "DROP TABLE fact_payment_attempts", "read_only_query"),
    ("delete", "DELETE FROM fact_payment_attempts WHERE 1 = 1", "read_only_query"),
    ("update", "UPDATE v_orders SET order_status = 'SUCCESS'", "read_only_query"),
    ("insert", "INSERT INTO fact_settlements VALUES ('x')", "read_only_query"),
    ("create_as", "CREATE TABLE stolen AS SELECT * FROM v_orders", "read_only_query"),
    ("alter", "ALTER TABLE dim_merchant ADD COLUMN x INTEGER", "read_only_query"),
    (
        "merge",
        "MERGE INTO dim_merchant t USING dim_merchant s ON t.merchant_id = s.merchant_id "
        "WHEN MATCHED THEN DELETE",
        "read_only_query",
    ),
    ("copy", "COPY v_orders TO 'out.csv'", "read_only_query"),
    ("attach", "ATTACH 'evil.db' AS evil", "read_only_query"),
    ("detach", "DETACH evil", "read_only_query"),
    ("pragma", "PRAGMA database_list", "read_only_query"),
    ("set", "SET enable_external_access = true", "read_only_query"),
    ("reset", "RESET threads", "read_only_query"),
    ("install", "INSTALL httpfs", "read_only_query"),
    ("load", "LOAD httpfs", "read_only_query"),
    ("call", "CALL pragma_version()", "read_only_query"),
    ("checkpoint", "CHECKPOINT", "read_only_query"),
    ("vacuum", "VACUUM", "read_only_query"),
    ("export", "EXPORT DATABASE 'dump'", "single_statement"),
    ("describe", "DESCRIBE v_orders", "read_only_query"),
    # Multiple statements, comment and casing tricks
    (
        "stacked",
        f"SELECT COUNT(*) FROM v_orders WHERE {TW}; DROP TABLE dim_merchant",
        "single_statement",
    ),
    (
        "comment_stack",
        f"SELECT COUNT(*) FROM v_orders WHERE {TW} /* harmless */;\n-- x\nDELETE FROM dim_merchant",
        "single_statement",
    ),
    ("casing_raw", f"sElEcT CoUnT(*) FrOm FACT_PAYMENT_ATTEMPTS WHERE {TW}", "tables_allowed"),
    ("quoted_raw", f'SELECT COUNT(*) FROM "fact_payment_attempts" WHERE {TW}', "tables_allowed"),
    ("empty", "   -- just a comment", "single_statement"),
    # File, network and catalog functions
    ("read_csv", "SELECT * FROM read_csv('/etc/passwd')", "functions_allowed"),
    ("read_csv_auto", "SELECT * FROM read_csv_auto('data/x.csv')", "functions_allowed"),
    ("read_parquet", "SELECT * FROM read_parquet('x.parquet')", "functions_allowed"),
    ("read_json", "SELECT * FROM read_json('x.json')", "functions_allowed"),
    ("glob", "SELECT * FROM glob('*')", "functions_allowed"),
    (
        "read_text_scalar",
        f"SELECT read_text('/etc/passwd') FROM v_orders WHERE {TW}",
        "functions_allowed",
    ),
    ("duckdb_settings", "SELECT * FROM duckdb_settings()", "functions_allowed"),
    ("getenv", f"SELECT getenv('HOME') FROM v_orders WHERE {TW}", "functions_allowed"),
    ("file_as_table", "SELECT * FROM 'data/paysight.duckdb'", "tables_allowed"),
    # Forbidden tables
    ("raw_dim", "SELECT * FROM dim_merchant", "tables_allowed"),
    ("information_schema", "SELECT * FROM information_schema.tables", "tables_allowed"),
    (
        "union_raw",
        f"SELECT order_id FROM v_orders WHERE {TW} "
        "UNION ALL SELECT attempt_id FROM fact_payment_attempts",
        "tables_allowed",
    ),
    ("cte_shadow", "WITH v_orders AS (SELECT 1 AS x) SELECT x FROM v_orders", "tables_allowed"),
    (
        "subquery_raw",
        f"SELECT COUNT(*) FROM v_orders WHERE {TW} AND order_id IN "
        "(SELECT order_id FROM fact_payment_attempts)",
        "tables_allowed",
    ),
    # PII and SELECT *
    ("pii_select", f"SELECT card_bin FROM v_attempts_clean WHERE {TW}", "columns_allowed"),
    (
        "pii_union",
        f"SELECT order_id FROM v_orders WHERE {TW} "
        f"UNION SELECT card_bin FROM v_attempts_clean WHERE {TW}",
        "columns_allowed",
    ),
    (
        "pii_subquery",
        f"SELECT COUNT(*) FROM (SELECT card_bin FROM v_attempts_clean WHERE {TW}) t",
        "columns_allowed",
    ),
    (
        "pii_cte",
        f"WITH x AS (SELECT customer_ref_hash FROM v_attempts_clean WHERE {TW}) "
        "SELECT COUNT(*) FROM x",
        "columns_allowed",
    ),
    (
        "pii_where",
        f"SELECT COUNT(*) FROM v_attempts_clean WHERE {TW} AND card_bin LIKE '91%'",
        "columns_allowed",
    ),
    (
        "pii_aggregate",
        f"SELECT COUNT(DISTINCT customer_ref_hash) FROM v_attempts_clean WHERE {TW}",
        "columns_allowed",
    ),
    ("star_fact", f"SELECT * FROM v_attempts_clean WHERE {TW}", "columns_allowed"),
    (
        "alias_star_fact",
        f"SELECT a.* FROM v_attempts_clean a WHERE {TW.replace('created', 'a.created')}",
        "columns_allowed",
    ),
    ("unknown_column", f"SELECT secret_column FROM v_orders WHERE {TW}", "columns_allowed"),
    # Time window
    ("no_time_filter", "SELECT COUNT(*) FROM v_orders", "time_window"),
    (
        "lower_bound_only",
        "SELECT COUNT(*) FROM v_orders WHERE created_at >= TIMESTAMP '2026-10-01'",
        "time_window",
    ),
    (
        "too_long",
        "SELECT COUNT(*) FROM v_orders WHERE created_at >= TIMESTAMP '2025-01-01' "
        "AND created_at < TIMESTAMP '2026-10-01'",
        "time_window",
    ),
    (
        "non_literal_bound",
        "SELECT COUNT(*) FROM v_orders WHERE created_at >= now() - INTERVAL 7 DAY "
        "AND created_at < now()",
        "time_window",
    ),
    (
        "time_inside_or",
        f"SELECT COUNT(*) FROM v_orders WHERE ({TW}) OR region = 'north'",
        "time_window",
    ),
    (
        "subquery_no_time",
        f"SELECT COUNT(*) FROM v_orders WHERE {TW} AND merchant_id IN "
        "(SELECT merchant_id FROM v_attempts_clean)",
        "time_window",
    ),
    # Cost guards
    (
        "cross_join",
        f"SELECT COUNT(*) FROM v_orders o CROSS JOIN v_settlements s WHERE {TW_O}",
        "join_guards",
    ),
    ("comma_join", f"SELECT COUNT(*) FROM v_orders o, v_settlements s WHERE {TW_O}", "join_guards"),
    (
        "natural_join",
        f"SELECT COUNT(*) FROM v_orders o NATURAL JOIN v_settlements s WHERE {TW_O}",
        "join_guards",
    ),
    (
        "too_many_joins",
        "SELECT COUNT(*) FROM v_settlements a "
        + " ".join(
            f"JOIN v_settlements s{i} ON a.merchant_id = s{i}.merchant_id" for i in range(5)
        ),
        "join_guards",
    ),
    (
        "non_literal_limit",
        f"SELECT order_id FROM v_orders WHERE {TW} LIMIT (SELECT 5)",
        "row_limit",
    ),
]


@pytest.mark.parametrize(("case", "sql", "failing_check"), ATTACKS, ids=[a[0] for a in ATTACKS])
def test_attack_is_blocked(
    small_engine: Engine, policies: dict[str, RolePolicy], case: str, sql: str, failing_check: str
) -> None:
    result = small_engine.run_sql(sql, policies["leadership"])
    assert result.status == "blocked", f"{case} was not blocked"
    assert result.frame is None and result.sql is None
    failed = {c.name for c in result.report.failures}
    assert failing_check in failed, f"{case}: expected {failing_check} to fail, got {failed}"


def test_corpus_is_large_enough() -> None:
    assert len(ATTACKS) >= 30


def test_report_always_lists_every_check(small_engine: Engine, policies: dict) -> None:
    result = small_engine.run_sql("DROP TABLE x", policies["leadership"])
    names = [c.name for c in result.report.checks]
    assert names == [*CHECK_NAMES, "cardinality_estimate", "execution_guard"]


# --------------------------------------------------------------------------------------------
# Allowed queries and rewrites
# --------------------------------------------------------------------------------------------


def test_valid_query_runs(small_engine: Engine, policies: dict) -> None:
    result = small_engine.run_sql(
        f"SELECT payment_method, COUNT(*) AS n FROM v_attempts_clean WHERE {TW} GROUP BY 1",
        policies["leadership"],
    )
    assert result.ok, result.report.failures
    assert result.row_count == 6
    assert all(c.passed for c in result.report.checks)


def test_risk_analyst_may_use_pii(small_engine: Engine, policies: dict) -> None:
    sql = (
        f"SELECT card_bin, COUNT(*) AS n FROM v_attempts_clean WHERE {TW} "
        "AND card_bin IS NOT NULL GROUP BY 1"
    )
    assert small_engine.run_sql(sql, policies["risk_analyst"]).ok
    assert small_engine.run_sql(sql, policies["analyst_north"]).status == "blocked"


def test_missing_limit_is_injected(small_engine: Engine, policies: dict) -> None:
    full = "created_at >= TIMESTAMP '2026-08-01' AND created_at < TIMESTAMP '2026-11-01'"
    result = small_engine.run_sql(
        f"SELECT order_id FROM v_orders WHERE {full}", policies["leadership"]
    )
    assert result.ok
    assert "LIMIT 10000" in result.sql
    assert result.row_count == 10_000


def test_oversized_limit_is_capped(small_engine: Engine, policies: dict) -> None:
    result = small_engine.run_sql(
        f"SELECT order_id FROM v_orders WHERE {TW} LIMIT 999999", policies["leadership"]
    )
    assert result.ok
    assert "LIMIT 10000" in result.sql and "999999" not in result.sql
    assert any("capped" in c.detail for c in result.report.checks)


def test_table_functions_on_allowlist_pass(small_engine: Engine, policies: dict) -> None:
    result = small_engine.run_sql("SELECT * FROM range(3)", policies["leadership"])
    assert result.ok and result.row_count == 3


def _regions(small_engine: Engine, role: RolePolicy, sql: str) -> set[str]:
    result = small_engine.run_sql(sql, role)
    assert result.ok, result.report.failures
    return set(result.frame.iloc[:, 0])


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT region, COUNT(*) FROM v_attempts_clean WHERE {TW} GROUP BY 1",
        "SELECT region, COUNT(*) FROM v_settlements GROUP BY 1",
        f"SELECT region FROM v_orders WHERE {TW} AND (region = 'north' OR 1 = 1) GROUP BY 1",
        f"WITH x AS (SELECT region FROM v_attempts_clean WHERE {TW}) "
        "SELECT region FROM x GROUP BY 1",
        f"SELECT region FROM v_orders WHERE {TW} GROUP BY 1 "
        f"UNION SELECT region FROM v_attempts_clean WHERE {TW} GROUP BY 1",
        f"SELECT o.region FROM v_orders o JOIN v_settlements s ON o.merchant_id = s.merchant_id "
        f"WHERE {TW_O} GROUP BY 1",
        f"SELECT REGION FROM V_ATTEMPTS_CLEAN WHERE {TW} GROUP BY 1",
    ],
    ids=["attempts", "settlements", "or_escape", "cte", "union", "join", "casing"],
)
def test_row_filter_rewrite_really_filters(small_engine: Engine, policies: dict, sql: str) -> None:
    assert _regions(small_engine, policies["analyst_north"], sql) == {"north"}
    assert len(_regions(small_engine, policies["leadership"], sql)) == 4


def test_row_filter_cannot_be_bypassed_by_asking_for_south(
    small_engine: Engine, policies: dict
) -> None:
    result = small_engine.run_sql(
        f"SELECT COUNT(*) AS n FROM v_attempts_clean WHERE {TW} AND region = 'south'",
        policies["analyst_north"],
    )
    assert result.ok and int(result.frame["n"].iloc[0]) == 0
    assert "region IN ('north')" in result.sql


def test_validation_alone_rewrites_for_north(small_engine: Engine, policies: dict) -> None:
    report = validate_sql(
        f"SELECT COUNT(*) FROM v_orders AS o WHERE {TW_O}",
        role=policies["analyst_north"],
        layer=get_semantic_layer(),
        catalog=small_engine.catalog,
        limits=get_settings().limits,
    )
    assert report.passed
    assert (
        "FROM (\n  SELECT\n    *\n  FROM v_orders\n  WHERE\n    region IN ('north')\n) AS o"
        in report.sql_out
    )


# --------------------------------------------------------------------------------------------
# Executor hardening, cache and timeout
# --------------------------------------------------------------------------------------------


def test_connection_is_hardened_even_without_validator(small_engine: Engine) -> None:
    con = small_engine._con  # bypass the validator on purpose
    with pytest.raises(duckdb.Error):
        con.execute("SELECT * FROM read_csv('pyproject.toml')").fetchall()
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE t AS SELECT 1")


def test_repeat_query_hits_cache(small_engine: Engine, policies: dict) -> None:
    sql = f"SELECT channel, COUNT(*) AS n FROM v_attempts_clean WHERE {TW} GROUP BY 1 ORDER BY 1"
    first = small_engine.run_sql(sql, policies["leadership"])
    second = small_engine.run_sql(sql + "  ", policies["leadership"])
    assert first.ok and second.ok
    assert second.cache_hit
    assert first.frame.equals(second.frame)


def test_cardinality_guard_blocks_huge_estimates(small_dataset, policies: dict) -> None:
    limits = get_settings().limits.model_copy(update={"max_estimated_rows": 10})
    engine = Engine(small_dataset.path, layer=get_semantic_layer(), limits=limits)
    try:
        result = engine.run_sql(f"SELECT COUNT(*) FROM v_orders WHERE {TW}", policies["leadership"])
    finally:
        engine.close()
    assert result.status == "blocked"
    assert result.report.checks[-2].name == "cardinality_estimate"
    assert result.report.checks[-2].status == "fail"


def test_timeout_interrupts_long_query(small_dataset, policies: dict) -> None:
    limits = get_settings().limits.model_copy(
        update={"query_timeout_s": 0.2, "max_estimated_rows": 10**15}
    )
    engine = Engine(small_dataset.path, layer=get_semantic_layer(), limits=limits)
    slow = (
        "SELECT COUNT(*) FROM v_attempts_clean a JOIN v_attempts_clean b "
        "ON a.merchant_id = b.merchant_id JOIN v_attempts_clean c ON b.merchant_id = c.merchant_id "
        "WHERE a.created_at >= TIMESTAMP '2026-08-01' AND a.created_at < TIMESTAMP '2026-11-01' "
        "AND b.created_at >= TIMESTAMP '2026-08-01' AND b.created_at < TIMESTAMP '2026-11-01' "
        "AND c.created_at >= TIMESTAMP '2026-08-01' AND c.created_at < TIMESTAMP '2026-11-01'"
    )
    try:
        result = engine.run_sql(slow, policies["leadership"])
        assert result.status == "timeout"
        assert result.report.checks[-1].name == "execution_guard"
        # The connection is still usable afterwards.
        assert engine.run_sql(
            f"SELECT COUNT(*) FROM v_orders WHERE {TW}", policies["leadership"]
        ).ok
    finally:
        engine.close()
