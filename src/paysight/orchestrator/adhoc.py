"""Ad-hoc SQL fallback (SPEC §9.6), for questions the semantic layer cannot express.

The LLM writes SQL against a compact schema of the views the role may query; columns the role
may not see are left out of the schema. The SQL goes through the same validator as compiled
queries. If it fails validation, the LLM gets one repair attempt with the exact failures. The
evidence is tagged ``ad_hoc``, which lowers confidence, and keeps every attempt's validation
report so the UI can show the failed attempt next to the repaired one.
"""

from __future__ import annotations

from paysight.execution.engine import QueryResult
from paysight.llm.prompts import load_prompt
from paysight.llm.replay import LLMClient
from paysight.observability.trace import Trace
from paysight.orchestrator.models import AdhocSQL, Evidence
from paysight.orchestrator.playbooks import PlanError, RunContext, _clean

ADHOC_CAVEAT = (
    "Part of this answer uses custom SQL written by the AI, outside the governed semantic layer. "
    "It was validated (read-only, governed views only, role filters applied) before running, "
    "but its logic has not been reviewed like the governed metrics."
)


def view_schema(ctx: RunContext) -> tuple[str, str]:
    """(comma-separated view names, compact schema text) for the role."""
    denied = set(ctx.role.denied_sensitivities)
    lines = []
    views = [v for v in ctx.role.allowed_views if v in ctx.engine.catalog]
    for view in views:
        cols = [
            f"{c} {t}"
            for c, t in ctx.engine.catalog[view].items()
            if ctx.layer.column_sensitivity(view, c) not in denied
        ]
        desc = ctx.layer.views[view].description
        lines.append(f"{view}: {desc}\n  columns: {', '.join(cols)}")
    return ", ".join(views), "\n".join(lines)


def _failures(result: QueryResult) -> str:
    reasons = [f"{c.name}: {c.detail}" for c in result.report.failures]
    return "; ".join(reasons) or result.error or result.status


def run_adhoc(
    ctx: RunContext, llm: LLMClient, trace: Trace, *, question: str, reason: str
) -> Evidence:
    """Ask the LLM for SQL, validate and run it (one repair), and record ad-hoc evidence."""
    views, schema = view_schema(ctx)
    prompt = load_prompt("adhoc_sql")
    as_of = ctx.settings.data.as_of_date.isoformat()
    attempts: list[tuple[AdhocSQL, QueryResult]] = []
    errors = ""
    for attempt in (1, 2):
        with trace.step("adhoc_sql" if attempt == 1 else "adhoc_repair") as step:
            draft, usage = llm.complete(
                prompt,
                AdhocSQL,
                question=question.strip(),
                views=views,
                view_schema=schema,
                as_of=as_of,
                errors=errors,
            )
            trace.add_usage(step, usage)
            result = ctx.engine.run_sql(draft.sql, ctx.role)
            attempts.append((draft, result))
            step.summary = "validated and ran" if result.ok else f"blocked: {_failures(result)}"
            step.details = {
                "sql": draft.sql,
                "checks": [c.model_dump() for c in result.report.checks],
            }
            if result.ok:
                break
            step.status = "blocked"
            errors = (
                "\nYOUR PREVIOUS SQL FAILED VALIDATION. Fix every problem and return "
                "corrected SQL.\n"
                f"Previous SQL:\n{draft.sql}\nFailures: {_failures(result)}\n"
            )
    draft, result = attempts[-1]
    if not result.ok:
        raise PlanError(
            f"custom SQL could not pass validation after one repair: {_failures(result)}"
        )

    ctx.caveats.append(ADHOC_CAVEAT)
    ctx.queries += 0 if result.cache_hit else 1
    rows = [_clean(r) for r in result.frame.head(50).to_dict("records")]
    key = dict(rows[0]) if len(rows) == 1 else {}
    key = {k: v for k, v in key.items() if isinstance(v, int | float)}
    key["repair_attempts"] = len(attempts) - 1
    evidence = Evidence(
        id=f"E{len(ctx.evidence) + 1}",
        title="Custom SQL (AI-written, validated)",
        kind="query",
        purpose=f"{draft.explanation} Why custom SQL: {reason}",
        sql=[a[1].report.sql_out or a[1].report.sql_in for a in attempts],
        validation=[a[1].report for a in attempts],
        runtime_ms=sum(a[1].runtime_ms for a in attempts),
        cache_hits=int(result.cache_hit),
        datasets=[v for v in ctx.engine.catalog if v in (result.sql or "")],
        filters=[],
        time_window="as written in the SQL",
        result=rows,
        key_numbers=key,
        ad_hoc=True,
        chart="table" if len(rows) <= 1 else "bar",
    )
    ctx.evidence.append(evidence)
    return evidence
