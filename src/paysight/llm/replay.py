"""Live, record and replay modes over committed cassettes (SPEC §12.3).

* ``live``: call the provider. If it is unavailable and a cassette exists, use the cassette.
* ``record``: call the provider and save the response as a cassette. A prompt that already has
  a cassette is replayed instead, so re-recording spends API quota only on new prompts. With
  ``PAYSIGHT_RERECORD=1``, cassettes recorded by a different model are replaced.
* ``replay``: cassettes only. A miss raises :class:`ReplayMissError`, which says how to record.

Cassette key: ``sha256(template_id + template_version + rendered prompt + schema name)``. It does
not include the provider or model, so a cassette recorded with one provider replays anywhere.
Cassettes are written as sorted, indented JSON so re-recordings produce readable diffs.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from paysight.config import Settings
from paysight.llm.base import (
    LLMError,
    LLMUnavailableError,
    Prices,
    Provider,
    RawResponse,
    ReplayMissError,
    StructuredLLM,
    Usage,
)
from paysight.llm.prompts import PromptTemplate
from paysight.llm.usage import SpendLedger

Mode = Literal["live", "record", "replay"]
T = TypeVar("T", bound=BaseModel)

API_KEY_ENV = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}


def cassette_key(prompt: PromptTemplate, system: str, user: str, schema: type[BaseModel]) -> str:
    """Provider-agnostic cassette key."""
    material = "\x1f".join([prompt.template_id, prompt.version, system, user, schema.__name__])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ModeInfo(BaseModel):
    """What the UI shows in its mode badge."""

    model_config = ConfigDict(frozen=True)

    mode: Mode
    provider: str
    model: str


class LLMClient:
    """The single entry point the orchestrator uses for LLM calls."""

    def __init__(
        self,
        *,
        mode: Mode,
        cassette_dir: Path,
        llm: StructuredLLM | None,
        provider_name: str,
        model: str,
        prices: Mapping[str, Prices] | None = None,
        ledger: SpendLedger | None = None,
    ) -> None:
        if mode in ("live", "record") and llm is None:
            raise LLMError(f"{mode} mode needs a configured provider")
        self.mode = mode
        self.cassette_dir = cassette_dir
        self._llm = llm
        self.info = ModeInfo(mode=mode, provider=provider_name, model=model)
        self._prices = dict(prices or {})
        self.ledger = ledger
        self.rerecord = os.environ.get("PAYSIGHT_RERECORD", "0") == "1"

    def _stale(self, path: Path) -> bool:
        """With PAYSIGHT_RERECORD=1, a cassette from a different model is replaced.

        A cassette from the current model is always reused, even when re-recording: separate
        runs in one recording session then share the same responses (see DECISIONS.md D30).
        """
        if not self.rerecord:
            return False
        return json.loads(path.read_text(encoding="utf-8")).get("model") != self.info.model

    def _path(self, prompt: PromptTemplate, key: str) -> Path:
        return self.cassette_dir / prompt.template_id / f"{key[:24]}.json"

    def _replay(
        self, path: Path, key: str, prompt: PromptTemplate, schema: type[T], source: str
    ) -> tuple[T, Usage]:
        if not path.exists():
            raise ReplayMissError(
                f"No recorded response for prompt '{prompt.template_id}' v{prompt.version} "
                f"(key {key[:12]}). Record it once with a real key: set LLM_MODE=record and an "
                "API key in .env, then rerun the same question (or `paysight eval run --mode "
                "record`)."
            )
        cassette = json.loads(path.read_text(encoding="utf-8"))
        if cassette.get("key") != key:
            raise ReplayMissError(f"cassette {path.name} has a different key; re-record it")
        parsed = schema.model_validate(cassette["response"])
        tokens = cassette.get("usage", {})
        recorded = RawResponse(
            text="",
            input_tokens=int(tokens.get("input_tokens", 0)),
            output_tokens=int(tokens.get("output_tokens", 0)),
            cache_write_tokens=int(tokens.get("cache_write_tokens", 0)),
            cache_read_tokens=int(tokens.get("cache_read_tokens", 0)),
        )
        model = cassette.get("model", "unknown")
        return parsed, Usage(
            template_id=prompt.template_id,
            provider=cassette.get("provider", "unknown"),
            model=model,
            input_tokens=recorded.input_tokens,
            output_tokens=recorded.output_tokens,
            cache_write_tokens=recorded.cache_write_tokens,
            cache_read_tokens=recorded.cache_read_tokens,
            latency_ms=0.0,
            cost_usd=self._prices.get(model, Prices()).cost(recorded),
            source=source,
        )

    def complete(self, prompt: PromptTemplate, schema: type[T], **values: str) -> tuple[T, Usage]:
        """Render ``prompt`` with ``values`` and return a validated ``schema`` instance."""
        system, user = prompt.render(**values)
        key = cassette_key(prompt, system, user, schema)
        path = self._path(prompt, key)
        if self.mode == "replay":
            return self._replay(path, key, prompt, schema, "replay")

        if self.mode == "record" and path.exists() and not self._stale(path):
            return self._replay(path, key, prompt, schema, "replay")  # keep, don't spend quota
        assert self._llm is not None
        if self.ledger is not None:
            self.ledger.check()
        try:
            parsed, usage, data = self._llm.complete_structured(
                system, user, schema, template_id=prompt.template_id
            )
        except LLMUnavailableError:
            if path.exists():
                return self._replay(path, key, prompt, schema, "replay-fallback")
            raise
        if self.mode == "record":
            path.parent.mkdir(parents=True, exist_ok=True)
            cassette = {
                "key": key,
                "template_id": prompt.template_id,
                "template_version": prompt.version,
                "schema": schema.__name__,
                "provider": usage.provider,
                "model": usage.model,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                },
                "response": data,
            }
            path.write_text(
                json.dumps(cassette, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            usage = usage.model_copy(update={"source": "record"})
        if self.ledger is not None:
            self.ledger.add(usage)  # after saving: a paid-for response is never thrown away
        return parsed, usage


def _provider(name: str, *, model: str, api_key: str, settings: Settings) -> Provider:
    kwargs = {
        "model": model,
        "api_key": api_key,
        "temperature": settings.llm.temperature,
        "timeout_s": settings.llm.request_timeout_s,
    }
    if name == "gemini":
        from paysight.llm.providers.gemini import GeminiProvider

        return GeminiProvider(**kwargs)
    if name == "anthropic":
        from paysight.llm.providers.anthropic import AnthropicProvider

        return AnthropicProvider(**kwargs)
    if name == "openai":
        from paysight.llm.providers.openai import OpenAIProvider

        return OpenAIProvider(**kwargs)
    raise LLMError(f"unknown LLM_PROVIDER {name!r}; use gemini, anthropic or openai")


def build_client(
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    mode_override: Mode | None = None,
    provider: Provider | None = None,
) -> LLMClient:
    """Build the client from settings and environment variables (``.env`` is loaded if present).

    With no API key for the chosen provider, the mode defaults to ``replay``, so a reviewer
    without a key gets the full demo from the committed cassettes. ``provider`` injects a
    ready-made provider (tests use the FakeProvider).
    """
    if env is None:
        load_dotenv(settings.root / ".env", override=False)
        env = os.environ
    name = (env.get("LLM_PROVIDER") or settings.llm.default_provider).strip().lower()
    if provider is not None:
        name = provider.name
    model = (env.get("LLM_MODEL") or "").strip() or settings.llm.models.get(name, "")
    if provider is not None:
        model = provider.model
    api_key = next((env[k] for k in API_KEY_ENV.get(name, ()) if env.get(k, "").strip()), "")
    requested = mode_override or (env.get("LLM_MODE") or "").strip().lower() or None
    mode: Mode = requested or ("live" if (api_key or provider) else "replay")  # type: ignore[assignment]
    if mode not in ("live", "record", "replay"):
        raise LLMError(f"unknown LLM_MODE {mode!r}; use live, record or replay")

    prices = {
        m: Prices(
            input=v.input, output=v.output, cache_write=v.cache_write, cache_read=v.cache_read
        )
        for m, v in settings.llm.prices_per_million_tokens.items()
    }
    llm = None
    if mode != "replay":
        if provider is None:
            if not api_key:
                keys = " or ".join(API_KEY_ENV.get(name, ("an API key",)))
                raise LLMError(f"{mode} mode needs {keys} in .env (or use LLM_MODE=replay)")
            provider = _provider(name, model=model, api_key=api_key, settings=settings)
        llm = StructuredLLM(
            provider, max_attempts=settings.llm.max_attempts, prices=prices.get(model, Prices())
        )
    return LLMClient(
        mode=mode,
        cassette_dir=settings.cassette_dir,
        llm=llm,
        provider_name=name,
        model=model,
        prices=prices,
        ledger=SpendLedger(settings.path(settings.llm.spend_ledger), settings.llm.budget_usd),
    )
