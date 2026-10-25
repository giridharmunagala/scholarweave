from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app
from backend.agents.blueprint import ModelReferenceSpec
from backend.agents.context_budget import _estimated_tokens, model_context_budget
from backend.agents.harness import model_input_messages, request_parameters
from backend.providers.schemas import ProviderCreate, ProviderModel
from backend.tests.test_api import wait_for_run
from backend.tests.test_chat_attachments import pdf_bytes


@pytest.fixture
def paper_context(test_settings):
    with TestClient(create_app(test_settings)) as client:
        documents = client.app.state.services.documents
        paper = documents.create_document_from_bytes(
            pdf_bytes(), filename="figures.pdf", title="Measured research",
        )
        output = io.BytesIO()
        with Image.new("RGB", (80, 40), "red") as image:
            image.save(output, format="PNG")
        documents.ingestion._persist_document_content(
            paper, [{
                "page": 1, "text": "Measured evidence: Figure 1 shows an improvement of 42 percent.",
                "bbox_coordinate_system": "normalized_0_1000",
                "image_bbox": [0, 0, 612, 792],
                "layout_blocks": [
                    {"label": "Figure", "bbox": [100, 200, 900, 700], "reading_order": 0,
                     "image_path": "figures/one.png", "markdown": "![Figure 1](figures/one.png)"},
                    {"label": "Caption", "bbox": [100, 710, 900, 800], "reading_order": 1,
                     "markdown": "Figure 1: an improvement of 42 percent."},
                ],
                "_figures": [{"path": "figures/one.png", "png": output.getvalue(), "block": 0}],
            }],
            enhancement_model=None, extraction_mode="test",
        )
        yield client, paper


@pytest.mark.parametrize(("vision", "max_images"), [(False, 2), (True, 1), (True, 0)])
def test_paper_context_preserves_layout_and_bounds_images(paper_context, vision, max_images):
    client, paper = paper_context
    documents = client.app.state.services.documents
    evidence = documents.paper_context(
        paper.id, [1], include_images=vision, max_images=max_images, include_text=True,
    )
    page = evidence["pages"][0]
    assert page["text"].startswith("Measured evidence")
    assert page["bbox_coordinate_system"] == "normalized_0_1000"
    assert page["layout_blocks"][0]["bbox"] == [100, 200, 900, 700]
    assert page["layout_blocks"][1]["reading_order"] == 1
    assert bool(evidence["source_content"]) is (vision and max_images > 0)
    if vision and max_images:
        location, image = evidence["source_content"]
        assert '"bbox": [100, 200, 900, 700]' in location["text"]
        with Image.open(io.BytesIO(base64.b64decode(image["image_url"]["url"].split(",", 1)[1]))) as decoded:
            assert decoded.size == (80, 40)
            assert decoded.getpixel((20, 20))[0] > 240
    elif vision:
        assert evidence["omitted_images"][0]["page_number"] == 1


def test_paper_context_reports_missing_visuals_without_losing_text(paper_context, monkeypatch):
    client, paper = paper_context
    documents = client.app.state.services.documents

    def missing(_artifact):
        raise FileNotFoundError("Figure file was removed")

    monkeypatch.setattr(documents, "artifact_bytes", missing)
    evidence = documents.paper_context(paper.id, [1], include_images=True, max_images=1, include_text=True)
    assert evidence["pages"][0]["text"]
    assert evidence["source_content"] == []
    assert "Figure file was removed" in evidence["warnings"][0]
    assert len(evidence["omitted_images"]) == 1
    text_only = documents.paper_context(paper.id, [1], include_text=True)
    assert text_only["warnings"] == []
    assert text_only["source_content"] == []


@pytest.mark.parametrize("figure_count", [0, 2])
def test_paper_context_uses_pdf_page_when_crops_are_absent_or_exceed_budget(paper_context, figure_count):
    client, paper = paper_context
    documents = client.app.state.services.documents
    artifact = next(item for item in documents.get_document_artifacts(paper.id) if item.kind == "extracted_image")
    png = documents.artifact_bytes(artifact)
    documents.ingestion._persist_document_content(
        paper, [{
            "page": 1, "text": "The PDF page contains measured research.",
            "layout_blocks": [{
                "label": "Figure", "bbox": [100, 200, 900, 700], "reading_order": index,
                "image_path": f"figures/{index}.png",
            } for index in range(figure_count)],
            "_figures": [{
                "path": f"figures/{index}.png", "png": png, "block": index,
            } for index in range(figure_count)],
        }],
        enhancement_model=None, extraction_mode="native",
    )
    evidence = documents.paper_context(paper.id, [1], include_images=True, max_images=1)
    assert evidence["included_images"][0]["kind"] == "PDF page"
    assert evidence["included_images"][0]["bbox"] == [0, 0, 1000, 1000]
    image = evidence["source_content"][1]
    with Image.open(io.BytesIO(base64.b64decode(image["image_url"]["url"].split(",", 1)[1]))) as decoded:
        assert max(decoded.size) <= 1280


def test_review_source_packet_reserves_room_for_layout_and_image_tokens(paper_context, stub_provider):
    client, paper = paper_context
    services = client.app.state.services
    profile = services.providers.create(ProviderCreate(
        name="Source budget", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
        models=[ProviderModel(name="vision", context_window_tokens=32768, capabilities={"chat", "tools", "vision"})],
    ))
    reference = ModelReferenceSpec(provider_profile_id=profile.id, model="vision")
    prepared = services.reviews.jobs.prepare(reference, "literature-compact-summary")
    compiled, budget, _ = prepared
    phrase = "Measured evidence and its caption remain connected. "
    packets = services.reviews._source_packets(
        [{"page": 1, "text": phrase * (budget.limit // len(phrase) + 1)}],
        {"document_id": paper.id}, prepared,
    )
    assert len(packets) > 1
    prompt, images = services.reviews.jobs._paper_input(packets[0], prepared)
    assert images
    items = [{"role": "user", "content": [{"type": "text", "text": prompt}, *images]}]
    parameters = request_parameters(
        compiled.entry_agent, model_input_messages(compiled.entry_agent, items, compiled.entry_agent.instructions),
        [], stream=False,
    )
    input_limit, _, _ = model_context_budget(compiled.context_window_tokens, compiled.entry_agent.model_settings.max_tokens)
    assert _estimated_tokens(parameters) <= input_limit
    summary_compiled, summary_input, _, _ = services.summaries._prepare(
        paper.id, model_reference=reference, mode="reviewed",
    )
    assert "normalized_0_1000" in str(summary_input)
    assert "100, 200, 900, 700" in str(summary_input)
    assert summary_compiled.entry_agent.binding.supports_vision


@pytest.mark.parametrize(("vision", "workspace", "api_mode"), [
    (True, False, "responses"), (True, True, "chat_completions"), (False, True, "chat_completions"),
])
def test_review_source_reads_use_the_consuming_models_vision_capability(
    paper_context, stub_provider, vision, workspace, api_mode,
):
    client, paper = paper_context
    services = client.app.state.services
    profile = services.providers.create(ProviderCreate(
        name="Review context", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
        api_mode=api_mode, models=[ProviderModel(
            name="review-model", context_window_tokens=32768,
            capabilities={"chat", "tools", *({"vision"} if vision else set())},
        )],
    ))
    reference = ModelReferenceSpec(provider_profile_id=profile.id, model="review-model")
    services.reviews.repository.create("context-review", {
        "main_model": reference.model_dump(), "embedding_model": reference.model_dump() if workspace else None,
    }, {
        "status": "running", "stage": "reading", "allowance_tokens": 1_000_000,
        "workspace_path": "research/context-review" if workspace else None,
    })
    prepared = services.reviews.jobs.prepare(reference, "literature-compact-summary")
    stub_provider.reply = "The measured result is 42 percent [p.1]."
    result = client.portal.call(
        services.reviews.jobs.call, "context-review", "reading", "source",
        {"document_id": paper.id, "records": [{"page": 1, "text": "Measured evidence."}]}, prepared,
    )
    assert "42 percent" in result
    user = next(message for message in reversed(stub_provider.requests[-1]["messages"]) if message["role"] == "user")
    parts = user["content"] if isinstance(user["content"], list) else [{"type": "text", "text": user["content"]}]
    packet = json.loads(next(part["text"] for part in parts if part.get("type") in {"text", "input_text"}))
    assert packet["paper_evidence"]["pages"][0]["layout_blocks"][0]["bbox"] == [100, 200, 900, 700]
    assert bool([part for part in parts if part.get("type") in {"image_url", "input_image"}]) is vision


@pytest.mark.parametrize(("action", "vision", "api_mode"), [
    ("reader", True, "chat_completions"),
    ("reader", False, "responses"),
    ("pages", True, "responses"),
    ("chunks", False, "chat_completions"),
    ("search", True, "chat_completions"),
])
def test_paper_questions_send_geometry_and_real_images_only_when_supported(
    paper_context, stub_provider, action, vision, api_mode,
):
    client, paper = paper_context
    services = client.app.state.services
    profile = services.providers.create(ProviderCreate(
        name="Paper context", kind="openai_compatible", base_url=f"{stub_provider.base_url}/v1",
        api_mode=api_mode, models=[ProviderModel(
            name="paper-model", context_window_tokens=32768,
            capabilities={"chat", "tools", *({"vision"} if vision else set())},
        )],
    ))
    chat = client.post("/api/agent/conversations", json={
        "model_reference": {"provider_profile_id": profile.id, "model": "paper-model"},
    }).json()
    question = "Explain the measured evidence."
    request = {"content": question, "web_enabled": False}
    if action == "reader":
        request["reading_context"] = {"document_id": paper.id, "page": 1, "selection": None}
    else:
        stub_provider.tool_plans = [(question, "read_research_paper", {
            "document_id": paper.id, "action": action, "start": 1 if action == "pages" else 0,
            "limit": 1, "offset": 0, "query": "measured" if action == "search" else None,
        })]
    stub_provider.reply = "Figure 1 reports an improvement of 42 percent [p.1]."
    response = client.post(f"/api/agent/conversations/{chat['id']}/messages", json=request)
    assert response.status_code == 202, response.text
    run = wait_for_run(client, response.json()["run"]["id"])
    assert run["status"] == "completed", run["error"]
    messages = stub_provider.requests[-1]["messages"]
    assert "normalized_0_1000" in json.dumps(messages)
    if action != "reader":
        tool_result = json.loads(next(message["content"] for message in messages if message.get("role") == "tool"))
        assert tool_result["paper_evidence"]["pages"][0]["layout_blocks"][0]["bbox"] == [100, 200, 900, 700]
    else:
        assert "100, 200, 900, 700" in json.dumps(messages)
    parts = [
        part for message in messages if isinstance(message.get("content"), list)
        for part in message["content"]
    ]
    images = [part for part in parts if part.get("type") in {"image_url", "input_image"}]
    assert bool(images) is vision
    if action != "reader":
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert tool_messages
        assert "data:image" not in json.dumps(tool_messages)
    if vision:
        image_url = images[0]["image_url"]
        url = image_url["url"] if isinstance(image_url, dict) else image_url
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
            assert image.size == (80, 40)
