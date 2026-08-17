from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.agents.blueprint import FunctionToolSpec
from backend.core.config import Settings
from backend.core.errors import ValidationError
from backend.persistence import create_session_factory
from backend.documents.models import Document
from backend.runtime.context import ScholarWeaveContext
from backend.bootstrap import create_services
from backend.tools.catalog import create_tool_catalog
from backend.tools.repository import FunctionToolRepository
from backend.tools.service import FunctionToolService


class Runtime:
    def __init__(self) -> None:
        self.calls = []

    async def invoke(self, catalog_id, arguments, context):
        self.calls.append((catalog_id, arguments, context.run_id))
        return {"ok": True}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_builtin_catalog_builds_sdk_function_tool() -> None:
    catalog = create_tool_catalog()
    tool = catalog.build_function_tool(
        FunctionToolSpec(id="list", catalog_id="documents.list")
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)

    output = await tool.on_invoke_tool(
        SimpleNamespace(context=context),
        "{}",
    )

    assert output == {"ok": True}
    assert runtime.calls == [("documents.list", {}, "run-1")]
    assert tool.name == "list_documents"


@pytest.mark.anyio
async def test_builtin_tools_unwrap_nested_agent_tool_context() -> None:
    catalog = create_tool_catalog()
    tool = catalog.build_function_tool(
        FunctionToolSpec(id="list", catalog_id="documents.list")
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)
    nested_context = SimpleNamespace(context=SimpleNamespace(context=context))

    output = await tool.on_invoke_tool(nested_context, "{}")

    assert output == {"ok": True}
    assert runtime.calls == [("documents.list", {}, "run-1")]


def test_extended_note_schema_avoids_unsupported_array_constraints() -> None:
    catalog = create_tool_catalog()
    tool = catalog.build_function_tool(
        FunctionToolSpec(id="note", catalog_id="extended.notes.save")
    )

    sources = tool.params_json_schema["properties"]["sources"]
    assert sources == {"type": "array", "items": {"type": "string"}}


@pytest.mark.parametrize("catalog_id", ["workspace.write", "artifacts.write"])
def test_builtin_json_content_tools_define_array_items(catalog_id: str) -> None:
    catalog = create_tool_catalog()
    tool = catalog.build_function_tool(
        FunctionToolSpec(id="write", catalog_id=catalog_id)
    )

    content_schema = tool.params_json_schema["properties"]["content"]
    assert "array" in content_schema["type"]
    assert content_schema["items"]["type"] == [
        "object",
        "string",
        "number",
        "boolean",
        "null",
    ]


@pytest.mark.anyio
async def test_sdk_catalog_exposes_blueprint_contract(test_settings) -> None:
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="catalog-run", tool_runtime=runtime)

        result = await runtime.invoke("sdk.catalog", {}, context)

        schema = result["agent_blueprint_schema"]
        assert {"entry_agent_id", "agents"} <= set(schema["required"])
        assert result["agent_blueprint_examples"][0]["tools"][0]["kind"] == "function"
    finally:
        await services.close()


@pytest.mark.anyio
async def test_autonomous_tool_search_finds_builtin_and_custom_tools(test_settings) -> None:
    services = create_services(test_settings)
    try:
        services.function_tools.create(
            name="extract_claims",
            description="Extract evidence-backed claims from research text.",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema=None,
            code="def invoke(arguments):\n    return {'claims': [arguments['text']]}\n",
            requires_approval=True,
        )
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="tool-search-run", tool_runtime=runtime)

        builtin = await runtime.invoke(
            "tools.search",
            {"query": "keyword"},
            context,
        )
        custom = await runtime.invoke(
            "tools.search",
            {"query": "evidence"},
            context,
        )

        assert "search_papers" in [tool["name"] for tool in builtin["tools"]]
        tag_tools = await runtime.invoke(
            "tools.search",
            {"query": "tags"},
            context,
        )
        assert {
            "search_workspace_file_tags",
            "set_workspace_file_tags",
        } <= {tool["name"] for tool in tag_tools["tools"]}
        paper_name_tools = await runtime.invoke(
            "tools.search",
            {"query": "display name"},
            context,
        )
        assert "set_paper_workspace_name" in {
            tool["name"] for tool in paper_name_tools["tools"]
        }
        note_tools = await runtime.invoke(
            "tools.search",
            {"query": "server-generated"},
            context,
        )
        assert "create_workspace_note" in {
            tool["name"] for tool in note_tools["tools"]
        }
        assert [tool["name"] for tool in custom["tools"]] == ["extract_claims"]
        assert custom["tools"][0]["requires_approval"] is True
    finally:
        await services.close()


@pytest.mark.anyio
async def test_workspace_markdown_and_tag_tools_are_wired(test_settings) -> None:
    services = create_services(test_settings)
    try:
        services.workspace.write_file("notes/paper.md", "# Paper\n\nOld")
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="workspace-run", tool_runtime=runtime)

        await runtime.invoke(
            "workspace.markdown.replace",
            {
                "path": "notes/paper.md",
                "old_text": "Old",
                "new_text": "New",
                "replace_all": False,
            },
            context,
        )
        await runtime.invoke(
            "workspace.markdown.append",
            {"path": "notes/paper.md", "content": "\nMore"},
            context,
        )
        tagged = await runtime.invoke(
            "workspace.tags.set",
            {"path": "notes/paper.md", "tags": ["paper", "attention"]},
            context,
        )
        matches = await runtime.invoke(
            "workspace.tags.search",
            {"tags": ["ATTENTION"]},
            context,
        )

        assert services.workspace.read_file("notes/paper.md").content == (
            "# Paper\n\nNew\n\nMore"
        )
        assert tagged["tags"] == ["paper", "attention"]
        assert matches == [
            {"path": "notes/paper.md", "tags": ["paper", "attention"]}
        ]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_workspace_note_creation_and_indexed_search_are_wired(test_settings) -> None:
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="note-run", tool_runtime=runtime)

        created = await runtime.invoke(
            "workspace.note.create",
            {
                "name": "Attention implementation ideas",
                "content": "Test grouped-query attention with a paged KV cache.",
                "tags": ["attention", "implementation"],
            },
            context,
        )
        found = await runtime.invoke(
            "workspace.search",
            {
                "query": "paged cache",
                "kinds": ["note"],
                "tags": ["attention"],
                "limit": 10,
                "offset": 0,
            },
            context,
        )

        assert created["path"] == f"notes/{created['note_id']}/note.md"
        assert created["name"] == "Attention implementation ideas"
        assert found == [
            {
                "path": created["path"],
                "name": "Attention implementation ideas",
                "kind": "note",
                "tags": ["note", "attention", "implementation"],
                "modified_at": found[0]["modified_at"],
            }
        ]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_paper_tools_report_and_reject_missing_extracted_content(
    test_settings,
) -> None:
    services = create_services(test_settings)
    try:
        document_id = "metadata-only-paper"
        with services.session_factory() as session:
            session.add(
                Document(
                    id=document_id,
                    title="Metadata Only",
                    source_filename="paper.pdf",
                    content_type="application/pdf",
                    status="ready",
                    page_count=22,
                    metadata_json={},
                )
            )
            session.commit()
        source = services.storage.write_text(
            test_settings.documents_dir,
            f"{document_id}/source/paper.pdf",
            "source",
        )
        services.documents.create_artifact_record(
            owner_type="document",
            kind="source_pdf",
            document_id=document_id,
            relative_path=source.relative_path,
            media_type="application/pdf",
            stored=source,
            storage_area="documents",
        )
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="paper-tools-run", tool_runtime=runtime)

        listed = await runtime.invoke("documents.list", {}, context)
        inspected = await runtime.invoke(
            "documents.inspect",
            {"document_id": document_id},
            context,
        )

        assert listed[0]["readable"] is False
        assert listed[0]["next_action"] == "inspect_paper"
        assert inspected["source_available"] is True
        assert inspected["readable"] is False
        assert "ingest_paper" in inspected["next_action"]
        with pytest.raises(ValueError, match="Inspect and ingest"):
            await runtime.invoke(
                "documents.read_chunks",
                {"document_id": document_id, "start": 0, "limit": 20},
                context,
            )
        with pytest.raises(ValueError, match="Inspect and ingest"):
            await runtime.invoke(
                "documents.read_pages",
                {"document_id": document_id, "start_page": 1, "limit": 5},
                context,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_blueprint_validation_returns_repairable_schema_issues(test_settings) -> None:
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="validation-run", tool_runtime=runtime)

        result = await runtime.invoke(
            "agents.validate",
            {
                "blueprint": {
                    "name": "Invalid flat blueprint",
                    "instructions": "Placed at the wrong level.",
                    "tools": [{"type": "function", "catalog_id": "documents.list"}],
                }
            },
            context,
        )

        assert result["valid"] is False
        assert any(issue.startswith("entry_agent_id:") for issue in result["issues"])
        assert any(issue.startswith("agents:") for issue in result["issues"])
        assert any(issue.startswith("tools.0:") for issue in result["issues"])
    finally:
        await services.close()


@pytest.mark.anyio
async def test_custom_function_tool_executes_in_sandbox(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    repository = FunctionToolRepository(create_session_factory(settings))
    service = FunctionToolService(repository, settings)
    document = service.create(
        name="uppercase",
        description="Uppercase text.",
        parameters_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        code="def invoke(arguments):\n    return {'text': arguments['text'].upper()}\n",
        requires_approval=True,
    )
    spec = FunctionToolSpec(
        id="uppercase-tool",
        catalog_id=f"custom:{document.latest_revision.id}",
    )
    tool = service.dynamic_factory(spec)
    assert tool is not None

    output = await tool.on_invoke_tool(
        SimpleNamespace(context=None),
        json.dumps({"text": "paper"}),
    )

    assert output == {"text": "PAPER"}
    assert tool.needs_approval is True


def test_custom_function_tool_rejects_non_sdk_contract(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "metadata.sqlite3",
    )
    settings.ensure_directories()
    service = FunctionToolService(
        FunctionToolRepository(create_session_factory(settings)),
        settings,
    )

    with pytest.raises(ValidationError, match=r"invoke\(arguments\)"):
        service.create(
            name="legacy_transform",
            description="Invalid.",
            parameters_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema=None,
            code="def transform(inputs, config):\n    return inputs\n",
            requires_approval=False,
        )
