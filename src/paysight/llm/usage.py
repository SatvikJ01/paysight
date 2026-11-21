"""Usage aggregation across a run (SPEC §12.4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from paysight.llm.base import Usage


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
