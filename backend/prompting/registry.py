from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from backend.core.errors import ValidationError

_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    id: str
    variables: frozenset[str] = frozenset()


class ToolPromptDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=4_000)
    parameter_descriptions: dict[str, str] = Field(default_factory=dict)


PROMPT_DEFINITIONS = {
    item.id: item
    for item in (
        PromptDefinition("global"),
        PromptDefinition("research"),
        PromptDefinition("deep-work-coordinator"),
        PromptDefinition("deep-work-worker"),
        PromptDefinition("fast-answer", frozenset({"web_search_limit"})),
        PromptDefinition("stop-and-answer"),
        PromptDefinition("context-compaction"),
        PromptDefinition("ocr-reconstruction"),
        PromptDefinition("ocr-triage"),
        PromptDefinition("ocr-validation"),
        PromptDefinition("provider-verification"),
        PromptDefinition(
            "run-continuation",
            frozenset({"epoch_index", "run_id", "goal_state"}),
        ),
        PromptDefinition("run-recovery", frozenset({"run_id"})),
        PromptDefinition("paper-summary"),
        PromptDefinition("paper-summary-overview"),
    )
}


class PromptRegistry:
    """Read-only access to the prompts and tool descriptions shipped with the app."""

    def __init__(
        self,
        _overrides_root: Path | None = None,
        defaults_root: Path | None = None,
    ) -> None:
        self.defaults_root = (
            defaults_root or Path(__file__).resolve().parent / "defaults"
        ).resolve()
        self.validate_defaults()

    @classmethod
    def defaults_only(cls) -> "PromptRegistry":
        return cls()

    @property
    def revision(self) -> str:
        files = [
            *(self.defaults_root / "prompts").glob("*.md"),
            *(self.defaults_root / "tools").glob("*.json"),
        ]
        digest = hashlib.sha256()
        for path in sorted(files):
            digest.update(path.relative_to(self.defaults_root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def validate_defaults(self) -> None:
        issues: list[str] = []
        for prompt_id in PROMPT_DEFINITIONS:
            try:
                self._prompt(prompt_id)
            except (OSError, ValidationError) as exc:
                issues.append(f"Prompt '{prompt_id}': {exc}")
        for path in sorted((self.defaults_root / "tools").glob("*.json")):
            try:
                self.tool_document(path.stem)
            except (OSError, ValidationError) as exc:
                issues.append(f"Tool prompt '{path.stem}': {exc}")
        if issues:
            raise ValidationError("Shipped prompt defaults are invalid.", issues=issues)

    def render(self, prompt_id: str, **variables: Any) -> str:
        definition = PROMPT_DEFINITIONS.get(prompt_id)
        if definition is None:
            raise ValidationError(f"Unknown prompt '{prompt_id}'.")
        supplied = set(variables)
        unknown = supplied - definition.variables
        missing = definition.variables - supplied
        if unknown or missing:
            raise ValidationError(
                f"Prompt '{prompt_id}' variables are invalid.",
                issues=[
                    *(f"Unknown variable '{name}'." for name in sorted(unknown)),
                    *(f"Missing variable '{name}'." for name in sorted(missing)),
                ],
            )
        content = self._prompt(prompt_id)
        return _VARIABLE_PATTERN.sub(
            lambda match: str(variables[match.group(1)]),
            content,
        ).strip()

    def tool_document(self, catalog_id: str) -> ToolPromptDocument:
        path = self.defaults_root / "tools" / f"{catalog_id}.json"
        try:
            return ToolPromptDocument.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValidationError(
                f"Required tool prompt '{catalog_id}.json' is missing."
            ) from exc
        except (PydanticValidationError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"Tool prompt '{catalog_id}' is invalid.",
                issues=[str(exc)],
            ) from exc

    def _prompt(self, prompt_id: str) -> str:
        path = self.defaults_root / "prompts" / f"{prompt_id}.md"
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValidationError(
                f"Required prompt '{prompt_id}.md' is missing."
            ) from exc
        if not content.strip():
            raise ValidationError(f"Prompt '{prompt_id}' cannot be empty.")
        found = set(_VARIABLE_PATTERN.findall(content))
        expected = PROMPT_DEFINITIONS[prompt_id].variables
        if found != expected:
            raise ValidationError(
                f"Prompt '{prompt_id}' has invalid template variables.",
                issues=[
                    *(f"Unknown variable '{name}'." for name in sorted(found - expected)),
                    *(f"Required variable '{name}' is not used." for name in sorted(expected - found)),
                ],
            )
        return content


@lru_cache(maxsize=1)
def default_prompt_registry() -> PromptRegistry:
    return PromptRegistry.defaults_only()
