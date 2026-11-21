"""Intent-time access pre-check tests (SPEC §8.1), mirroring demo questions B5, B6 and B10."""

from __future__ import annotations

from paysight.safety.access import RolePolicy, precheck
from paysight.semantic.loader import get_semantic_layer
from paysight.semantic.models import Filter


def _check(role: RolePolicy, metrics: list[str], dims: list[str], filters: list[Filter]):
    return precheck(
        role, metrics=metrics, dimensions=dims, filters=filters, layer=get_semantic_layer()
    )


def test_b5_regional_analyst_asking_about_south_is_denied(policies: dict) -> None:
    decision = _check(
        policies["analyst_north"],
        ["failed_attempts"],
        ["merchant_name"],
        [Filter(dimension="region", op="=", values=["south"])],
    )
    assert not decision.allowed
    assert "south" in decision.reasons[0]
    assert decision.suggestion and "north" in decision.suggestion


def test_regional_analyst_in_scope_is_allowed(policies: dict) -> None:
    decision = _check(
        policies["analyst_north"],
        ["failed_attempts"],
        ["merchant_name"],
        [Filter(dimension="region", op="in", values=["north"])],
    )
    assert decision.allowed


def test_b6_leadership_asking_for_bins_is_denied(policies: dict) -> None:
    decision = _check(policies["leadership"], ["failed_attempts"], ["card_bin"], [])
    assert not decision.allowed
    assert "card_bin" in decision.reasons[0]
    assert decision.suggestion and "Risk analyst" in decision.suggestion


def test_pii_in_filter_only_is_also_denied(policies: dict) -> None:
    decision = _check(
        policies["leadership"],
        ["attempts"],
        [],
        [Filter(dimension="customer_ref_hash", op="=", values=["abc"])],
    )
    assert not decision.allowed


def test_b10_risk_analyst_may_use_bins(policies: dict) -> None:
    decision = _check(policies["risk_analyst"], ["failed_attempts"], ["card_bin"], [])
    assert decision.allowed


def test_every_role_has_a_description(policies: dict) -> None:
    assert set(policies) == {"leadership", "analyst_north", "risk_analyst"}
    assert all(p.description and p.label for p in policies.values())
