"""Load and cross-check the semantic layer."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from paysight.config import project_root
from paysight.semantic.models import SemanticLayer


def load_semantic_layer(path: Path | None = None) -> SemanticLayer:
    """Parse ``config/semantic_layer.yaml``; keys starting with ``_`` hold YAML anchors only.

    Raises:
        ValueError: if a metric or dimension references an unknown view, or names collide.
    """
    file = path or project_root() / "config" / "semantic_layer.yaml"
    raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    layer = SemanticLayer(**{k: v for k, v in raw.items() if not k.startswith("_")})

    names = [m.name for m in layer.metrics] + [d.name for d in layer.dimensions]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"duplicate metric/dimension names: {sorted(duplicates)}")
    for m in layer.metrics:
        if m.view not in layer.views:
            raise ValueError(f"metric {m.name} uses unknown view {m.view}")
    for d in layer.dimensions:
        unknown = set(d.sources) - set(layer.views)
        if unknown:
            raise ValueError(f"dimension {d.name} uses unknown views {sorted(unknown)}")
        for view in d.sources:
            if d.sources[view] in layer.views[view].pii_columns and d.sensitivity != "pii":
                raise ValueError(f"dimension {d.name} exposes a pii column but is not marked pii")
    return layer


@lru_cache(maxsize=1)
def get_semantic_layer() -> SemanticLayer:
    """The process-wide semantic layer, loaded once."""
    return load_semantic_layer()
