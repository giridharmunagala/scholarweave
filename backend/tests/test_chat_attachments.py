from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from backend.app import create_app
from backend.core.errors import NotFoundError
from backend.tests.test_api import configure_provider, wait_for_run


def pdf_bytes(text: str = "Proposal bibliography: Sparse Methods studies efficient research retrieval.") -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 50 720 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def upload(client: TestClient, name: str, content: bytes) -> dict:
    response = client.post(
        "/api/agent/attachments",
        files={"file": (name, content, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def conversation(client: TestClient, stub_provider, *, deep_work: bool = False) -> dict:
    profile_id = configure_provider(client, stub_provider)
    return client.post(
        f"/api/{'deep-work' if deep_work else 'agent'}/conversations",
        json={"model_reference": {"provider_profile_id": profile_id, "model": "stub-model"}},
    ).json()


@pytest.mark.parametrize("extension", ["md", "txt", "MD"])
def test_text_upload_is_safe_indexed_and_durable(test_settings, extension):
    app = create_app(test_settings)
    with TestClient(app) as client:
        content = "# Proposal\n\nCompare spectroscopy research.\r\n"
        attachment = upload(client, f"..\\outside\\proposal.{extension}", content.encode())
        assert attachment["path"].startswith("inbox/attachments/")
        assert ".." not in attachment["path"]
        assert attachment["document_id"] is None
        assert attachment["size_bytes"] == len(content.encode())
        assert app.state.services.workspace.read_file(attachment["path"]).content == content
        assert client.get(
            "/api/workspace/search", params={"query": "spectroscopy"},
        ).json()[0]["path"] == attachment["path"]
        assert client.get("/api/agent/conversations").json() == []
        assert client.get("/api/runs").json() == []
    with TestClient(create_app(test_settings)) as client:
        assert client.get(
            "/api/workspace/files/content", params={"path": attachment["path"]},
        ).json()["content"] == content
        assert client.get(
            "/api/workspace/search", params={"query": "spectroscopy"},
        ).json()[0]["path"] == attachment["path"]


def test_reupload_reuses_text_without_overwriting_edits(test_settings):
    with TestClient(create_app(test_settings)) as client:
        first = upload(client, "tasks.md", b"Summarize my references.")
        second = upload(client, "tasks.md", b"Summarize my references.")
        assert first["path"] == second["path"]
        workspace = client.app.state.services.workspace
        workspace.write_file(first["path"], "User's later edits.")
        third = upload(client, "tasks.md", b"Summarize my references.")
        assert third["path"] != first["path"]
        assert workspace.read_file(first["path"]).content == "User's later edits."
        assert upload(client, "tasks.md", b"Summarize my references.")["path"] == third["path"]
        other = upload(client, "tasks.md", b"A different task.")
        assert other["path"] not in {first["path"], third["path"]}


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("tasks.py", b"print('no')", "Markdown"),
        ("tasks.docx", b"not supported", "Markdown"),
        ("tasks.md", b"", "empty"),
        ("tasks.txt", b" \n ", "non-empty"),
        ("tasks.md", b"\xff\xfe", "UTF-8"),
        ("tasks.txt", b"binary\x00text", "readable"),
        ("proposal.pdf", b"not a pdf", "readable PDF"),
    ],
    ids=["code", "word", "empty", "blank", "encoding", "binary", "invalid-pdf"],
)
def test_invalid_uploads_do_not_create_workspace_files(test_settings, filename, content, message):
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/agent/attachments", files={"file": (filename, content)})
        assert response.status_code == 400, response.text
        assert message in response.json()["message"]
        assert client.app.state.services.workspace.list_files() == []
        assert client.get("/api/documents").json() == []


@pytest.mark.parametrize("filename", ["large.md", "large.txt", "large.pdf"])
def test_attachment_upload_size_is_bounded(test_settings, filename):
    test_settings.max_upload_bytes = 30
    test_settings.max_workspace_file_bytes = 20
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/agent/attachments", files={"file": (filename, b"x" * 31)})
        assert response.status_code == 400
        assert "maximum allowed size" in response.json()["message"]
        assert client.app.state.services.workspace.list_files() == []


def test_pdf_upload_prepares_and_reuses_local_source(test_settings, monkeypatch):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        source = pdf_bytes()
        attachment = upload(client, "proposal.pdf", source)
        document_id = attachment["document_id"]
        assert document_id
        assert attachment["name"] == "proposal.pdf"
        assert attachment["media_type"] == "application/pdf"
        assert attachment["size_bytes"] == len(source)
        services = app.state.services
        details = services.documents.get_document_details(document_id)
        assert details[0].status == "ready"
        assert details[2]
        original = next(artifact for artifact in details[1] if artifact.kind == "source_pdf")
        assert services.documents.artifact_bytes(original) == source
        assert original.metadata_json["storage_area"] == "workspace"
        assert "bibliography" in services.workspace.read_file(attachment["path"]).content
        hits = client.get("/api/workspace/search", params={"query": "bibliography"}).json()
        assert hits[0]["path"] == attachment["path"]
        repeat = upload(client, "renamed.pdf", source)
        assert repeat["path"] == attachment["path"]
        assert repeat["document_id"] == document_id
        assert len(client.get("/api/documents").json()) == 1
        assert client.get("/api/runs").json() == []


def test_pdf_preparation_failure_is_not_a_ready_attachment(test_settings, monkeypatch):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        source = pdf_bytes("")
        for _ in range(2):
            response = client.post(
                "/api/agent/attachments", files={"file": ("scanned.pdf", source)},
            )
            assert response.status_code == 400, response.text
            assert "requires OCR" in response.json()["message"]
        assert len(client.get("/api/documents").json()) == 1
        assert all("/attachments/" not in item.path for item in app.state.services.workspace.list_files())
        assert client.get("/api/runs").json() == []


def test_pdf_reattachment_preserves_text_edits(test_settings, monkeypatch):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        source = pdf_bytes()
        first = upload(client, "proposal.pdf", source)
        workspace = app.state.services.workspace
        workspace.write_file(first["path"], "Researcher's annotated copy.")
        second = upload(client, "proposal.pdf", source)
        assert second["path"] != first["path"]
        assert second["document_id"] == first["document_id"]
        assert workspace.read_file(first["path"]).content == "Researcher's annotated copy."
        assert upload(client, "proposal.pdf", source)["path"] == second["path"]


@pytest.mark.parametrize("kind", ["source_pdf", "extracted_markdown"])
def test_pdf_reattachment_repairs_missing_files(test_settings, monkeypatch, kind):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        source = pdf_bytes()
        first = upload(client, "proposal.pdf", source)
        services = app.state.services
        artifact = next(
            item for item in services.documents.get_document_artifacts(first["document_id"])
            if item.kind == kind
        )
        base = test_settings.workspace_dir if kind == "source_pdf" else test_settings.artifacts_dir
        services.storage.delete_stored_file(base, artifact.relative_path)
        second = upload(client, "proposal.pdf", source)
        assert first["path"] == second["path"]
        assert first["document_id"] == second["document_id"]
        assert services.documents.artifact_bytes(artifact)
        assert len(client.get("/api/documents").json()) == 1


def test_pdf_reattachment_does_not_overwrite_changed_source(test_settings, monkeypatch):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        source = pdf_bytes()
        first = upload(client, "proposal.pdf", source)
        services = app.state.services
        artifact = next(
            item for item in services.documents.get_document_artifacts(first["document_id"])
            if item.kind == "source_pdf"
        )
        changed = pdf_bytes("A changed source PDF must remain untouched by a later upload.")
        services.storage.write_workspace_document(artifact.relative_path, changed)
        response = client.post("/api/agent/attachments", files={"file": ("proposal.pdf", source)})
        assert response.status_code == 400, response.text
        assert "changed outside" in response.json()["message"]
        assert services.documents.artifact_bytes(artifact) == changed


@pytest.mark.parametrize("invalid_kind", ["encrypted", "page-tree"])
def test_structural_pdf_errors_are_actionable(test_settings, invalid_kind):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    if invalid_kind == "encrypted":
        writer.encrypt("password")
    else:
        del writer._root_object["/Pages"]["/Kids"]
    output = io.BytesIO()
    writer.write(output)
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/agent/attachments", files={"file": ("proposal.pdf", output.getvalue())})
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "validation_error"
        assert client.get("/api/documents").json() == []


def test_pdf_extracted_text_limit_is_explicit(test_settings, monkeypatch):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    # Paper templates fit, but a larger extracted document must not be silently truncated.
    test_settings.max_workspace_file_bytes = 300
    with TestClient(app) as client:
        response = client.post(
            "/api/agent/attachments",
            files={"file": ("long.pdf", pdf_bytes("Research evidence. " * 40))},
        )
        assert response.status_code == 400, response.text
        assert "extracted text exceeds" in response.json()["message"]
        assert len(client.get("/api/documents").json()) == 1


@pytest.mark.parametrize("effort", ["auto", "quick", "thorough"])
@pytest.mark.parametrize(
    ("goal", "file_text", "answer"),
    [
        ("Summarize this proposal.", "# Proposal\nStudy spectroscopy with sparse data.",
         "The proposal studies spectroscopy using sparse data."),
        ("Do something with this document.", "# Proposal\nStudy spectroscopy.",
         "Would you like a summary, answers to questions, or a review of its references?"),
        ("Execute the tasks defined in this file.", "# Tasks\nBuild and run a web application.",
         "I can't do that in ScholarWeave. I support research, document Q&A, and summarization, not building software."),
    ],
    ids=["summary", "clarification", "refusal"],
)
def test_file_task_reading_clarification_and_refusal(
    test_settings, stub_provider, effort, goal, file_text, answer,
):
    with TestClient(create_app(test_settings)) as client:
        chat = conversation(client, stub_provider)
        attachment = upload(client, "tasks.md", file_text.encode())
        stub_provider.tool_plans = [(goal, "read_research_note", {"path": attachment["path"]})]
        stub_provider.reply = answer
        endpoint = f"/api/agent/conversations/{chat['id']}/messages"
        response = client.post(endpoint, json={
            "content": goal, "attachment_paths": [attachment["path"]], "web_enabled": False,
            "response_effort": effort,
        })
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        record = client.app.state.services.runs.get(run["id"])
        assert [attempt.catalog_id for attempt in record.tool_attempts] == ["research.notes.read"]
        assert record.tool_attempts[0].result_json["content"] == file_text
        assert not record.runtime_metadata_json.get("work_plan")
        assert run["final_output"] == answer
        instructions = json.dumps(stub_provider.requests[0]["messages"])
        assert "cannot execute code" in instructions
        assert "Ask one focused question" in instructions
        assert "Attached workspace files (saved and indexed)" in instructions
        assert attachment["path"] in instructions
        assert len(client.app.state.services.workspace.list_files()) == 1
        detail = client.get(f"/api/agent/conversations/{chat['id']}").json()
        assert attachment["path"] in json.dumps(detail["items"])
        stub_provider.tool_plans = []
        follow_up = client.post(endpoint, json={"content": "Discuss the same file.", "web_enabled": False})
        assert wait_for_run(client, follow_up.json()["run"]["id"])["status"] == "completed"
        assert attachment["path"] in json.dumps(stub_provider.requests[-1]["messages"])


def test_bad_attachment_paths_cannot_start_runs(test_settings, stub_provider):
    with TestClient(create_app(test_settings)) as client:
        chat = conversation(client, stub_provider)
        endpoint = f"/api/agent/conversations/{chat['id']}/messages"
        for path in ["../outside.md", str(test_settings.workspace_dir / "absolute.md"), "missing.md", "code.py"]:
            response = client.post(endpoint, json={"content": "Summarize it.", "attachment_paths": [path]})
            assert response.status_code in {400, 404}, response.text
        attachment = upload(client, "tasks.md", b"Research instructions.")
        for payload in [
            {"fast_answer": True, "attachment_paths": [attachment["path"]]},
            {"attachment_paths": [attachment["path"]] * 11},
            {"attachment_paths": [""]},
        ]:
            response = client.post(endpoint, json={"content": "Summarize it.", **payload})
            assert response.status_code == 422, response.text
        assert client.get("/api/runs").json() == []
        assert client.get(f"/api/agent/conversations/{chat['id']}").json()["last_message_preview"] == ""
        assert stub_provider.requests == []


def test_missing_pdf_source_cannot_start_a_chat_task(test_settings, stub_provider, monkeypatch):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        chat = conversation(client, stub_provider)
        attachment = upload(client, "proposal.pdf", pdf_bytes())
        services = app.state.services
        source = next(
            item for item in services.documents.get_document_artifacts(attachment["document_id"])
            if item.kind == "source_pdf"
        )
        services.storage.delete_stored_file(test_settings.workspace_dir, source.relative_path)
        response = client.post(f"/api/agent/conversations/{chat['id']}/messages", json={
            "content": "Summarize this PDF.", "attachment_paths": [attachment["path"]],
        })
        assert response.status_code == 404, response.text
        assert "Upload the PDF again" in response.json()["message"]
        assert client.get("/api/runs").json() == []


def test_orphan_pdf_attachment_returns_actionable_error(test_settings):
    with TestClient(create_app(test_settings)) as client:
        path = "library/papers/proposal--orphan/attachments/source.md"
        client.app.state.services.workspace.write_file(path, "An orphaned PDF extract.")
        with pytest.raises(NotFoundError, match="no longer in the library"):
            client.app.state.services.conversation_attachments.describe(path)


def test_pdf_attachment_qa_uses_existing_paper_tools(test_settings, stub_provider, monkeypatch):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        chat = conversation(client, stub_provider)
        attachment = upload(client, "proposal.pdf", pdf_bytes())
        goal = "What is proposed in this PDF?"
        stub_provider.tool_plans = [(goal, "read_research_paper", {
            "document_id": attachment["document_id"], "action": "pages",
            "query": None, "start": 1, "limit": 5, "offset": 0,
        })]
        stub_provider.reply = "The proposal concerns efficient research retrieval (p.1)."
        response = client.post(f"/api/agent/conversations/{chat['id']}/messages", json={
            "content": goal, "attachment_paths": [attachment["path"]],
            "research_mode": "learn", "web_enabled": False,
        })
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        attempts = app.state.services.runs.get(run["id"]).tool_attempts
        assert [attempt.catalog_id for attempt in attempts] == ["research.paper.read"]
        assert attempts[0].status == "completed"
        assert "Sparse Methods" in json.dumps(attempts[0].result_json)
        assert attachment["document_id"] in json.dumps(stub_provider.requests[0]["messages"])


@pytest.mark.parametrize("deep_work", [False, True])
def test_reference_summaries_account_for_local_and_missing_papers(
    test_settings, stub_provider, monkeypatch, deep_work,
):
    app = create_app(test_settings)
    monkeypatch.setattr(app.state.services.document_ocr, "available", lambda: False)
    with TestClient(app) as client:
        chat = conversation(client, stub_provider, deep_work=deep_work)
        first = upload(client, "Sparse_Methods.pdf", pdf_bytes("Sparse Methods evaluates retrieval accuracy using sparse data."))
        second = upload(client, "Dense_Methods.pdf", pdf_bytes("Dense Methods evaluates retrieval accuracy using dense data."))
        proposal = upload(client, "references.md", b"# References\n1. Sparse Methods\n2. Dense Methods\n3. Unavailable Study")
        goal = "Summarize all referenced papers in chat using my local library."
        stub_provider.tool_plans = [
            (goal, "read_research_note", {"path": proposal["path"]}),
            (goal, "search_research_library", {
                "query": "Methods", "document_id": None, "ignore_document_ids": [], "limit": 3,
            }),
        ]
        if deep_work:
            stub_provider.tool_plans.append((goal, "create_work_plan", {"items": [
                {"id": "summaries", "title": "Summarize the two available papers."},
                {"id": "missing", "title": "Resolve Unavailable Study."},
            ]}))
        for item in (first, second):
            stub_provider.tool_plans.append((goal, "read_research_paper", {
                "document_id": item["document_id"], "action": "pages",
                "query": None, "start": 1, "limit": 5, "offset": 0,
            }))
        if deep_work:
            stub_provider.tool_plans.extend([
                (goal, "update_work_item", {"id": "summaries", "status": "completed", "summary": "Read both local papers, p.1 each."}),
                (goal, "update_work_item", {"id": "missing", "status": "blocked", "summary": "Unavailable Study is not local; web access is disabled."}),
            ])
        stub_provider.reply = (
            "Sparse Methods studies sparse retrieval (p.1); Dense Methods studies dense retrieval (p.1). "
            "Unavailable Study could not be summarized because it is not local and web access is off."
        )
        response = client.post(f"/api/agent/conversations/{chat['id']}/messages", json={
            "content": goal, "attachment_paths": [proposal["path"]], "web_enabled": False,
        })
        assert response.status_code == 202, response.text
        run = wait_for_run(client, response.json()["run"]["id"])
        assert run["status"] == "completed", run["error"]
        record = app.state.services.runs.get(run["id"])
        assert all(attempt.status == "completed" for attempt in record.tool_attempts)
        assert [attempt.catalog_id for attempt in record.tool_attempts].count("research.paper.read") == 2
        assert run["final_output"] == stub_provider.reply
        assert len(client.get("/api/documents").json()) == 2
        assert "acquire_research_source" not in stub_provider.tools_offered
