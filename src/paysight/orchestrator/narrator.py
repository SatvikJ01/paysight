"""Narration (SPEC §11.2): the LLM writes the answer from a compact evidence bundle only."""

from __future__ import annotations

import difflib
import json
import re

from paysight.llm.base import Usage
from paysight.llm.prompts import load_prompt
from paysight.llm.replay import LLMClient
from paysight.orchestrator.models import AnswerDraft, Evidence

MAX_ROWS_PER_EVIDENCE = 12


def compact_bundle(evidence: list[Evidence]) -> str:
    """The evidence the narrator sees: ids, titles, windows, filters, key numbers, top rows."""
    bundle = [
        {
            "id": e.id,
            "title": e.title,
            "purpose": e.purpose,
            "time_window": e.time_window,
            "filters": e.filters,
            "datasets": e.datasets,
            "key_numbers": e.key_numbers,
            "rows": e.result[:MAX_ROWS_PER_EVIDENCE],
            **({"llm_suggested": True} if e.llm_suggested else {}),
            **({"ad_hoc_sql": True} if e.ad_hoc else {}),
        }
        for e in evidence
    ]
    return json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), default=str)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def narrate(
    llm: LLMClient,
    *,
    question: str,
    restatement: str,
    assumptions: list[str],
    caveats: list[str],
    evidence: list[Evidence],
    failures: str = "",
) -> tuple[AnswerDraft, Usage]:
    """One narrator call. ``failures`` carries grounding feedback on the single retry."""
    return llm.complete(
        load_prompt("narrator"),
        AnswerDraft,
        question=question.strip(),
        restatement=restatement,
        assumptions=_bullets(assumptions),
        caveats=_bullets(caveats),
        evidence=compact_bundle(evidence),
        failures=failures,
    )


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")))


def merge_caveats(computed: list[str], narrated: list[str]) -> list[str]:
    """Computed caveats, plus narrator caveats that are not restatements of them.

    The narrator is asked to restate every computed caveat in plain words, so its list mostly
    paraphrases ours. A narrated caveat is dropped when it is textually close to a computed one,
    or when all of its numbers already appear in a single computed caveat.
    """
    out = list(dict.fromkeys(computed))
    for caveat in narrated:
        nums = _numbers(caveat)
        duplicate = any(
            difflib.SequenceMatcher(None, caveat.lower(), c.lower()).ratio() >= 0.6
            or (nums and nums <= _numbers(c))
            for c in out
        )
        if not duplicate:
            out.append(caveat)
    return out
