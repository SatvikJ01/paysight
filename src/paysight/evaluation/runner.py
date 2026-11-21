"""Run the evaluation: Paysight and the baseline on every case, scored with the same rules.

``paysight eval run --mode replay`` is deterministic: every LLM response comes from cassettes.
``--mode record`` records missing cassettes (spend-guarded) and then scores the same way.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from paysight.config import Settings
from paysight.evaluation.baseline import run_baseline
from paysight.evaluation.questions import Case, load_cases
from paysight.evaluation.scorer import (
    CheckOutcome,
    SystemOutput,
    from_baseline,
    from_paysight,
    resolved_window,
    score,
    score_intent,
)
from paysight.llm.base import LLMError
from paysight.orchestrator.pipeline import Paysight, build_paysight

SYSTEMS = ("paysight", "baseline")
BEHAVIORS = ("answer", "clarify", "deny_access", "refuse_unsafe")
INTENT_FIELDS = ("question_type", "metric", "window")


def _gold(app: Paysight, eval_dir: Path, case: Case) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for check in case.checks:
        if check.type == "numeric":
            sql = (eval_dir / str(check.gold)).read_text(encoding="utf-8")  # type: ignore[attr-defined]
            result = app.engine.run_internal(sql, [], purpose=f"eval gold for {case.id}")
            values[str(check.gold)] = (  # type: ignore[attr-defined]
                float(result.frame.iloc[0, 0]) if result.ok and len(result.frame) else None
            )
    return values


def _run_case(app: Paysight, case: Case, *, baseline: bool = True) -> dict[str, SystemOutput]:
    as_of = app.settings.data.as_of_date.isoformat()
    t0 = time.perf_counter()
    result = app.ask(case.question, case.role)
    latency = (time.perf_counter() - t0) * 1000
    trace = json.loads(Path(result.trace_path).read_text()) if result.trace_path else {}
    outputs = {"paysight": from_paysight(result, latency_ms=latency, trace=trace)}
    if not baseline:
        return outputs
    try:
        base = run_baseline(
            app.engine, app.llm, app.policies[case.role], case.question, as_of=as_of
        )
        outputs["baseline"] = from_baseline(base)
    except LLMError as exc:
        if "budget" in str(exc).lower():
            raise
        outputs["baseline"] = SystemOutput(
            system="baseline", behavior="error", core_text=str(exc), full_text=str(exc)
        )
    return outputs


def _pct(values: list[bool]) -> float | None:
    return None if not values else round(100 * sum(values) / len(values), 1)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 0)


def _summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def collect(system: str, ctype: str) -> list[CheckOutcome]:
        return [o for r in rows for o in r["outcomes"][system] if o.type == ctype]

    out: list[dict[str, Any]] = []

    def add(metric: str, fn) -> None:
        out.append({"metric": metric, **{s: fn(s) for s in SYSTEMS}})

    add("Cases fully correct (%)", lambda s: _pct([r["passed"][s] for r in rows]))
    add("Numeric accuracy (%)", lambda s: _pct([o.passed for o in collect(s, "numeric")]))
    add(
        "Driver recall@2 (%)",
        lambda s: (
            None
            if not collect(s, "drivers")
            else round(100 * statistics.mean(o.recall or 0 for o in collect(s, "drivers")), 1)
        ),
    )
    add("Root-cause checks passed (%)", lambda s: _pct([o.passed for o in collect(s, "drivers")]))
    add(
        "Anomaly window hit rate (%)",
        lambda s: _pct([o.passed for o in collect(s, "anomaly_window")]),
    )
    for behavior in BEHAVIORS:
        add(
            f"Behavior accuracy: {behavior} (%)",
            lambda s, b=behavior: _pct(
                [
                    o.passed
                    for r in rows
                    for o in r["outcomes"][s]
                    if o.type == "behavior" and r["expected_behavior"] == b
                ]
            ),
        )
    add(
        "Mentions (DQ, reconciliation, anomaly) (%)",
        lambda s: _pct([o.passed for o in collect(s, "mentions")]),
    )
    for field in INTENT_FIELDS:
        add(
            f"Intent stage: {field} (%)",
            lambda s, f=field: (
                _pct([v for r in rows if (v := r["intent"].get(f)) is not None])
                if s == "paysight"
                else None
            ),
        )
    add(
        "Grounding pass rate (%)",
        lambda s: _pct([r["out"][s].grounded for r in rows if r["out"][s].grounded is not None]),
    )
    add(
        "Latency p50 (ms, this run)",
        lambda s: _quantile([r["out"][s].latency_ms for r in rows], 0.5),
    )
    add(
        "Latency p95 (ms, this run)",
        lambda s: _quantile([r["out"][s].latency_ms for r in rows], 0.95),
    )
    add("LLM calls", lambda s: sum(r["out"][s].llm_calls for r in rows))
    add(
        "Tokens in / out",
        lambda s: (
            f"{sum(r['out'][s].input_tokens for r in rows):,} / "
            f"{sum(r['out'][s].output_tokens for r in rows):,}"
        ),
    )
    add(
        "Estimated cost (USD, recorded usage)",
        lambda s: round(sum(r["out"][s].cost_usd for r in rows), 3),
    )
    return out


def _score_case(
    app: Paysight, eval_dir: Path, case: Case, *, baseline: bool = True
) -> dict[str, Any]:
    """Run one case (Paysight, plus the baseline unless ``baseline`` is off) and score it."""
    systems = SYSTEMS if baseline else ("paysight",)
    gold = _gold(app, eval_dir, case)
    outputs = _run_case(app, case, baseline=baseline)
    outcomes = {
        s: [score(c, outputs[s], gold.get(str(getattr(c, "gold", "")))) for c in case.checks]
        for s in systems
    }
    expected_behavior = next(
        (str(c.behavior) for c in case.checks if c.type == "behavior"),  # type: ignore[attr-defined]
        None,
    )
    return {
        "case": case,
        "out": outputs,
        "outcomes": outcomes,
        "expected_behavior": expected_behavior,
        "passed": {s: all(o.passed is not False for o in outcomes[s]) for s in systems},
        "intent": score_intent(case.intent, outputs["paysight"]) if case.intent else {},
    }


def _sql_source(out: SystemOutput) -> str:
    """Governed / ad_hoc / none (no query ran), as on the answer badge."""
    if not out.evidence:
        return "none"
    return "ad_hoc" if out.adhoc else "governed"


def _robustness(rows: list[dict[str, Any]], variant_rows: list[dict[str, Any]]) -> dict:
    """Per paraphrase group: does every phrasing pass, and does the parse agree?"""
    groups = []
    for row in rows:
        case = row["case"]
        members = [row] + [v for v in variant_rows if v["case"].group == case.id]
        if len(members) == 1:
            continue
        parses = {
            (i.question_type, resolved_window(i)) if (i := m["out"]["paysight"].intent) else None
            for m in members
        }
        groups.append(
            {
                "id": case.id,
                "phrasings": len(members),
                "passed": sum(m["passed"]["paysight"] for m in members),
                "all_passed": all(m["passed"]["paysight"] for m in members),
                "same_parse": len(parses) == 1,
                "failed_phrasings": [
                    m["case"].question for m in members if not m["passed"]["paysight"]
                ],
            }
        )
    return {
        "groups": groups,
        "score_pct": _pct([g["all_passed"] for g in groups]),
        "paraphrase_pass_pct": _pct([v["passed"]["paysight"] for v in variant_rows]),
    }


def run_eval(settings: Settings, *, mode: str | None, only: list[str] | None = None) -> dict:
    """Run every case (or ``only`` these ids) and write ``eval/reports/latest.{json,md}``."""
    eval_dir = settings.root / "eval"
    cases = [c for c in load_cases(eval_dir / "questions.yaml") if not only or c.id in only]
    app = build_paysight(settings, mode=mode)
    rows = [_score_case(app, eval_dir, case) for case in cases]
    variant_rows = [
        _score_case(app, eval_dir, variant, baseline=False)
        for case in cases
        for variant in case.variants()
    ]

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": app.llm.info.mode,
        "model": app.llm.info.model,
        "n_questions": len(rows),
        "summary": _summary(rows),
        "robustness": _robustness(rows, variant_rows),
        "questions": [
            {
                "id": r["case"].id,
                "category": r["case"].category,
                "role": r["case"].role,
                "paysight": "pass" if r["passed"]["paysight"] else "fail",
                "baseline": "pass" if r["passed"]["baseline"] else "fail",
                "paysight_behavior": r["out"]["paysight"].behavior,
                "baseline_behavior": r["out"]["baseline"].behavior,
                "paysight_sql": _sql_source(r["out"]["paysight"]),
                "intent": r["intent"],
            }
            for r in rows
        ],
        "failures": [
            {
                "id": r["case"].id,
                "system": s,
                "reason": "; ".join(
                    f"{o.type}: {o.detail}" for o in r["outcomes"][s] if o.passed is False
                ),
            }
            for r in rows + variant_rows
            for s in r["passed"]
            if not r["passed"][s]
        ],
    }
    out_dir = eval_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "latest.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: dict) -> str:
    """The report as markdown (embedded in the README)."""
    lines = [
        "# Paysight evaluation",
        "",
        f"{report['n_questions']} questions · mode `{report['mode']}` · model "
        f"`{report['model']}` · generated {report['generated_at']}",
        "",
        "## Paysight vs naive text-to-SQL baseline",
        "",
        "| Metric | Paysight | Baseline |",
        "|---|---|---|",
    ]
    for row in report["summary"]:
        fmt = lambda v: "–" if v is None else str(v)  # noqa: E731
        lines.append(f"| {row['metric']} | {fmt(row['paysight'])} | {fmt(row['baseline'])} |")
    rob = report.get("robustness") or {}
    if rob.get("groups"):
        lines += [
            "",
            "## Phrasing robustness (Paysight)",
            "",
            f"Robustness score: **{rob['score_pct']}%** of {len(rob['groups'])} question "
            "groups pass with every phrasing (the original plus its paraphrases). "
            f'Paraphrases alone: {rob["paraphrase_pass_pct"]}% pass. "Same parse" compares '
            "the parsed question type and resolved window; a refusal by the deterministic "
            "guard has no parse, so it differs from a refusal by the intent step.",
            "",
            "| Group | Phrasings passed | Same parse |",
            "|---|---|---|",
        ]
        for g in rob["groups"]:
            same = "yes" if g["same_parse"] else "no"
            lines.append(f"| {g['id']} | {g['passed']}/{g['phrasings']} | {same} |")
    lines += [
        "",
        "## Per question",
        "",
        "| Case | Category | Paysight | Baseline |",
        "|---|---|---|---|",
    ]
    for q in report["questions"]:
        lines.append(f"| {q['id']} | {q['category']} | {q['paysight']} | {q['baseline']} |")
    lines += ["", "## Failures", ""]
    lines += [f"- **{f['id']}** ({f['system']}): {f['reason']}" for f in report["failures"]]
    if not report["failures"]:
        lines.append("None.")
    return "\n".join(lines) + "\n"
