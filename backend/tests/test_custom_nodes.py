from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.schemas import WorkflowDefinition


def _spec() -> dict:
    return {
        "name": "prefix-text",
        "label": "Prefix text",
        "description": "Adds a configured suffix.",
        "category": "custom",
        "tags": ["test"],
        "inputs": [{"name": "text", "kind": "text"}],
        "outputs": [{"name": "result", "kind": "text"}],
        "config_fields": [
            {"name": "suffix", "kind": "text", "required": True, "default": "!"},
        ],
        "code": (
            "def transform(inputs, config):\n"
            "    print('custom stdout')\n"
            "    return {'result': inputs['text'] + config['suffix']}\n"
        ),
    }


def test_custom_node_crud_revisions_catalog_and_archived_resolution(test_settings) -> None:
    app = create_app(test_settings)
    client = TestClient(app)
    created = client.post("/api/custom-nodes", json=_spec())
    assert created.status_code == 200
    first = created.json()
    old_type = first["latest_revision"]["node_type"]
    assert any(node["type"] == old_type for node in client.get("/api/nodes").json())

    revised_spec = _spec()
    revised_spec["label"] = "Prefix text v2"
    revised_spec["code"] = "def transform(inputs, config):\n    return {'result': inputs['text']}"
    revised = client.put(f"/api/custom-nodes/{first['id']}", json=revised_spec)
    assert revised.status_code == 200
    body = revised.json()
    assert body["latest_revision"]["revision"] == 2
    assert len(body["revisions"]) == 2
    assert body["latest_revision"]["node_type"] != old_type
    assert any(node["type"] == body["latest_revision"]["node_type"] for node in client.get("/api/nodes").json())
    assert app.state.services.registry.get(old_type).type_name == old_type

    assert client.post(f"/api/custom-nodes/{first['id']}/archive").status_code == 200
    assert not any(node["type"].startswith("custom:") for node in client.get("/api/nodes").json())
    assert app.state.services.registry.get(old_type).type_name == old_type
    assert client.get("/api/custom-nodes").json() == []
    assert len(client.get("/api/custom-nodes", params={"include_archived": "true"}).json()) == 1


def test_custom_node_sample_validates_config_outputs_and_stdout(test_settings) -> None:
    client = TestClient(create_app(test_settings))
    spec = _spec()
    spec.pop("name")
    sample = client.post("/api/custom-nodes/sample", json={"spec": spec, "inputs": {"text": "hi"}, "config": {}})
    assert sample.status_code == 200
    assert sample.json() == {"output": {"result": "hi!"}, "stdout": "custom stdout\n"}

    invalid_config = client.post(
        "/api/custom-nodes/sample",
        json={"spec": spec, "inputs": {"text": "hi"}, "config": {"extra": True}},
    )
    assert invalid_config.status_code == 422

    bad_output = dict(spec)
    bad_output["code"] = "def transform(inputs, config):\n    return {'wrong': 'x'}"
    result = client.post("/api/custom-nodes/sample", json={"spec": bad_output, "inputs": {"text": "hi"}, "config": {}})
    assert result.status_code == 400
    assert "undeclared output" in result.json()["detail"]

    missing_output = dict(spec)
    missing_output["code"] = "def transform(inputs, config):\n    return {}"
    result = client.post(
        "/api/custom-nodes/sample", json={"spec": missing_output, "inputs": {"text": "hi"}, "config": {}}
    )
    assert result.status_code == 400
    assert "required outputs" in result.json()["detail"]


def test_workflow_executes_pinned_custom_node_and_emits_stdout(test_settings) -> None:
    app = create_app(test_settings)
    client = TestClient(app)
    created = client.post("/api/custom-nodes", json=_spec()).json()
    node_type = created["latest_revision"]["node_type"]
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "Custom runtime",
            "nodes": [
                {
                    "id": "custom",
                    "type": node_type,
                    "config": {"suffix": "?"},
                    "static_inputs": {"text": "hello"},
                },
                {"id": "output", "type": "final_output"},
            ],
            "edges": [
                {
                    "source_node_id": "custom",
                    "source_port": "result",
                    "target_node_id": "output",
                    "target_port": "content",
                }
            ],
        }
    )

    async def run() -> tuple[object, list[object]]:
        services = app.state.services
        started = await services.executor.start_run(workflow, {})
        for _ in range(100):
            await asyncio.sleep(0.02)
            current = services.executor.load_run(started.id)
            if current and current.status in {"completed", "failed", "cancelled"}:
                return current, services.executor.load_events(started.id)
        raise AssertionError("workflow did not finish")

    run, events = asyncio.run(run())
    assert run.status == "completed", run.error
    assert run.output_json == "hello?"
    assert any(event.event_type == "node.stdout" and "custom stdout" in event.payload_json["text"] for event in events)
