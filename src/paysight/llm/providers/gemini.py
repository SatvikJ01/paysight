"""Google Gemini via ``google-genai``, using a JSON response schema."""

from __future__ import annotations

import importlib
from functools import cache

from pydantic import BaseModel

from paysight.llm.base import Provider, RawResponse


@cache
def _transport_errors() -> tuple[type[BaseException], ...]:
    """Timeout/transport exception classes from whichever HTTP library is installed.

    google-genai works with either ``httpx`` or its fork ``httpx2``; their classes are distinct,
    so both are checked when present.
    """
    found: list[type[BaseException]] = []
    for name in ("httpx2", "httpx"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        found += [module.TimeoutException, module.TransportError]
    return tuple(found)


class GeminiProvider(Provider):
    """Gemini structured output: ``response_mime_type=application/json`` plus a JSON schema."""

    name = "gemini"

    def __init__(self, *, model: str, api_key: str, temperature: float, timeout_s: float) -> None:
        from google import genai
        from google.genai import types

        self.model = model
        self._types = types
        self._temperature = temperature
        self._client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000))
        )

    def raw_json(self, system: str, user: str, schema: type[BaseModel]) -> RawResponse:
        """One ``generate_content`` call constrained to ``schema``."""
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=self._temperature,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
        )
        response = self._client.models.generate_content(
            model=self.model, contents=user, config=config
        )
        meta = response.usage_metadata
        output_tokens = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
        return RawResponse(
            text=response.text or "",
            input_tokens=meta.prompt_token_count or 0,
            output_tokens=output_tokens,
        )

    def is_retryable(self, exc: BaseException) -> bool:
        """429, 5xx and transport timeouts are retryable."""
        from google.genai import errors

        if isinstance(exc, errors.APIError):
            return exc.code == 429 or (exc.code or 0) >= 500
        return isinstance(exc, _transport_errors())
