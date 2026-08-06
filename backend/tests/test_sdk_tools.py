from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.agents.blueprint import FunctionToolSpec
from backend.core.config import Settings
from backend.core.errors import ValidationError
from backend.persistence import create_session_factory
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
