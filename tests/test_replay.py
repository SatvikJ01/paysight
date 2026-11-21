"""Replay: Appendix B questions answer fully from the committed cassettes, with no API key."""

from __future__ import annotations

from pathlib import Path

import pytest

from paysight.config import get_settings
from paysight.data.generator import GenerationResult
from paysight.execution.engine import Engine
from paysight.llm.replay import build_client
from paysight.orchestrator.pipeline import Paysight
from paysight.safety.access import get_policies
from paysight.semantic.loader import get_semantic_layer

HERO = (
    "Why did payment success rate drop over the last two weeks compared with the two weeks before?"
)


@pytest.fixture(scope="module")
def replay_app(full_dataset: GenerationResult, tmp_path_factory: pytest.TempPathFactory):
    settings = get_settings()
    engine = Engine(full_dataset.path, layer=get_semantic_layer(), limits=settings.limits)
    llm = build_client(settings, env={"LLM_PROVIDER": "gemini"})  # no key -> replay
    assert llm.info.mode == "replay"
    app = Paysight(
        settings=settings,
        layer=get_semantic_layer(),
        engine=engine,
        llm=llm,
        policies=get_policies(),
        runs_dir=tmp_path_factory.mktemp("runs"),
    )
    yield app
    engine.close()


@pytest.mark.slow
def test_hero_question_answers_fully_from_cassettes(replay_app: Paysight) -> None:
    result = replay_app.ask(HERO, "leadership")
    assert result.kind == "answer", result.message
    assert result.llm_mode.startswith("replay")
    assert result.answer is not None and result.answer.facts
    assert result.grounding_score == 1.0
    text = " ".join([result.answer.headline, *(f.text for f in result.answer.facts)])
    assert "NovaMart" in text and "Harbor" in text
    assert result.confidence is not None


@pytest.mark.slow
@pytest.mark.skip(reason="B3 choice narrations must be re-recorded (orphaned, see DECISIONS D25)")
def test_clarification_and_its_choices_replay(replay_app: Paysight) -> None:
    question = "Why is success rate down?"
    clarification = replay_app.ask(question, "leadership")
    assert clarification.kind == "clarification" and clarification.queries_executed == 0
    for choice in ("last_7", "last_14"):
        answered = replay_app.ask(question, "leadership", choice=choice)
        assert answered.kind == "answer", answered.message
        assert answered.grounding_score == 1.0


def test_cassettes_are_committed_for_every_template() -> None:
    root = Path(get_settings().cassette_dir)
    assert (root / "intent").is_dir() and any((root / "intent").glob("*.json"))
    assert any((root / "narrator").glob("*.json"))
