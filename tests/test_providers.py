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


def _anthropic_response(**over):
    usage = SimpleNamespace(
        input_tokens=5, output_tokens=2, cache_creation_input_tokens=900, cache_read_input_tokens=0
    )
    base = {
        "content": [SimpleNamespace(type="text", text='{"label": "a"}')],
        "usage": usage,
        "stop_reason": "end_turn",
        "stop_details": None,
    }
    return SimpleNamespace(**{**base, **over})


def test_anthropic_uses_structured_outputs_caching_and_no_temperature() -> None:
    provider = AnthropicProvider(**{**KW, "model": "claude-sonnet-5-5"})
    request = provider.build_request("sys", "user", Answer)
    assert "tools" not in request and "tool_choice" not in request
    assert "temperature" not in request
    fmt = request["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"] == {"type": "between_tools"}


def test_anthropic_thinking_per_model() -> None:
    haiku = AnthropicProvider(**{**KW, "model": "claude-haiku-5-5"}).build_request("s", "u", Answer)
    opus = AnthropicProvider(**{**KW, "model": "claude-opus-5-5"}).build_request("s", "u", Answer)
    assert haiku["thinking"] == {"type": "disabled"}
    assert "thinking" not in opus and opus["output_config"]["effort"] == "low"


def test_anthropic_reads_json_text_and_cache_usage() -> None:
    provider = AnthropicProvider(**{**KW, "model": "claude-sonnet-5-5"})
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: _anthropic_response())
    )
    raw = provider.raw_json("sys", "user", Answer)
    assert raw.text == '{"label": "a"}' and raw.input_tokens == 5
    assert raw.cache_write_tokens == 900 and raw.cache_read_tokens == 0


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_anthropic_refusal_and_truncation_are_errors(stop: str) -> None:
    provider = AnthropicProvider(**{**KW, "model": "claude-sonnet-5-5"})
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: _anthropic_response(stop_reason=stop))
    )
    with pytest.raises(Exception, match=r"declined|max_tokens"):
        provider.raw_json("sys", "user", Answer)


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
