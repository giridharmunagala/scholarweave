from __future__ import annotations

from types import SimpleNamespace
from dataclasses import dataclass
from typing import Any
import asyncio
import pytest
from openai import AsyncOpenAI

from backend.agents.blueprint import ModelReferenceSpec
from backend.agents.compiler import AgentCompiler
from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import ModelBinding
from backend.bootstrap import create_services
from backend.persistence.database import create_session_factory
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.documents.summaries import (
    PAPER_SUMMARY_COMPLETION_POLICY_ID,
    PaperSummaryService,
    paper_summary_blueprint,
    validate_paper_summary_completion,
)
from backend.core.errors import NotFoundError, ValidationError
from backend.workspace.layout import WorkspaceLayout
from backend.workspace.repository import WorkspaceRepository
from backend.workspace.service import WorkspaceService
from backend.tools.catalog import create_tool_catalog
from backend.tools.policy import ToolInputError
from backend.conversations.turns import (
    deep_work_blueprint,
    research_blueprint,
    validate_paper_work_completion,
)


@dataclass
class FakeCompiled:
    context_window_tokens: int
    entry_agent: Any
    completion_validator: Any = None
    completion_policy_id: str | None = None


@pytest.mark.anyio
@pytest.mark.parametrize(("model", "declared", "expected"), [
    ("qwen-27b", ("none", "high"), "none"),
    ("qwen-27b", ("high",), None),
    ("unknown-model", None, None),
])
async def test_main_agent_summary_call_uses_runtime_owned_model_and_reasoning(
    test_settings, stub_provider, monkeypatch, model, declared, expected,
):
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="runtime-summary.pdf", title="Runtime summary",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [
        {"text": "The result is 42 units.", "citation": "p.1"},
    ])
    client = AsyncOpenAI(base_url=f"{stub_provider.base_url}/v1", api_key="stub")
    binding = ModelBinding(
        client=client, model_name=model, provider_kind="openai_compatible",
        context_window_tokens=80000, reasoning_efforts=declared,
    )
    reference = ModelReferenceSpec(provider_profile_id="parent-provider", model=model)
    resolved_references = []

    def resolve(selected, **kwargs):
        resolved_references.append({
            "provider_profile_id": selected.provider_profile_id, "model": selected.model,
        })
        return binding

    compiler = AgentCompiler(
        SimpleNamespace(resolve_agent_model=resolve),
        create_tool_catalog(PromptRegistry(test_settings.prompt_config_dir)), settings=test_settings,
    )
    monkeypatch.setattr(services.summaries, "_compiler", compiler)
    original_responses = stub_provider.responses

    def responses(payload):
        offered = {tool["function"]["name"] for tool in payload.get("tools", [])}
        if "summarize_research_paper" in offered:
            stub_provider.call_tool = "summarize_research_paper"
            stub_provider.tool_arguments = {"document_id": document.id, "mode": "reviewed"}
        else:
            stub_provider.call_tool = "save_paper_summary_version"
            stub_provider.tool_arguments = {
                "document_id": document.id,
                "content": "# Summary\n\n" + "The extraction reports 42 units [p.1]. " * 7,
                "review_summary": "Checked the result against the source and citation.",
            }
        return original_responses(payload)

    monkeypatch.setattr(stub_provider, "responses", responses)
    try:
        compiled = compiler.compile(research_blueprint(reference.model_dump()))
        parent = services.runs.create(
            compiled, "Save a reviewed summary of this paper.", conversation_id=None,
            reasoning_effort="high",
        )
        async with asyncio.timeout(20):
            while parent.status not in {"completed", "failed", "cancelled"}:
                await asyncio.sleep(0.05)
                parent = services.runs.get(parent.id)
        assert parent.status == "completed", parent.error
        assert resolved_references == [reference.model_dump(), reference.model_dump()]
        main_requests = []
        summary_requests = []
        for request in stub_provider.requests:
            tools = {tool["function"]["name"]: tool["function"] for tool in request["tools"]}
            if "summarize_research_paper" in tools:
                main_requests.append(request)
                schema = tools["summarize_research_paper"]["parameters"]
                assert set(schema["properties"]) == {"document_id", "mode"}
                assert request["reasoning_effort"] == "high"
            else:
                summary_requests.append(request)
                if expected is None:
                    assert "reasoning_effort" not in request
                else:
                    assert request["reasoning_effort"] == expected
            assert request["model"] == model
        assert len(main_requests) == len(summary_requests) == 2
        versions = services.summaries.versions(document.id)
        assert len(versions) == 1
        assert versions[0]["model"]["provider_profile_id"] == reference.provider_profile_id
        assert versions[0]["coverage_complete"] is True
    finally:
        await client.close()
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("declared", "expected"), [
    (("none", "high"), "none"), (("high",), None), ((), None),
])
async def test_research_summary_tool_uses_serial_isolated_writer(
    test_settings, stub_provider, declared, expected, monkeypatch,
):
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="dedicated.pdf", title="Dedicated",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [
        {"text": "The result is 42 units.", "citation": "p.1"},
    ])
    client = AsyncOpenAI(base_url=f"{stub_provider.base_url}/v1", api_key="stub")
    binding = ModelBinding(
        client=client, model_name="qwen-27b", provider_kind="openai_compatible",
        context_window_tokens=80000, reasoning_efforts=declared,
    )
    compiler = AgentCompiler(
        SimpleNamespace(resolve_agent_model=lambda *args, **kwargs: binding),
        create_tool_catalog(), settings=test_settings,
    )
    monkeypatch.setattr(services.summaries, "_compiler", compiler)
    runtime = services.runs._tool_runtime
    parent = services.runs._repository.create(
        conversation_id=None, agent_name="Research", input_value="A private research instruction",
        blueprint=research_blueprint({}).model_dump(mode="json"),
    )
    forwarded_events = []

    async def emit(kind, payload):
        forwarded_events.append((kind, payload))

    context = ScholarWeaveContext(
        run_id=parent.id, tool_runtime=runtime, event_sink=SimpleNamespace(emit=emit),
    )
    stub_provider.call_tool = "save_paper_summary_version"
    stub_provider.tool_arguments = {
        "document_id": document.id,
        "content": "# Summary\n\n" + "The supplied extraction reports 42 units [p.1]. " * 5,
        "review_summary": "Verified the result and citation; no unsupported claims.",
    }
    try:
        async with asyncio.timeout(20):
            results = await asyncio.gather(*(
                runtime.invoke("research.summary.run", {
                    "document_id": document.id, "mode": mode,
                }, context) for mode in ("reviewed", "overview")
            ))
        runs = [services.runs.get(result["summary_run_id"]) for result in results]
        assert all(run.completion_policy_id == PAPER_SUMMARY_COMPLETION_POLICY_ID for run in runs)
        assert runs[0].finished_at <= runs[1].started_at
        assert runs[0].finished_at <= runs[1].created_at
        assert all(result["coverage_complete"] for result in results)
        assert any(item["action"] == "summary_saved" for item in context.metadata["paper_activity"])
        assert any(item["action"] == "read" for item in context.metadata["paper_activity"])
        assert not any(item["action"] == "notes_saved" for item in context.metadata["paper_activity"])
        with pytest.raises(ValidationError) as incomplete:
            validate_paper_work_completion(context)
        assert "notes.md" in str(incomplete.value.issues)
        await runtime.invoke("research.notes.save", {
            "target": "paper_notes", "mode": "append", "document_id": document.id,
            "path": None, "name": None, "content": "Parent-owned findings: 42 units [p.1].", "tags": [],
        }, context)
        validate_paper_work_completion(context)
        telemetry = [payload for kind, payload in forwarded_events if kind == "model.telemetry"]
        assert len(telemetry) == len(stub_provider.requests)
        assert len({item["model_call_id"] for item in telemetry}) == len(telemetry)
        assert all(item["context_scope"] == "delegate" and item["delegated"] for item in telemetry)
        assert not any(kind == "model.stream" for kind, _ in forwarded_events)
        for request in stub_provider.requests:
            if expected is None:
                assert "reasoning_effort" not in request
            else:
                assert request["reasoning_effort"] == expected
            assert request["max_tokens"] == 20000
            assert "A private research instruction" not in str(request["messages"])
            offered = {tool["function"]["name"] for tool in request["tools"]}
            assert "save_paper_summary_version" in offered
            assert offered <= {
                "read_paper_summary_batch", "paper_summary_checkpoint", "save_paper_summary_version",
                "read_tool_result",
            }
        for blueprint in (research_blueprint({}), deep_work_blueprint({})):
            assert "research.summary.run" in {tool.catalog_id for tool in blueprint.tools}
            assert not {"research.summary.read", "research.summary.checkpoint", "research.summary.save"} & {
                tool.catalog_id for tool in blueprint.tools
            }
    finally:
        await client.close()
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "parent_cancel"])
async def test_summary_forwards_compaction_telemetry_at_every_terminal_status(monkeypatch, status):
    payload = {"model_call_id": "child-compaction-call", "context_scope": "compaction", "usage_complete": True}
    child = SimpleNamespace(
        id="child", status="running" if status == "parent_cancel" else status, error="Child failed.",
        runtime_metadata_json={
            "paper_summary_parent_run_id": "parent", "paper_summary_document_id": "paper",
            "paper_summary_mode": "reviewed",
        },
        events=[SimpleNamespace(sequence=1, event_type="model.telemetry", payload_json=payload)],
    )
    parent = SimpleNamespace(blueprint_json=research_blueprint({}).model_dump(mode="json"), events=[])
    created = []
    runs = SimpleNamespace(
        get=lambda run_id: parent if run_id == "parent" else child,
        create=lambda *args, **kwargs: created.append("child") or SimpleNamespace(id="child", status="pending"),
    )

    async def cancel(run_id):
        assert run_id == "child"
        child.status = "cancelled"
        child.events.append(SimpleNamespace(
            sequence=2, event_type="model.telemetry",
            payload_json={**payload, "model_call_id": "cancelled-child-call", "completed": False},
        ))

    runs.cancel = cancel
    receipts = {}

    def read_file(path):
        if path not in receipts:
            raise FileNotFoundError(path)
        return SimpleNamespace(content=receipts[path])

    workspace = SimpleNamespace(
        paper_folder=lambda document_id: WorkspaceLayout.paper_folder(document_id, "Paper"),
        read_file=read_file, write_file=lambda path, content, **kwargs: receipts.update({path: content}),
    )
    service = PaperSummaryService(None, runs, None, workspace, None)
    compiled = SimpleNamespace(entry_agent=SimpleNamespace(binding=SimpleNamespace(
        model_name="qwen-27b", provider_kind="openai_compatible",
    )))
    monkeypatch.setattr(service, "_prepare", lambda *args, **kwargs: (compiled, "Summarize.", {}, "revision"))
    monkeypatch.setattr(service, "version", lambda *args: ({}, "Saved summary."))
    events = []
    forwarded_event = asyncio.Event()

    async def emit(kind, data):
        events.append((kind, data))
        parent.events.append(SimpleNamespace(event_type=kind, payload_json=data))
        forwarded_event.set()

    context = ScholarWeaveContext(
        run_id="parent", tool_runtime=None, event_sink=SimpleNamespace(emit=emit),
    )
    call = service.run_for_agent({"document_id": "paper", "mode": "reviewed"}, context)
    if status == "parent_cancel":
        task = asyncio.create_task(call)
        await asyncio.wait_for(forwarded_event.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif status == "completed":
        await call
    else:
        with pytest.raises(ValidationError):
            await call
    expected_events = 2 if status == "parent_cancel" else 1
    assert len(events) == expected_events
    assert events[0][0] == "model.telemetry"
    forwarded = events[0][1]
    assert forwarded["model_call_id"] == payload["model_call_id"]
    assert forwarded["context_scope"] == "compaction"
    assert forwarded["delegated"] is True
    assert "delegated" not in payload
    replay = service.run_for_agent({"document_id": "paper", "mode": "reviewed"}, context)
    if status == "completed":
        await replay
    else:
        with pytest.raises(ValidationError):
            await replay
    assert created == ["child"]
    assert len(events) == expected_events


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["malformed", "missing-run-id", "write-failed"])
async def test_summary_job_receipt_failures_do_not_start_untracked_work(monkeypatch, failure):
    parent = SimpleNamespace(blueprint_json=research_blueprint({}).model_dump(mode="json"))
    created = []
    cancelled = []

    async def cancel(run_id):
        cancelled.append(run_id)

    runs = SimpleNamespace(
        get=lambda run_id: parent,
        create=lambda *args, **kwargs: created.append("child") or SimpleNamespace(id="child"),
        cancel=cancel,
    )

    def read_file(path):
        if failure == "write-failed":
            raise FileNotFoundError(path)
        return SimpleNamespace(content=[] if failure == "malformed" else {"run_id": ""})

    def write_file(*args, **kwargs):
        raise OSError("Receipt storage unavailable")

    service = PaperSummaryService(
        None, runs, None, SimpleNamespace(
            paper_folder=lambda document_id: WorkspaceLayout.paper_folder(document_id, "Paper"),
            read_file=read_file, write_file=write_file,
        ), None,
    )
    compiled = SimpleNamespace(entry_agent=SimpleNamespace(binding=SimpleNamespace(
        model_name="qwen-27b", provider_kind="openai_compatible",
    )))
    monkeypatch.setattr(service, "_prepare", lambda *args, **kwargs: (compiled, "Summarize.", {}, "revision"))
    context = ScholarWeaveContext(run_id="parent", tool_runtime=None)
    expected_error = OSError if failure == "write-failed" else ValidationError
    with pytest.raises(expected_error):
        await service.run_for_agent({"document_id": "paper", "mode": "reviewed"}, context)
    expected_jobs = ["child"] if failure == "write-failed" else []
    assert created == expected_jobs
    assert cancelled == expected_jobs


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["overview", "reviewed"])
async def test_full_paper_over_old_character_caps_is_inlined(test_settings, mode):
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="full.pdf", title="Full",
    )
    services.documents.repository.mark_ready(document.id, page_count=80, metadata={})
    services.retrieval.replace_document_chunks(document.id, [
        {"text": f"Page {index} findings. " * 40, "citation": f"p.{index}"}
        for index in range(1, 81)
    ])
    captured = []
    compiled = FakeCompiled(
        context_window_tokens=80000,
        entry_agent=SimpleNamespace(binding=SimpleNamespace(model_name="test", provider_kind="openai")),
    )
    service = PaperSummaryService(
        SimpleNamespace(compile=lambda blueprint: compiled),
        SimpleNamespace(create=lambda agent, instruction, **kwargs: captured.append((instruction, kwargs))),
        services.documents, services.workspace, PromptRegistry(test_settings.prompt_config_dir),
    )
    try:
        service.start(document.id, model_reference=ModelReferenceSpec(), mode=mode)
        instruction, options = captured[0]
        assert len(instruction) > 12000
        assert "Page 80 findings." in instruction
        pending = options["runtime_metadata"]["_paper_summary_checkpoint_states"][document.id]["pending_checkpoint"]
        assert pending["has_more"] is False
        assert pending["next_start"] is None
        assert len(pending["coverage"]["spans"]) == 80
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize(("character", "fits"), [("a", True), ("漢", False)])
async def test_full_source_budget_uses_serialized_tokens_not_raw_character_count(test_settings, character, fits):
    from backend.agents.context_budget import _REFERENCE_LIFETIME_INSTRUCTIONS, _request_tokens
    from backend.tests.harness_support import FakeClient

    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="token-budget.pdf", title="Token budget",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    text = character * 150000
    services.retrieval.replace_document_chunks(document.id, [{"text": text, "citation": "p.1"}])
    binding = ModelBinding(
        client=FakeClient.failing(AssertionError("Sizing must not call a provider.")),
        model_name="qwen-27b", provider_kind="openai_compatible",
        context_window_tokens=80000,
    )
    compiler = AgentCompiler(
        SimpleNamespace(resolve_agent_model=lambda *args, **kwargs: binding),
        create_tool_catalog(), settings=test_settings,
    )
    service = PaperSummaryService(
        compiler, services.runs, services.documents, services.workspace,
        PromptRegistry(test_settings.prompt_config_dir),
    )
    try:
        compiled, instruction, metadata, _ = service._prepare(
            document.id, model_reference=ModelReferenceSpec(), mode="reviewed",
        )
        assert (text in instruction) is fits
        if fits:
            context = ScholarWeaveContext(
                run_id="source-sizing", tool_runtime=services.runs._tool_runtime, metadata=metadata,
            )
            estimate = _request_tokens(
                compiled.entry_agent, [{"role": "user", "content": instruction}],
                compiled.entry_agent.instructions + "\n\n" + _REFERENCE_LIFETIME_INSTRUCTIONS,
                context,
            )
            assert 37500 < estimate <= 52000
            assert len(text) / 12000 == 12.5
        else:
            assert "resume at its exact cursor" in instruction
            assert "_paper_summary_checkpoint_states" not in metadata
    finally:
        await services.close()


@pytest.mark.anyio
async def test_changed_checkpointed_source_cannot_authorize_a_new_revision(test_settings):
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="revision.pdf", title="Revision",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    runtime = services.runs._tool_runtime
    old = ScholarWeaveContext(run_id="old-version", tool_runtime=runtime)
    new = ScholarWeaveContext(run_id="new-version", tool_runtime=runtime)
    try:
        services.retrieval.replace_document_chunks(document.id, [{"text": "Old result.", "citation": "p.1"}])
        await runtime._read_paper_summary_batch({"document_id": document.id, "action": "chunks", "start": 0}, old)
        runtime._append_summary_evidence(old, document.id, "Old result [p.1].")
        services.retrieval.replace_document_chunks(document.id, [{"text": "New result.", "citation": "p.1"}])
        await runtime._read_paper_summary_batch({"document_id": document.id, "action": "chunks", "start": 0}, new)
        runtime._append_summary_evidence(new, document.id, "New result [p.1].")
        with pytest.raises(ValueError, match="current source version"):
            runtime._save_paper_summary_version({
                "document_id": document.id, "content": "# Old result [p.1]", "review_summary": "Old source.",
            }, old)
        with pytest.raises(ValueError, match="extraction changed"):
            runtime._paper_summary_state(old, document.id)
        assert not any(item["action"] == "read" for item in old.metadata["paper_activity"])
        assert services.summaries.versions(document.id) == []
        await runtime._read_paper_summary_batch({"document_id": document.id, "action": "chunks", "start": 0}, old)
        saved = runtime._save_paper_summary_version({
            "document_id": document.id, "content": "# New result [p.1]", "review_summary": "New source.",
        }, old)
        assert saved["source_version"] == services.documents.source_revision(document.id)["source_version"]
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("review_action", ["chunks", "pages"])
async def test_overlapping_overview_preserves_durable_review_progress(test_settings, review_action):
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="overlap.pdf", title="Overlap",
    )
    services.documents.repository.mark_ready(document.id, page_count=6, metadata={})
    services.retrieval.replace_document_chunks(document.id, [
        {"text": "1234567890", "citation": f"p.{index + 1}"} for index in range(6)
    ])
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(run_id="overlap", tool_runtime=runtime)
    try:
        state = runtime._paper_summary_state(context, document.id)
        base = 1 if review_action == "pages" else 0
        for name, action, start, end, more in (
            ("review", review_action, base, base + 4, True),
            ("overview", "chunks", 0, 1, True),
            ("tail", review_action, base + 2, base + 5, False),
        ):
            state["pending_checkpoint"] = {
                "id": name, "action": action, "start": start, "offset": 0,
                "has_more": more, "next_start": end + 1 if more else None, "next_offset": 0,
                "coverage": {"kind": action, "spans": [
                    {"index": index, "offset": 0, "end_offset": 10, "complete": True}
                    for index in range(start, end + 1)
                ]},
            }
            evidence = runtime._append_summary_evidence(context, document.id, f"{name} [p.1].")
            if more:
                assert evidence["next_start"] == base + 5
                assert evidence["action"] == review_action
                assert evidence["contiguous"] is True
        assert evidence["complete"] is True
        assert evidence["next_start"] is None
        durable = services.documents.summary_evidence(document.id, state["source_version"])
        assert durable["complete"] is True
    finally:
        await services.close()


def test_coverage_can_fill_character_gaps_after_out_of_order_reads():
    from backend.tools.runtime import ApplicationToolRuntime

    records = [
        {"coverage": {"kind": "chunks", "spans": [
            {"index": 0, "offset": 50, "end_offset": 100, "complete": True},
        ]}, "source_end": [1, 0]},
        {"coverage": {"kind": "chunks", "spans": [
            {"index": 0, "offset": 0, "end_offset": 25, "complete": False},
        ]}},
    ]
    incomplete = ApplicationToolRuntime._summary_coverage_progress(records, "chunks")
    assert incomplete["next_start"] == 0 and incomplete["next_offset"] == 25
    assert incomplete["complete"] is False
    records.append({"coverage": {"kind": "chunks", "spans": [
        {"index": 0, "offset": 25, "end_offset": 50, "complete": False},
    ]}})
    assert ApplicationToolRuntime._summary_coverage_progress(records, "chunks")["complete"] is True


@pytest.mark.anyio
@pytest.mark.parametrize(("mode", "requested", "model", "expected"), [
    ("overview", None, "Qwen3.8-27B-Q6_K.gguf", "none"),
    ("overview", None, "qwen-27b", "none"),
    ("reviewed", None, "qwen-27b", "none"),
    ("overview", "low", "Qwen3.8-27B-Q6_K.gguf", "low"),
    ("reviewed", None, "Qwen3.8-27B-Q6_K.gguf", "none"),
    ("reviewed", "xhigh", "Qwen3.8-27B-Q6_K.gguf", "xhigh"),
    ("overview", None, "unknown-model", None),
])
async def test_summary_selects_supported_nonreasoning_unless_explicitly_requested(
    test_settings, mode, requested, model, expected,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="reasoning.pdf", title="Reasoning",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [{"text": "A supported result.", "citation": "p.1"}])
    captured = []
    compiled = FakeCompiled(
        context_window_tokens=32768,
        entry_agent=SimpleNamespace(binding=SimpleNamespace(model_name=model, provider_kind="openai_compatible")),
    )

    def create_run(agent, instruction, **kwargs):
        captured.append(kwargs["reasoning_effort"])
        return SimpleNamespace(id=f"run-{len(captured)}")

    service = PaperSummaryService(
        SimpleNamespace(compile=lambda blueprint: compiled), SimpleNamespace(create=create_run),
        services.documents, services.workspace, PromptRegistry(test_settings.prompt_config_dir),
    )
    reference = ModelReferenceSpec(provider_profile_id="selected-profile", model=model)
    try:
        service.start(document.id, model_reference=reference, reasoning_effort=requested, mode=mode)
        service.start_batch([document.id], model_reference=reference, reasoning_effort=requested, mode=mode)
        assert captured == [expected, expected]
    finally:
        await services.close()


@pytest.mark.parametrize(("declared", "requested", "expected"), [
    (None, None, "none"),
    ((), None, None),
    (("high",), None, None),
    (("none", "high"), None, "none"),
    (("none", "high"), "high", "high"),
])
def test_summary_reasoning_honors_declared_provider_capabilities(declared, requested, expected):
    from backend.documents.summaries import _effective_summary_reasoning

    compiled = SimpleNamespace(entry_agent=SimpleNamespace(binding=SimpleNamespace(
        provider_kind="openai_compatible", model_name="qwen-27b", reasoning_efforts=declared,
    )))
    assert _effective_summary_reasoning(compiled, "reviewed", requested) == expected


@pytest.mark.parametrize(("model", "declared", "requested"), [
    ("qwen-27b", (), "none"),
    ("qwen-27b", ("high",), "none"),
    ("qwen-27b", None, "low"),
    ("Qwen3.6-27B", None, "none"),
    ("unknown-model", None, "high"),
    ("unknown-model", None, "none"),
])
def test_summary_explicit_unsupported_reasoning_is_rejected(model, declared, requested):
    from backend.documents.summaries import _effective_summary_reasoning

    compiled = SimpleNamespace(entry_agent=SimpleNamespace(binding=SimpleNamespace(
        provider_kind="openai_compatible", model_name=model, reasoning_efforts=declared,
    )))
    with pytest.raises(ValidationError, match="does not advertise support"):
        _effective_summary_reasoning(compiled, "reviewed", requested)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["overview", "reviewed"])
@pytest.mark.parametrize("repair_after", [None, 1, 41])
async def test_summary_final_without_save_is_repaired_or_rejected(
    test_settings, stub_provider, monkeypatch, mode, repair_after,
) -> None:
    test_settings.agent_max_epochs = 2
    test_settings.agent_run_timeout_seconds = 0.01
    test_settings.agent_context_window_tokens = 131_072
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="required-save.pdf", title="Required save",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [
        {"text": "The measured result is 42 units.", "citation": "p.1"},
    ])
    client = AsyncOpenAI(base_url=f"{stub_provider.base_url}/v1", api_key="stub")
    binding = ModelBinding(
        client=client, model_name="stub-model", provider_kind="openai_compatible",
        reasoning_efforts=("none",),
    )
    compiler = AgentCompiler(
        SimpleNamespace(resolve_agent_model=lambda *args, **kwargs: binding),
        create_tool_catalog(), settings=test_settings,
    )
    service = PaperSummaryService(
        compiler, services.runs, services.documents, services.workspace,
        PromptRegistry(test_settings.prompt_config_dir),
    )
    stub_provider.reply = "The paper reports 42 units [p.1]. Here is my summary."
    if repair_after is not None:
        repair_plan = [(
            "Your attempted final answer did not satisfy",
            "save_paper_summary_version",
            {
                "document_id": document.id,
                "content": (
                    "# Cited summary\n\nThe measured result is 42 units [p.1]. "
                    "This statement is grounded in the supplied extraction. "
                    "The short excerpt provides no further methodological or experimental "
                    "detail, so the summary does not infer datasets, comparisons, or limitations."
                ),
                "review_summary": "Checked the supplied result and cited its page; preserved the evidence limits.",
            },
        )]

        def delayed_save(_payload):
            if len(stub_provider.requests) >= repair_after:
                stub_provider.tool_plans = repair_plan
            return stub_provider.reply

        monkeypatch.setattr(stub_provider, "_reply_for", delayed_save)
    try:
        run, _ = service.start(
            document.id, model_reference=ModelReferenceSpec(), mode=mode,
            reasoning_effort="none" if mode == "overview" else None,
        )
        if repair_after is None:
            async with asyncio.timeout(15):
                while sum(
                    event.event_type == "run.epoch.completed"
                    and event.payload_json.get("terminal_reason") == "completion_rejected"
                    for event in services.runs.get(run.id).events
                ) < 3:
                    await asyncio.sleep(0.01)
            assert services.runs.get(run.id).status == "running"
            assert service.versions(document.id) == []
            await services.runs.cancel(run.id)
        else:
            await asyncio.wait_for(services.runs._tasks[run.id], timeout=60)
        result = services.runs.get(run.id)
        assert result.conversation_id is None
        assert result.blueprint_json["run"]["max_turns"] is None
        assert result.completion_policy_id == PAPER_SUMMARY_COMPLETION_POLICY_ID
        if mode == "overview":
            assert all(request.get("reasoning_effort") == "none" for request in stub_provider.requests)
        assert any(
            "save_paper_summary_version" in str(request.get("messages"))
            and "Your attempted final answer did not satisfy" in str(request.get("messages"))
            for request in stub_provider.requests
        )
        versions = service.versions(document.id)
        if repair_after is not None:
            assert result.status == "completed"
            assert len(versions) == 1
            assert versions[0]["mode"] == mode
            assert len(stub_provider.requests) == repair_after + (1 if mode == "overview" else 2)
            saves = [
                attempt for attempt in result.tool_attempts
                if attempt.catalog_id == "research.summary.save"
            ]
            assert len(saves) == 1
            assert saves[0].status == "completed"
            context = ScholarWeaveContext(
                run_id=run.id, tool_runtime=services.runs._tool_runtime,
                metadata=result.runtime_metadata_json,
            )
            validate_paper_summary_completion(context, workspace=services.workspace)
            services.workspace.delete_file(versions[0]["path"])
            with pytest.raises(ValidationError, match="not been durably saved"):
                validate_paper_summary_completion(context, workspace=services.workspace)
        else:
            assert result.status == "cancelled"
            assert versions == []
            assert len(stub_provider.requests) >= 3
    finally:
        await services.close()
        await client.close()


class Documents:
    def get_document(self, document_id: str):
        if document_id != "paper-1":
            return None
        return SimpleNamespace(id=document_id, title="A Strong Paper")


@pytest.mark.anyio
async def test_summary_batch_is_bounded_prevalidated_and_uses_one_explicit_model(test_settings) -> None:
    services = create_services(test_settings)
    documents = [
        services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF", filename=f"batch-{index}.pdf", title=f"Batch {index}",
        )
        for index in range(2)
    ]
    for document in documents:
        services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
        services.retrieval.replace_document_chunks(document.id, [{"text": "Fact 42.", "citation": "p.1"}])
    compiled_models = []
    queued = []

    def compile_blueprint(blueprint):
        compiled_models.append(blueprint.agents[0].model)
        return FakeCompiled(
            context_window_tokens=32768,
            entry_agent=SimpleNamespace(binding=SimpleNamespace(
                model_name="batch-model", provider_kind="openai_compatible", reasoning_efforts=("none", "low"),
            )),
        )

    def create_run(agent, instruction, **kwargs):
        record = SimpleNamespace(id=f"batch-run-{len(queued)}")
        queued.append((record, kwargs))
        return record

    service = PaperSummaryService(
        SimpleNamespace(compile=compile_blueprint), SimpleNamespace(create=create_run),
        services.documents, services.workspace, PromptRegistry(test_settings.prompt_config_dir),
    )
    selected = ModelReferenceSpec(provider_profile_id="selected-profile", model="batch-model")
    try:
        for ids in ([], [documents[0].id] * 51, [""]):
            with pytest.raises(ValidationError):
                service.start_batch(ids, model_reference=selected, reasoning_effort=None)
        with pytest.raises(ValidationError, match="explicit provider"):
            service.start_batch([documents[0].id], model_reference=ModelReferenceSpec(), reasoning_effort=None)
        with pytest.raises(NotFoundError):
            service.start_batch([documents[0].id, "missing"], model_reference=selected, reasoning_effort=None)
        assert queued == []
        with pytest.raises(ValidationError, match="distinct"):
            service.start_batch(
                [documents[0].id, documents[0].id], model_reference=selected, reasoning_effort=None,
            )
        assert queued == []
        pairs = service.start_batch(
            [documents[1].id, documents[0].id],
            model_reference=selected, reasoning_effort="low",
        )
        assert len(pairs) == 2
        assert all(revision for _, revision in pairs)
        assert [run for run, _ in pairs] == [run for run, _ in queued]
        assert all(model == selected for model in compiled_models)
        metadata = [kwargs["runtime_metadata"] for _, kwargs in queued]
        assert [item["paper_summary_document_id"] for item in metadata] == [documents[1].id, documents[0].id]
        assert [item["paper_summary_batch_index"] for item in metadata] == [0, 1]
        assert [item["summary_batch_previous_run_id"] for item in metadata] == [None, pairs[0][0].id]
        assert len({item["paper_summary_batch_id"] for item in metadata}) == 1
        assert all(item["paper_summary_batch_size"] == 2 for item in metadata)
        assert all(item["inference_priority"] == "background" for item in metadata)
        assert all(kwargs["reasoning_effort"] == "low" for _, kwargs in queued)
    finally:
        await services.close()


@pytest.mark.anyio
async def test_overview_uses_bounded_excerpt_and_never_replaces_reviewed_summary(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="overview.pdf", title="Overview",
    )
    services.documents.repository.mark_ready(document.id, page_count=2, metadata={})
    services.retrieval.replace_document_chunks(document.id, [
        {"text": "The apparent method uses sparse attention. " * 10000, "citation": "p.1"},
        {"text": "Unseen later results.", "citation": "p.2"},
    ])
    queued = []
    blueprints = []

    def compile_blueprint(blueprint):
        blueprints.append(blueprint)
        return FakeCompiled(
            context_window_tokens=32768,
            entry_agent=SimpleNamespace(binding=SimpleNamespace(model_name="same-model", provider_kind="openai_compatible")),
        )

    service = PaperSummaryService(
        SimpleNamespace(compile=compile_blueprint),
        SimpleNamespace(create=lambda agent, instruction, **kwargs: queued.append((instruction, kwargs))),
        services.documents, services.workspace, PromptRegistry(test_settings.prompt_config_dir),
    )
    try:
        paper = services.workspace.ensure_paper_folder(document.id, document.title)
        services.workspace.write_file(str(paper["summary_path"]), "# Existing reviewed summary")
        services.workspace.write_file(str(paper["notes_path"]), "# User notes")
        service.start(document.id, model_reference=ModelReferenceSpec(), reasoning_effort=None, mode="overview")
        instruction, options = queued[0]
        assert 12000 < len(instruction) < 32768 * 4
        assert "Unseen later results." not in instruction
        assert blueprints[0].run.max_turns is None
        assert [tool.catalog_id for tool in blueprints[0].tools] == ["research.summary.save"]
        assert "150-250 words" in blueprints[0].agents[0].instructions
        metadata = options["runtime_metadata"]
        assert metadata["paper_summary_mode"] == "overview"
        context = ScholarWeaveContext(
            run_id="overview-version", tool_runtime=services.runs._tool_runtime, metadata=metadata,
        )
        with pytest.raises(ToolInputError, match="source citation"):
            services.runs._tool_runtime._save_paper_summary_version({
                "document_id": document.id, "content": "An uncited overview.",
                "review_summary": "Only the supplied excerpt was checked.",
            }, context)
        assert service.versions(document.id) == []
        saved = services.runs._tool_runtime._save_paper_summary_version({
            "document_id": document.id, "content": "# Overview\n\nSparse attention [p.1]. Later results were not read.",
            "review_summary": "Only the supplied excerpt was checked.",
        }, context)
        assert saved["status"] == "overview"
        assert saved["mode"] == "overview"
        assert saved["review_complete"] is False
        assert saved["coverage_complete"] is False
        assert saved["canonical_updated"] is False
        assert saved["next_start"] == 0
        assert saved["next_offset"] > 0
        assert services.workspace.read_file(str(paper["summary_path"])).content == "# Existing reviewed summary"
        assert services.workspace.read_file(str(paper["notes_path"])).content == "# User notes"
        service.start(document.id, model_reference=ModelReferenceSpec(), reasoning_effort=None, mode="reviewed")
        assert blueprints[1].run.max_turns is None
        assert len(blueprints[1].tools) == 3
        assert "resume at its exact cursor" in queued[1][0]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_short_paper_direct_summary_is_complete_immutable_and_preserves_late_edits(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="short.pdf", title="Short",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [{"text": "Result 42.", "citation": "p.1"}])
    calls = []
    compiled = FakeCompiled(
        context_window_tokens=32768,
        entry_agent=SimpleNamespace(binding=SimpleNamespace(model_name="test-model", provider_kind="openai")),
    )
    service = PaperSummaryService(
        SimpleNamespace(compile=lambda blueprint: compiled),
        SimpleNamespace(create=lambda agent, instruction, **kwargs: calls.append((instruction, kwargs))),
        services.documents, services.workspace, PromptRegistry(test_settings.prompt_config_dir),
    )
    try:
        service.start(document.id, model_reference=ModelReferenceSpec(), reasoning_effort=None)
        instruction, kwargs = calls[0]
        assert "Result 42." in instruction
        assert "Do not inspect, prepare, or reread" in instruction
        context = ScholarWeaveContext(
            run_id="short-version", tool_runtime=services.runs._tool_runtime,
            metadata=kwargs["runtime_metadata"],
        )
        paper = services.workspace.ensure_paper_folder(document.id, document.title)
        services.workspace.write_file(str(paper["notes_path"]), "# Notes\n\nKeep user notes.")
        services.workspace.write_file(str(paper["summary_path"]), "# Newer manual summary")
        arguments = {
            "document_id": document.id,
            "content": "# Summary\n\nThe exact result is 42 [p.1].",
            "review_summary": "Checked the exact result.",
        }
        saved = services.runs._tool_runtime._save_paper_summary_version(arguments, context)
        assert saved["coverage_complete"] is True
        assert saved["status"] == "reviewed"
        assert saved["model"]["model"] == "test-model"
        assert saved["canonical_updated"] is False
        assert services.workspace.read_file(str(paper["summary_path"])).content == "# Newer manual summary"
        assert services.workspace.read_file(str(paper["notes_path"])).content.endswith("Keep user notes.")
        repeated = services.runs._tool_runtime._save_paper_summary_version(arguments, context)
        assert repeated == saved
        with pytest.raises(ValueError, match="immutable"):
            services.runs._tool_runtime._save_paper_summary_version(
                {**arguments, "content": "# Changed result"}, context,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_summary_save_marks_noncontiguous_coverage_partial(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="partial.pdf", title="Partial",
    )
    services.documents.repository.mark_ready(document.id, page_count=3, metadata={})
    services.retrieval.replace_document_chunks(document.id, [
        {"text": f"Evidence {index}", "citation": f"p.{index + 1}"} for index in range(3)
    ])
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(run_id="partial-version", tool_runtime=runtime)
    try:
        result = await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "chunks", "start": 2}, context,
        )
        assert result["has_more"] is False
        saved = runtime._save_paper_summary_version({
            "document_id": document.id, "content": "# Results\n\nEvidence 2 [p.3].",
            "review_summary": "Only checked the last page.",
        }, context)
        assert saved["status"] == "partial"
        assert saved["coverage_complete"] is False
        assert services.workspace.read_file(saved["path"]).content.startswith("> Partial summary:")
        reread = runtime._read_research_note({"path": saved["canonical_path"]}, context)
        assert reread["summary_check"]["needs_regeneration"] is True
    finally:
        await services.close()


@pytest.mark.anyio
async def test_evidence_recovers_after_mirror_write_failure_and_rejects_changed_source(test_settings, monkeypatch) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="recover.pdf", title="Recover",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [{"text": "Fact 42.", "citation": "p.1"}])
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(run_id="recover", tool_runtime=runtime)
    try:
        batch = await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "chunks", "start": 0}, context,
        )
        original = services.workspace.write_file

        def fail_mirror(*args, **kwargs):
            raise OSError("Interrupted mirror write")

        monkeypatch.setattr(services.workspace, "write_file", fail_mirror)
        with pytest.raises(OSError, match="Interrupted"):
            await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "append", "content": "Fact 42 [p.1]."}, context,
            )
        assert runtime._paper_summary_state(context, document.id)["pending_checkpoint"]
        monkeypatch.setattr(services.workspace, "write_file", original)
        resumed = ScholarWeaveContext(run_id="recover-next", tool_runtime=runtime)
        restored = await runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "read"}, resumed,
        )
        assert restored["complete"] is True
        assert "Fact 42" in restored["content"]
        assert services.workspace.read_file(batch["checkpoint_path"]).content["complete"] is True
        # An old pending read cannot be attributed to a new extraction.
        services.retrieval.replace_document_chunks(document.id, [{"text": "Fact 99.", "citation": "p.1"}])
        with pytest.raises(ValueError, match="extraction changed"):
            await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "append", "content": "Fact 42 [p.1]."}, context,
            )
        fresh = await runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "read"}, context,
        )
        assert fresh["status"] == "empty"
    finally:
        await services.close()


@pytest.mark.anyio
async def test_late_evidence_write_reconciles_without_duplicate_or_overwrite(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="late.pdf", title="Late",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [{"text": "Fact 42.", "citation": "p.1"}])
    runtime = services.runs._tool_runtime
    first = ScholarWeaveContext(run_id="first", tool_runtime=runtime)
    late = ScholarWeaveContext(run_id="late", tool_runtime=runtime)
    try:
        for context in (first, late):
            await runtime._read_paper_summary_batch(
                {"document_id": document.id, "action": "chunks", "start": 0}, context,
            )
        await runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "append", "content": "Verified 42 [p.1]."}, first,
        )
        result = await runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "append", "content": "A different rendition [p.1]."}, late,
        )
        records = services.workspace.read_file(result["checkpoint_path"]).content["records"]
        assert len(records) == 1
        assert records[0]["content"] == "Verified 42 [p.1]."
        assert "pending_checkpoint" not in runtime._paper_summary_state(late, document.id)
    finally:
        await services.close()


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
    assert blueprint.run.max_turns is None
    assert blueprint.run.max_input_characters is None
    assert blueprint.run.max_output_characters is None
    assert blueprint.agents[0].model_settings.parallel_tool_calls is False
    assert "citation" in blueprint.agents[0].instructions.casefold()
    assert "an empty checkpoint needs no read" in blueprint.agents[0].instructions.casefold()
    assert "source-versioned evidence" in blueprint.agents[0].instructions
    assert "no arbitrary page or batch cap" in blueprint.agents[0].instructions
    assert "explicitly partial summary" in blueprint.agents[0].instructions


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
    folder = workspace.ensure_paper_folder("paper-1", "A Strong Paper")["folder"]
    workspace.write_file(
        f"{folder}/summaries/run-1.md",
        "# Versioned summary\n\nClaim [p.1]. Another result [p.2].\n",
    )
    workspace.write_file(
        f"{folder}/summaries/run-1.json",
        {
            "id": "run-1",
            "document_id": "paper-1",
            "run_id": "run-1",
            "path": f"{folder}/summaries/run-1.md",
            "created_at": "2026-09-03T10:00:00+00:00",
            "prompt_revision": "abc",
            "review_summary": "Citations checked.",
            "citation_count": 2,
            "status": "reviewed",
        },
    )
    workspace.write_file(f"{folder}/notes.md", "# Notes\n\nKeep me.\n")
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
    assert path == f"{folder}/summary.md"
    assert workspace.read_file(path).content == content
    assert workspace.read_file(f"{folder}/notes.md").content.endswith("Keep me.\n")
    session_factory.kw["bind"].dispose()
