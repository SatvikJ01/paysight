"""Versioned prompt templates (SPEC §12.5).

Each template is a markdown file in this package with YAML front matter (``template_id``,
``version``) and two sections, ``## system`` and ``## user``. Placeholders use
``string.Template`` syntax (``$question``), so JSON braces in prompts need no escaping.
Rendering must be deterministic, because the rendered text is part of the cassette key.
"""

from __future__ import annotations

import re
from functools import cache
from importlib import resources
from string import Template

import yaml
from pydantic import BaseModel, ConfigDict

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^## (system|user)\s*$", re.MULTILINE)


class PromptTemplate(BaseModel):
    """A parsed prompt template."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    version: str
    system: str
    user: str

    def render(self, **values: str) -> tuple[str, str]:
        """Substitute ``$placeholders``; raises ``KeyError`` if one is missing."""
        return Template(self.system).substitute(values), Template(self.user).substitute(values)


def parse_prompt(text: str) -> PromptTemplate:
    """Parse a template file's text."""
    match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
    if not match:
        raise ValueError("prompt file needs YAML front matter between --- lines")
    meta = yaml.safe_load(match.group(1))
    parts = _SECTION.split(match.group(2))
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if set(sections) != {"system", "user"}:
        raise ValueError("prompt file needs exactly one '## system' and one '## user' section")
    return PromptTemplate(
        template_id=str(meta["template_id"]),
        version=str(meta["version"]),
        system=sections["system"],
        user=sections["user"],
    )


@cache
def load_prompt(template_id: str) -> PromptTemplate:
    """Load ``<template_id>.md`` from this package."""
    text = resources.files(__name__).joinpath(f"{template_id}.md").read_text(encoding="utf-8")
    prompt = parse_prompt(text)
    if prompt.template_id != template_id:
        raise ValueError(f"{template_id}.md declares template_id {prompt.template_id!r}")
    return prompt
