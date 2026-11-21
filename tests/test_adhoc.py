"""Ad-hoc SQL fallback (SPEC §9.6) end to end with the fake provider."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from paysight.config import get_settings
from paysight.llm.base import StructuredLLM
from paysight.llm.providers.fake import FakeProvider
from paysight.llm.replay import LLMClient
from paysight.orchestrator.models import AdhocSQL, AnswerDraft, IntentResult
from paysight.orchestrator.narrator import compact_bundle
from paysight.orchestrator.pipeline import Paysight
from paysight.safety.access import get_policies
from paysight.semantic.loader import get_semantic_layer

TW = "created_at >= TIMESTAMP '2026-10-01' AND created_at < TIMESTAMP '2026-11-01'"
SHARE = "SELECT AVG(CASE WHEN attempt_count > 1 THEN 1.0 ELSE 0.0 END) AS retry_share"
NO_TIME = f"{SHARE} FROM v_orders"
GOOD = f"{NO_TIME} WHERE {TW}"

CUSTOM = IntentResult(
    question_type="metric_lookup",
    restatement="Share of October orders that needed a retry.",
    time_expr="month:2026-10",
    needs_custom_sql=True,
    custom_sql_reason="share of orders with more than one attempt is not a catalog metric",
)
NOT_EXPRESSIBLE = IntentResult(
    question_type="metric_lookup",
    restatement="Orders by issuer in October.",
    metrics=["orders"],
    dimensions=["issuer_name"],  # issuer_name is not available on v_orders -> CompileError
    time_expr="month:2026-10",
)


def _build(engine, tmp_path: Path, intent: IntentResult, sqls: list[str]):
    seen: dict[str, list[str]] = {"adhoc_prompts": []}
    queue = list(sqls)

    def handler(system: str, user: str, schema: type[BaseModel]):
        if schema is IntentResult:
            return intent.model_dump()
        if schema is AdhocSQL:
            seen["adhoc_prompts"].append(system)
            return AdhocSQL(
                sql=queue.pop(0), explanation="Share of orders with retries."
            ).model_dump()
        bundle = json.loads(user.split("Evidence bundle (JSON):", 1)[1].strip())
        fact = {"text": "See the custom query result.", "evidence_ids": [bundle[0]["id"]]}
        return AnswerDraft(headline=fact["text"], facts=[fact]).model_dump()

    fake = FakeProvider(handler)
    llm = LLMClient(
        mode="live",
        cassette_dir=tmp_path / "c",
        provider_name="fake",
        model="fake",
        llm=StructuredLLM(fake, max_attempts=1, wait_max_s=0),
    )
    app = Paysight(
        settings=get_settings(),
        layer=get_semantic_layer(),
        engine=engine,
        llm=llm,
        policies=get_policies(),
        runs_dir=tmp_path / "runs",
    )
    return app, fake, seen


def test_first_sql_blocked_then_repaired(small_engine, tmp_path) -> None:
    app, _, seen = _build(small_engine, tmp_path, CUSTOM, [NO_TIME, GOOD])
    result = app.ask("What share of October orders needed more than one attempt?", "leadership")
    assert result.kind == "answer", result.message
    adhoc = next(e for e in result.evidence if e.ad_hoc)
    assert [v.passed for v in adhoc.validation] == [False, True]
    assert adhoc.validation[0].failures[0].name == "time_window"
    assert adhoc.meta["repair_attempts"] == 1
    # Process metadata never reaches the narrator as a number it could state as a fact.
    assert "repair_attempts" not in compact_bundle(result.evidence)
    assert 0 < adhoc.key_numbers["retry_share"] < 0.2
    assert "YOUR PREVIOUS SQL FAILED VALIDATION" in seen["adhoc_prompts"][1]
    assert result.confidence.level != "High"
    assert result.sql_source == "ad_hoc"
    assert any("custom SQL written by the AI" in c for c in result.caveats)
    assert [s.name for s in result.plan][:1] == ["adhoc_sql"]
    trace = json.loads(Path(result.trace_path).read_text())
    assert [s["name"] for s in trace["steps"] if s["name"].startswith("adhoc")] == [
        "adhoc_sql",
        "adhoc_repair",
    ]


def test_sql_that_still_fails_after_repair_is_an_error(small_engine, tmp_path) -> None:
    app, _, _ = _build(small_engine, tmp_path, CUSTOM, [NO_TIME, "DELETE FROM v_orders"])
    result = app.ask("What share of October orders needed more than one attempt?", "leadership")
    assert result.kind == "error"
    assert "could not pass validation after one repair" in result.message
    assert not any(e.ad_hoc for e in result.evidence) and result.answer is None


def test_compile_error_falls_back_to_adhoc(small_engine, tmp_path) -> None:
    sql = (
        "SELECT a.issuer_name, COUNT(DISTINCT a.order_id) AS orders FROM v_attempts_clean a "
        f"WHERE a.{TW.replace(' AND created_at', ' AND a.created_at')} GROUP BY 1 ORDER BY 2 DESC"
    )
    app, _, _ = _build(small_engine, tmp_path, NOT_EXPRESSIBLE, [sql])
    result = app.ask("How many orders did each issuer handle in October?", "leadership")
    assert result.kind == "answer", result.message
    assert result.evidence[0].ad_hoc is True


@pytest.mark.parametrize(("role", "visible"), [("leadership", False), ("risk_analyst", True)])
def test_schema_hides_columns_the_role_cannot_use(small_engine, tmp_path, role, visible) -> None:
    app, _, seen = _build(small_engine, tmp_path, CUSTOM, [GOOD])
    app.ask("What share of October orders needed more than one attempt?", role)
    assert bool(re.search(r"\bcard_bin\b", seen["adhoc_prompts"][0])) is visible


def test_resolved_entities_reach_the_sql_writer(small_engine, tmp_path) -> None:
    intent = IntentResult(
        **CUSTOM.model_dump(exclude={"filters"}),
        filters=[{"dimension": "merchant_group", "op": "=", "values": ["novamart"]}],
    )
    app, _, seen = _build(small_engine, tmp_path, intent, [GOOD])
    result = app.ask("What share of NovaMart's October orders needed a retry?", "leadership")
    assert result.kind == "answer", result.message
    # Resolved against the data ("novamart" -> "NovaMart") and named with its column.
    assert (
        "merchant_group = 'NovaMart' (column: v_attempts_clean.merchant_group"
        in (seen["adhoc_prompts"][0])
    )
    assert result.evidence[0].filters == ["merchant_group = NovaMart"]


def test_llm_failure_while_writing_sql_is_reported_not_raised(small_engine, tmp_path) -> None:
    from paysight.llm.base import LLMUnavailableError

    app, fake, _ = _build(small_engine, tmp_path, CUSTOM, [GOOD])
    original = fake._handler

    def failing(system: str, user: str, schema: type[BaseModel]):
        if schema is AdhocSQL:
            return LLMUnavailableError("provider down")
        return original(system, user, schema)

    fake._handler = failing
    result = app.ask("What share of October orders needed more than one attempt?", "leadership")
    assert result.kind == "error"
    assert result.message.startswith("The analysis could not be completed")
