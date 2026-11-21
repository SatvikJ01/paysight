"""Role policies and the intent-time access pre-check (SPEC §8.1).

This is the first of two enforcement layers. The second, in :mod:`paysight.safety.validator`,
re-checks every SQL statement and rewrites row filters into the AST, so a mistake here can never
leak data. The pre-check exists to refuse early, explain why, and suggest an allowed
alternative, without executing anything.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from paysight.config import project_root
from paysight.semantic.models import Filter, SemanticLayer, Sensitivity


class RolePolicy(BaseModel):
    """What one role may see."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    label: str
    description: str
    allowed_views: list[str]
    denied_sensitivities: list[Sensitivity] = Field(default_factory=list)
    row_filters: dict[str, list[str]] = Field(default_factory=dict)  # column -> allowed values


class AccessDecision(BaseModel):
    """Outcome of :func:`precheck`."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    suggestion: str | None = None


def load_policies(path: Path | None = None) -> dict[str, RolePolicy]:
    """Parse ``config/access_policies.yaml`` into role policies keyed by role name."""
    file = path or project_root() / "config" / "access_policies.yaml"
    raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    return {name: RolePolicy(name=name, **body) for name, body in raw["roles"].items()}


@lru_cache(maxsize=1)
def get_policies() -> dict[str, RolePolicy]:
    """The process-wide role policies, loaded once."""
    return load_policies()


_SENSITIVITY_WORDS = {"pii": "personal or card data", "internal": "internal data"}


def precheck(
    role: RolePolicy,
    *,
    metrics: list[str],
    dimensions: list[str],
    filters: list[Filter],
    layer: SemanticLayer,
) -> AccessDecision:
    """Check requested metrics, dimensions and filters against ``role`` before any query runs."""
    reasons: list[str] = []
    suggestions: list[str] = []

    for name in metrics:
        metric = layer.metric(name)
        if metric.view not in role.allowed_views:
            reasons.append(f"The metric '{name}' is not available to the {role.label} role.")

    used_dims = list(dict.fromkeys([*dimensions, *(f.dimension for f in filters)]))
    for name in used_dims:
        dim = layer.dimension(name)
        if dim.sensitivity in role.denied_sensitivities:
            reasons.append(
                f"'{name}' is {_SENSITIVITY_WORDS.get(dim.sensitivity, dim.sensitivity)}, which "
                f"the {role.label} role cannot access."
            )
            suggestions.append(
                "You can break the same question down by issuer, payment method or failure code "
                "instead; card-level detail needs the Risk analyst role."
            )

    for f in filters:
        dim = layer.dimension(f.dimension)
        for column, allowed in role.row_filters.items():
            if column not in dim.sources.values() or f.op not in ("=", "in"):
                continue
            outside = [str(v) for v in f.values if str(v) not in allowed]
            if outside:
                reasons.append(
                    f"Your role ({role.label}) is limited to {column} = {', '.join(allowed)}; "
                    f"you asked about {', '.join(outside)}."
                )
                suggestions.append(
                    f"You can see {' and '.join(allowed)} {column} merchants; ask the same "
                    f"question for the {allowed[0]} {column}."
                )

    return AccessDecision(
        allowed=not reasons,
        reasons=reasons,
        suggestion=" ".join(dict.fromkeys(suggestions)) or None,
    )
