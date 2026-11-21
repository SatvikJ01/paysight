"""Provider wiring against stubbed SDK clients (no network) and real SDK error classes."""

from __future__ import annotations

from types import SimpleNamespace

import httpx2 as httpx  # the HTTP stack these SDK versions use
import pytest
from pydantic import BaseModel

from paysight.llm.providers.anthropic import AnthropicProvider
from paysight.llm.providers.gemini import GeminiProvider
from paysight.llm.providers.openai import OpenAIProvider

KW = {"model": "m", "api_key": "test-not-a-real-key", "temperature": 0.0, "timeout_s": 5}
REQ = httpx.Request("POST", "https://example.invalid")


class Answer(BaseModel):
    label: str


def test_gemini_request_and_usage() -> None:
    provider = GeminiProvider(**KW)
    seen = {}

    def generate_content(*, model, contents, config):
        seen.update(model=model, contents=contents, config=config)
        meta = SimpleNamespace(
            prompt_token_count=10, candidates_token_count=4, thoughts_token_count=3
        )
        return SimpleNamespace(text='{"label": "g"}', usage_metadata=meta)

    provider._client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    raw = provider.raw_json("sys", "user", Answer)
    assert raw.text == '{"label": "g"}' and (raw.input_tokens, raw.output_tokens) == (10, 7)
    assert seen["config"].response_mime_type == "application/json"
    assert seen["config"].response_json_schema == Answer.model_json_schema()
    assert seen["config"].system_instruction == "sys"


def test_gemini_retry_classification() -> None:
    from google.genai import errors

    provider = GeminiProvider(**KW)
    assert provider.is_retryable(errors.APIError(429, {"error": {"message": "slow down"}}))
    assert provider.is_retryable(errors.APIError(503, {"error": {"message": "unavailable"}}))
    assert not provider.is_retryable(errors.APIError(400, {"error": {"message": "bad"}}))
    daily = errors.APIError(429, {"error": {"message": "quota GenerateRequestsPerDayPerProject"}})
    assert not provider.is_retryable(daily)
    assert provider.is_retryable(httpx.ReadTimeout("t"))
    assert not provider.is_retryable(ValueError("x"))


def test_anthropic_forced_tool_call() -> None:
    provider = AnthropicProvider(**KW)
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        block = SimpleNamespace(type="tool_use", input={"label": "a"})
        return SimpleNamespace(
            content=[block], usage=SimpleNamespace(input_tokens=5, output_tokens=2)
        )

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    raw = provider.raw_json("sys", "user", Answer)
    assert raw.text == '{"label": "a"}' and raw.input_tokens == 5
    assert seen["tool_choice"] == {"type": "tool", "name": "Answer"}
    assert seen["tools"][0]["input_schema"] == Answer.model_json_schema()


def test_anthropic_retry_classification() -> None:
    import anthropic

    provider = AnthropicProvider(**KW)
    rate = anthropic.RateLimitError("slow", response=httpx.Response(429, request=REQ), body=None)
    server = anthropic.InternalServerError(
        "x", response=httpx.Response(500, request=REQ), body=None
    )
    bad = anthropic.BadRequestError("x", response=httpx.Response(400, request=REQ), body=None)
    assert provider.is_retryable(rate) and provider.is_retryable(server)
    assert provider.is_retryable(anthropic.APITimeoutError(request=REQ))
    assert not provider.is_retryable(bad)


def test_openai_parse_and_refusal() -> None:
    provider = OpenAIProvider(**KW)

    def parse(**kwargs):
        assert kwargs["response_format"] is Answer
        message = SimpleNamespace(content='{"label": "o"}', refusal=None)
        usage = SimpleNamespace(prompt_tokens=7, completion_tokens=3)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=parse))
    )
    raw = provider.raw_json("sys", "user", Answer)
    assert raw.text == '{"label": "o"}' and raw.output_tokens == 3

    def refuse(**kwargs):
        message = SimpleNamespace(content=None, refusal="no")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    provider._client.chat.completions.parse = refuse
    with pytest.raises(Exception, match="refused"):
        provider.raw_json("sys", "user", Answer)


def test_openai_retry_classification() -> None:
    import openai

    provider = OpenAIProvider(**KW)
    rate = openai.RateLimitError("slow", response=httpx.Response(429, request=REQ), body=None)
    bad = openai.BadRequestError("x", response=httpx.Response(400, request=REQ), body=None)
    assert provider.is_retryable(rate)
    assert provider.is_retryable(openai.APITimeoutError(request=REQ))
    assert not provider.is_retryable(bad)
