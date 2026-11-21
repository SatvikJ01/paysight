"""Per-question traces (SPEC §15) and logging setup.

Each question gets a ``run_id``. Steps record name, status, start time, duration, a short
summary, LLM tokens/cost, and validation results or denials. Traces are written to
``runs/<run_id>.json`` and rendered in the UI's Trace tab.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from paysight.llm.base import Usage

StepStatus = Literal["ok", "denied", "refused", "blocked", "error", "skipped"]


class TraceStep(BaseModel):
    """One timed step of a run."""

    name: str
    status: StepStatus = "ok"
    started_at: str
    duration_ms: float = 0.0
    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    llm_source: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Trace(BaseModel):
    """All steps of one question."""

    run_id: str
    question: str
    role: str
    llm_mode: str
    steps: list[TraceStep] = Field(default_factory=list)

    @property
    def total_ms(self) -> float:
        """Sum of step durations."""
        return sum(s.duration_ms for s in self.steps)

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens over all LLM steps."""
        return sum(s.input_tokens + s.output_tokens for s in self.steps)

    @property
    def total_cost_usd(self) -> float:
        """Estimated cost over all LLM steps."""
        return sum(s.cost_usd for s in self.steps)

    @contextmanager
    def step(self, name: str) -> Iterator[TraceStep]:
        """Time a step; the caller fills in status/summary/details on the yielded record."""
        record = TraceStep(name=name, started_at=datetime.now().isoformat(timespec="milliseconds"))
        t0 = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            record.status = "error"
            record.summary = record.summary or f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.duration_ms = (time.perf_counter() - t0) * 1000
            self.steps.append(record)

    @staticmethod
    def add_usage(record: TraceStep, usage: Usage) -> None:
        """Attach LLM usage to a step."""
        # All prompt tokens (uncached + cache writes + cache reads); cost prices them separately.
        record.input_tokens += (
            usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
        )
        record.output_tokens += usage.output_tokens
        record.cost_usd += usage.cost_usd
        record.llm_source = usage.source

    def write(self, runs_dir: Path) -> Path:
        """Write ``runs/<run_id>.json`` and return its path."""
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{self.run_id}.json"
        payload = self.model_dump() | {
            "total_ms": self.total_ms,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path


def new_run_id() -> str:
    """A sortable, unique run id."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
        )


def configure_logging() -> None:
    """Standard logging; JSON lines when ``PAYSIGHT_LOG_JSON=1``."""
    handler = logging.StreamHandler()
    if os.environ.get("PAYSIGHT_LOG_JSON", "0") == "1":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("paysight")
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
