from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.prompting.registry import PROMPT_DEFINITIONS, PromptRegistry
from backend.tools.catalog import create_tool_catalog


def test_shipped_prompts_are_complete(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)

    for name, definition in PROMPT_DEFINITIONS.items():
        variables = {variable: "test-value" for variable in definition.variables}
        assert registry.render(name, **variables).strip(), name


def test_prompt_template_variables_are_strict(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)

    with pytest.raises(ValidationError, match="variables are invalid"):
        registry.render("fast-answer", web_search_limit=1, extra="value")


def test_every_tool_has_complete_parameter_guidance(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)
    for definition in create_tool_catalog(registry).definitions():
        document = registry.tool_document(definition.catalog_id)
        schema_properties = set((definition.parameters_schema or {}).get("properties", {}))
        assert document.description.strip(), definition.catalog_id
        assert set(document.parameter_descriptions) == schema_properties
