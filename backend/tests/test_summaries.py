from __future__ import annotations

from types import SimpleNamespace

from backend.agents.blueprint import ModelReferenceSpec
from backend.persistence.database import create_session_factory
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.documents.summaries import PaperSummaryService, paper_summary_blueprint
from backend.workspace.repository import WorkspaceRepository
from backend.workspace.service import WorkspaceService


class Documents:
    def get_document(self, document_id: str):
        if document_id != "paper-1":
            return None
        return SimpleNamespace(id=document_id, title="A Strong Paper")


def test_summary_blueprint_is_one_model_job_with_direct_read_and_save(test_settings) -> None:
    prompts = PromptRegistry(test_settings.prompt_config_dir)

    blueprint = paper_summary_blueprint(ModelReferenceSpec().model_dump(), prompts)

    assert blueprint.entry_agent_id == "summarizer"
    assert [agent.id for agent in blueprint.agents] == ["summarizer"]
    assert blueprint.agent_tools == []
    assert blueprint.run.exclusive_inference is False
    assert {tool.catalog_id for tool in blueprint.tools} == {
        "research.summary.checkpoint",
        "research.summary.read",
        "research.summary.save",
    }
    assert blueprint.run.max_turns == 40
    assert blueprint.agents[0].model_settings.parallel_tool_calls is False
    assert "citation" in blueprint.agents[0].instructions.casefold()


def test_summary_blueprint_uses_selected_main_model(test_settings) -> None:
    prompts = PromptRegistry(test_settings.prompt_config_dir)
    blueprint = paper_summary_blueprint(
        {"provider_profile_id": "main-provider", "model": "main-model"},
        prompts,
    )

    assert blueprint.agents[0].model.model == "main-model"


def test_summary_versions_are_listed_and_promoted_without_touching_notes(test_settings) -> None:
    session_factory = create_session_factory(test_settings)
    workspace = WorkspaceService(
        SafeStorage(test_settings),
        WorkspaceRepository(session_factory),
    )
    workspace.ensure_paper_folder("paper-1", "A Strong Paper")
    workspace.write_file(
        "papers/paper-1/summaries/run-1.md",
        "# Versioned summary\n\nClaim [p.1]. Another result [p.2].\n",
    )
    workspace.write_file(
        "papers/paper-1/summaries/run-1.json",
        {
            "id": "run-1",
            "document_id": "paper-1",
            "run_id": "run-1",
            "path": "papers/paper-1/summaries/run-1.md",
            "created_at": "2026-09-03T10:00:00+00:00",
            "prompt_revision": "abc",
            "review_summary": "Citations checked.",
            "citation_count": 2,
            "status": "reviewed",
        },
    )
    workspace.write_file("papers/paper-1/notes.md", "# Notes\n\nKeep me.\n")
    service = PaperSummaryService(  # type: ignore[arg-type]
        compiler=None,
        runs=None,
        documents=Documents(),
        workspace=workspace,
        prompts=PromptRegistry(test_settings.prompt_config_dir),
    )

    versions = service.versions("paper-1")
    version, path, content = service.promote("paper-1", "run-1")

    assert [item["id"] for item in versions] == ["run-1"]
    assert version["prompt_revision"] == "abc"
    assert path == "papers/paper-1/summary.md"
    assert workspace.read_file(path).content == content
    assert workspace.read_file("papers/paper-1/notes.md").content.endswith("Keep me.\n")
    session_factory.kw["bind"].dispose()
