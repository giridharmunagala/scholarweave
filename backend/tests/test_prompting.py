from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.conversations.turns import deep_work_blueprint, research_blueprint
from backend.prompting.registry import PROMPT_DEFINITIONS, PromptRegistry
from backend.tools.catalog import create_tool_catalog


def test_shipped_prompts_are_complete(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)

    assert set(PROMPT_DEFINITIONS) == {
        "global",
        "research",
        "skills",
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
        "paper-summary-overview",
    }
    assert "evidence-backed answer" in registry.render("research")
    assert registry.render("fast-answer", web_search_limit=2).startswith(
        "Fast-answer mode is active. Make at most 2"
    )


def test_main_agent_prompts_use_notes_and_dedicated_summary_tools(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)

    for blueprint in (research_blueprint({}, prompts=registry), deep_work_blueprint({}, prompts=registry)):
        prompt = blueprint.agents[0].instructions
        assert "save_research_note" in prompt
        assert "summarize_research_paper" in prompt
        assert "list_workspace" in prompt
        assert "workspace_index" in prompt
        assert "BM25" in prompt
        assert "paper_summary_writer" not in prompt
        assert "research_note_writer" not in prompt


def test_chat_prompts_separate_effort_sources_and_artifacts(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)
    for blueprint in (research_blueprint({}, prompts=registry), deep_work_blueprint({}, prompts=registry)):
        prompt = blueprint.agents[0].instructions
        assert "set_conversation_title" not in prompt
        assert "Answer ordinary chat" in prompt
        assert "Quick does not mean web-only" in prompt
        assert "Summaries belong in the conversation unless the user asks to save them" in prompt
    assert "general conversation" in registry.render("global")
    assert "cannot execute code" in registry.render("global")


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


def test_note_guidance_requires_scoped_append_first_discovery(test_settings) -> None:
    registry = PromptRegistry(test_settings.prompt_config_dir)
    for name in ("research", "deep-work-worker"):
        prompt = registry.render(name).lower()
        for instruction in (
            "focused vocabulary variants", "supporting standalone", "only this note",
            "expected_sha256", "append", "explicit full", "partial",
            "/library/notes?path=",
        ):
            assert instruction in prompt
    definition = next(
        item for item in create_tool_catalog(registry).definitions()
        if item.catalog_id == "research.notes.save"
    )
    schema = definition.parameters_schema
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["mode"]["enum"] == ["append", "patch", "insert_after", "overwrite", None]
