"""OpenAI via the ``openai`` SDK, using structured outputs (``chat.completions.parse``)."""

from __future__ import annotations

from pydantic import BaseModel

from paysight.llm.base import LLMError, Provider, RawResponse


class OpenAIProvider(Provider):
    """Structured outputs with the schema as ``response_format``."""

    name = "openai"

    def __init__(self, *, model: str, api_key: str, temperature: float, timeout_s: float) -> None:
        import openai

        self.model = model
        # Temperature is not sent: several current OpenAI models only accept the default.
        del temperature
        self._client = openai.OpenAI(api_key=api_key, max_retries=0, timeout=timeout_s)

    def raw_json(self, system: str, user: str, schema: type[BaseModel]) -> RawResponse:
        """One ``chat.completions.parse`` call."""
        response = self._client.chat.completions.parse(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
        )
        message = response.choices[0].message
        if message.refusal:
            raise LLMError(f"OpenAI refused: {message.refusal}")
        usage = response.usage
        return RawResponse(
            text=message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )

    def is_retryable(self, exc: BaseException) -> bool:
        """Rate limits, 5xx and connection/timeouts are retryable."""
        import openai

        if isinstance(exc, openai.RateLimitError | openai.APIConnectionError):
            return True
        return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500
