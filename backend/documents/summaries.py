from __future__ import annotations

from typing import Any

from backend.agents.blueprint import AgentBlueprint, ModelReferenceSpec, ReasoningEffort
from backend.agents.compiler import AgentCompiler
from backend.core.errors import NotFoundError, ValidationError
from backend.documents import DocumentService
from backend.prompting.registry import PromptRegistry
from backend.runs.service import RunService
from backend.workspace.service import WorkspaceService


class PaperSummaryService:
    def __init__(
        self,
        compiler: AgentCompiler,
        runs: RunService,
        documents: DocumentService,
        workspace: WorkspaceService,
        prompts: PromptRegistry,
    ) -> None:
        self._compiler = compiler
        self._runs = runs
        self._documents = documents
        self._workspace = workspace
        self._prompts = prompts

    def start(
        self,
        document_id: str,
        *,
        model_reference: ModelReferenceSpec,
        reasoning_effort: ReasoningEffort | None,
    ):
        document = self._documents.get_document(document_id)
        if document is None:
            raise NotFoundError("Paper was not found.")
        details = self._documents.get_document_details(document_id)
        if details is None or not details[2]:
            raise ValidationError("Paper must be ingested before it can be summarized.")
        self._workspace.ensure_paper_folder(document.id, document.title)
        blueprint = paper_summary_blueprint(
            model_reference.model_dump(mode="json"),
            self._prompts,
        )
        compiled = self._compiler.compile(blueprint)
        prompt_revision = self._prompts.revision
        instruction = (
            f"Create a reviewed summary version for document_id={document_id!r}, titled "
            f"{document.title!r}. This is a fresh summary job, so do not read the empty summary "
            "checkpoint before inspecting and reading the first paper batch. Read the paper "
            "directly, draft and self-check the complete "
            "citation-grounded summary, then call save_paper_summary_version exactly once. The save "
            "updates summary.md and retains an immutable version. Finish with the saved version path "
            "and material evidence limits."
        )
        return (
            self._runs.create(
                compiled,
                instruction,
                conversation_id=None,
                reasoning_effort=reasoning_effort,
                runtime_metadata={
                    "paper_summary_document_id": document_id,
                    "prompt_revision": prompt_revision,
                },
            ),
            prompt_revision,
        )

    def versions(self, document_id: str) -> list[dict[str, Any]]:
        self._require_document(document_id)
        prefix = f"papers/{document_id}/summaries/"
        versions: list[dict[str, Any]] = []
        for entry in self._workspace.list_files():
            if not entry.path.startswith(prefix) or not entry.path.endswith(".json"):
                continue
            document = self._workspace.read_file(entry.path)
            if isinstance(document.content, dict):
                versions.append(dict(document.content))
        return sorted(versions, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def version(self, document_id: str, version_id: str) -> tuple[dict[str, Any], str]:
        version = next(
            (item for item in self.versions(document_id) if item.get("id") == version_id),
            None,
        )
        if version is None:
            raise NotFoundError("Paper summary version was not found.")
        summary = self._workspace.read_file(str(version["path"]))
        if not isinstance(summary.content, str):
            raise ValidationError("Paper summary version is not Markdown.")
        return version, summary.content

    def promote(self, document_id: str, version_id: str) -> tuple[dict[str, Any], str, str]:
        version, content = self.version(document_id, version_id)
        document = self._require_document(document_id)
        paper = self._workspace.ensure_paper_folder(document.id, document.title)
        promoted = self._workspace.write_file(
            str(paper["summary_path"]),
            content,
            tags=["paper", f"paper:{document_id}", "summary"],
        )
        return version, promoted.path, content

    def _require_document(self, document_id: str):
        document = self._documents.get_document(document_id)
        if document is None:
            raise NotFoundError("Paper was not found.")
        return document


def paper_summary_blueprint(
    model_reference: dict[str, Any],
    prompts: PromptRegistry,
) -> AgentBlueprint:
    model = ModelReferenceSpec.model_validate(model_reference or {})
    return AgentBlueprint.model_validate(
        {
            "name": "ScholarWeave paper summary",
            "description": "Citation-grounded, reviewed, versioned paper summary.",
            "entry_agent_id": "summarizer",
            "agents": [
                {
                    "id": "summarizer",
                    "name": "Paper Summarizer",
                    "description": "Reads, summarizes, self-checks, and saves one paper.",
                    "instructions": (
                        f"{prompts.render('paper-summary')}\n\n"
                        "Complete the work in this single agent job. Read the paper directly, "
                        "self-check the complete draft against the gathered evidence, and call "
                        "save_paper_summary_version exactly once."
                    ),
                    "model": model,
                    "model_settings": {"parallel_tool_calls": False},
                    "tool_ids": ["summary-read", "summary-checkpoint", "summary-save"],
                },
            ],
            "tools": [
                {
                    "id": "summary-read",
                    "kind": "function",
                    "catalog_id": "research.summary.read",
                },
                {
                    "id": "summary-checkpoint",
                    "kind": "function",
                    "catalog_id": "research.summary.checkpoint",
                },
                {
                    "id": "summary-save",
                    "kind": "function",
                    "catalog_id": "research.summary.save",
                },
            ],
            "run": {"max_turns": 40},
        }
    )
