"""Anthropic Claude via the ``anthropic`` SDK, using structured outputs (``output_config.format``).

Request shape, chosen for the current Claude models (see DECISIONS.md D27):

* **Structured outputs, not forced tool use.** ``tool_choice`` forcing a tool returns a 400 on
  Claude Sonnet 5.5 and Opus 5.5. The pydantic schema is converted with the SDK's
  ``transform_schema`` and sent as ``output_config.format``, which guarantees a JSON text block.
* **No ``temperature``.** Current models reject non-default sampling parameters.
* **Thinking off where the model allows it.** Thinking is billed as output, and intent parsing
  and narration from computed evidence do not need it: ``between_tools`` on Sonnet 5.5,
  ``disabled`` on Haiku 5.5. Opus 5.5 cannot disable thinking, so it runs at low effort.
* **Prompt caching on the static prefix.** Paysight's system prompts are fixed per template (the
  intent prompt carries the semantic catalog and examples), so one ``cache_control`` breakpoint
  on the system block lets every later call read it from cache.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from paysight.llm.base import LLMError, Provider, RawResponse

_MAX_TOKENS = 16_000
_THINKING: dict[str, dict[str, Any]] = {
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-haiku-5-5": {"type": "disabled"},
}
_EFFORT: dict[str, str] = {"claude-opus-5-5": "low"}


class AnthropicProvider(Provider):
    """Claude with a JSON-schema-constrained text response."""

    name = "anthropic"

    def __init__(self, *, model: str, api_key: str, temperature: float, timeout_s: float) -> None:
        import anthropic

        self.model = model
        del temperature  # rejected by current Claude models; see the module docstring
        # Retries are handled by StructuredLLM, so the SDK's own retries are turned off.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout_s)

    def build_request(self, system: str, user: str, schema: type[BaseModel]) -> dict[str, Any]:
        """The Messages API request for one call (pure; unit-tested)."""
        from anthropic import transform_schema

        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": transform_schema(schema)}
        }
        if self.model in _EFFORT:
            output_config["effort"] = _EFFORT[self.model]
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        if self.model in _THINKING:
            request["thinking"] = _THINKING[self.model]
        return request

    def raw_json(self, system: str, user: str, schema: type[BaseModel]) -> RawResponse:
        """One ``messages.create`` call; returns the JSON text and token usage."""
        response = self._client.messages.create(**self.build_request(system, user, schema))
        if response.stop_reason == "refusal":
            raise LLMError(f"Claude declined the request ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude hit max_tokens ({_MAX_TOKENS}) before finishing the JSON")
        text = next((b.text for b in response.content if b.type == "text"), "")
        usage = response.usage
        return RawResponse(
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
        )

    def is_retryable(self, exc: BaseException) -> bool:
        """Rate limits, overloads, 5xx and connection/timeouts are retryable."""
        import anthropic

        if isinstance(exc, anthropic.RateLimitError | anthropic.APIConnectionError):
            return True
        return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500
