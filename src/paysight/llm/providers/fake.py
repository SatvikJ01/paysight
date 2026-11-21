"""A scripted provider for tests: no network, fully deterministic."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from paysight.llm.base import Provider, RawResponse

Handler = Callable[[str, str, type[BaseModel]], dict[str, Any] | str | BaseException]


class FakeTransientError(RuntimeError):
    """A retryable failure (stands in for a 429/5xx)."""


class FakeProvider(Provider):
    """Answers with ``handler(system, user, schema)``.

    The handler may return a dict (serialized as JSON), raw text (to simulate malformed output),
    or an exception instance to raise. ``calls`` records every request.
    """

    name = "fake"

    def __init__(self, handler: Handler, *, model: str = "fake-model") -> None:
        self.model = model
        self._handler = handler
        self.calls: list[tuple[str, str, str]] = []

    def raw_json(self, system: str, user: str, schema: type[BaseModel]) -> RawResponse:
        """Return the handler's scripted answer, with token counts from text length."""
        self.calls.append((system, user, schema.__name__))
        result = self._handler(system, user, schema)
        if isinstance(result, BaseException):
            raise result
        text = result if isinstance(result, str) else json.dumps(result)
        return RawResponse(
            text=text, input_tokens=(len(system) + len(user)) // 4, output_tokens=len(text) // 4
        )

    def is_retryable(self, exc: BaseException) -> bool:
        """Only :class:`FakeTransientError` is retryable."""
        return isinstance(exc, FakeTransientError)
