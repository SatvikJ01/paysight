"""A deliberately naive text-to-SQL baseline (SPEC §14.2).

One LLM call turns the question and the raw view schema into SQL; the SQL runs under the same
validator and role filters as Paysight; one more LLM call writes the answer from the rows. No
semantic layer, playbooks, decomposition, data-quality scan, access pre-check or grounding check.
It is given the same basic SQL rules as Paysight's ad-hoc prompt, so that its results measure
analysis quality rather than knowledge of validator rules (DECISIONS.md D33).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from paysight.execution.engine import Engine, QueryResult
from paysight.llm.base import Usage
from paysight.llm.prompts import load_prompt
from paysight.llm.replay import LLMClient
from paysight.orchestrator.playbooks import _clean
from paysight.safety.access import RolePolicy


class BaselineSQL(BaseModel):
    """The baseline's SQL (or none, with a note)."""

    sql: str | None
    note: str


class BaselineAnswer(BaseModel):
    """The baseline's final answer."""

    status: Literal["answered", "refused", "needs_clarification", "access_denied", "failed"]
    answer: str


@dataclass
class BaselineResult:
    """Everything the scorer needs from one baseline run."""

    status: str
    answer: str
    sql: str | None
    query: QueryResult | None
    rows: list[dict] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    latency_ms: float = 0.0


def raw_schema(engine: Engine, role: RolePolicy) -> tuple[str, str]:
    """View names and their columns with types, with no descriptions or metric definitions."""
    views = [v for v in role.allowed_views if v in engine.catalog]
    lines = [f"{v}({', '.join(f'{c} {t}' for c, t in engine.catalog[v].items())})" for v in views]
    return ", ".join(views), "\n".join(lines)


def run_baseline(
    engine: Engine, llm: LLMClient, role: RolePolicy, question: str, *, as_of: str
) -> BaselineResult:
    """Two LLM calls around one validated query."""
    t0 = time.perf_counter()
    views, schema = raw_schema(engine, role)
    draft, usage_sql = llm.complete(
        load_prompt("baseline_sql"),
        BaselineSQL,
        question=question.strip(),
        views=views,
        table_schema=schema,
        as_of=as_of,
    )
    query = engine.run_sql(draft.sql, role) if draft.sql else None
    rows = (
        [_clean(r) for r in query.frame.head(50).to_dict("records")] if query and query.ok else []
    )
    if query is None:
        status = f"no query was run ({draft.note})"
    elif query.ok:
        status = f"ok, {query.row_count} rows"
    else:
        status = "blocked: " + "; ".join(f"{c.name}: {c.detail}" for c in query.report.failures)
    answer, usage_ans = llm.complete(
        load_prompt("baseline_answer"),
        BaselineAnswer,
        question=question.strip(),
        sql=draft.sql or "(none)",
        query_status=status,
        rows=json.dumps(rows, default=str, ensure_ascii=False),
    )
    return BaselineResult(
        status=answer.status,
        answer=answer.answer,
        sql=draft.sql,
        query=query,
        rows=rows,
        usages=[usage_sql, usage_ans],
        latency_ms=(time.perf_counter() - t0) * 1000,
    )
