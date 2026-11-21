"""Anthropic Claude via the ``anthropic`` SDK, using a forced tool call as the JSON schema."""

from __future__ import annotations

import json

from pydantic import BaseModel

from paysight.llm.base import LLMError, Provider, RawResponse

_MAX_TOKENS = 8192


class AnthropicProvider(Provider):
    """Structured output by forcing the model to call one tool whose input is ``schema``."""

    name = "anthropic"

    def __init__(self, *, model: str, api_key: str, temperature: float, timeout_s: float) -> None:
        import anthropic

        self.model = model
        self._temperature = temperature
        # Retries are handled by StructuredLLM, so the SDK's own retries are turned off.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout_s)

    def raw_json(self, system: str, user: str, schema: type[BaseModel]) -> RawResponse:
        """One ``messages.create`` call with ``tool_choice`` forcing the schema tool."""
        tool_name = schema.__name__
        response = self._client.messages.create(
            model=self.model,
            max_tokens=_MAX_TOKENS,
            temperature=self._temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": tool_name,
                    "description": f"Return the {tool_name} object.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
        )
        block = next((b for b in response.content if b.type == "tool_use"), None)
        if block is None:
            raise LLMError(f"Anthropic returned no {tool_name} tool call")
        return RawResponse(
            text=json.dumps(block.input),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def is_retryable(self, exc: BaseException) -> bool:
        """Rate limits, overloads, 5xx and connection/timeouts are retryable."""
        import anthropic

        if isinstance(exc, anthropic.RateLimitError | anthropic.APIConnectionError):
            return True
        return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500
