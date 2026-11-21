"""Usage aggregation across a run (SPEC §12.4)."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from paysight.llm.base import BudgetExceededError, Usage


class UsageTotals(BaseModel):
    """Totals over many LLM calls."""

    model_config = ConfigDict(frozen=True)

    calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float


def total_usage(records: list[Usage]) -> UsageTotals:
    """Sum tokens, latency and cost over ``records``."""
    return UsageTotals(
        calls=len(records),
        input_tokens=sum(r.input_tokens for r in records),
        output_tokens=sum(r.output_tokens for r in records),
        latency_ms=sum(r.latency_ms for r in records),
        cost_usd=sum(r.cost_usd for r in records),
    )


class SpendLedger:
    """Cumulative spend on live LLM calls, persisted so it spans separate CLI runs.

    ``check()`` runs before every live call and refuses once the budget is reached; ``add()``
    records each call's cost and raises as soon as the total goes over the budget, so a
    recording session or eval run stops at the first call that crosses it.
    """

    def __init__(self, path: Path, budget_usd: float) -> None:
        self.path = path
        self.budget_usd = budget_usd
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"total_usd": 0.0, "calls": []}

    @property
    def total_usd(self) -> float:
        """Spend recorded so far."""
        return float(self._load()["total_usd"])

    def check(self) -> None:
        """Raise :class:`BudgetExceededError` if the budget is already used up."""
        total = self.total_usd
        if total >= self.budget_usd:
            raise BudgetExceededError(
                f"LLM budget of ${self.budget_usd:.2f} reached (spent ${total:.4f}); "
                "no further live calls will be made"
            )

    def add(self, usage: Usage) -> float:
        """Record one live call; raise if the new total exceeds the budget."""
        with self._lock:
            data = self._load()
            data["total_usd"] = float(data["total_usd"]) + usage.cost_usd
            data["calls"].append(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "template": usage.template_id,
                    "model": usage.model,
                    "input": usage.input_tokens,
                    "cache_write": usage.cache_write_tokens,
                    "cache_read": usage.cache_read_tokens,
                    "output": usage.output_tokens,
                    "cost_usd": round(usage.cost_usd, 6),
                }
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            total = data["total_usd"]
        if total > self.budget_usd:
            raise BudgetExceededError(
                f"LLM spend ${total:.4f} went over the ${self.budget_usd:.2f} budget; stopping"
            )
        return total
