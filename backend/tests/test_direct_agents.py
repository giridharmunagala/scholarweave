from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.bootstrap import create_services
from backend.documents.models import Document
from backend.runtime.context import ScholarWeaveContext
from backend.tests.test_sdk_api import configure_provider


def _ready_paper(services, *, title: str = "Test paper") -> str:
    document = Document(
        title=title,
        source_filename="paper.pdf",
        content_type="application/pdf",
        status="ready",
        page_count=2,
        metadata_json={},
    )
    with services.session_factory() as session:
        session.add(document)
        session.commit()
        session.refresh(document)
    relative_path = f"documents/{document.id}/manifest.json"
    stored = services.storage.write_json(
        services.settings.artifacts_dir,
        relative_path,
        {
            "document_id": document.id,
            "title": title,
            "pages": [
                {"page": 1, "text": "Core contribution and method."},
                {"page": 2, "text": "Publisher boilerplate."},
            ],
        },
    )
    services.documents.create_artifact_record(
        owner_type="document",
        kind="extracted_manifest",
        document_id=document.id,
        relative_path=relative_path,
        media_type="application/json",
        stored=stored,
    )
    return document.id


def test_direct_agents_are_fixed_and_chat_without_builder(
    test_settings,
    stub_provider,
) -> None:
    app = create_app(test_settings)
    document_id = _ready_paper(app.state.services)
    with TestClient(app) as client:
        configure_provider(client, stub_provider)
        stub_provider.tool_plans = [
            (
                "What is the contribution?",
                "read_retained_paper_pages",
                {"document_id": document_id, "start_page": 1, "limit": 10},
            )
        ]
        agents = client.get("/api/research-agents")

        assert agents.status_code == 200
        assert [agent["key"] for agent in agents.json()] == [
            "summary",
            "open_areas",
            "qa",
            "paper_cleaner",
        ]

        created = client.post(
            "/api/research-agent-conversations",
            json={
                "agent_key": "qa",
                "document_ids": [document_id],
                "title": "Paper questions",
                "model_reference": {},
            },
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]

        sent = client.post(
            f"/api/research-agent-conversations/{conversation_id}/messages",
            json={"content": "What is the contribution?"},
        )
        assert sent.status_code == 202, sent.text
        run_id = sent.json()["run"]["id"]
        deadline = time.monotonic() + 10
        run = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert run is not None
        assert run["status"] == "completed", run
        assert run["agent_name"] == "Q & A bot"
        assert client.get("/api/agents").json() == []
        detail = client.get(
            f"/api/research-agent-conversations/{conversation_id}"
        ).json()
        assert detail["agent_key"] == "qa"
        assert any(item["role"] == "assistant" for item in detail["items"])

        incomplete = client.post(
            "/api/research-agent-conversations",
            json={
                "agent_key": "summary",
                "document_ids": [document_id],
                "title": "Incomplete summary",
                "model_reference": {},
            },
        )
        incomplete_id = incomplete.json()["id"]
        started = client.post(
            f"/api/research-agent-conversations/{incomplete_id}/messages",
            json={"content": "Summarize without reading the paper."},
        )
        failed_run_id = started.json()["run"]["id"]
        deadline = time.monotonic() + 10
        failed_run = None
        while time.monotonic() < deadline:
            failed_run = client.get(f"/api/runs/{failed_run_id}").json()
            if failed_run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.05)

        assert failed_run is not None
        assert failed_run["status"] == "failed"
        assert "must read every retained page" in failed_run["error"]
        failed_detail = client.get(
            f"/api/research-agent-conversations/{incomplete_id}"
        ).json()
        assert failed_detail["items"] == []
        assert app.state.services.direct_agents.repository.list_summaries() == []


@pytest.mark.anyio
async def test_cleaner_decisions_gate_pages_and_summary_repository(test_settings) -> None:
    services = create_services(test_settings)
    document_id = _ready_paper(services)
    runtime = services.runs._tool_runtime
    cleaner_context = ScholarWeaveContext(
        run_id="cleaner-run",
        tool_runtime=runtime,
        metadata={
            "direct_agent_key": "paper_cleaner",
            "direct_agent_document_ids": [document_id],
        },
    )

    all_pages = await runtime.invoke(
        "research.pages.read_all",
        {"document_id": document_id, "start_page": 1, "limit": 10},
        cleaner_context,
    )
    assert [page["page_number"] for page in all_pages["pages"]] == [1, 2]

    saved = await runtime.invoke(
        "research.page_decisions.save",
        {
            "document_id": document_id,
            "decisions": [
                {"page_number": 1, "decision": "keep", "reason": "Research content."},
                {"page_number": 2, "decision": "no_keep", "reason": "Boilerplate."},
            ],
        },
        cleaner_context,
    )
    assert saved["total_saved"] == 2
    services.direct_agents._completion_validator(
        "paper_cleaner",
        [document_id],
    )(cleaner_context)

    summary_context = ScholarWeaveContext(
        run_id="summary-run",
        tool_runtime=runtime,
        metadata={
            "direct_agent_key": "summary",
            "direct_agent_document_ids": [document_id],
        },
    )
    retained = await runtime.invoke(
        "research.pages.read_retained",
        {"document_id": document_id, "start_page": 1, "limit": 10},
        summary_context,
    )
    assert [page["page_number"] for page in retained["pages"]] == [1]

    await runtime.invoke(
        "research.summaries.save",
        {
            "document_id": document_id,
            "contribution": "A contribution.",
            "contributions_detail": "Detailed contribution.",
            "experimentation_results": "Reported results.",
            "open_areas": [{"statement": "Future work.", "citation": "p.1"}],
        },
        summary_context,
    )
    services.direct_agents._completion_validator(
        "summary",
        [document_id],
    )(summary_context)
    summary_file = services.workspace.read_file(
        f"papers/{document_id}/summary.md"
    )
    assert "A contribution." in summary_file.content
    assert summary_file.tags == ("paper", f"paper:{document_id}", "summary")
    open_areas_context = ScholarWeaveContext(
        run_id="open-areas-run",
        tool_runtime=runtime,
        metadata={
            "direct_agent_key": "open_areas",
            "direct_agent_document_ids": [],
        },
    )
    summaries = await runtime.invoke(
        "research.summaries.list",
        {},
        open_areas_context,
    )
    assert summaries[0]["document_id"] == document_id
    assert summaries[0]["open_areas"] == [
        {"statement": "Future work.", "citation": "p.1"}
    ]
    await services.close()
