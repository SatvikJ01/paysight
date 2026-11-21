"""The provider interface and the robustness wrapper shared by every provider (SPEC §12.1–12.2).

A provider implements one raw call, :meth:`Provider.raw_json`, which returns the model's JSON
text plus token counts. :class:`StructuredLLM` adds what every provider needs:

* retries with jittered exponential backoff on rate limits, 5xx and timeouts (at most
  ``max_attempts`` calls);
* pydantic validation of the output (malformed JSON counts as a validation failure), with
  **one** re-ask that includes the validation errors;
* a :class:`Usage` record (tokens, latency, estimated cost) for every call.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Base class for LLM-layer failures."""


class LLMUnavailableError(LLMError):
    """The provider could not be reached or kept failing after retries."""


class SchemaValidationFailedError(LLMError):
    """The model's output did not match the schema, even after one re-ask."""


class ReplayMissError(LLMError):
    """Replay mode found no cassette for this prompt."""


class Usage(BaseModel):
    """Tokens, latency and estimated cost of one LLM call."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float
    source: str  # "live", "record" or "replay"
    reasked: bool = False


@dataclass(frozen=True)
class RawResponse:
    """What a provider returns: the JSON text plus token counts."""

    text: str
    input_tokens: int
    output_tokens: int


class Provider(ABC):
    """One LLM vendor. Implementations make exactly one API call per :meth:`raw_json`."""

    name: str
    model: str

    @abstractmethod
    def raw_json(self, system: str, user: str, schema: type[BaseModel]) -> RawResponse:
        """Ask for JSON text matching ``schema``; raise vendor errors unchanged."""

    @abstractmethod
    def is_retryable(self, exc: BaseException) -> bool:
        """Whether ``exc`` is a rate limit, 5xx or timeout worth retrying."""


def reask_message(user: str, errors: str) -> str:
    """The follow-up prompt after a schema validation failure."""
    return (
        f"{user}\n\nYour previous answer did not match the required JSON schema. "
        f"Validation errors:\n{errors}\nReturn only a corrected JSON object."
    )


class StructuredLLM:
    """Retries, schema validation with one re-ask, and usage accounting around a provider."""

    def __init__(
        self,
        provider: Provider,
        *,
        max_attempts: int,
        price_in: float,
        price_out: float,
        wait_max_s: float = 20.0,
    ) -> None:
        self.provider = provider
        self._max_attempts = max_attempts
        self._price_in = price_in
        self._price_out = price_out
        self._wait_max_s = wait_max_s

    def _call(self, system: str, user: str, schema: type[BaseModel]) -> RawResponse:
        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_random_exponential(multiplier=0.5, max=self._wait_max_s),
            retry=retry_if_exception(self.provider.is_retryable),
            reraise=False,
        )
        try:
            return retrying(self.provider.raw_json, system, user, schema)
        except RetryError as exc:
            cause = exc.last_attempt.exception()
            raise LLMUnavailableError(
                f"{self.provider.name} failed after {self._max_attempts} attempts: {cause}"
            ) from cause

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimated USD cost from the configured price table."""
        return (input_tokens * self._price_in + output_tokens * self._price_out) / 1_000_000

    def complete_structured(
        self, system: str, user: str, schema: type[T], *, template_id: str
    ) -> tuple[T, Usage, dict[str, Any]]:
        """Return a validated ``schema`` instance, its usage, and the raw JSON that produced it."""
        t0 = time.perf_counter()
        raw = self._call(system, user, schema)
        tokens_in, tokens_out = raw.input_tokens, raw.output_tokens
        reasked = False
        try:
            parsed = schema.model_validate_json(raw.text)
        except ValidationError as first:
            reasked = True
            raw = self._call(system, reask_message(user, str(first)), schema)
            tokens_in += raw.input_tokens
            tokens_out += raw.output_tokens
            try:
                parsed = schema.model_validate_json(raw.text)
            except ValidationError as second:
                raise SchemaValidationFailedError(
                    f"{template_id}: output still invalid after one re-ask: {second}"
                ) from second
        usage = Usage(
            template_id=template_id,
            provider=self.provider.name,
            model=self.provider.model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            latency_ms=(time.perf_counter() - t0) * 1000,
            cost_usd=self.cost(tokens_in, tokens_out),
            source="live",
            reasked=reasked,
        )
        return parsed, usage, json.loads(raw.text)
