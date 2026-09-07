from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
import httpx

from backend.agents.blueprint import FunctionToolSpec, ModelReferenceSpec, SessionPolicySpec
from backend.conversations.turns import (
    RESEARCH_TOOL_IDS,
    deep_work_blueprint,
    research_blueprint,
    validate_paper_work_completion,
)
from backend.core.errors import ValidationError
from backend.bootstrap import create_services
from backend.documents.models import Document
from backend.research.sources import WebSourceUnavailable
from backend.agents.context import ScholarWeaveContext
from backend.persistence.files import StorageError
from backend.tools.catalog import APPLICATION_TOOLS, _factory, create_tool_catalog
from backend.tools.runtime import (
    ApplicationToolRuntime,
    _paper_model_provenance,
    create_work_plan,
    update_work_item,
    work_plan,
)


class Runtime:
    def __init__(self) -> None:
        self.calls = []

    async def invoke(self, catalog_id, arguments, context):
        self.calls.append((catalog_id, arguments, context.run_id))
        return {"ok": True}


@pytest.mark.anyio
async def test_summary_tool_exposes_only_document_and_mode() -> None:
    runtime = Runtime()
    tool = create_tool_catalog().build_function_tool(
        FunctionToolSpec(id="summary", catalog_id="research.summary.run"),
    )
    context = ScholarWeaveContext(run_id="summary-contract", tool_runtime=runtime)
    arguments = {"document_id": "paper", "mode": "reviewed"}
    assert await tool.on_invoke_tool(
        SimpleNamespace(context=context, tool_call_id="summary"), json.dumps(arguments),
    ) == {"ok": True}
    assert set(tool.params_json_schema["properties"]) == {"document_id", "mode"}
    assert set(tool.params_json_schema["required"]) == {"document_id", "mode"}
    assert tool.params_json_schema["additionalProperties"] is False
    assert runtime.calls == [("research.summary.run", arguments, context.run_id)]


@pytest.mark.anyio
async def test_workspace_discovery_tools_are_bound_and_invoke_real_services(test_settings) -> None:
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="discovery", tool_runtime=runtime)
        catalog = create_tool_catalog()

        async def invoke(catalog_id, arguments):
            tool = catalog.build_function_tool(FunctionToolSpec(id="discovery", catalog_id=catalog_id))
            return await tool.on_invoke_tool(
                SimpleNamespace(context=context, tool_call_id=catalog_id), json.dumps(arguments),
            )

        for blueprint in (research_blueprint(ModelReferenceSpec()), deep_work_blueprint(ModelReferenceSpec())):
            bound = {tool.catalog_id for tool in blueprint.tools}
            assert {"research.workspace.list", "research.workspace.index", "research.notes.search"} <= bound
        first = services.workspace.create_note(name="First", content="quasar")
        services.workspace.create_note(name="Second", content="quasar")
        page = await invoke("research.workspace.list", {"collection": "notes", "limit": 1, "offset": 0})
        second = await invoke("research.workspace.list", {
            "collection": "notes", "limit": 1, "offset": page["next_offset"],
        })
        assert page["next_offset"] == 1 and second["next_offset"] is None
        assert page["results"][0]["path"] != second["results"][0]["path"]
        hits = await invoke("research.notes.search", {
            "query": "quasar", "kinds": ["note"], "tags": [], "limit": 1, "offset": 1,
        })
        assert len(hits) == 1 and hits[0]["score"] > 0 and hits[0]["excerpt"]
        assert await invoke("research.workspace.list", {
            "collection": "papers", "limit": 1, "offset": 0,
        }) == {"collection": "papers", "results": [], "next_offset": None}
        assert (await invoke("research.workspace.index", {"action": "status"}))["indexed_files"] == 2
        services.storage.write_workspace_file(first.path, "externalneedle")
        await invoke("research.workspace.index", {"action": "refresh"})
        hits = await invoke("research.notes.search", {
            "query": "externalneedle", "kinds": [], "tags": [], "limit": 10, "offset": None,
        })
        assert hits[0]["path"] == first.path
        assert (await invoke("research.notes.read", {"path": first.path}))["content"] == "externalneedle"
    finally:
        await services.close()


def test_paper_provenance_prefers_task_local_identity_over_shared_metadata(monkeypatch) -> None:
    context = ScholarWeaveContext(
        run_id="provenance", tool_runtime=Runtime(),
        metadata={"active_model": {"model": "racing-coordinator"}},
    )
    monkeypatch.setattr(
        "backend.tools.runtime.current_tool_model_identity",
        lambda: {"model": "actual-delegate", "provider_kind": "openai_compatible"},
    )
    assert _paper_model_provenance(context) == {"model": "actual-delegate", "provider_kind": "openai_compatible"}
    context.metadata["paper_summary_model"] = {"model": "dedicated-summary"}
    assert _paper_model_provenance(context) == {"model": "actual-delegate", "provider_kind": "openai_compatible"}
    context.metadata["paper_summary_model"] = {
        "model": "actual-delegate", "provider_kind": "openai_compatible", "provider_profile_id": "profile",
    }
    assert _paper_model_provenance(context) == context.metadata["paper_summary_model"]
    context.metadata.pop("paper_summary_model")
    monkeypatch.setattr("backend.tools.runtime.current_tool_model_identity", lambda: None)
    assert _paper_model_provenance(context) == {"model": "racing-coordinator"}


@pytest.mark.anyio
@pytest.mark.parametrize("raw", [
    "{", "[]", "null", '{"title":null}', '{"title":5}',
    '{"title":"ok","unexpected":true}', '{"title":""}',
    json.dumps({"title": "x" * 1000}),
])
async def test_catalog_rejects_invalid_arguments_before_any_mutation(raw) -> None:
    runtime = Runtime()
    tool = create_tool_catalog().build_function_tool(
        FunctionToolSpec(id="title", catalog_id="conversation.title.set"),
    )
    context = ScholarWeaveContext(run_id="validation", tool_runtime=runtime)
    with pytest.raises(RuntimeError) as error:
        await tool.on_invoke_tool(SimpleNamespace(context=context, tool_call_id="invalid"), raw)
    payload = json.loads(str(error.value))
    assert payload["category"] == "invalid_input"
    assert payload["unknown_outcome"] is False
    assert payload["retryable"] is False
    assert len(payload["message"]) < 350
    assert runtime.calls == []


@pytest.mark.anyio
async def test_catalog_validates_nullable_fields_and_nested_required_properties() -> None:
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="validation", tool_runtime=runtime)
    invocation = SimpleNamespace(context=context, tool_call_id="nullable")
    catalog = create_tool_catalog()
    paper = catalog.build_function_tool(FunctionToolSpec(id="read", catalog_id="research.paper.read"))
    arguments = {"document_id": "paper", "action": "inspect", "start": None, "limit": 1, "query": None, "offset": None}
    assert await paper.on_invoke_tool(invocation, json.dumps(arguments)) == {"ok": True}
    assert runtime.calls[-1][1] == arguments
    plan = catalog.build_function_tool(FunctionToolSpec(id="plan", catalog_id="work.plan.create"))
    with pytest.raises(RuntimeError) as error:
        await plan.on_invoke_tool(invocation, '{"items":[{"title":"missing id"}]}')
    assert "items.0" in json.loads(str(error.value))["message"]
    assert len(runtime.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("strict", [False, True])
async def test_catalog_validator_preserves_schema_optional_properties(strict) -> None:
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="optional", tool_runtime=runtime)
    schema = {
        "type": "object",
        "properties": {"required": {"type": "string"}, "optional": {"type": ["string", "null"]}},
        "required": ["required"], "additionalProperties": False,
    }
    tool = _factory("test.optional", "optional", "Optional input", schema, strict)(
        FunctionToolSpec(id="optional", catalog_id="test.optional"),
    )
    for arguments in ({"required": "yes"}, {"required": "yes", "optional": None}):
        assert await tool.on_invoke_tool(
            SimpleNamespace(context=context, tool_call_id="optional"), json.dumps(arguments),
        ) == {"ok": True}
    assert len(runtime.calls) == 2


@pytest.mark.anyio
async def test_tool_retries_keep_transport_timeouts_and_retry_after_without_global_deadline(
    test_settings, monkeypatch,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(run_id="no-deadline", tool_runtime=runtime)
    test_settings.tool_call_timeout_seconds = 0.001
    test_settings.tool_read_retry_attempts = 4
    calls = 0

    async def rate_limited(arguments, context):
        nonlocal calls
        calls += 1
        if calls == 2:
            return {"ready": True}
        response = httpx.Response(
            429, headers={"Retry-After": "0.03"},
            request=httpx.Request("GET", "http://localhost/source"),
        )
        raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)

    monkeypatch.setattr(runtime, "_read_research_paper", rate_limited)
    try:
        started = asyncio.get_running_loop().time()
        assert await runtime.invoke("research.paper.read", {"action": "pages"}, context) == {"ready": True}
        assert asyncio.get_running_loop().time() - started >= 0.03
        assert calls == 2

        async def slow_read(arguments, context):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            raise httpx.ReadTimeout("slow source")

        monkeypatch.setattr(runtime, "_read_research_paper", slow_read)
        monkeypatch.setattr("backend.tools.runtime.retry_delay", lambda *args: 0)
        calls = 0
        with pytest.raises(httpx.ReadTimeout):
            await runtime.invoke("research.paper.read", {"action": "pages"}, context)
        assert calls == 4

        calls = 0
        with pytest.raises(httpx.ReadTimeout):
            await runtime.invoke("research.paper.read", {"action": "prepare"}, context)
        assert calls == 1
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("handler_kind", ["async", "sync", "returned_awaitable"])
async def test_legacy_tool_timeout_does_not_interrupt_handler_completion(
    test_settings, monkeypatch, handler_kind,
) -> None:
    test_settings.tool_call_timeout_seconds = 0.001
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext("slow-tool", runtime)
    expected = {"evidence": "Completed after the legacy deadline."}

    async def async_read(arguments, context):
        await asyncio.sleep(0.03)
        return expected

    def sync_read(arguments, context):
        threading.Event().wait(0.03)
        return expected

    def returned_awaitable(arguments, context):
        return async_read(arguments, context)

    monkeypatch.setattr(runtime, "_read_research_paper", {
        "async": async_read, "sync": sync_read, "returned_awaitable": returned_awaitable,
    }[handler_kind])
    try:
        assert await runtime.invoke("research.paper.read", {"action": "pages"}, context) == expected
    finally:
        await services.close()


@pytest.mark.anyio
async def test_async_tool_remains_cancellable_without_whole_operation_timeout(
    test_settings, monkeypatch,
) -> None:
    test_settings.tool_call_timeout_seconds = 0.001
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    run = services.runs._repository.create(
        conversation_id=None, agent_name="Cancelable read", input_value="read", blueprint={},
    )
    context = ScholarWeaveContext(run.id, runtime)
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def read(arguments, context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(runtime, "_read_research_paper", read)
    task = asyncio.create_task(runtime.invoke("research.paper.read", {"action": "pages"}, context))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.02)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        attempts = services.runs.get(run.id).tool_attempts
        assert len(attempts) == 1
        assert attempts[0].status == "cancelled"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("interrupt", ["cancel", "none"])
async def test_dispatched_write_is_joined_before_mutation_lock_released(test_settings, monkeypatch, interrupt) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(run_id="joined-write", tool_runtime=runtime)
    entered = threading.Event()
    release = threading.Event()
    completed: list[str] = []

    def write(arguments, context):
        if arguments["name"] == "first":
            entered.set()
            assert release.wait(timeout=2)
        completed.append(arguments["name"])
        return {"saved": arguments["name"]}

    monkeypatch.setattr(runtime, "_save_research_note", write)
    test_settings.tool_call_timeout_seconds = 0.001
    first = asyncio.create_task(runtime.invoke("research.notes.save", {"name": "first"}, context))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if interrupt == "cancel":
            first.cancel()
        await asyncio.sleep(0.05)
        assert not first.done()
        assert runtime._mutation_lock.locked()
        second = asyncio.create_task(runtime.invoke("research.notes.save", {"name": "second"}, context))
        await asyncio.sleep(0.02)
        assert completed == []
        assert not second.done()
        release.set()
        if interrupt == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            assert await first == {"saved": "first"}
        assert await second == {"saved": "second"}
        assert completed == ["first", "second"]
        assert not runtime._mutation_lock.locked()
    finally:
        release.set()
        await asyncio.gather(first, *( [second] if second is not None else []), return_exceptions=True)
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("catalog_id,handler_name", [
    ("research.notes.read", "_read_research_note"),
    ("research.summary.checkpoint", "_paper_summary_checkpoint"),
])
async def test_fresh_tool_outputs_are_not_clipped_to_a_global_budget(
    test_settings, monkeypatch, catalog_id, handler_name,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    run = services.runs._repository.create(
        conversation_id=None, agent_name="Reader", input_value="Read all evidence", blueprint={},
    )
    context = ScholarWeaveContext(run.id, runtime)
    text = "Exact finding π with quoted \"evidence\".\n" * 1800
    result = {"content": text, "final_checkpoint": text}

    def read(arguments, active_context):
        return result

    monkeypatch.setattr(runtime, handler_name, read)
    try:
        assert await runtime.bound_tool_result(catalog_id, result, context) is result
        assert await runtime.bound_tool_result(catalog_id, text, context) == text
        assert await runtime.invoke(catalog_id, {"action": "read"}, context) is result
        assert not list(test_settings.artifacts_dir.rglob("tool-results"))
    finally:
        await services.close()


@pytest.mark.anyio
async def test_explicit_cached_result_slice_is_not_reclipped(test_settings) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext("explicit-slice", runtime)
    text = "Exact quoted \"evidence\" π\n" * 5000
    try:
        stored = await runtime.bound_tool_result("lookup", text, context, max_tokens=1)
        serialized = json.dumps(text, ensure_ascii=False)
        tool = create_tool_catalog().build_function_tool(
            FunctionToolSpec(id="read", catalog_id="tool.results.read"),
        )
        arguments = {"result_ref": stored["result_ref"], "offset": 17, "limit": 60_000}
        page = await tool.on_invoke_tool(
            SimpleNamespace(context=context, tool_call_id="slice"), json.dumps(arguments),
        )
        assert page == {
            "result_ref": stored["result_ref"],
            "offset": 17,
            "content": serialized[17:60_017],
            "has_more": True,
            "next_offset": 60_017,
            "size_characters": len(serialized),
        }
        assert await runtime.bound_tool_result("tool.results.read", page, context) is page
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["pages", "chunks"])
async def test_paper_reads_honor_requested_items_without_hidden_output_caps(
    test_settings, action,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="full-paper.pdf", title="Full paper",
    )
    texts = ["Long cited text π with \"quotes\".\n" * 1000] + [
        f"Evidence for page {index}." for index in range(2, 71)
    ]
    services.documents.repository.mark_ready(document.id, page_count=len(texts), metadata={})
    services.retrieval.replace_document_chunks(
        document.id, [{"text": text, "citation": f"p.{index + 1}"} for index, text in enumerate(texts)],
    )
    manifest = services.storage.write_json(
        test_settings.artifacts_dir, f"documents/{document.id}/manifest.json",
        {"pages": [{"page": index + 1, "text": text} for index, text in enumerate(texts)]},
    )
    services.documents.create_artifact_record(
        owner_type="document", kind="extracted_manifest", document_id=document.id,
        relative_path=manifest.relative_path, media_type="application/json",
        stored=manifest, storage_area="artifacts",
    )
    context = ScholarWeaveContext("full-paper", runtime)
    first_index = 1 if action == "pages" else 0
    key = "pages" if action == "pages" else "chunks"
    cursor_key = "next_page" if action == "pages" else "next_start"
    try:
        tool = create_tool_catalog().build_function_tool(
            FunctionToolSpec(id="paper", catalog_id="research.paper.read"),
        )
        result = await tool.on_invoke_tool(
            SimpleNamespace(context=context, tool_call_id="paper"),
            json.dumps({
                "document_id": document.id, "action": action, "start": first_index,
                "limit": 11, "offset": 17, "query": None,
            }),
        )
        assert [item["text"] for item in result[key]] == [texts[0][17:]] + texts[1:11]
        assert all(item["text_complete"] for item in result[key])
        assert result[key][0]["offset"] == 17
        assert result["has_more"] is True
        assert result[cursor_key] == first_index + 11
        assert result["next_offset"] == 0
        summary = await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": action, "start": first_index}, context,
        )
        assert [item["text"] for item in summary[key]] == texts
        assert summary["has_more"] is False
        assert summary["next_start"] is None
        assert len(summary["coverage"]["spans"]) == 70
    finally:
        await services.close()


@pytest.mark.anyio
@pytest.mark.parametrize("available_chars", [12_000, 80_000])
async def test_context_sized_summary_batches_checkpoint_only_visible_spans(
    test_settings, available_chars,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="context-sized.pdf", title="Context sized",
    )
    texts = ["Quoted \"finding\" π.\n" * 1800, "Last cited result." * 1000]
    services.documents.repository.mark_ready(document.id, page_count=2, metadata={})
    services.retrieval.replace_document_chunks(
        document.id, [{"text": text, "citation": f"p.{index + 1}"} for index, text in enumerate(texts)],
    )
    context = ScholarWeaveContext("context-sized-summary", runtime)
    reconstructed = ["", ""]
    batch_count = 0
    try:
        while True:
            full = await runtime._read_paper_summary_batch(
                {"document_id": document.id, "action": "chunks", "start": None}, context,
            )
            visible = runtime.constrain_paper_summary_batch(
                full, context, max_chars=available_chars,
            )
            assert visible is not None
            assert len(json.dumps(visible, ensure_ascii=False)) <= available_chars
            pending = runtime._paper_summary_state(context, document.id)["pending_checkpoint"]
            assert pending["id"] == visible["batch_id"]
            assert pending["coverage"] == visible["coverage"]
            for chunk, span in zip(visible["chunks"], visible["coverage"]["spans"]):
                index = chunk["chunk_index"]
                assert chunk["offset"] == len(reconstructed[index])
                reconstructed[index] += chunk["text"]
                assert span["end_offset"] == len(reconstructed[index])
            batch_count += 1
            appended = await runtime._paper_summary_checkpoint(
                {
                    "document_id": document.id, "action": "append",
                    "content": f"Visible batch {batch_count} findings [p.1].",
                },
                context,
            )
            assert appended["complete"] is (not visible["has_more"])
            if not visible["has_more"]:
                break
        assert reconstructed == texts
        assert batch_count > 1 if available_chars == 12_000 else batch_count == 1
    finally:
        await services.close()


@pytest.mark.anyio
async def test_archived_pending_summary_cannot_claim_complete_coverage(test_settings) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="archived-source.pdf", title="Archived source",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [{"text": "Source evidence.", "citation": "p.1"}])
    context = ScholarWeaveContext("archived-summary", runtime)
    try:
        result = await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "chunks", "start": 0}, context,
        )
        runtime.mark_paper_summary_batch_unavailable(result, context)
        with pytest.raises(ValueError, match="archived"):
            await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "append", "content": "Unseen claim [p.1]."},
                context,
            )
        with pytest.raises(ValueError, match="archived"):
            runtime._save_paper_summary_version(
                {
                    "document_id": document.id, "content": "Unseen summary [p.1].",
                    "review_summary": "Unseen source.",
                },
                context,
            )
        assert not runtime._load_summary_evidence(
            runtime._paper_summary_state(context, document.id),
        )["complete"]
        await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "chunks", "start": 0}, context,
        )
        appended = await runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "append", "content": "Reread evidence [p.1]."},
            context,
        )
        assert appended["complete"] is True
    finally:
        await services.close()


@pytest.mark.anyio
async def test_context_batch_sizing_retries_original_without_stale_coverage(test_settings) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="retry-source.pdf", title="Retry source",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(
        document.id, [{"text": "Exact source evidence. " * 3000, "citation": "p.1"}],
    )
    context = ScholarWeaveContext("retry-summary", runtime)
    try:
        original = await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "chunks", "start": 0}, context,
        )
        first = runtime.constrain_paper_summary_batch(original, context, max_chars=20_000)
        assert first is not None
        smaller = runtime.constrain_paper_summary_batch(original, context, max_chars=12_000)
        assert smaller is not None
        assert smaller["next_offset"] < first["next_offset"]
        state = runtime._paper_summary_state(context, document.id)
        assert state["pending_checkpoint"]["coverage"] == smaller["coverage"]
        restored = runtime.constrain_paper_summary_batch(original, context, max_chars=100_000)
        assert restored is original
        assert state["pending_checkpoint"]["coverage"] == original["coverage"]
        assert state["pending_checkpoint"]["has_more"] is False
        assert runtime.constrain_paper_summary_batch(original, context, max_chars=1) is None
        assert runtime.constrain_paper_summary_batch(
            {**original, "batch_id": "unrelated"}, context, max_chars=100_000,
        ) is None
        assert runtime.constrain_paper_summary_batch(
            {"document_id": document.id}, context, max_chars=100_000,
        ) is None
        assert runtime.constrain_paper_summary_batch(original, context, max_chars=12_000) is not None
        runtime.mark_paper_summary_batch_unavailable(original, context)
        assert state["pending_checkpoint"]["content_unavailable"] is True
        with pytest.raises(ValueError, match="archived"):
            await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "append", "content": "Incomplete evidence."},
                context,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_full_paper_reads_search_and_durable_evidence(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="bounded.pdf", title="Bounded",
    )
    services.documents.repository.mark_ready(document.id, page_count=2, metadata={})
    texts = ['Needle exact result 42. ' + '\\ " \n猫' * 3500, "The final conclusion."]
    services.retrieval.replace_document_chunks(
        document.id, [{"text": text, "citation": f"p.{index + 1}"} for index, text in enumerate(texts)],
    )
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(
        run_id="budgeted", tool_runtime=runtime,
        metadata={"paper_summary_model": {"model": "local-test"}},
    )
    try:
        hit = await runtime._read_research_paper(
            {"document_id": document.id, "action": "search", "query": "needle", "limit": 3},
            context,
        )
        assert hit["matches"][0]["citation"] == "p.1"
        assert hit["matches"][0]["chunk_index"] == 0
        assert context.metadata["paper_activity"][-1]["citations"] == ["p.1"]
        assert hit["matches"][0]["excerpt"] is True
        reconstructed = ["", ""]
        cursor, offset = 0, 0
        batches = 0
        while True:
            batch = await runtime._read_paper_summary_batch(
                {"document_id": document.id, "action": "chunks", "start": cursor, "offset": offset},
                context,
            )
            assert len(json.dumps(batch, ensure_ascii=False)) > 12_000
            assert not (await runtime.bound_tool_result("research.summary.read", batch, context)).get("result_ref")
            for chunk in batch["chunks"]:
                reconstructed[chunk["chunk_index"]] += chunk["text"]
            evidence = f"Batch {batches}: exact finding 42 and continuation [p.1]."
            appended = await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "append", "content": evidence}, context,
            )
            assert appended["document_id"] == document.id
            assert appended["batch_id"] == batch["batch_id"]
            # Retry after the verified write must not duplicate evidence.
            await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "append", "content": evidence}, context,
            )
            batches += 1
            if not batch["has_more"]:
                break
            cursor, offset = batch["next_start"], batch["next_offset"]
            context = ScholarWeaveContext(run_id=f"resumed-{batches}", tool_runtime=runtime)
            resumed = await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "read"}, context,
            )
            assert (resumed["resume_start"], resumed["resume_offset"]) == (cursor, offset)
        assert reconstructed == texts
        assert batches == 1
        assert appended["complete"] is True
        index = services.workspace.read_file(appended["checkpoint_path"]).content
        assert len(index["records"]) == batches
        assert index["records"][0]["model"] == {"model": "local-test"}
        assert index["source_hash"] and index["extraction_hash"]
        old_path = appended["checkpoint_path"]
        services.retrieval.replace_document_chunks(document.id, [{"text": "Different extraction"}])
        with pytest.raises(ValueError, match="extraction changed"):
            await runtime._paper_summary_checkpoint(
                {"document_id": document.id, "action": "read"}, context,
            )
        changed = await runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "read"}, context,
        )
        assert changed["status"] == "empty"
        assert changed["checkpoint_path"] != old_path
        assert services.workspace.read_file(old_path).content["complete"] is True
    finally:
        await services.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_research_catalog_builds_sdk_function_tools() -> None:
    catalog = create_tool_catalog()
    tool = catalog.build_function_tool(
        FunctionToolSpec(id="search", catalog_id="research.sources.search")
    )
    runtime = Runtime()
    context = ScholarWeaveContext(run_id="run-1", tool_runtime=runtime)

    output = await tool.on_invoke_tool(
        SimpleNamespace(context=context),
        '{"provider":"arxiv","query":"small language models"}',
    )

    assert output == {"ok": True}
    assert runtime.calls == [
        (
            "research.sources.search",
            {"provider": "arxiv", "query": "small language models"},
            "run-1",
        )
    ]
    assert tool.name == "search_research_sources"


def test_catalog_contains_research_and_persistence_tools() -> None:
    expected = {catalog_id for _, catalog_id in RESEARCH_TOOL_IDS}
    assert {item[0] for item in APPLICATION_TOOLS} == {
        *expected,
        "conversation.title.set",
        "tool.results.read",
        "research.summary.save",
        "research.summary.read",
        "research.summary.checkpoint",
        "work.plan.create",
        "work.plan.update",
        "work.plan.read",
    }
    assert all(hasattr(ApplicationToolRuntime, item[5]) for item in APPLICATION_TOOLS)
    for definition in create_tool_catalog().definitions():
        schema = definition.parameters_schema
        assert schema["additionalProperties"] is False
        assert all(
            "description" in property_schema
            for property_schema in schema["properties"].values()
        )


@pytest.mark.anyio
async def test_conversation_title_tool_is_first_turn_only(test_settings) -> None:
    services = create_services(test_settings)
    conversation = services.conversation_turns.create_conversation(
        title="New research",
        model_reference=ModelReferenceSpec(),
    )
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(
        run_id="title-run",
        conversation_id=conversation.id,
        tool_runtime=runtime,
        metadata={"allow_conversation_title_update": True},
    )
    try:
        result = await runtime.invoke(
            "conversation.title.set",
            {"title": "  Evidence-Based Retrieval   Review  "},
            context,
        )

        assert result["title"] == "Evidence-Based Retrieval Review"
        assert services.conversations.get(conversation.id).title == result["title"]
        with pytest.raises(ValueError, match="already been set"):
            await runtime.invoke(
                "conversation.title.set",
                {"title": "A second title"},
                context,
            )
        with pytest.raises(ValueError, match="first turn"):
            await runtime.invoke(
                "conversation.title.set",
                {"title": "A later title"},
                ScholarWeaveContext(
                    run_id="later-run",
                    conversation_id=conversation.id,
                    tool_runtime=runtime,
                ),
            )
    finally:
        await services.close()


def test_only_main_agent_receives_title_tool_on_first_turn() -> None:
    first_research = research_blueprint({}, first_turn=True)
    later_research = research_blueprint({}, first_turn=False)
    first_deep_work = deep_work_blueprint({}, first_turn=True)

    assert "set-title" in first_research.agents[0].tool_ids
    assert "set-title" not in later_research.agents[0].tool_ids
    assert "set-title" in first_deep_work.agents[0].tool_ids
    assert "set-title" not in first_deep_work.agents[1].tool_ids


@pytest.mark.anyio
async def test_context_history_and_tool_cache_survive_run_cleanup(test_settings) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    previous = services.runs._repository.create(
        conversation_id="durable", agent_name="Previous", input_value="old", blueprint={},
    )
    context = ScholarWeaveContext(previous.id, runtime, conversation_id="durable")
    items = [
        {"role": "user", "content": "Keep exact π, 🧪, newlines\nand \"quotes\"."},
        {"type": "function_call", "name": "lookup", "call_id": "c1", "arguments": '{"q":"π"}'},
        {"type": "function_call_output", "call_id": "c1", "output": {"values": [1, None, False]}},
    ]
    try:
        history = runtime.store_context_history(items, context)
        assert history == runtime.store_context_history(items, context)
        tool_value = {"evidence": "exact tool text " * 100}
        tool = await runtime.bound_tool_result("lookup", tool_value, context, max_tokens=1)
        assert history["result_ref"].startswith("conversations/durable/context-history/")
        assert tool["result_ref"].startswith("conversations/durable/tool-results/")
        legacy = services.storage.write_text(
            test_settings.artifacts_dir, f"runs/{previous.id}/tool-results/old.json", "old",
        )
        standalone = services.runs._repository.create(
            conversation_id=None, agent_name="Standalone", input_value="old", blueprint={},
        )
        standalone_cache = runtime.store_context_history(
            items, ScholarWeaveContext(standalone.id, runtime),
        )
        assert services.runs.clear_history() == 2
        assert not services.runs._repository.exists(previous.id)
        assert not legacy.absolute_path.exists()
        assert not (test_settings.artifacts_dir / standalone_cache["result_ref"]).exists()
        later = ScholarWeaveContext("later", runtime, conversation_id="durable")
        page = runtime._read_tool_result(
            {"result_ref": history["result_ref"], "offset": 0, "limit": 16000}, later,
        )
        assert json.loads(page["content"]) == items
        assert history["size_bytes"] == len(page["content"].encode("utf-8"))
        for item, entry in zip(items, history["index"]):
            assert json.loads(page["content"][entry["offset"]:entry["offset"] + entry["length"]]) == item
        assert json.loads(runtime._read_tool_result(
            {"result_ref": tool["result_ref"], "offset": 0, "limit": 16000}, later,
        )["content"]) == tool_value
    finally:
        await services.close()


@pytest.mark.anyio
async def test_context_history_chunks_large_unicode_archive_and_indexes(test_settings) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext("large", runtime, conversation_id="large")
    items = [{"role": "tool", "name": "web", "content": "🧪π" * 1_100_000}]
    try:
        history = runtime.store_context_history(items, context)
        assert history["size_bytes"] > test_settings.max_artifact_bytes
        assert history["result_ref"].endswith(".manifest.json")
        serialized = "[" + json.dumps(items[0], ensure_ascii=False) + "]"
        reconstructed: list[str] = []
        offset = 0
        while True:
            page = runtime._read_tool_result(
                {"result_ref": history["result_ref"], "offset": offset, "limit": 16000}, context,
            )
            reconstructed.append(page["content"])
            if not page["has_more"]:
                break
            offset = page["next_offset"]
        assert "".join(reconstructed) == serialized
        assert all(
            path.stat().st_size <= test_settings.max_artifact_bytes
            for path in test_settings.artifacts_dir.rglob("*") if path.is_file()
        )
        many = runtime.store_context_history([{"role": "user", "content": str(i)} for i in range(100)], context)
        assert "index" not in many and "index_ref" in many
        index = json.loads(runtime._read_tool_result(
            {"result_ref": many["index_ref"], "offset": 0, "limit": 16000}, context,
        )["content"])
        last = runtime._read_tool_result(
            {"result_ref": many["result_ref"], "offset": index[-1]["offset"], "limit": index[-1]["length"]}, context,
        )
        assert json.loads(last["content"]) == {"role": "user", "content": "99"}
    finally:
        await services.close()


@pytest.mark.anyio
async def test_context_history_scopes_and_traversal_are_guarded(test_settings) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext("active", runtime, conversation_id="first")
    try:
        stored = runtime.store_context_history([{"role": "user", "content": "private"}], context)
        for forbidden_context in (
            ScholarWeaveContext("active", runtime, conversation_id="second"),
            ScholarWeaveContext("active", runtime),
        ):
            with pytest.raises(ValueError, match="active run or its conversation"):
                runtime._read_tool_result(
                    {"result_ref": stored["result_ref"], "offset": 0, "limit": 256}, forbidden_context,
                )
        for path in (
            "conversations/first/../second/context-history/a.json",
            "conversations\\first\\context-history\\..\\..\\second\\a.json",
            "conversations/first./context-history/a.json",
            "conversations/first/notes/a.json",
            "/conversations/first/context-history/a.json",
            "C:\\conversations\\first\\context-history\\a.json",
        ):
            with pytest.raises(ValueError, match="result_ref"):
                runtime._read_tool_result({"result_ref": path, "offset": 0, "limit": 256}, context)
        standalone = ScholarWeaveContext("standalone", runtime)
        standalone_ref = runtime.store_context_history([{"role": "user", "content": "local"}], standalone)["result_ref"]
        assert standalone_ref.startswith("runs/standalone/context-history/")
        assert runtime._read_tool_result(
            {"result_ref": standalone_ref, "offset": 0, "limit": 256}, standalone,
        )["content"]
        with pytest.raises(ValueError, match="active run or its conversation"):
            runtime._read_tool_result(
                {"result_ref": standalone_ref, "offset": 0, "limit": 256}, context,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_context_history_storage_failures_raise_without_false_success(test_settings, monkeypatch) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext("failure", runtime)
    try:
        existing = runtime.store_context_history([{"role": "user", "content": "deduplicated"}], context)
        def fail_write(*args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(services.storage, "write_bytes", fail_write)
        assert runtime.store_context_history([{"role": "user", "content": "deduplicated"}], context) == existing
        with pytest.raises(OSError, match="disk full"):
            runtime.store_context_history([{"role": "user", "content": "must keep"}], context)
        with pytest.raises(OSError, match="disk full"):
            await runtime.bound_tool_result("lookup", {"text": "large" * 100}, context, max_tokens=1)
        with pytest.raises(TypeError):
            runtime.store_context_history([{"role": "user", "content": object()}], context)
        monkeypatch.setattr(test_settings, "max_artifact_bytes", 16)
        with pytest.raises(StorageError, match="manifest exceeds"):
            runtime.store_context_history([{"role": "user", "content": "large" * 100}], context)
    finally:
        await services.close()


@pytest.mark.anyio
async def test_conversation_deletion_removes_only_its_durable_caches(test_settings) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    try:
        conversations = [
            services.conversations.create(
                title=title, kind="research", model_reference={}, session_policy=SessionPolicySpec(),
            ) for title in ("Delete", "Keep")
        ]
        references: list[str] = []
        for conversation in conversations:
            context = ScholarWeaveContext("run-" + conversation.id, runtime, conversation_id=conversation.id)
            references.append(runtime.store_context_history([{"role": "user", "content": "exact"}], context)["result_ref"])
            await runtime.bound_tool_result("lookup", {"text": "tool" * 100}, context, max_tokens=1)
        await services.conversations.delete(conversations[0].id)
        assert not (test_settings.artifacts_dir / "conversations" / conversations[0].id).exists()
        assert (test_settings.artifacts_dir / references[1]).exists()
        assert services.conversations.get(conversations[1].id).title == "Keep"
    finally:
        await services.close()


@pytest.mark.anyio
async def test_tool_results_can_be_read_across_runs_in_one_conversation(
    test_settings,
) -> None:
    services = create_services(test_settings)
    repository = services.runs._repository
    previous = repository.create(
        conversation_id="conversation-1",
        agent_name="Previous",
        input_value="previous",
        blueprint={},
    )
    active = repository.create(
        conversation_id="conversation-1",
        agent_name="Active",
        input_value="active",
        blueprint={},
    )
    unrelated = repository.create(
        conversation_id="conversation-2",
        agent_name="Unrelated",
        input_value="unrelated",
        blueprint={},
    )
    previous_result = services.storage.write_text(
        test_settings.artifacts_dir,
        f"runs/{previous.id}/tool-results/result.json",
        '{"evidence":"retained"}',
    )
    unrelated_result = services.storage.write_text(
        test_settings.artifacts_dir,
        f"runs/{unrelated.id}/tool-results/result.json",
        '{"evidence":"private"}',
    )
    context = ScholarWeaveContext(
        run_id=active.id,
        conversation_id="conversation-1",
        tool_runtime=services.runs._tool_runtime,
    )

    try:
        assert previous_result.relative_path == (
            f"runs/{previous.id}/tool-results/result.json"
        )
        result = services.runs._tool_runtime._read_tool_result(
            {"result_ref": previous_result.relative_path, "offset": 0, "limit": 100},
            context,
        )
        doubled_separators = previous_result.relative_path.replace("/", "\\\\")
        normalized = services.runs._tool_runtime._read_tool_result(
            {"result_ref": doubled_separators, "offset": 0, "limit": 100},
            context,
        )
        assert "retained" in result["content"]
        assert normalized["result_ref"] == previous_result.relative_path
        with pytest.raises(ValueError, match="active run or its conversation"):
            services.runs._tool_runtime._read_tool_result(
                {
                    "result_ref": unrelated_result.relative_path,
                    "offset": 0,
                    "limit": 100,
                },
                context,
            )
        for unsafe_ref in (
            f"runs/{active.id}/../{unrelated.id}/tool-results/result.json",
            f"/runs/{active.id}/tool-results/result.json",
            f"C:\\runs\\{active.id}\\tool-results\\result.json",
            f"\\\\server\\runs\\{active.id}\\tool-results\\result.json",
        ):
            with pytest.raises(ValueError, match="result_ref"):
                services.runs._tool_runtime._read_tool_result(
                    {"result_ref": unsafe_ref, "offset": 0, "limit": 100},
                    context,
                )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_paper_chunk_reads_return_a_non_overlapping_cursor(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document_id = "paged-paper"
    try:
        with services.session_factory() as session:
            session.add(
                Document(
                    id=document_id,
                    title="Paged Paper",
                    source_filename="paged.pdf",
                    content_type="application/pdf",
                    status="ready",
                    page_count=3,
                    metadata_json={},
                )
            )
            session.commit()

        services.retrieval.replace_document_chunks(
            document_id,
            [
                {
                    "text": f"Chunk {index}",
                    "page_start": index + 1,
                    "page_end": index + 1,
                    "citation": f"p.{index + 1}",
                }
                for index in range(3)
            ],
        )
        context = ScholarWeaveContext(
            run_id="chunk-run",
            tool_runtime=services.runs._tool_runtime,
        )

        result = await services.runs._tool_runtime.invoke(
            "research.paper.read",
            {
                "document_id": document_id,
                "action": "chunks",
                "start": 1,
                "limit": 1,
            },
            context,
        )

        assert result["chunk_count"] == 3
        assert result["start"] == 1
        assert result["has_more"] is True
        assert result["next_start"] == 2
        assert [chunk["chunk_index"] for chunk in result["chunks"]] == [1]
        empty_context = ScholarWeaveContext(
            run_id="empty-chunk-run",
            tool_runtime=services.runs._tool_runtime,
        )
        empty = await services.runs._tool_runtime.invoke(
            "research.paper.read",
            {
                "document_id": document_id,
                "action": "chunks",
                "start": 3,
                "limit": 1,
            },
            empty_context,
        )
        assert empty["chunks"] == []
        assert empty["next_start"] is None
        assert "paper_activity" not in empty_context.metadata
    finally:
        await services.close()


@pytest.mark.anyio
async def test_startup_reconciles_existing_papers_into_workspace(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document_id = "legacy-paper"
    folder = services.workspace.paper_folder(document_id, "Legacy Paper")
    summary_path = f"{folder}/summary.md"
    notes_path = f"{folder}/notes.md"
    try:
        with services.session_factory() as session:
            session.add(
                Document(
                    id=document_id,
                    title="Legacy Paper",
                    source_filename="legacy.pdf",
                    content_type="application/pdf",
                    status="ready",
                    page_count=3,
                    metadata_json={},
                )
            )
            session.commit()
        services.storage.write_workspace_file(
            summary_path,
            "# Legacy Paper\n\nPreserve this summary.\n",
        )
        services.workspace.write_file(
            notes_path,
            "# Notes: Legacy Paper\n\nPreserve this note.\n",
            tags=["custom"],
        )

        await services.start()

        summary = services.workspace.read_file(summary_path)
        notes = services.workspace.read_file(notes_path)
        assert summary.content == "# Legacy Paper\n\nPreserve this summary.\n"
        assert summary.paper_id == document_id
        assert summary.paper_name == "Legacy Paper"
        assert summary.tags == ("paper", f"paper:{document_id}", "summary")
        assert notes.content == "# Notes: Legacy Paper\n\nPreserve this note.\n"
        assert notes.tags == (
            "custom",
            "paper",
            f"paper:{document_id}",
            "notes",
        )
        assert {item.path for item in services.workspace.search(query="Preserve this")} == {
            notes_path,
            summary_path,
        }
    finally:
        await services.close()


def test_web_capability_removes_external_tools_from_both_chat_modes() -> None:
    for blueprint in (
        research_blueprint({}, web_enabled=False),
        deep_work_blueprint({}, web_enabled=False),
    ):
        assert {
            tool.catalog_id for tool in blueprint.tools
        }.isdisjoint({"research.sources.search", "research.sources.acquire"})
        assert all(
            "search-sources" not in agent.tool_ids
            and "acquire-source" not in agent.tool_ids
            for agent in blueprint.agents
        )


@pytest.mark.anyio
async def test_fast_answer_limits_searches_and_page_acquisition(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)

    async def search_web(query: str, limit: int):
        return {
            "query": query,
            "provider": "duckduckgo",
            "results": [
                {"title": "Allowed", "url": "https://example.com/allowed", "snippet": "Evidence"}
            ],
        }

    async def download_web_page(_arguments, _context):
        return {"source_id": "allowed-source"}

    try:
        runtime = services.runs._tool_runtime
        monkeypatch.setattr(services.research_search, "search_web", search_web)
        context = ScholarWeaveContext(
            run_id="fast-run",
            tool_runtime=runtime,
            metadata={"fast_answer": True, "web_search_limit": 1},
        )

        result = await runtime.invoke(
            "research.sources.search",
            {"provider": "web", "query": "bounded search"},
            context,
        )

        assert result["search_budget"] == {"used": 1, "limit": 1, "remaining": 0}
        with pytest.raises(ValueError, match="limit was reached"):
            await runtime.invoke(
                "research.sources.search",
                {"provider": "web", "query": "second search"},
                context,
            )
        with pytest.raises(ValueError, match="only permits web"):
            await runtime.invoke(
                "research.sources.search",
                {"provider": "arxiv", "query": "not permitted"},
                context,
            )
        with pytest.raises(ValueError, match="returned by its web search"):
            await runtime.invoke(
                "research.sources.acquire",
                {
                    "kind": "web_page",
                    "url": "https://example.com/not-in-results",
                    "title": None,
                },
                context,
            )
        monkeypatch.setattr(runtime, "_download_web_page", download_web_page)
        acquired = await runtime.invoke(
            "research.sources.acquire",
            {
                "kind": "web_page",
                "url": "https://example.com/allowed",
                "title": None,
            },
            context,
        )
        assert acquired == {"source_id": "allowed-source"}
        with pytest.raises(ValueError, match="after acquiring"):
            await runtime.invoke(
                "research.sources.search",
                {"provider": "web", "query": "after acquisition"},
                context,
            )
    finally:
        await services.close()


def test_work_plan_tracks_pending_items() -> None:
    context = ScholarWeaveContext(run_id="work-run", tool_runtime=Runtime())

    created = create_work_plan(
        {
            "items": [
                {"id": "sources", "title": "Collect sources"},
                {"id": "synthesis", "title": "Write synthesis"},
            ]
        },
        context,
    )
    assert [item["id"] for item in created["pending"]] == ["sources", "synthesis"]

    update_work_item(
        {
            "id": "sources",
            "status": "completed",
            "summary": "Collected two primary sources.",
        },
        context,
    )
    assert [item["id"] for item in work_plan(context)["pending"]] == ["synthesis"]

    finished = update_work_item(
        {
            "id": "synthesis",
            "status": "blocked",
            "summary": "The requested dataset is private.",
        },
        context,
    )
    assert finished["complete"] is True


@pytest.mark.anyio
async def test_unavailable_web_page_is_a_successful_research_result(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)

    async def unavailable(url: str):
        raise WebSourceUnavailable(url, "The remote page returned HTTP 403.")

    monkeypatch.setattr(services.source_downloads, "download_web_page", unavailable)
    try:
        runtime = services.runs._tool_runtime
        result = await runtime.invoke(
            "research.sources.acquire",
            {
                "kind": "web_page",
                "url": "https://example.com/blocked",
                "title": None,
            },
            ScholarWeaveContext(run_id="web-run", tool_runtime=runtime),
        )
    finally:
        await services.close()

    assert result["status"] == "unavailable"
    assert result["url"] == "https://example.com/blocked"
    assert result["reason"] == "The remote page returned HTTP 403."


@pytest.mark.anyio
async def test_research_source_tool_accepts_a_public_pdf(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    calls: list[tuple[str, str | None, bool]] = []
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="research.pdf",
        title="Research paper",
    )

    async def download_pdf(
        url: str,
        *,
        title: str | None = None,
        arxiv_only: bool = False,
    ):
        calls.append((url, title, arxiv_only))
        return document

    try:
        runtime = services.runs._tool_runtime
        monkeypatch.setattr(services.source_downloads, "download_pdf", download_pdf)
        result = await runtime.invoke(
            "research.sources.acquire",
            {
                "kind": "paper",
                "url": "https://papers.example/research.pdf",
                "title": "Research paper",
            },
            ScholarWeaveContext(run_id="paper-run", tool_runtime=runtime),
        )
        notes = services.workspace.read_file(result["notes_path"]).content
    finally:
        await services.close()

    assert result["document_id"] == document.id
    assert result["summary_path"] == f"{services.workspace.paper_folder(document.id)}/summary.md"
    assert result["notes_path"] == f"{services.workspace.paper_folder(document.id)}/notes.md"
    assert "https://papers.example/research.pdf" in notes
    assert calls == [
        ("https://papers.example/research.pdf", "Research paper", False),
    ]


@pytest.mark.anyio
async def test_paper_summary_updates_canonical_summary_only_through_save_tool(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="durable-summary.pdf",
        title="Durable summary",
    )
    services.documents.repository.mark_ready(document.id, page_count=1, metadata={})
    services.retrieval.replace_document_chunks(document.id, [{"text": "Complete source evidence."}])
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(
        run_id="summary-run",
        tool_runtime=runtime,
        metadata={
            "paper_activity": [
                {
                    "action": "read",
                    "document_id": document.id,
                    "title": document.title,
                }
            ]
        },
    )
    try:
        await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "chunks", "start": 0}, context,
        )
        saved = await runtime.invoke(
            "research.summary.save",
            {
                "document_id": document.id,
                "content": (
                    "# Summary\n\n"
                    "This deliberately uncited summary is long enough to represent substantive "
                    "content and verifies that saving no longer rejects a result based on a minimum "
                    "citation count. The prompt still asks the model for citations, but persistence "
                    "does not impose a separate citation-format gate on the completed summary."
                ),
                "review_summary": "Self-reviewed without a citation-count gate.",
            },
            context,
        )
        await runtime.invoke(
            "research.notes.save",
            {
                "target": "paper_notes",
                "mode": "append",
                "document_id": document.id,
                "path": None,
                "name": None,
                "content": "A durable specialist finding with [p.2].",
                "tags": ["paper"],
            },
            context,
        )
        canonical = services.workspace.read_file(saved["canonical_path"]).content
        notes = services.workspace.read_file(f"{services.workspace.paper_folder(document.id)}/notes.md").content
    finally:
        await services.close()

    assert saved["canonical_path"] == f"{services.workspace.paper_folder(document.id)}/summary.md"
    assert "deliberately uncited summary" in canonical
    assert saved["citation_count"] == 0
    assert "A durable specialist finding with [p.2]." in notes
    assert {
        item["action"] for item in context.metadata["paper_activity"]
    } >= {"read", "summary_saved", "notes_saved"}
    validate_paper_work_completion(context)
    assert not hasattr(runtime, "persist_run_output")


@pytest.mark.anyio
async def test_paper_summary_checkpoint_survives_run_cleanup_and_is_idempotent(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="large-paper.pdf",
        title="Large paper",
    )
    context = ScholarWeaveContext(
        run_id="large-summary-run",
        tool_runtime=services.runs._tool_runtime,
        metadata={
            "paper_activity": [
                {
                    "action": "read",
                    "document_id": document.id,
                    "title": document.title,
                }
            ]
        },
    )
    context.metadata["paper_activity"][0]["source_version"] = services.documents.source_revision(document.id)["source_version"]
    first_entry = "Pages 1-3: contribution evidence [p.1]. " * 10
    second_entry = "Pages 4-6: evaluation evidence [p.5]. " * 10
    try:
        first = await services.runs._tool_runtime.invoke(
            "research.summary.checkpoint",
            {
                "document_id": document.id,
                "action": "append",
                "content": first_entry,
                "offset": None,
                "limit": None,
            },
            context,
        )
        await services.runs._tool_runtime.invoke(
            "research.summary.checkpoint",
            {
                "document_id": document.id,
                "action": "append",
                "content": second_entry,
                "offset": None,
                "limit": None,
            },
            context,
        )
        page = await services.runs._tool_runtime.invoke(
            "research.summary.checkpoint",
            {
                "document_id": document.id,
                "action": "read",
                "content": None,
                "offset": 0,
                "limit": 256,
            },
            context,
        )
        checkpoint_path = test_settings.workspace_dir / first["checkpoint_path"]
        assert checkpoint_path.is_file()
        assert page["status"] == "available"
        assert page["has_more"] is True
        assert page["next_offset"] == 256
        assert first_entry[:80] in page["content"]

        services.documents.delete_run_artifacts(context.run_id)
        assert checkpoint_path.exists()
        resumed = ScholarWeaveContext(
            run_id="next-summary-run", tool_runtime=services.runs._tool_runtime,
        )
        reused = await services.runs._tool_runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "read", "offset": 0, "limit": 8000},
            resumed,
        )
        assert first_entry.strip() in reused["content"]
        await services.runs._tool_runtime._paper_summary_checkpoint(
            {"document_id": document.id, "action": "append", "content": first_entry},
            resumed,
        )
        records = services.workspace.read_file(first["checkpoint_path"]).content["records"]
        assert len(records) == 2
    finally:
        await services.close()


@pytest.mark.anyio
async def test_paper_summary_reader_continues_after_five_without_overlap(
    test_settings,
    monkeypatch,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    with services.session_factory() as session:
        session.add(Document(id="paper-1", title="Paper", source_filename="paper.pdf", content_type="application/pdf", status="ready"))
        session.commit()
    delegated_calls: list[dict[str, Any]] = []

    async def read_batch(arguments, _context):
        delegated_calls.append(arguments)
        start = int(arguments["start"])
        limit = 64
        return {
            "pages": [],
            "has_more": True,
            "next_page": start + limit,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    context = ScholarWeaveContext(run_id="bounded-summary-run", tool_runtime=runtime)
    try:
        results = []
        starts = [1, 65, 129, 193, 257, 321]
        for start in starts:
            results.append(
                await runtime._read_paper_summary_batch(
                    {
                        "document_id": "paper-1",
                        "action": "pages",
                        "start": start,
                    },
                    context,
                )
            )
            runtime._paper_summary_state(context, "paper-1").pop(
                "pending_checkpoint",
                None,
            )
    finally:
        await services.close()

    assert [result["summary_batch"] for result in results] == [1, 2, 3, 4, 5, 6]
    assert [call["limit"] for call in delegated_calls] == [None] * 6
    assert [result["next_start"] for result in results] == [65, 129, 193, 257, 321, 385]
    assert len(delegated_calls) == 6


@pytest.mark.anyio
async def test_paper_summary_reader_requires_checkpoint_before_next_batch(
    test_settings,
    monkeypatch,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    with services.session_factory() as session:
        session.add(Document(id="paper-1", title="Paper", source_filename="paper.pdf", content_type="application/pdf", status="ready"))
        session.commit()

    async def read_batch(arguments, _context):
        start = int(arguments["start"])
        return {
            "pages": [
                {
                    "page_number": page,
                    "citation": f"p.{page}",
                    "text": f"page {page}",
                }
                for page in range(start, start + 10)
            ],
            "has_more": True,
            "next_page": start + 10,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    context = ScholarWeaveContext(run_id="gated-summary-run", tool_runtime=runtime)
    try:
        first = await runtime._read_paper_summary_batch(
            {"document_id": "paper-1", "action": "pages", "start": 1},
            context,
        )
        with pytest.raises(ValueError, match="must be appended"):
            await runtime._read_paper_summary_batch(
                {"document_id": "paper-1", "action": "pages", "start": 11},
                context,
            )
    finally:
        await services.close()

    assert first["checkpoint_required"] is True
    assert first["coverage"]["start"] == 1
    assert first["coverage"]["end"] == 10
    assert len(first["coverage"]["spans"]) == 10
    assert first["next_start"] == 11
    assert first["checkpoint_path"].startswith(f"{services.workspace.paper_folder('paper-1')}/evidence/")


@pytest.mark.anyio
@pytest.mark.parametrize("evidence", [
    "Contribution and evidence [p.1].",
    "Contribution and evidence [p.1]. " * 1500,
], ids=["short", "large"])
async def test_final_paper_summary_checkpoint_is_returned_without_reread(
    test_settings,
    monkeypatch,
    evidence,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="short-paper.pdf",
        title="Short paper",
    )
    runtime = services.runs._tool_runtime
    context = ScholarWeaveContext(run_id="final-checkpoint-run", tool_runtime=runtime)

    async def read_batch(arguments, active_context):
        runtime._record_paper_activity(
            active_context,
            document,
            "read",
        )
        return {
            "pages": [
                {
                    "page_number": 1,
                    "citation": "p.1",
                    "text": "paper evidence",
                }
            ],
            "has_more": False,
            "next_page": 2,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    try:
        await runtime._read_paper_summary_batch(
            {"document_id": document.id, "action": "pages", "start": 1},
            context,
        )
        appended = await runtime._paper_summary_checkpoint(
            {
                "document_id": document.id,
                "action": "append",
                "content": evidence,
                "offset": None,
                "limit": None,
            },
            context,
        )
        tool = create_tool_catalog().build_function_tool(
            FunctionToolSpec(id="checkpoint", catalog_id="research.summary.checkpoint"),
        )
        for limit in (None, 20_000):
            page = await tool.on_invoke_tool(
                SimpleNamespace(context=context, tool_call_id="checkpoint"),
                json.dumps({
                    "document_id": document.id, "action": "read", "content": None,
                    "offset": 0, "limit": limit,
                }),
            )
            checkpoint = evidence.strip() + "\n"
            assert page["content"] == checkpoint[:limit]
            assert page["has_more"] == (limit is not None and len(checkpoint) > limit)
            assert page["next_offset"] == (limit if page["has_more"] else None)
    finally:
        await services.close()

    assert appended["has_more_paper"] is False
    assert appended["final_checkpoint"] == evidence.strip() + "\n"
    assert "without rereading" in appended["instruction"]


@pytest.mark.anyio
async def test_parallel_paper_summary_batch_reads_leave_one_pending_batch(
    test_settings,
    monkeypatch,
) -> None:
    services = create_services(test_settings)
    runtime = services.runs._tool_runtime
    with services.session_factory() as session:
        session.add(Document(id="paper-1", title="Paper", source_filename="paper.pdf", content_type="application/pdf", status="ready"))
        session.commit()

    async def read_batch(arguments, _context):
        await asyncio.sleep(0)
        start = int(arguments["start"])
        return {
            "pages": [{"page_number": start, "text": "page"}],
            "has_more": True,
            "next_page": start + 10,
        }

    monkeypatch.setattr(runtime, "_read_research_paper", read_batch)
    context = ScholarWeaveContext(run_id="parallel-summary-run", tool_runtime=runtime)
    try:
        results = await asyncio.gather(
            runtime._read_paper_summary_batch(
                {"document_id": "paper-1", "action": "pages", "start": 1},
                context,
            ),
            runtime._read_paper_summary_batch(
                {"document_id": "paper-1", "action": "pages", "start": 11},
                context,
            ),
            return_exceptions=True,
        )
    finally:
        await services.close()

    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert "must be appended" in str(errors[0])


@pytest.mark.anyio
async def test_reading_substantive_canonical_summary_reuses_it(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="existing-summary.pdf",
        title="Existing summary",
    )
    paper = services.workspace.ensure_paper_folder(document.id, document.title)
    substantive = (
        "# Existing summary\n\n"
        "## Contribution\n\n"
        "The paper introduces a durable contribution supported by primary evidence. "
        "It describes the mechanism, assumptions, and intended operating conditions in enough "
        "detail to distinguish this reviewed summary from the generated placeholder. "
        "The evaluation reports another result and its limitations without requiring a fixed "
        "number of specially formatted citations for this existing summary to remain reusable."
    )
    services.workspace.write_file(
        paper["summary_path"],
        substantive,
        tags=["paper", f"paper:{document.id}", "summary"],
    )
    context = ScholarWeaveContext(
        run_id="reuse-summary-run",
        tool_runtime=services.runs._tool_runtime,
    )
    try:
        result = await services.runs._tool_runtime.invoke(
            "research.notes.read",
            {"path": paper["summary_path"]},
            context,
        )
    finally:
        await services.close()

    assert result["summary_check"]["status"] == "reusable"
    assert result["summary_check"]["needs_regeneration"] is False
    assert context.metadata["paper_activity"][-1]["action"] == "summary_reused"
    context.metadata["paper_activity"].extend(
        [
            {
                "action": "read",
                "document_id": document.id,
                "title": document.title,
            },
            {
                "action": "notes_saved",
                "document_id": document.id,
                "title": document.title,
            },
        ]
    )
    validate_paper_work_completion(context)


@pytest.mark.anyio
async def test_reading_summary_template_requires_regeneration(test_settings) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="summary-template.pdf",
        title="Summary template",
    )
    paper = services.workspace.ensure_paper_folder(document.id, document.title)
    context = ScholarWeaveContext(
        run_id="incomplete-summary-run",
        tool_runtime=services.runs._tool_runtime,
    )
    try:
        result = await services.runs._tool_runtime.invoke(
            "research.notes.read",
            {"path": paper["summary_path"]},
            context,
        )
    finally:
        await services.close()

    assert result["summary_check"]["status"] == "incomplete"
    assert result["summary_check"]["needs_regeneration"] is True
    assert "paper_activity" not in context.metadata


def test_chat_agent_can_use_persistence_tools_directly() -> None:
    blueprint = research_blueprint({})
    researcher_tools = set(blueprint.agents[0].tool_ids)

    assert {
        "save-note",
        "summarize-paper",
    }.issubset(researcher_tools)
    assert not {"summary-read", "summary-checkpoint", "save-summary"} & researcher_tools
    assert blueprint.agent_tools == []


def test_paper_completion_gate_is_activated_by_model_selected_activity() -> None:
    validate_paper_work_completion(
        ScholarWeaveContext(
            run_id="non-paper-run",
            tool_runtime=Runtime(),
        )
    )

    context = ScholarWeaveContext(
        run_id="paper-run",
        tool_runtime=Runtime(),
        metadata={
            "paper_activity": [
                {
                    "action": "read",
                    "document_id": "paper-1",
                    "title": "Required Paper",
                },
                {
                    "action": "summary_saved",
                    "document_id": "paper-1",
                    "title": "Required Paper",
                },
            ],
        },
    )

    with pytest.raises(ValidationError) as raised:
        validate_paper_work_completion(context)

    assert raised.value.issues == (
        "Required Paper (paper-1): populate notes.md through save_research_note",
    )
    context.metadata["paper_activity"].append(
        {
            "action": "notes_saved",
            "document_id": "paper-1",
            "title": "Required Paper",
        }
    )
    validate_paper_work_completion(context)


@pytest.mark.anyio
async def test_paper_persistence_rejects_writes_before_extracted_content_is_read(
    test_settings,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF",
        filename="unread.pdf",
        title="Unread Paper",
    )
    context = ScholarWeaveContext(
        run_id="unread-run",
        tool_runtime=services.runs._tool_runtime,
    )
    try:
        with pytest.raises(ValueError, match="after extracted pages or chunks"):
            await services.runs._tool_runtime.invoke(
                "research.notes.save",
                {
                    "target": "paper_notes",
                    "mode": "append",
                    "document_id": document.id,
                    "path": None,
                    "name": None,
                    "content": "Unsupported note.",
                    "tags": [],
                },
                context,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_research_note_tools_create_search_read_and_append(test_settings) -> None:
    services = create_services(test_settings)
    try:
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="note-run", tool_runtime=runtime)
        created = await runtime.invoke(
            "research.notes.save",
            {
                "target": "new_note",
                "mode": "overwrite",
                "document_id": None,
                "path": None,
                "name": "Attention ideas",
                "content": "Test grouped-query attention.",
                "tags": ["attention"],
            },
            context,
        )
        found = await runtime.invoke(
            "research.notes.search",
            {
                "query": "grouped-query",
                "kinds": ["note"],
                "tags": ["attention"],
                "limit": 10,
            },
            context,
        )
        await runtime.invoke(
            "research.notes.save",
            {
                "target": "path",
                "mode": "append",
                "document_id": None,
                "path": created["path"],
                "name": None,
                "content": "Compare paged KV caches.",
                "tags": ["attention", "systems"],
            },
            context,
        )
        read = await runtime.invoke(
            "research.notes.read",
            {"path": created["path"]},
            context,
        )

        assert found[0]["path"] == created["path"]
        assert "Compare paged KV caches." in read["content"]
        assert read["tags"] == ["attention", "systems"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_research_library_reports_unprepared_paper(test_settings) -> None:
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
        context = ScholarWeaveContext(run_id="paper-run", tool_runtime=runtime)

        listed = await runtime.invoke(
            "research.library.search",
            {"query": None, "document_id": None, "limit": 10},
            context,
        )
        inspected = await runtime.invoke(
            "research.library.search",
            {"query": None, "document_id": document_id, "limit": 10},
            context,
        )
        title_matches = await runtime.invoke(
            "research.library.search",
            {"query": "Metadata Only", "document_id": None, "limit": 10},
            context,
        )
        filename_matches = await runtime.invoke(
            "research.library.search",
            {"query": "paper.pdf", "document_id": None, "limit": 10},
            context,
        )
        literal_null_matches = await runtime.invoke(
            "research.library.search",
            {"query": "Metadata Only", "document_id": "null", "limit": 10},
            context,
        )
        literal_null_list = await runtime.invoke(
            "research.library.search",
            {"query": "", "document_id": "null", "limit": 10},
            context,
        )
        literal_null_query = await runtime.invoke(
            "research.library.search",
            {"query": "null", "document_id": None, "limit": 10},
            context,
        )
        missing = await runtime.invoke(
            "research.library.search",
            {"query": None, "document_id": "missing-paper", "limit": 10},
            context,
        )

        assert listed[0]["readable"] is False
        assert inspected["source_available"] is True
        assert inspected["readable"] is False
        assert title_matches[0]["document_id"] == document_id
        assert title_matches[0]["matched_fields"] == ["title"]
        assert filename_matches[0]["document_id"] == document_id
        assert filename_matches[0]["matched_fields"] == ["source_filename"]
        assert literal_null_matches[0]["document_id"] == document_id
        assert literal_null_list[0]["document_id"] == document_id
        assert literal_null_query == []
        assert missing["found"] is False
        assert missing["requested_document_id"] == "missing-paper"
        with pytest.raises(ValueError, match="Inspect and ingest"):
            await runtime.invoke(
                "research.paper.read",
                {
                    "document_id": document_id,
                    "action": "chunks",
                    "start": 0,
                    "limit": 10,
                },
                context,
            )
    finally:
        await services.close()


@pytest.mark.anyio
async def test_research_library_ranks_metadata_words_and_can_ignore_results(
    test_settings,
) -> None:
    services = create_services(test_settings)
    try:
        documents = [
            ("doc-both", "Neural Attention Systems"),
            ("doc-neural", "Neural Retrieval"),
            ("doc-attention", "Efficient Attention"),
            ("doc-partial", "Attentional Models"),
            ("doc-author", "Database Indexes"),
        ]
        with services.session_factory() as session:
            session.add_all(
                Document(
                    id=document_id,
                    title=title,
                    source_filename=f"{document_id}.pdf",
                    content_type="application/pdf",
                    status="ready",
                    metadata_json=(
                        {"authors": ["Ada Lovelace"]}
                        if document_id == "doc-author"
                        else {}
                    ),
                )
                for document_id, title in documents
            )
            session.commit()

        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="ranked-library-search", tool_runtime=runtime)
        first = await runtime.invoke(
            "research.library.search",
            {
                "query": "the neural and attention",
                "document_id": None,
                "ignore_document_ids": None,
                "limit": 3,
            },
            context,
        )
        second = await runtime.invoke(
            "research.library.search",
            {
                "query": "the neural and attention",
                "document_id": None,
                "ignore_document_ids": [item["document_id"] for item in first],
                "limit": 3,
            },
            context,
        )

        assert [item["document_id"] for item in first] == [
            "doc-both",
            "doc-attention",
            "doc-neural",
        ]
        assert first[0]["matched_words"] == ["neural", "attention"]
        assert first[1]["matched_words"] == ["attention"]
        assert second == []
        author = await runtime.invoke(
            "research.library.search",
            {
                "query": "a paper by lovelace",
                "document_id": None,
                "ignore_document_ids": None,
                "limit": 3,
            },
            context,
        )
        assert [item["document_id"] for item in author] == ["doc-author"]
        assert author[0]["matched_fields"] == ["authors"]
        assert author[0]["authors"] == ["Ada Lovelace"]
    finally:
        await services.close()


@pytest.mark.anyio
async def test_research_library_organization_tool_creates_and_assigns_folders(
    test_settings,
) -> None:
    services = create_services(test_settings)
    try:
        document = services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF",
            filename="organized.pdf",
            title="Organized Paper",
        )
        runtime = services.runs._tool_runtime
        context = ScholarWeaveContext(run_id="organize-library", tool_runtime=runtime)

        created = await runtime.invoke(
            "research.library.organize",
            {
                "action": "create_folder",
                "folder_name": "Methods",
                "document_id": None,
                "folder_id": None,
            },
            context,
        )
        folder_id = created["folder"]["id"]
        moved = await runtime.invoke(
            "research.library.organize",
            {
                "action": "move_paper",
                "folder_name": None,
                "document_id": document.id,
                "folder_id": folder_id,
            },
            context,
        )
        listing = await runtime.invoke(
            "research.library.organize",
            {
                "action": "list",
                "folder_name": None,
                "document_id": None,
                "folder_id": None,
            },
            context,
        )

        assert moved["paper"]["folder_id"] == folder_id
        assert listing["folders"] == [{"id": folder_id, "name": "Methods"}]
        assert listing["papers"] == [
            {
                "document_id": document.id,
                "title": "Organized Paper",
                "folder_id": folder_id,
            }
        ]
    finally:
        await services.close()
