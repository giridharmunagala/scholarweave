from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.prompting.registry import PROMPT_DEFINITIONS, PromptRegistry
from backend.tools.catalog import create_tool_catalog


def test_shipped_prompts_are_complete(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)

    assert set(PROMPT_DEFINITIONS) == {
        "global",
        "research",
        "deep-work-coordinator",
        "deep-work-worker",
        "fast-answer",
        "stop-and-answer",
        "context-compaction",
        "ocr-reconstruction",
        "ocr-triage",
        "ocr-validation",
        "provider-verification",
        "run-continuation",
        "run-recovery",
        "paper-summary",
    }
    assert "evidence-backed answer" in registry.render("research")
    assert registry.render("fast-answer", web_search_limit=2).startswith(
        "Fast-answer mode is active. Make at most 2"
    )


def test_main_agent_prompts_use_direct_persistence_tools(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)

    for prompt_id in ("research", "deep-work-coordinator"):
        prompt = registry.render(prompt_id)
        assert "save_research_note" in prompt
        assert "save_paper_summary_version" in prompt
        assert "paper_summary_writer" not in prompt
        assert "research_note_writer" not in prompt


def test_main_agent_prompts_name_only_the_first_turn(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)

    for prompt_id in ("research", "deep-work-coordinator"):
        prompt = registry.render(prompt_id)
        assert "When `set_conversation_title` is available" in prompt
        assert "call it exactly once" in prompt
        assert "unavailable after the first turn" in prompt


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
