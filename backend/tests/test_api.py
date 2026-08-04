from __future__ import annotations

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from backend.app import create_app
from backend.config import Settings
from backend.models import NodeRun, Run, RunEvent


def test_api_exposes_health_nodes_and_templates(test_settings) -> None:
    client = TestClient(create_app(test_settings))

    health = client.get("/api/health")
    nodes = client.get("/api/nodes")
    templates = client.get("/api/workflows/templates")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert nodes.status_code == 200
    assert any(node["type"] == "pdf_ingest" for node in nodes.json())
    assert templates.status_code == 200
    assert len(templates.json()) >= 3


def test_settings_survive_app_restart(test_settings) -> None:
    first_client = TestClient(create_app(test_settings))
    response = first_client.put(
        "/api/settings",
        json={
            "default_generation_model": "test-generation",
            "max_context_chars": 12345,
            "ocr_llm_enhancement_enabled": True,
            "ocr_llm_model": "test-vision",
            "ocr_llm_triage_model": "test-triage",
        },
    )
    assert response.status_code == 200

    restarted_settings = Settings(
        data_dir=test_settings.data_dir,
        workspace_dir=test_settings.workspace_dir,
        frontend_dist_dir=test_settings.frontend_dist_dir,
    )
    second_client = TestClient(create_app(restarted_settings))

    persisted = second_client.get("/api/settings")
    assert persisted.status_code == 200
    assert persisted.json()["default_generation_model"] == "test-generation"
    assert persisted.json()["max_context_chars"] == 12345
    assert persisted.json()["ocr_llm_enhancement_enabled"] is True
    assert persisted.json()["ocr_llm_model"] == "test-vision"
    assert persisted.json()["ocr_llm_triage_model"] == "test-triage"

    cleared = second_client.put("/api/settings", json={"ocr_llm_model": None, "ocr_llm_triage_model": None})
    assert cleared.status_code == 200
    assert cleared.json()["ocr_llm_model"] is None
    assert cleared.json()["ocr_llm_triage_model"] is None


def test_notes_api_lists_and_reads_nested_markdown(test_settings) -> None:
    note_path = test_settings.workspace_dir / "research" / "models.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("# Models\n\nLocal notes.", encoding="utf-8")
    (test_settings.workspace_dir / "ignored.txt").write_text("Not a note", encoding="utf-8")
    client = TestClient(create_app(test_settings))

    listing = client.get("/api/notes")
    content = client.get("/api/notes/content", params={"path": "research/models.md"})
    escaped = client.get("/api/notes/content", params={"path": "../outside.md"})

    assert listing.status_code == 200
    assert [note["path"] for note in listing.json()] == ["research/models.md"]
    assert content.status_code == 200
    assert content.json()["content"] == "# Models\n\nLocal notes."
    assert escaped.status_code == 400

    deleted = client.delete("/api/notes", params={"path": "research/models.md"})
    assert deleted.status_code == 204
    assert not note_path.exists()
    assert client.get("/api/notes/content", params={"path": "research/models.md"}).status_code == 404


def test_document_delete_removes_database_record_and_local_files(test_settings, tmp_path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    with pdf_path.open("wb") as output:
        writer.write(output)
    client = TestClient(create_app(test_settings))
    with pdf_path.open("rb") as pdf:
        uploaded = client.post(
            "/api/documents",
            files={"file": ("paper.pdf", pdf, "application/pdf")},
        )
    assert uploaded.status_code == 200
    document = uploaded.json()
    source = document["artifacts"][0]
    source_path = test_settings.documents_dir / source["relative_path"]
    assert source_path.exists()

    deleted = client.delete(f"/api/documents/{document['id']}")

    assert deleted.status_code == 204
    assert not source_path.exists()
    assert client.get(f"/api/documents/{document['id']}").status_code == 404


def test_workflow_and_terminal_run_deletion_preserves_history_boundaries(test_settings) -> None:
    app = create_app(test_settings)
    client = TestClient(app)
    definition = {
        "name": "Deletable workflow",
        "nodes": [
            {"id": "message", "type": "text_input", "config": {"value": "hello"}},
            {"id": "output", "type": "final_output"},
        ],
        "edges": [
            {
                "source_node_id": "message",
                "source_port": "text",
                "target_node_id": "output",
                "target_port": "content",
            }
        ],
    }
    created = client.post(
        "/api/workflows",
        json={"name": definition["name"], "definition": definition},
    )
    assert created.status_code == 200
    workflow = created.json()
    version_id = workflow["latest_version"]["id"]
    services = app.state.services
    with services.session_factory() as session:
        terminal_run = Run(
            workflow_version_id=version_id,
            workflow_name=definition["name"],
            status="completed",
            input_json={},
            output_json="hello",
            workflow_json=definition,
        )
        active_run = Run(
            workflow_name="Active workflow",
            status="running",
            input_json={},
            workflow_json=definition,
        )
        session.add_all([terminal_run, active_run])
        session.flush()
        session.add(
            NodeRun(
                run_id=terminal_run.id,
                node_path="output",
                node_id="output",
                node_type="final_output",
                status="completed",
                input_json={},
                output_json={"result": "hello"},
            )
        )
        session.add(
            RunEvent(
                run_id=terminal_run.id,
                event_type="run.completed",
                payload_json={"output": "hello"},
            )
        )
        session.commit()
        terminal_run_id = terminal_run.id
        active_run_id = active_run.id
    stored = services.storage.write_text(
        test_settings.artifacts_dir,
        f"runs/{terminal_run_id}/output.txt",
        "hello",
    )
    services.documents.create_artifact_record(
        owner_type="run",
        kind="run_output",
        run_id=terminal_run_id,
        relative_path=stored.relative_path,
        media_type="text/plain",
        stored=stored,
    )

    workflow_deleted = client.delete(f"/api/workflows/{workflow['id']}")

    assert workflow_deleted.status_code == 204
    assert client.get(f"/api/workflows/{workflow['id']}").status_code == 404
    retained_run = client.get(f"/api/runs/{terminal_run_id}")
    assert retained_run.status_code == 200
    assert retained_run.json()["workflow_version_id"] is None
    assert client.delete(f"/api/runs/{active_run_id}").status_code == 409

    run_deleted = client.delete(f"/api/runs/{terminal_run_id}")

    assert run_deleted.status_code == 204
    assert not stored.absolute_path.exists()
    assert client.get(f"/api/runs/{terminal_run_id}").status_code == 404


def test_app_restart_marks_interrupted_runs_as_failed(test_settings) -> None:
    first_app = create_app(test_settings)
    with first_app.state.services.session_factory() as session:
        run = Run(
            workflow_name="Interrupted workflow",
            status="running",
            input_json={},
            workflow_json={"name": "Interrupted workflow", "nodes": [], "edges": []},
        )
        session.add(run)
        session.flush()
        session.add(
            NodeRun(
                run_id=run.id,
                node_path="ingest",
                node_id="ingest",
                node_type="pdf_ingest",
                status="running",
                input_json={},
            )
        )
        session.commit()
        run_id = run.id

    restarted_app = create_app(test_settings)
    response = TestClient(restarted_app).get(f"/api/runs/{run_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert "application restart" in response.json()["error"]
    assert response.json()["node_runs"][0]["status"] == "failed"
    assert "application restart" in response.json()["node_runs"][0]["error"]


def test_export_python_endpoint_returns_a_parseable_script(test_settings) -> None:
    client = TestClient(create_app(test_settings))

    import ast

    templates = client.get("/api/workflows/templates").json()
    hierarchical = next(t for t in templates if t["name"] == "Hierarchical summary")
    response = client.post("/api/workflows/export/python", json=hierarchical)

    assert response.status_code == 200
    body = response.json()
    assert body["filename"].endswith(".py")
    ast.parse(body["source"])


def test_verify_provider_endpoint_reports_an_unreachable_provider(test_settings) -> None:
    client = TestClient(create_app(test_settings))

    client.put("/api/settings", json={"ollama_base_url": "http://127.0.0.1:9"})
    response = client.post("/api/agents/verify")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "ollama"
    assert body["reachable"] is False
    assert body["detail"]


def test_verify_provider_endpoint_confirms_a_working_provider(test_settings, stub_provider) -> None:
    client = TestClient(create_app(test_settings))

    client.put("/api/settings", json={"ollama_base_url": stub_provider.base_url, "default_generation_model": "stub-model"})
    stub_provider.call_tool = "record_colour"
    stub_provider.tool_arguments = {"colour": "teal"}
    stub_provider.reply = "done"
    response = client.post("/api/agents/verify")

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True, body["detail"]
    assert body["tool_calling"] is True, body["detail"]
