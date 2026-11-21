"""Evaluation cases loaded from ``eval/questions.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CheckType = Literal["numeric", "behavior", "mentions", "drivers", "anomaly_window", "adhoc"]


class Check(BaseModel):
    """One expectation on an answer; type-specific fields are kept as extras."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: CheckType


class IntentExpectation(BaseModel):
    """Expected parse: question type (one or a list), a metric, the resolved window or "none"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_type: str | list[str] | None = None
    metric: str | None = None
    window: str | None = None

    @property
    def question_types(self) -> list[str]:
        """Accepted question types (empty if not scored)."""
        qt: Any = self.question_type
        return [] if qt is None else [qt] if isinstance(qt, str) else list(qt)


class Case(BaseModel):
    """One evaluation question."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    question: str
    role: str
    category: str
    checks: list[Check] = Field(min_length=1)
    intent: IntentExpectation | None = None
    paraphrases: list[str] = Field(default_factory=list)
    group: str | None = None  # set on paraphrase variants: the id of the original case

    def variants(self) -> list[Case]:
        """Paraphrased copies (ids ``<id>~p1``, ``~p2``…) with the same checks and intent."""
        return [
            self.model_copy(
                update={"id": f"{self.id}~p{i}", "question": q, "paraphrases": [], "group": self.id}
            )
            for i, q in enumerate(self.paraphrases, start=1)
        ]


def load_cases(path: Path) -> list[Case]:
    """Parse the question file; ids must be unique."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [Case(**c) for c in raw["questions"]]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in questions file")
    return cases
