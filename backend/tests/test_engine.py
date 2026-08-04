from __future__ import annotations

import asyncio

from backend.app import create_backend_services
from backend.schemas import WorkflowDefinition


def test_engine_executes_map_subflow_and_persists_runs(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Mapper",
            "nodes": [
                {"id": "items", "type": "text_input", "config": {"input_key": "items"}},
                {
                    "id": "mapper",
                    "type": "map_subflow",
                    "config": {
                        "subflow": {
                            "name": "Echo item",
                            "nodes": [
                                {"id": "item", "type": "text_input", "config": {"input_key": "item"}},
                                {"id": "final", "type": "final_output"},
                            ],
                            "edges": [
                                {"source_node_id": "item", "source_port": "value", "target_node_id": "final", "target_port": "content"}
                            ],
                        }
                    },
                },
                {"id": "reduce", "type": "reduce_combine", "config": {"mode": "join_text"}},
                {"id": "final", "type": "final_output", "config": {"artifact_name": "mapped-output", "format": "txt"}},
            ],
            "edges": [
                {"source_node_id": "items", "source_port": "value", "target_node_id": "mapper", "target_port": "items"},
                {"source_node_id": "mapper", "source_port": "results", "target_node_id": "reduce", "target_port": "items"},
                {"source_node_id": "reduce", "source_port": "value", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )

    async def runner() -> tuple[str, object, list[str]]:
        run = await services.executor.start_run(workflow, {"items": ["alpha", "beta"]})
        for _ in range(40):
            await asyncio.sleep(0.05)
            current = services.executor.load_run(run.id)
            if current and current.status in {"completed", "failed", "cancelled"}:
                node_paths = [node_run.node_path for node_run in services.executor.load_node_runs(run.id)]
                return current.status, current.output_json, node_paths
        raise AssertionError("Run did not finish in time")

    status, output, node_paths = asyncio.run(runner())

    assert status == "completed"
    assert output == "alpha\n\nbeta"
    assert any(path.startswith("mapper[0].") for path in node_paths)
    assert any(path.startswith("mapper[1].") for path in node_paths)


def test_engine_cancels_running_workflow(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Slow final",
            "nodes": [
                {"id": "message", "type": "text_input", "config": {"value": "hello"}},
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "message", "source_port": "text", "target_node_id": "final", "target_port": "content"}
            ],
        }
    )
    node = services.registry.get("final_output")
    original = node.execute

    async def slow_execute(context, inputs, config):
        await asyncio.sleep(0.2)
        return await original(context, inputs, config)

    node.execute = slow_execute  # type: ignore[method-assign]

    async def runner() -> str:
        run = await services.executor.start_run(workflow, {})
        await asyncio.sleep(0.05)
        await services.executor.cancel_run(run.id)
        for _ in range(40):
            await asyncio.sleep(0.05)
            current = services.executor.load_run(run.id)
            if current and current.status in {"completed", "failed", "cancelled"}:
                return current.status
        raise AssertionError("Run did not reach a terminal state")

    try:
        status = asyncio.run(runner())
    finally:
        node.execute = original  # type: ignore[method-assign]

    assert status == "cancelled"


async def _run(services, workflow: WorkflowDefinition, inputs: dict):
    run = await services.executor.start_run(workflow, inputs)
    for _ in range(200):
        await asyncio.sleep(0.05)
        current = services.executor.load_run(run.id)
        if current and current.status in {"completed", "failed", "cancelled"}:
            return current
    raise AssertionError("Run did not finish in time")


def _if_else_workflow(value: str) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "If else",
            "nodes": [
                {"id": "source", "type": "text_input", "config": {"value": value}},
                {
                    "id": "route",
                    "type": "if_else",
                    "config": {
                        "condition": {
                            "type": "predicate",
                            "source": "inputs",
                            "path": "value",
                            "operator": "equals",
                            "value": "yes",
                        }
                    },
                },
                {"id": "yes", "type": "workflow_output", "config": {"key": "yes"}},
                {"id": "no", "type": "workflow_output", "config": {"key": "no"}},
            ],
            "edges": [
                {"source_node_id": "source", "source_port": "value", "target_node_id": "route", "target_port": "value"},
                {"source_node_id": "route", "source_port": "true", "target_node_id": "yes", "target_port": "value"},
                {"source_node_id": "route", "source_port": "false", "target_node_id": "no", "target_port": "value"},
            ],
        }
    )


def test_if_else_routes_both_branches_and_omits_inactive_workflow_output(test_settings) -> None:
    services = create_backend_services(test_settings)

    yes_run = asyncio.run(_run(services, _if_else_workflow("yes"), {}))
    no_run = asyncio.run(_run(services, _if_else_workflow("no"), {}))

    assert yes_run.status == no_run.status == "completed"
    assert yes_run.output_json == {"yes": "yes"}
    assert no_run.output_json == {"no": "no"}
    yes_nodes = {row.node_id: row for row in services.executor.load_node_runs(yes_run.id)}
    no_nodes = {row.node_id: row for row in services.executor.load_node_runs(no_run.id)}
    assert yes_nodes["no"].status == "skipped"
    assert no_nodes["yes"].status == "skipped"
    assert yes_nodes["route"].output_json == {"matched": True, "true": "yes"}
    assert no_nodes["route"].output_json == {"matched": False, "false": "no"}


def test_run_when_skips_node_records_event_and_propagates_inactivity(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Run when",
            "nodes": [
                {"id": "source", "type": "text_input", "config": {"input_key": "value"}},
                {
                    "id": "conditional",
                    "type": "plain_text",
                    "run_when": {
                        "type": "predicate",
                        "source": "workflow",
                        "path": "enabled",
                        "operator": "truthy",
                    },
                },
                {"id": "output", "type": "workflow_output", "config": {"key": "result"}},
            ],
            "edges": [
                {"source_node_id": "source", "source_port": "value", "target_node_id": "conditional", "target_port": "value"},
                {"source_node_id": "conditional", "source_port": "text", "target_node_id": "output", "target_port": "value"},
            ],
        }
    )

    true_run = asyncio.run(_run(services, workflow, {"value": "hello", "enabled": True}))
    run = asyncio.run(_run(services, workflow, {"value": "hello", "enabled": False}))

    assert true_run.status == "completed"
    assert true_run.output_json == "hello"
    assert run.status == "completed"
    assert run.output_json == {}
    rows = {row.node_id: row for row in services.executor.load_node_runs(run.id)}
    assert rows["conditional"].status == "skipped"
    assert rows["conditional"].input_json == {"value": "hello"}
    assert rows["conditional"].error == "run_when condition evaluated to false"
    assert rows["conditional"].finished_at is not None
    assert rows["output"].status == "skipped"
    skipped_events = [event.payload_json for event in services.executor.load_events(run.id) if event.event_type == "node.skipped"]
    assert any(event["node_path"] == "conditional" and event["reason"].startswith("run_when") for event in skipped_events)


def test_branch_skip_propagates_through_optional_target_and_merge_uses_active_input(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Branch merge",
            "nodes": [
                {"id": "source", "type": "text_input", "config": {"value": "yes"}},
                {
                    "id": "route",
                    "type": "if_else",
                    "config": {
                        "condition": {
                            "type": "predicate",
                            "source": "inputs",
                            "path": "value",
                            "operator": "equals",
                            "value": "yes",
                        }
                    },
                },
                {"id": "first", "type": "plain_text"},
                {"id": "second", "type": "plain_text"},
                {"id": "optional_sink", "type": "final_output", "config": {"output_key": "inactive"}},
                {"id": "merge", "type": "merge"},
                {"id": "output", "type": "workflow_output", "config": {"key": "result"}},
            ],
            "edges": [
                {"source_node_id": "source", "source_port": "value", "target_node_id": "route", "target_port": "value"},
                {"source_node_id": "route", "source_port": "true", "target_node_id": "first", "target_port": "value"},
                {"source_node_id": "first", "source_port": "text", "target_node_id": "second", "target_port": "value"},
                {"source_node_id": "route", "source_port": "false", "target_node_id": "optional_sink", "target_port": "content"},
                {"source_node_id": "route", "source_port": "true", "target_node_id": "merge", "target_port": "left"},
                {"source_node_id": "route", "source_port": "false", "target_node_id": "merge", "target_port": "right"},
                {"source_node_id": "merge", "source_port": "value", "target_node_id": "output", "target_port": "value"},
            ],
        }
    )

    run = asyncio.run(_run(services, workflow, {}))

    assert run.status == "completed"
    assert run.output_json == {"left": "yes", "right": None}
    rows = {row.node_id: row for row in services.executor.load_node_runs(run.id)}
    assert rows["first"].status == rows["second"].status == "completed"
    assert rows["optional_sink"].status == "skipped"
    assert rows["merge"].status == "completed"


def _note_folder_workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "Note folder",
            "nodes": [
                {"id": "name", "type": "text_input", "config": {"input_key": "name"}},
                {"id": "title", "type": "text_input", "config": {"input_key": "title"}},
                {"id": "summary", "type": "text_input", "config": {"input_key": "summary"}},
                {"id": "sections", "type": "text_input", "config": {"input_key": "sections"}},
                {"id": "notes", "type": "write_note_folder"},
                {"id": "final", "type": "final_output"},
            ],
            "edges": [
                {"source_node_id": "name", "source_port": "text", "target_node_id": "notes", "target_port": "folder"},
                {"source_node_id": "title", "source_port": "value", "target_node_id": "notes", "target_port": "title"},
                {"source_node_id": "summary", "source_port": "value", "target_node_id": "notes", "target_port": "summary"},
                {"source_node_id": "sections", "source_port": "value", "target_node_id": "notes", "target_port": "items"},
                {"source_node_id": "notes", "source_port": "paths", "target_node_id": "final", "target_port": "content"},
            ],
        }
    )


def test_note_folder_writes_summary_and_sections_into_one_workspace_folder(test_settings) -> None:
    """Notes only lists the workspace, so the summariser has to land its files there."""
    services = create_backend_services(test_settings)
    run = asyncio.run(
        _run(
            services,
            _note_folder_workflow(),
            {
                "name": "**Name:** Sparse Attention Scaling\n",
                "title": {"id": "doc-1", "title": "Scaling Laws for Sparse Attention"},
                "summary": "## Summary\nIt scales.",
                "sections": ["First batch of notes.", "SKIP", "  ", "Second batch of notes."],
            },
        )
    )

    assert run.status == "completed", run.error
    assert run.output_json == [
        "sparse-attention-scaling/summary.md",
        "sparse-attention-scaling/section-01.md",
        "sparse-attention-scaling/section-02.md",
    ]

    notes = services.storage.list_workspace_markdown()
    assert [note.relative_path for note in notes] == [
        "sparse-attention-scaling/section-01.md",
        "sparse-attention-scaling/section-02.md",
        "sparse-attention-scaling/summary.md",
    ]
    _, first = services.storage.read_workspace_markdown("sparse-attention-scaling/section-01.md")
    assert first.startswith("# Section 1 — Scaling Laws for Sparse Attention")
    assert "First batch of notes." in first


def test_note_folder_rewrite_drops_notes_left_by_a_longer_run(test_settings) -> None:
    services = create_backend_services(test_settings)
    workflow = _note_folder_workflow()
    inputs = {
        "name": "Sparse Attention Scaling",
        "title": "Scaling Laws for Sparse Attention",
        "summary": "First pass.",
        "sections": ["one", "two", "three"],
    }
    asyncio.run(_run(services, workflow, inputs))
    asyncio.run(_run(services, workflow, {**inputs, "sections": ["one"]}))

    assert [note.relative_path for note in services.storage.list_workspace_markdown()] == [
        "sparse-attention-scaling/section-01.md",
        "sparse-attention-scaling/summary.md",
    ]


def test_note_folder_falls_back_to_the_title_when_the_model_says_nothing(test_settings) -> None:
    services = create_backend_services(test_settings)
    run = asyncio.run(
        _run(
            services,
            _note_folder_workflow(),
            {"name": "   ", "title": "Scaling Laws for Sparse Attention", "summary": "Body.", "sections": []},
        )
    )

    assert run.status == "completed", run.error
    assert run.output_json == ["scaling-laws-for-sparse-attention/summary.md"]
