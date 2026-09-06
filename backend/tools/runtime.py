from __future__ import annotations

import asyncio
import inspect
import json
import hashlib
import threading
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from stop_words import get_stop_words

from backend.core.errors import NotFoundError
from backend.core.config import Settings
from backend.conversations.service import ConversationService
from backend.documents import DocumentService
from backend.documents.formatting import manifest_pages
from backend.documents.retrieval import RetrievalService
from backend.agents.context import ScholarWeaveContext, ToolReceipt
from backend.agents.harness import current_tool_model_identity
from backend.runs.repository import LeaseOwnershipError, RunRepository
from backend.utils import to_jsonable
from backend.persistence.files import SafeStorage, StorageError
from backend.prompting.registry import PromptRegistry
from backend.research.search import ResearchSearchService
from backend.research.sources import SourceDownloadService, WebSourceUnavailable
from backend.tools.catalog import APPLICATION_TOOL_HANDLERS
from backend.tools.failures import classify_tool_error
from backend.tools.policy import ToolInputError, operation_policy, retry_delay
from backend.workspace.service import WorkspaceDocument, WorkspaceService


_WEB_SEARCH_USAGE_KEY = "web_search_requests_used"
_WEB_SEARCH_CACHE_KEY = "web_search_results"
_WEB_SEARCH_ATTEMPTS_KEY = "web_search_attempted_queries"
PLAN_KEY = "work_plan"
_NULL_TOOL_LITERALS = {"null", "none"}
_SEARCH_STOP_WORDS = frozenset(get_stop_words("en"))
_PAPER_CITATION_PATTERN = re.compile(
    r"\[(?:p{1,2}\.?\s*\d+(?:\s*[-–]\s*\d+)?|chunk[^\]]*)\]",
    flags=re.IGNORECASE,
)


def _paper_summary_citations(content: str) -> set[str]:
    return {match.group(0) for match in _PAPER_CITATION_PATTERN.finditer(content)}


def _paper_model_provenance(context: ScholarWeaveContext) -> dict[str, Any] | None:
    task_model = current_tool_model_identity()
    dedicated = context.metadata.get("paper_summary_model")
    if task_model is not None:
        if isinstance(dedicated, dict) and all(
            dedicated.get(key) == task_model.get(key) for key in ("model", "provider_kind")
        ):
            return {**dedicated, **task_model}
        return dict(task_model)
    model = dedicated or context.metadata.get("active_model")
    return dict(model) if isinstance(model, dict) else None


def _nullable_tool_string(
    value: Any,
    parameter_name: str,
    *,
    coerce_null_literal: bool = False,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{parameter_name} must be a string or null.")
    normalized = value.strip()
    if not normalized or (
        coerce_null_literal and normalized.casefold() in _NULL_TOOL_LITERALS
    ):
        return None
    return normalized


def _tool_string_list(value: Any, parameter_name: str) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ValueError(f"{parameter_name} must be an array of strings or null.")
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{parameter_name} must contain only non-empty strings.")
        normalized.add(item.strip())
    return normalized


def _normalize_result_ref(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("result_ref must be a relative path string.")
    raw = value.strip()
    if (
        not raw
        or "\x00" in raw
        or raw.startswith(("/", "\\"))
        or re.match(r"^[a-zA-Z]:", raw)
    ):
        raise ValueError("result_ref must be a safe relative cache path.")
    normalized = re.sub(r"[\\/]+", "/", raw)
    parts = normalized.split("/")
    if (
        len(parts) < 3
        or parts[0] not in {"runs", "conversations"}
        or any(
            part in {"", ".", ".."} or ":" in part or part.endswith((".", " "))
            for part in parts
        )
    ):
        raise ValueError("result_ref must be a safe relative cache path.")
    return PurePosixPath(*parts).as_posix()


def _persistable_tool_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return to_jsonable(
        {
            key: value
            for key, value in metadata.items()
            if key not in {"_steering_inbox", "active_epoch_id", "epoch_index", "goal_state"}
        }
    )


def _document_metadata_matches(
    document: Any,
    query_tokens: list[str],
) -> tuple[list[str], list[str]]:
    metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
    fields = {
        "title": document.title,
        "source_filename": document.source_filename,
        "authors": " ".join(_document_authors(metadata)),
    }
    matched_fields: list[str] = []
    matched_words: set[str] = set()
    for name, value in fields.items():
        if not isinstance(value, str):
            continue
        field_matches = set(query_tokens) & set(_search_words(value))
        if field_matches:
            matched_fields.append(name)
            matched_words.update(field_matches)
    return matched_fields, [token for token in query_tokens if token in matched_words]


def _document_authors(metadata: dict[str, Any]) -> list[str]:
    raw_authors = metadata.get("authors", metadata.get("author"))
    if isinstance(raw_authors, str):
        return [raw_authors]
    if not isinstance(raw_authors, list):
        return []
    authors: list[str] = []
    for raw_author in raw_authors:
        if isinstance(raw_author, str):
            name = raw_author
        elif isinstance(raw_author, dict):
            name = str(raw_author.get("name") or "").strip()
            if not name:
                name = " ".join(
                    str(raw_author.get(part) or "").strip()
                    for part in ("given", "family")
                ).strip()
        else:
            continue
        if name.strip():
            authors.append(name.strip())
    return authors


def _search_key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _search_words(value: str) -> list[str]:
    return [
        token
        for token in _search_key(value).split()
        if token not in _SEARCH_STOP_WORDS
    ]


def create_work_plan(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    """Create the run's immutable set of work-item identities and initial states."""
    if context.metadata.get(PLAN_KEY):
        raise ValueError("This run already has a work plan.")
    raw_items = arguments.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 10:
        raise ValueError("A work plan must contain between 1 and 10 items.")

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Each work item must be an object.")
        item_id = str(raw_item.get("id") or "").strip()
        title = str(raw_item.get("title") or "").strip()
        if not item_id or not title:
            raise ValueError("Each work item requires a non-empty id and title.")
        if item_id in seen:
            raise ValueError(f"Duplicate work item id '{item_id}'.")
        seen.add(item_id)
        items.append(
            {
                "id": item_id,
                "title": title,
                "status": "pending",
                "summary": "",
            }
        )
    context.metadata[PLAN_KEY] = items
    return work_plan(context)


def update_work_item(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    """Update one work item's state while enforcing terminal-state summaries."""
    items = _work_items(context)
    item_id = str(arguments.get("id") or "").strip()
    status = str(arguments.get("status") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    if status not in {"in_progress", "completed", "blocked"}:
        raise ValueError("Work item status must be in_progress, completed, or blocked.")
    item = next((candidate for candidate in items if candidate["id"] == item_id), None)
    if item is None:
        raise ValueError(f"Unknown work item '{item_id}'.")
    if status in {"completed", "blocked"} and not summary:
        raise ValueError("Completed or blocked work items require a summary.")
    item["status"] = status
    item["summary"] = summary
    return work_plan(context)


def work_plan(context: ScholarWeaveContext) -> dict[str, Any]:
    """Return a detached snapshot of all work items and the unfinished subset."""
    items = _work_items(context)
    pending = [
        dict(item)
        for item in items
        if item["status"] not in {"completed", "blocked"}
    ]
    return {
        "items": [dict(item) for item in items],
        "pending": pending,
        "complete": not pending,
    }


def _work_items(context: ScholarWeaveContext) -> list[dict[str, str]]:
    items = context.metadata.get(PLAN_KEY)
    if not isinstance(items, list) or not items:
        raise ValueError("Create a work plan before reading or updating it.")
    return items


class ApplicationToolRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        documents: DocumentService,
        retrieval: RetrievalService,
        storage: SafeStorage,
        workspace: WorkspaceService,
        research_search: ResearchSearchService,
        source_downloads: SourceDownloadService,
        conversations: ConversationService,
        prompts: PromptRegistry | None = None,
        run_repository: RunRepository | None = None,
    ) -> None:
        self._settings = settings
        self._documents = documents
        self._retrieval = retrieval
        self._storage = storage
        self._workspace = workspace
        self._research_search = research_search
        self._source_downloads = source_downloads
        self._conversations = conversations
        self._prompts = prompts
        self._runs = run_repository
        self._paper_summary_lock = asyncio.Lock()
        self._summary_save_lock = threading.RLock()
        self._mutation_lock = asyncio.Lock()
        self._cache_lock = threading.RLock()
        self._summary_runner = None

    def set_summary_runner(self, runner: Any) -> None:
        self._summary_runner = runner

    async def _summarize_research_paper(
        self, arguments: dict[str, Any], context: ScholarWeaveContext,
    ) -> Any:
        if self._summary_runner is None:
            raise RuntimeError("The dedicated summary writer is unavailable.")
        return await self._summary_runner(arguments, context)

    async def invoke(
        self,
        catalog_id: str,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
        *,
        tool_call_id: str | None = None,
    ) -> Any:
        """Dispatch one catalog tool and record its full result and activity metadata."""
        handler_name = APPLICATION_TOOL_HANDLERS.get(catalog_id)
        if handler_name is None:
            raise ValueError(f"Unknown application tool '{catalog_id}'.")
        handler = getattr(self, handler_name)
        journal = (
            self._runs
            if self._runs is not None and self._runs.exists(context.run_id)
            else None
        )
        policy = operation_policy(catalog_id, arguments)
        attempts = self._settings.tool_read_retry_attempts if policy.safe_retry else 1
        provider_call_id = tool_call_id
        call_id = f"invoke-{uuid.uuid4()}"
        for attempt_number in range(1, attempts + 1):
            if journal is not None and journal.cancel_requested(context.run_id):
                raise asyncio.CancelledError
            lease_getter = getattr(context.event_sink, "current_lease", None)
            lease = lease_getter() if lease_getter is not None else None
            if journal is not None and lease_getter is not None and lease is None:
                raise LeaseOwnershipError(
                    f"Run {context.run_id} has no active lease for tool journaling."
                )
            attempt = (
                (
                    journal.begin_tool_attempt_owned(
                        lease,
                        epoch_id=_active_epoch_id(context),
                        tool_call_id=call_id,
                        catalog_id=catalog_id,
                        attempt=attempt_number,
                        arguments=arguments,
                    )
                    if lease is not None
                    else journal.begin_tool_attempt(
                        run_id=context.run_id,
                        epoch_id=_active_epoch_id(context),
                        tool_call_id=call_id,
                        catalog_id=catalog_id,
                        attempt=attempt_number,
                        arguments=arguments,
                    )
                )
                if journal is not None
                else None
            )
            await context.emit(
                "tool.attempt.started",
                {
                    "catalog_id": catalog_id,
                    "tool_call_id": call_id,
                    "provider_call_id": provider_call_id,
                    "attempt": attempt_number,
                },
            )
            dispatched = False
            mutation_locked = False
            try:
                try:
                    if policy.mutating:
                        await self._mutation_lock.acquire()
                        mutation_locked = True
                    dispatched = True
                    result = await self._dispatch_tool_handler(
                        handler, arguments, context,
                        mutating=policy.mutating,
                    )
                finally:
                    if mutation_locked:
                        self._mutation_lock.release()
                if attempt is not None:
                    self._finish_tool_attempt(
                        journal,
                        context,
                        attempt.id,
                        status="completed",
                        result=result,
                        result_ref=(
                            result.get("result_ref")
                            if isinstance(result, dict)
                            else None
                        ),
                    )
                await context.emit(
                    "tool.attempt.completed",
                    {
                        "catalog_id": catalog_id,
                        "tool_call_id": call_id,
                        "provider_call_id": provider_call_id,
                        "attempt": attempt_number,
                    },
                )
                await context.emit("tool.application_completed", {"catalog_id": catalog_id})
                return result

            except asyncio.CancelledError:
                if attempt is not None:
                    unknown = policy.mutating and dispatched
                    status = "unknown_outcome" if unknown else "cancelled"
                    self._finish_tool_attempt(
                        journal,
                        context,
                        attempt.id,
                        status=status,
                        failure_category=(
                            "cancelled_after_dispatch" if unknown else "cancelled"
                        ),
                        error=(
                            None
                            if not unknown
                            else "Cancellation occurred after a write was dispatched."
                        ),
                    )
                raise
            except Exception as error:
                category, transient = classify_tool_error(error)
                delay = retry_delay(error, attempt_number) if policy.safe_retry and transient else 0.0
                retryable = (
                    policy.safe_retry and transient and attempt_number < attempts
                )
                unknown = policy.mutating and dispatched and not isinstance(error, ToolInputError)
                status = "unknown_outcome" if unknown else "failed"
                if attempt is not None:
                    self._finish_tool_attempt(
                        journal,
                        context,
                        attempt.id,
                        status=status,
                        failure_category=category,
                        retryable=retryable,
                        error=f"{type(error).__name__}: {error}",
                    )
                await context.emit(
                    "tool.attempt.failed",
                    {
                        "catalog_id": catalog_id,
                        "tool_call_id": call_id,
                        "provider_call_id": provider_call_id,
                        "attempt": attempt_number,
                        "category": category,
                        "retryable": retryable,
                        "outcome": status,
                    },
                )
                if retryable:
                    await asyncio.sleep(delay)
                    continue
                raise
        raise RuntimeError("Tool invocation ended without a result.")

    @staticmethod
    async def _dispatch_tool_handler(
        handler: Any,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
        *,
        mutating: bool,
    ) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(arguments, context)
        worker = asyncio.create_task(asyncio.to_thread(handler, arguments, context))
        try:
            result = await (asyncio.shield(worker) if mutating else worker)
        except asyncio.CancelledError:
            if mutating:
                # Cancellation cannot stop a Python thread. Keep the mutation lock until
                # the dispatched write finishes, even under repeated run cancellation.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled() and worker.exception() is None:
                    late_result = worker.result()
                    if inspect.iscoroutine(late_result):
                        late_result.close()
                    elif isinstance(late_result, asyncio.Future):
                        late_result.cancel()
            raise
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _finish_tool_attempt(
        journal: RunRepository,
        context: ScholarWeaveContext,
        attempt_id: str,
        **values: Any,
    ) -> None:
        lease_getter = getattr(context.event_sink, "current_lease", None)
        lease = lease_getter() if lease_getter is not None else None
        if lease_getter is not None and lease is None:
            raise LeaseOwnershipError(
                f"Run {context.run_id} has no active lease for tool journaling."
            )
        if lease is not None:
            journal.finish_tool_attempt_owned(lease, attempt_id, **values)
        else:
            journal.finish_tool_attempt(attempt_id, **values)

    def _set_conversation_title(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, str]:
        if not context.metadata.get("allow_conversation_title_update"):
            raise ValueError("Conversation titles can only be set during the first turn.")
        if context.metadata.get("conversation_title_set"):
            raise ValueError("The conversation title has already been set.")
        if context.conversation_id is None:
            raise ValueError("A conversation is required to set its title.")

        record = self._conversations.set_title(
            context.conversation_id,
            str(arguments["title"]),
        )
        context.metadata["conversation_title_set"] = True
        return {"conversation_id": record.id, "title": record.title}

    @staticmethod
    def _create_work_plan(
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        return create_work_plan(arguments, context)

    @staticmethod
    def _update_work_item(
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        return update_work_item(arguments, context)

    @staticmethod
    def _read_work_plan(
        _arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        return work_plan(context)

    def _save_paper_summary_version(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        with self._summary_save_lock:
            return self._save_paper_summary_version_locked(arguments, context)

    def _save_paper_summary_version_locked(
        self, arguments: dict[str, Any], context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"]).strip()
        content = str(arguments["content"]).strip()
        review_summary = str(arguments["review_summary"]).strip()
        mode = context.metadata.get("paper_summary_mode", "reviewed")
        citations = _paper_summary_citations(content)
        if mode == "overview" and not citations:
            raise ToolInputError(
                "Include at least one source citation in brackets, such as [p.1] or [chunk 0], "
                "using a page or chunk supplied in the paper evidence."
            )
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        self._require_paper_read(context, document_id)
        state = self._paper_summary_state(context, document_id)
        if state.get("pending_checkpoint"):
            self._append_summary_evidence(context, document_id, content)
        evidence = self._load_summary_evidence(state)
        complete = bool(evidence.get("complete"))
        reviewed = complete and mode == "reviewed"
        if not complete:
            content = (
                "> Partial summary: full source coverage was not completed. "
                "Unread material may change these conclusions.\n\n" + content
            )
        paper = self._workspace.ensure_paper_folder(document.id, document.title)
        revision = self._prompts.revision if self._prompts is not None else None
        created_at = datetime.now(timezone.utc).isoformat()
        version_id = context.run_id
        path = f"{paper['folder']}/summaries/{version_id}.md"
        metadata_path = f"{paper['folder']}/summaries/{version_id}.json"
        try:
            previous = self._workspace.read_file(path)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if previous.content != content + "\n":
                raise ValueError("This run already saved a different immutable summary version.")
            try:
                existing_metadata = self._workspace.read_file(metadata_path).content
            except FileNotFoundError:
                existing_metadata = None
            if isinstance(existing_metadata, dict):
                self._record_paper_activity(
                    context, document, "summary_saved",
                    path=existing_metadata.get("canonical_path"), version_path=path,
                    coverage_complete=existing_metadata.get("coverage_complete", False),
                    review_complete=existing_metadata.get("review_complete", existing_metadata.get("coverage_complete", False)),
                )
                return existing_metadata
        metadata = {
            "id": version_id,
            "document_id": document_id,
            "run_id": context.run_id,
            "path": path,
            "created_at": created_at,
            "prompt_revision": revision,
            "review_summary": review_summary,
            "citation_count": len(citations),
            "status": "overview" if mode == "overview" else ("reviewed" if complete else "partial"),
            "mode": mode,
            "review_complete": reviewed,
            "coverage_complete": complete,
            "coverage": {
                "kind": evidence.get("action"),
                "checkpointed_batches": sum(bool(record.get("coverage")) for record in evidence["records"]),
                "exact_spans_path": state["checkpoint_path"],
            },
            "next_start": evidence.get("next_start"),
            "next_offset": evidence.get("next_offset", 0),
            "evidence_path": state["checkpoint_path"],
            "model": _paper_model_provenance(context),
            "content_hash": hashlib.sha256((content + "\n").encode()).hexdigest(),
            **{key: state[key] for key in ("source_hash", "extraction_hash", "source_version")},
        }
        saved = self._workspace.write_file(
            path,
            content + "\n",
            tags=["paper", f"paper:{document_id}", "summary-version"],
        )
        canonical = self._workspace.read_file(str(paper["summary_path"]))
        original_hash = context.metadata.get("paper_summary_canonical_hash")
        canonical_updated = mode != "overview" and (original_hash is None or (
            hashlib.sha256(str(canonical.content).encode()).hexdigest() == original_hash
        ))
        if canonical_updated:
            canonical = self._workspace.write_file(
                str(paper["summary_path"]), content + "\n",
                tags=["paper", f"paper:{document_id}", "summary"],
            )
        metadata["canonical_path"] = canonical.path
        metadata["canonical_updated"] = canonical_updated
        self._workspace.write_file(
            metadata_path,
            metadata,
            tags=["paper", f"paper:{document_id}", "summary-version-metadata"],
        )
        if canonical_updated:
            self._workspace.write_file(
                f"{paper['folder']}/summary.provenance.json", metadata,
                tags=["paper", f"paper:{document_id}", "summary-provenance"],
            )
        self._append_workspace_receipt(context, saved, "Created")
        if canonical_updated:
            self._append_workspace_receipt(context, canonical, "Updated")
        self._record_paper_activity(
            context,
            document,
            "summary_saved",
            path=canonical.path,
            version_path=saved.path,
            coverage_complete=complete,
            review_complete=reviewed,
        )
        return metadata

    async def _paper_summary_checkpoint(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"]).strip()
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        action = str(arguments["action"])
        async with self._paper_summary_lock:
            state = self._paper_summary_state(context, document_id)
            relative_path = state["checkpoint_path"]
            if action == "append":
                self._require_paper_read(context, document_id)
                content = _nullable_tool_string(arguments.get("content"), "content")
                if content is None:
                    raise ValueError("content is required when appending a summary checkpoint.")
                pending = state.get("pending_checkpoint")
                evidence = self._append_summary_evidence(context, document_id, content)
                checkpoint = self._evidence_text(evidence)
                final_checkpoint = checkpoint if evidence.get("complete") else None
                return {
                    "status": "reconciled" if state.get("last_append_reconciled") else "appended",
                    "document_id": document_id,
                    "batch_id": state.get("last_record_id"),
                    "source_version": state["source_version"],
                    "existing_evidence_retained": bool(state.get("last_append_reconciled")),
                    "checkpoint_path": relative_path,
                    "size_characters": len(checkpoint),
                    "checkpointed_batch": (
                        pending.get("batch") if isinstance(pending, dict) else None
                    ),
                    "coverage": (
                        pending.get("coverage") if isinstance(pending, dict) else None
                    ),
                    "has_more_paper": (
                        pending.get("has_more") if isinstance(pending, dict) else None
                    ),
                    "next_start": (
                        pending.get("next_start") if isinstance(pending, dict) else None
                    ),
                    "next_offset": evidence.get("next_offset", 0),
                    "complete": bool(evidence.get("complete")),
                    "final_checkpoint": final_checkpoint,
                    "instruction": (
                        "The checkpoint write was verified. Draft directly from final_checkpoint "
                        "without rereading it."
                        if final_checkpoint is not None
                        else "The checkpoint write was verified. The previous raw batch is now "
                        "discardable; follow next_start and next_offset while more content remains. "
                        "If the run budget is low, save an explicitly partial summary."
                    ),
                }

            if action == "read":
                evidence = self._load_summary_evidence(state)
                checkpoint = self._evidence_text(evidence)
                offset = int(arguments.get("offset") or 0)
                limit = int(arguments["limit"]) if arguments.get("limit") is not None else len(checkpoint)
                if offset < 0 or limit < 0 or (arguments.get("limit") is not None and limit == 0):
                    raise ValueError("offset must be nonnegative and limit must be positive.")
                content = checkpoint[offset : offset + limit]
                if content.strip():
                    self._record_paper_activity(
                        context, document, "read", evidence_reused=True,
                        citations=sorted(citation.strip("[]") for citation in _paper_summary_citations(content)),
                    )
                return {
                    "status": "available" if checkpoint else "empty",
                    "document_id": document_id,
                    "checkpoint_path": relative_path,
                    "offset": offset,
                    "content": content,
                    "has_more": offset + len(content) < len(checkpoint),
                    "next_offset": (
                        offset + len(content)
                        if offset + len(content) < len(checkpoint)
                        else None
                    ),
                    "size_characters": len(checkpoint),
                    "source_version": state["source_version"],
                    "complete": bool(evidence.get("complete")),
                    "resume_action": evidence.get("action", "pages"),
                    "resume_start": evidence.get("next_start"),
                    "resume_offset": evidence.get("next_offset", 0),
                }

        raise ValueError(f"Unknown paper summary checkpoint action '{action}'.")

    def _load_summary_evidence(self, state: dict[str, Any]) -> dict[str, Any]:
        durable = self._documents.summary_evidence(state["document_id"], state["source_version"])
        try:
            content = self._workspace.read_file(state["checkpoint_path"]).content
        except FileNotFoundError:
            content = None
        if durable is not None:
            if content != durable:
                self._workspace.write_file(
                    state["checkpoint_path"], durable,
                    tags=["paper-evidence", f"paper:{state['document_id']}"],
                )
                if self._workspace.read_file(state["checkpoint_path"]).content != durable:
                    raise RuntimeError("Paper evidence checkpoint repair verification failed.")
            return durable
        if content is None:
            return {"records": [], "complete": False}
        if not isinstance(content, dict) or content.get("source_version") != state["source_version"]:
            raise ValueError("Paper evidence source version is invalid.")
        return content

    @staticmethod
    def _evidence_text(evidence: dict[str, Any]) -> str:
        return "\n\n".join(str(record["content"]).strip() for record in evidence["records"]) + (
            "\n" if evidence["records"] else ""
        )

    @staticmethod
    def _summary_coverage_progress(records: list[dict[str, Any]], action: str) -> dict[str, Any]:
        spans: dict[int, list[dict[str, Any]]] = {}
        source_ends: list[tuple[int, int]] = []
        for record in records:
            coverage = record.get("coverage")
            if not isinstance(coverage, dict) or coverage.get("kind") != action:
                continue
            for span in coverage.get("spans", []):
                spans.setdefault(int(span["index"]), []).append(span)
            if record.get("source_end") is not None:
                source_ends.append((int(record["source_end"][0]), int(record["source_end"][1])))
        index = 1 if action == "pages" else 0
        first_index = index
        offset = 0
        while index in spans:
            item_complete = False
            for span in sorted(spans[index], key=lambda item: (item["offset"], item["end_offset"])):
                if int(span["offset"]) > offset:
                    break
                offset = max(offset, int(span["end_offset"]))
                item_complete = item_complete or bool(span["complete"])
            if not item_complete:
                break
            index += 1
            offset = 0
        complete = bool(source_ends) and (index, offset) >= max(source_ends)
        return {
            "action": action,
            "contiguous": index > first_index or offset > 0,
            "next_start": None if complete else index,
            "next_offset": 0 if complete else offset,
            "complete": complete,
        }

    def _append_summary_evidence(
        self, context: ScholarWeaveContext, document_id: str, content: str,
    ) -> dict[str, Any]:
        with self._summary_save_lock:
            state = self._paper_summary_state(context, document_id)
            evidence = self._load_summary_evidence(state)
            pending = state.get("pending_checkpoint")
            if isinstance(pending, dict) and pending.get("content_unavailable"):
                raise ValueError(
                    "The pending source batch was archived before its evidence was checkpointed. "
                    "Read the batch again before appending evidence or saving a complete summary."
                )
            record_id = (
                pending["id"] if isinstance(pending, dict)
                else next(
                    (r["id"] for r in evidence["records"] if r["content"] == content.strip()),
                    hashlib.sha256(content.strip().encode()).hexdigest(),
                )
            )
            existing = next((r for r in evidence["records"] if r["id"] == record_id), None)
            state["last_append_reconciled"] = existing is not None and existing["content"] != content.strip()
            if existing is None:
                record = {
                    "id": record_id, "content": content.strip(),
                    "coverage": pending.get("coverage") if isinstance(pending, dict) else None,
                    "run_id": context.run_id,
                    "prompt_revision": self._prompts.revision if self._prompts else None,
                    "model": _paper_model_provenance(context),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                if isinstance(pending, dict) and not pending["has_more"]:
                    spans = pending["coverage"].get("spans", [])
                    if spans:
                        last = max(spans, key=lambda span: (span["index"], span["end_offset"]))
                        record["source_end"] = [
                            int(last["index"]) + (1 if last["complete"] else 0),
                            0 if last["complete"] else int(last["end_offset"]),
                        ]
                evidence["records"].append(record)
                evidence.update({key: state[key] for key in (
                    "source_version", "source_hash", "extraction_hash",
                )})
                if isinstance(pending, dict):
                    if not evidence.get("complete"):
                        progress = self._summary_coverage_progress(
                            evidence["records"], pending["action"],
                        )
                        traversal = evidence.get("action")
                        if not progress["complete"] and traversal in {"pages", "chunks"} and traversal != pending["action"]:
                            progress = self._summary_coverage_progress(evidence["records"], traversal)
                        evidence.update(progress)
                # SQLite is authoritative; the guarded workspace mirror can be repaired
                # after a crash between commit and file write without losing earlier evidence.
                self._documents.save_summary_evidence(document_id, evidence)
                self._workspace.write_file(
                    state["checkpoint_path"], evidence,
                    tags=["paper-evidence", f"paper:{document_id}"],
                )
                if self._workspace.read_file(state["checkpoint_path"]).content != evidence:
                    raise RuntimeError("Paper evidence checkpoint verification failed.")
            state.pop("pending_checkpoint", None)
            state["checkpoint_size_characters"] = len(self._evidence_text(evidence))
            state["complete"] = bool(evidence.get("complete"))
            state["last_record_id"] = record_id
            return evidence

    async def bound_tool_result(
        self,
        catalog_id: str,
        result: Any,
        context: ScholarWeaveContext,
        *,
        max_tokens: int | None = None,
    ) -> Any:
        """Preserve fresh output unless the caller explicitly requests an archive preview."""
        if max_tokens is None:
            return result
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive.")
        if isinstance(result, dict) and result.get("result_ref") and result.get("truncated"):
            return result
        if catalog_id == "tool.results.read":
            return result
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        limit = max_tokens * 4
        if len(serialized) <= limit:
            return result
        stored = self._store_cached_text(serialized, context, "tool-results")
        await context.emit(
            "tool.result.stored",
            {
                "catalog_id": catalog_id,
                **stored,
            },
        )
        return {
            "truncated": True,
            **stored,
            "preview": serialized[:limit],
            "instruction": "Use read_tool_result with this result_ref for targeted slices.",
        }

    def store_context_history(
        self,
        items: list[dict[str, Any]],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        """Persist lossless JSON history before replacing any model context."""
        serialized_items = [
            json.dumps(item, ensure_ascii=False, allow_nan=False) for item in items
        ]
        index: list[dict[str, Any]] = []
        offset = 1
        for position, (item, serialized) in enumerate(zip(items, serialized_items)):
            index.append({
                "item": position,
                "role": str(item.get("role") or item.get("type") or "")[:80],
                "name": str(item.get("name") or item.get("tool_call_id") or item.get("call_id") or "")[:80],
                "offset": offset,
                "length": len(serialized),
            })
            offset += len(serialized) + 2
        stored = self._store_cached_text(
            "[" + ",\n".join(serialized_items) + "]", context, "context-history"
        )
        serialized_index = json.dumps(index, ensure_ascii=False)
        if len(serialized_index) <= 4_096:
            stored["index"] = index
        else:
            stored["index_ref"] = self._store_cached_text(
                serialized_index, context, "context-history"
            )["result_ref"]
        stored["item_count"] = len(items)
        return stored

    def _cache_scope(self, context: ScholarWeaveContext) -> str:
        kind = "conversations" if context.conversation_id else "runs"
        identity = context.conversation_id or context.run_id
        if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
            raise StorageError("Invalid context cache scope.")
        return f"{kind}/{identity}"

    def _store_cached_text(
        self,
        content: str,
        context: ScholarWeaveContext,
        category: str,
    ) -> dict[str, Any]:
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        prefix = f"{self._cache_scope(context)}/{category}/{digest}"
        maximum = self._settings.max_artifact_bytes
        if maximum < 4:
            raise StorageError("Artifact size limit is too small for context caching.")
        with self._cache_lock:
            if len(data) <= maximum:
                result_ref = f"{prefix}.json"
                self._write_cached_bytes(result_ref, data)
            else:
                # Character-aligned chunks keep read_tool_result offsets independent of UTF-8.
                chunk_size = maximum // 4
                chunks: list[dict[str, Any]] = []
                for offset in range(0, len(content), chunk_size):
                    chunk = content[offset : offset + chunk_size]
                    chunk_ref = f"{prefix}/part-{len(chunks):06d}.json"
                    chunks.append({
                        "result_ref": chunk_ref,
                        "offset": offset,
                        "length": len(chunk),
                    })
                manifest = json.dumps({
                    "format": "scholarweave.context-cache.v1",
                    "size_characters": len(content),
                    "size_bytes": len(data),
                    "chunks": chunks,
                }, ensure_ascii=False).encode("utf-8")
                if len(manifest) > maximum:
                    raise StorageError("Context cache manifest exceeds maximum artifact size.")
                for chunk in chunks:
                    self._write_cached_bytes(
                        chunk["result_ref"],
                        content[chunk["offset"] : chunk["offset"] + chunk["length"]].encode("utf-8"),
                    )
                result_ref = f"{prefix}.manifest.json"
                self._write_cached_bytes(result_ref, manifest)
        return {
            "result_ref": result_ref,
            "size_bytes": len(data),
            "size_characters": len(content),
        }

    def _write_cached_bytes(self, result_ref: str, data: bytes) -> None:
        self._guard_cache_path(result_ref)
        try:
            existing = self._storage.read_text(self._settings.artifacts_dir, result_ref)
        except FileNotFoundError:
            existing = None
        if existing is not None and existing.encode("utf-8") == data:
            return
        self._storage.write_bytes(self._settings.artifacts_dir, result_ref, data)
        if self._storage.read_text(self._settings.artifacts_dir, result_ref).encode("utf-8") != data:
            raise StorageError("Context cache write verification failed.")

    def _guard_cache_path(self, result_ref: str) -> None:
        parts = PurePosixPath(result_ref).parts
        resolved = self._storage._safe_path(self._settings.artifacts_dir, result_ref)
        scope = self._settings.artifacts_dir.resolve() / parts[0] / parts[1]
        try:
            resolved.relative_to(scope)
        except ValueError as exc:
            raise StorageError("result_ref escapes its run or conversation cache.") from exc

    def store_context_checkpoint(
        self,
        checkpoint: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        checkpoint_id = str(checkpoint.get("checkpoint_id") or uuid.uuid4())
        relative_path = f"runs/{context.run_id}/checkpoints/{checkpoint_id}.json"
        stored = self._storage.write_json(
            self._settings.artifacts_dir,
            relative_path,
            checkpoint,
        )
        return {
            "result_ref": stored.relative_path,
            "size_bytes": stored.size_bytes,
        }

    def _read_tool_result(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        result_ref = self._authorized_result_ref(arguments["result_ref"], context)
        offset = int(arguments["offset"])
        limit = int(arguments.get("limit", 10))
        if offset < 0 or limit < 1:
            raise ValueError("offset must be nonnegative and limit must be positive.")
        content = self._storage.read_text(self._settings.artifacts_dir, result_ref)
        size = len(content)
        if result_ref.endswith(".manifest.json"):
            manifest = json.loads(content)
            if manifest.get("format") != "scholarweave.context-cache.v1":
                raise StorageError("Unsupported context cache manifest.")
            size = int(manifest["size_characters"])
            slices: list[str] = []
            for chunk in manifest["chunks"]:
                start = int(chunk["offset"])
                end = start + int(chunk["length"])
                if start >= offset + limit or end <= offset:
                    continue
                chunk_ref = self._authorized_result_ref(chunk["result_ref"], context)
                if not chunk_ref.startswith(result_ref.removesuffix(".manifest.json") + "/"):
                    raise StorageError("Context cache chunk is outside its archive.")
                text = self._storage.read_text(self._settings.artifacts_dir, chunk_ref)
                if len(text) != end - start:
                    raise StorageError("Context cache chunk length mismatch.")
                slices.append(text[max(0, offset - start) : min(end, offset + limit) - start])
            content = "".join(slices)
        else:
            content = content[offset : offset + limit]
        return {
            "result_ref": result_ref,
            "offset": offset,
            "content": content,
            "has_more": offset + limit < size,
            "next_offset": min(size, offset + limit),
            "size_characters": size,
        }

    def _result_ref_is_accessible(
        self,
        result_ref: str,
        context: ScholarWeaveContext,
    ) -> bool:
        try:
            self._authorized_result_ref(result_ref, context)
        except ValueError:
            return False
        return True

    def _authorized_result_ref(
        self,
        result_ref: Any,
        context: ScholarWeaveContext,
    ) -> str:
        normalized = _normalize_result_ref(result_ref)
        parts = PurePosixPath(normalized).parts
        self._guard_cache_path(normalized)
        if parts[0] == "conversations":
            if (
                parts[1] != context.conversation_id
                or len(parts) < 4
                or parts[2] not in {"context-history", "tool-results"}
            ):
                raise ValueError(
                    "result_ref does not belong to the active run or its conversation."
                )
            return normalized
        referenced_run_id = parts[1]
        if referenced_run_id == context.run_id:
            return normalized
        if self._runs is None or context.conversation_id is None:
            raise ValueError(
                "result_ref does not belong to the active run or its conversation."
            )
        try:
            referenced_run = self._runs.get(referenced_run_id)
        except NotFoundError:
            raise ValueError(
                "result_ref does not belong to the active run or its conversation."
            ) from None
        if referenced_run.conversation_id != context.conversation_id:
            raise ValueError(
                "result_ref does not belong to the active run or its conversation."
            )
        return normalized

    async def _search_research_sources(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        provider = str(arguments["provider"])
        query = str(arguments["query"])
        if context.metadata.get("fast_answer"):
            if context.metadata.get("fast_answer_page_acquired"):
                raise ValueError(
                    "Fast-answer mode cannot search again after acquiring a result page."
                )
            if provider != "web":
                raise ValueError("Fast-answer mode only permits web searches.")
        if provider == "web":
            result = await self._search_web({"query": query, "limit": 10}, context)
            if context.metadata.get("fast_answer"):
                urls = context.metadata.setdefault("fast_answer_result_urls", [])
                if not isinstance(urls, list):
                    raise ValueError("The fast-answer result URL list is invalid.")
                urls.extend(
                    str(item["url"])
                    for item in result.get("results", [])
                    if isinstance(item, dict) and item.get("url")
                )
            return self._annotate_local_source_results(result)
        if provider == "arxiv":
            result = await self._research_search.search_arxiv(query, 10)
            return self._annotate_local_source_results(result, match_title=True)
        if provider == "wikipedia":
            result = await self._research_search.search_wikipedia(query, 10)
            return self._annotate_local_source_results(result)
        raise ValueError(f"Unknown research source provider '{provider}'.")

    async def _acquire_research_source(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        kind = str(arguments["kind"])
        url = str(arguments["url"])
        title = _nullable_tool_string(arguments.get("title"), "title")
        if context.metadata.get("fast_answer"):
            allowed_urls = context.metadata.get("fast_answer_result_urls", [])
            if kind != "web_page" or not isinstance(allowed_urls, list) or url not in allowed_urls:
                raise ValueError(
                    "Fast-answer mode can only acquire web pages returned by its web search."
                )
            context.metadata["fast_answer_page_acquired"] = True
        if kind == "paper":
            return await self._download_paper(
                {"pdf_url": url, "title": title},
                context,
            )
        if kind == "web_page":
            return await self._download_web_page({"url": url}, context)
        raise ValueError(f"Unknown research source kind '{kind}'.")

    def _search_research_library(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> Any:
        query = _nullable_tool_string(arguments.get("query"), "query") or ""
        document_id = _nullable_tool_string(
            arguments.get("document_id"),
            "document_id",
            coerce_null_literal=True,
        )
        ignored_document_ids = _tool_string_list(
            arguments.get("ignore_document_ids"),
            "ignore_document_ids",
        )
        selected_document = (
            self._documents.get_document(document_id) if document_id else None
        )
        if document_id and selected_document is None:
            return {
                "found": False,
                "requested_document_id": document_id,
                "query": query or None,
                "results": [],
                "warning": (
                    "No local paper has that document ID. Search by title or filename with "
                    "document_id set to JSON null."
                ),
            }
        if query:
            query_tokens = list(dict.fromkeys(_search_words(query)))
            documents = (
                [selected_document]
                if document_id
                else self._documents.list_documents()
            )
            documents = [
                document
                for document in documents
                if document is not None and document.id not in ignored_document_ids
            ]
            matches: list[dict[str, Any]] = []
            for document in documents:
                matched_fields, matched_words = _document_metadata_matches(
                    document,
                    query_tokens,
                )
                if not matched_words:
                    continue
                matches.append(
                    {
                        **self._document_search_result(document),
                        "match_type": "metadata",
                        "matched_fields": matched_fields,
                        "matched_words": matched_words,
                        "match_score": len(matched_words),
                    }
                )
            matches.sort(
                key=lambda match: (
                    -int(match["match_score"]),
                    str(match["title"]).casefold(),
                    str(match["document_id"]),
                )
            )
            return matches[: min(3, int(arguments["limit"]))]
        if document_id:
            return self._inspect_paper({"document_id": document_id}, context)
        return self._list_documents({}, context)

    def _organize_research_library(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        action = str(arguments["action"])
        if action == "list":
            return {
                "folders": [
                    {"id": folder.id, "name": folder.name}
                    for folder in self._documents.list_folders()
                ],
                "papers": [
                    {
                        "document_id": document.id,
                        "title": document.title,
                        "folder_id": (document.metadata_json or {}).get("folder_id"),
                    }
                    for document in self._documents.list_documents()
                ],
            }
        if action == "create_folder":
            folder_name = _nullable_tool_string(
                arguments.get("folder_name"),
                "folder_name",
            )
            if folder_name is None:
                raise ValueError("create_folder requires folder_name.")
            folder = self._documents.create_folder(folder_name)
            return {"folder": {"id": folder.id, "name": folder.name}}
        if action == "move_paper":
            document_id = _nullable_tool_string(
                arguments.get("document_id"),
                "document_id",
            )
            if document_id is None:
                raise ValueError("move_paper requires document_id.")
            folder_id = _nullable_tool_string(
                arguments.get("folder_id"),
                "folder_id",
                coerce_null_literal=True,
            )
            document = self._documents.assign_folder(document_id, folder_id)
            return {
                "paper": {
                    "document_id": document.id,
                    "title": document.title,
                    "folder_id": (document.metadata_json or {}).get("folder_id"),
                }
            }
        raise ValueError(f"Unknown library organization action '{action}'.")

    async def _read_research_paper(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> Any:
        document_id = _nullable_tool_string(
            arguments.get("document_id"),
            "document_id",
            coerce_null_literal=True,
        )
        if document_id is None:
            raise ValueError("document_id is required when reading a paper.")
        action = str(arguments["action"])
        if action == "inspect":
            return self._inspect_paper({"document_id": document_id}, context)
        if action == "prepare":
            inspection = self._inspect_paper({"document_id": document_id}, context)
            if inspection["readable"]:
                return inspection
            return await self._ingest_paper({"document_id": document_id}, context)
        if action == "search":
            query = _nullable_tool_string(arguments.get("query"), "query")
            if not query:
                raise ValueError("query is required for paper content search.")
            document = self._documents.get_document(document_id)
            if document is None:
                raise ValueError("Paper was not found.")
            hits = self._retrieval.keyword_search(
                query, document_id=document_id, top_k=int(arguments.get("limit") or 5),
            )
            bounded, _, _ = self._bounded_source_items(hits, offset=0)
            if bounded:
                self._record_paper_activity(
                    context, document, "read",
                    citations=sorted({str(item["citation"]) for item in bounded if item.get("citation")}),
                )
            return {
                "document_id": document_id, "query": query, "matches": bounded,
                "coverage": "targeted search, not a complete paper read",
                "instruction": "Use chunk_index and match_offset with action=chunks to expand a hit.",
            }
        start = arguments.get("start")
        limit = int(arguments["limit"]) if arguments.get("limit") is not None else None
        if action == "pages":
            return self._read_paper_pages(
                {
                    "document_id": document_id,
                    "start_page": max(1, int(start or 1)),
                    "limit": limit,
                    "offset": arguments.get("offset"),
                    "_max_output_chars": arguments.get("_max_output_chars"),
                },
                context,
            )
        if action == "chunks":
            return self._read_document_chunks(
                {
                    "document_id": document_id,
                    "start": max(0, int(start or 0)),
                    "limit": limit,
                    "offset": arguments.get("offset"),
                    "_max_output_chars": arguments.get("_max_output_chars"),
                },
                context,
            )
        raise ValueError(f"Unknown paper read action '{action}'.")

    async def _read_paper_summary_batch(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> Any:
        action = str(arguments["action"])
        document_id = str(arguments["document_id"]).strip()
        delegated = {
            "document_id": arguments["document_id"],
            "action": action,
            "start": arguments.get("start"),
            "offset": arguments.get("offset"),
            "limit": None,
        }
        if action in {"inspect", "prepare"}:
            result = await self._read_research_paper(delegated, context)
            state = self._paper_summary_state(context, document_id)
            evidence = self._load_summary_evidence(state)
            return {
                **result,
                "evidence_available": bool(evidence["records"]),
                "checkpoint_path": state["checkpoint_path"],
                "complete": bool(evidence.get("complete")),
                "resume_action": evidence.get("action", "pages"),
                "resume_start": evidence.get("next_start"),
                "resume_offset": evidence.get("next_offset", 0),
            }

        async with self._paper_summary_lock:
            state = self._paper_summary_state(context, document_id)
            pending = state.get("pending_checkpoint")
            if isinstance(pending, dict) and pending.get("content_unavailable"):
                state.pop("pending_checkpoint")
                pending = None
            if isinstance(pending, dict):
                # A replay of the same read may occur after recovery before its checkpoint.
                if (
                    action == pending.get("action")
                    and arguments.get("start") == pending.get("requested_start")
                    and int(arguments.get("offset") or 0) == pending.get("offset", 0)
                ):
                    result = await self._read_research_paper(
                        {**delegated, "start": pending["start"], "offset": pending["offset"]},
                        context,
                    )
                    return {**result, **pending, "batch_id": pending["id"], "checkpoint_required": True}
                raise ValueError(
                    "The previous paper-summary batch must be appended to "
                    f"{pending['checkpoint_path']} before another batch can be read."
                )
            evidence = self._load_summary_evidence(state)
            if delegated["start"] is None:
                if evidence.get("complete"):
                    return {
                        "complete": True, "evidence_available": True, "has_more": False,
                        "checkpoint_path": state["checkpoint_path"],
                        "instruction": "Read the existing evidence checkpoint and synthesize; do not reread the paper.",
                    }
                delegated["start"] = evidence.get("next_start") or (1 if action == "pages" else 0)
                delegated["offset"] = evidence.get("next_offset", 0)
            delegated["start"] = max(1 if action == "pages" else 0, int(delegated["start"]))
            result = await self._read_research_paper(delegated, context)
            completed = int(state.get("batches_read", 0)) + 1
            state["batches_read"] = completed
            if isinstance(result, dict):
                coverage = self._paper_summary_coverage(action, delegated, result)
                next_start = self._paper_summary_next_start(action, result)
                checkpoint_path = state["checkpoint_path"]
                state["pending_checkpoint"] = {
                    "id": hashlib.sha256(json.dumps(
                        [state["source_version"], coverage], sort_keys=True,
                    ).encode()).hexdigest(),
                    "batch": completed,
                    "action": action,
                    "requested_start": arguments.get("start"),
                    "start": int(delegated["start"]),
                    "offset": int(delegated.get("offset") or 0),
                    "coverage": coverage,
                    "has_more": bool(result.get("has_more")),
                    "next_start": next_start,
                    "next_offset": int(result.get("next_offset") or 0),
                    "checkpoint_path": checkpoint_path,
                }
                return {
                    **result,
                    "next_start": next_start,
                    "summary_batch": completed,
                    "batch_id": state["pending_checkpoint"]["id"],
                    "source_version": state["source_version"],
                    "checkpoint_required": True,
                    "checkpoint_path": checkpoint_path,
                    "coverage": coverage,
                    "instruction": (
                        "Append compact cited evidence before the next batch; follow exact next_start "
                        "and next_offset. No fixed page cap. If the run budget is exhausted, save "
                        "an explicitly partial summary with unread coverage; never claim completeness."
                    ),
                }
            return result

    def constrain_paper_summary_batch(
        self,
        result: dict[str, Any],
        context: ScholarWeaveContext,
        *,
        max_chars: int,
    ) -> dict[str, Any] | None:
        """Fit pending source coverage to space in the actual next model request."""
        document_id = str(result.get("document_id") or "")
        states = context.metadata.get("_paper_summary_checkpoint_states", {})
        state = states.get(document_id, {}) if isinstance(states, dict) else {}
        pending = state.get("pending_checkpoint")
        if not result.get("batch_id") or not isinstance(pending, dict) or result["batch_id"] not in {
            pending.get("id"), pending.get("source_batch_id"),
        }:
            return None
        source_batch_id = pending.get("source_batch_id") or pending["id"]
        if max_chars < 1:
            return None
        if len(json.dumps(result, ensure_ascii=False)) <= max_chars:
            state["pending_checkpoint"] = {
                **pending,
                "id": result["batch_id"],
                "source_batch_id": source_batch_id,
                "coverage": result["coverage"],
                "has_more": bool(result.get("has_more")),
                "next_start": self._paper_summary_next_start(pending["action"], result),
                "next_offset": int(result.get("next_offset") or 0),
                "content_unavailable": False,
            }
            return result
        action = pending["action"]
        key = "pages" if action == "pages" else "chunks"
        sources = result.get(key)
        if not isinstance(sources, list) or not sources:
            return None
        source_budget = max_chars
        while source_budget > 0:
            try:
                selected, _, _ = self._bounded_source_items(
                    sources, offset=0, max_chars=source_budget,
                )
            except ValueError:
                return None
            for original, selected_item in zip(sources, selected):
                selected_item["offset"] += int(original.get("offset") or 0)
                selected_item["text_complete"] = (
                    selected_item["text_complete"] and bool(original.get("text_complete", True))
                )
            last = selected[-1]
            index = int(last["page_number" if action == "pages" else "chunk_index"])
            next_offset = (
                0 if last["text_complete"] else last["offset"] + len(last["text"])
            )
            has_more = bool(result.get("has_more")) or len(selected) < len(sources) or bool(next_offset)
            next_start = index + (1 if last["text_complete"] else 0) if has_more else None
            resized = {
                **result,
                key: selected,
                "has_more": has_more,
                "next_start": next_start,
                "next_offset": next_offset,
            }
            if action == "pages":
                resized["next_page"] = next_start
            coverage = self._paper_summary_coverage(action, pending, resized)
            batch_id = hashlib.sha256(json.dumps(
                [state["source_version"], coverage], sort_keys=True,
            ).encode()).hexdigest()
            resized.update(coverage=coverage, batch_id=batch_id)
            overflow = len(json.dumps(resized, ensure_ascii=False)) - max_chars
            if overflow <= 0:
                state["pending_checkpoint"] = {
                    **pending,
                    "id": batch_id,
                    "source_batch_id": source_batch_id,
                    "coverage": coverage,
                    "has_more": has_more,
                    "next_start": next_start,
                    "next_offset": next_offset,
                    "content_unavailable": False,
                }
                return resized
            source_budget -= overflow
        return None

    @staticmethod
    def mark_paper_summary_batch_unavailable(
        result: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> None:
        """Prevent archived, uncheckpointed raw text from becoming claimed coverage."""
        states = context.metadata.get("_paper_summary_checkpoint_states", {})
        state = states.get(str(result.get("document_id") or ""), {}) if isinstance(states, dict) else {}
        pending = state.get("pending_checkpoint")
        if result.get("batch_id") and isinstance(pending, dict) and result["batch_id"] in {
            pending.get("id"), pending.get("source_batch_id"),
        }:
            pending["content_unavailable"] = True

    @staticmethod
    def _paper_summary_next_start(action: str, result: dict[str, Any]) -> int | None:
        next_start = result.get("next_page", result.get("next_start"))
        if not result.get("has_more") or next_start is None:
            return None
        return int(next_start)

    def _paper_summary_state(
        self,
        context: ScholarWeaveContext,
        document_id: str,
    ) -> dict[str, Any]:
        states = context.metadata.setdefault("_paper_summary_checkpoint_states", {})
        if not isinstance(states, dict):
            states = {}
            context.metadata["_paper_summary_checkpoint_states"] = states
        state = states.setdefault(document_id, {})
        if not isinstance(state, dict):
            state = {}
            states[document_id] = state
        revision = self._documents.source_revision(document_id)
        if state.get("source_version") != revision["source_version"]:
            stale_revision = state.get("source_version") is not None
            state.clear()
            state.update(revision)
            state["document_id"] = document_id
            state["checkpoint_path"] = (
                f"papers/{document_id}/evidence/{revision['source_version']}/index.json"
            )
            if stale_revision:
                activity = context.metadata.get("paper_activity", [])
                context.metadata["paper_activity"] = [
                    item for item in activity if not (
                        isinstance(item, dict) and item.get("document_id") == document_id
                        and item.get("action") in {"read", "summary_saved", "summary_reused"}
                    )
                ]
                raise ValueError("Paper extraction changed; read the new source again before saving its evidence.")
        state.setdefault("document_id", document_id)
        return state

    @staticmethod
    def _paper_summary_coverage(
        action: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if action == "pages":
            pages = result.get("pages")
            numbers = [
                int(page["page_number"])
                for page in pages
                if isinstance(page, dict) and page.get("page_number") is not None
            ] if isinstance(pages, list) else []
            return {
                "kind": "pages",
                "start": min(numbers) if numbers else int(arguments.get("start") or 1),
                "end": max(numbers) if numbers else None,
                "spans": [
                    {"index": int(page["page_number"]), "offset": int(page.get("offset") or 0),
                     "end_offset": int(page.get("offset") or 0) + len(str(page.get("text") or "")),
                     "complete": bool(page.get("text_complete", True))}
                    for page in pages or []
                ],
            }
        chunks = result.get("chunks")
        indexes = [
            int(chunk["chunk_index"])
            for chunk in chunks
            if isinstance(chunk, dict) and chunk.get("chunk_index") is not None
        ] if isinstance(chunks, list) else []
        return {
            "kind": "chunks",
            "start": min(indexes) if indexes else int(arguments.get("start") or 0),
            "end": max(indexes) if indexes else None,
            "spans": [
                {"index": int(chunk["chunk_index"]), "offset": int(chunk.get("offset") or 0),
                 "end_offset": int(chunk.get("offset") or 0) + len(str(chunk.get("text") or "")),
                 "complete": bool(chunk.get("text_complete", True))}
                for chunk in chunks or []
            ],
        }

    async def _read_research_web_page(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> Any:
        query = _nullable_tool_string(arguments.get("query"), "query") or ""
        if query:
            return await self._search_web_page(
                {
                    "source_id": arguments["source_id"],
                    "query": query,
                    "top_k": arguments["limit"],
                },
                context,
            )
        return await self._read_web_page(
            {
                "source_id": arguments["source_id"],
                "start": arguments["start"],
                "limit": arguments["limit"],
            },
            context,
        )

    def _search_research_notes(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return self._search_workspace(
            {
                "query": _nullable_tool_string(arguments.get("query"), "query"),
                "kinds": arguments["kinds"],
                "tags": arguments["tags"],
                "limit": arguments["limit"],
                "offset": arguments.get("offset") or 0,
            },
            context,
        )

    def _read_research_note(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        result = self._read_workspace(arguments, context)
        path = str(result["path"])
        tags = result["tags"]
        content = result["content"]
        if not (
            path.startswith("papers/")
            and path.endswith("/summary.md")
            and isinstance(content, str)
        ):
            return result

        paper_tag = next(
            (
                tag
                for tag in tags
                if isinstance(tag, str) and tag.startswith("paper:")
            ),
            None,
        )
        document_id = paper_tag.removeprefix("paper:") if paper_tag else None
        document = self._documents.get_document(document_id) if document_id else None
        reusable = len(content.strip()) >= 200 and not content.startswith("> Partial summary:")
        reason = "Canonical summary contains substantive content." if reusable else (
            "Canonical summary is empty, still a template, partial, or lacks substantive content."
        )
        if document is not None:
            try:
                provenance = self._workspace.read_file(
                    f"papers/{document_id}/summary.provenance.json"
                ).content
            except FileNotFoundError:
                provenance = None
            if isinstance(provenance, dict) and provenance.get("content_hash") == hashlib.sha256(content.encode()).hexdigest():
                current = self._documents.source_revision(document.id)
                if provenance.get("source_version") != current["source_version"]:
                    reusable = False
                    reason = "Paper source or extraction changed since this generated summary."
                elif not provenance.get("coverage_complete"):
                    reusable = False
                    reason = "The generated summary has incomplete source coverage."
                elif provenance.get("review_complete") is False:
                    reusable = False
                    reason = "This overview has not undergone a complete paper review."
        result["summary_check"] = {
            "status": "reusable" if reusable else "incomplete",
            "document_id": document_id,
            "needs_regeneration": not reusable,
            "reason": reason,
        }
        if reusable and document is not None:
            self._record_paper_activity(
                context,
                document,
                "summary_reused",
                path=path,
            )
        return result

    def _save_research_note(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        target = str(arguments["target"])
        mode = str(arguments["mode"])
        content = str(arguments["content"])
        tags = list(arguments["tags"])
        if target == "new_note":
            name = _nullable_tool_string(arguments.get("name"), "name")
            if not name:
                raise ValueError("A new research note requires a name.")
            return self._create_workspace_note(
                {"name": name, "content": content, "tags": tags},
                context,
            )

        if target == "paper_notes":
            document_id = _nullable_tool_string(
                arguments.get("document_id"),
                "document_id",
                coerce_null_literal=True,
            )
            if not document_id:
                raise ValueError("Paper notes require a document_id.")
            self._require_paper_read(context, document_id)
            paper = self._ensure_paper_workspace({"document_id": document_id}, context)
            path = str(paper["notes_path"])
            paper_document = self._documents.get_document(document_id)
        elif target == "path":
            path = _nullable_tool_string(arguments.get("path"), "path")
            if not path:
                raise ValueError("Updating a research note by path requires a path.")
        else:
            raise ValueError(f"Unknown research note target '{target}'.")

        if mode == "append":
            document = self._workspace.append_markdown(path, content)
        elif mode == "overwrite":
            document = self._workspace.write_file(
                path,
                content,
                tags=tags if tags else None,
            )
        else:
            raise ValueError(f"Unknown research note save mode '{mode}'.")
        if tags and mode == "append":
            document = self._workspace.set_tags(path, tags)
        self._append_workspace_receipt(context, document, "Updated")
        if target == "paper_notes" and paper_document is not None:
            self._record_paper_activity(
                context,
                paper_document,
                "notes_saved",
                path=document.path,
            )
        return self._workspace_result(document)

    def _list_documents(
        self,
        _arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        documents = []
        for document in self._documents.list_documents():
            details = self._documents.get_document_details(document.id)
            artifacts = details[1] if details else []
            chunks = details[2] if details else []
            has_manifest = any(
                artifact.kind == "extracted_manifest" for artifact in artifacts
            )
            readable = document.status == "ready" and has_manifest and bool(chunks)
            documents.append(
                {
                    **self._document_search_result(document),
                    "readable": readable,
                    "chunk_count": len(chunks),
                    "next_action": None if readable else "inspect_paper",
                }
            )
        return documents

    @staticmethod
    def _document_search_result(document: Any) -> dict[str, Any]:
        metadata = document.metadata_json if isinstance(document.metadata_json, dict) else {}
        return {
            "id": document.id,
            "document_id": document.id,
            "title": document.title,
            "source_filename": document.source_filename,
            "authors": _document_authors(metadata),
            "source_url": metadata.get("source_url"),
            "status": document.status,
            "page_count": document.page_count,
        }

    def _annotate_local_source_results(
        self,
        result: dict[str, Any],
        *,
        match_title: bool = False,
    ) -> dict[str, Any]:
        for item in result.get("results", []):
            if not isinstance(item, dict):
                continue
            url = item.get("pdf_url") or item.get("url")
            if not isinstance(url, str):
                continue
            title = (
                item.get("title")
                if match_title and isinstance(item.get("title"), str)
                else None
            )
            document = self._source_downloads.find_document(url, title=title)
            item["already_in_library"] = document is not None
            item["local_document_id"] = document.id if document is not None else None
        return result

    def _inspect_paper(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        artifacts = self._documents.get_document_artifacts(document_id)
        chunk_count, chunk_chars = self._retrieval.chunk_stats(document_id)
        source = next(
            (artifact for artifact in artifacts if artifact.kind == "source_pdf"),
            None,
        )
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "extracted_manifest"),
            None,
        )
        manifest = (
            self._documents.artifact_content(manifest_artifact)
            if manifest_artifact is not None
            else None
        )
        content = manifest.get("content", {}) if isinstance(manifest, dict) else {}
        sections = manifest.get("sections", []) if isinstance(manifest, dict) else []
        readable = (
            document.status == "ready"
            and manifest_artifact is not None
            and bool(chunk_count)
            and bool(content.get("char_count") or chunk_chars)
        )
        return {
            "document_id": document.id,
            "title": document.title,
            "source_filename": document.source_filename,
            "status": document.status,
            "page_count": document.page_count,
            "source_available": source is not None,
            "readable": readable,
            "content": {
                "char_count": int(content.get("char_count") or chunk_chars),
                "chunk_count": chunk_count,
                "nonempty_page_count": content.get("nonempty_page_count"),
            },
            "sections": sections,
            "next_action": (
                None
                if readable
                else "Call ingest_paper; it uses native PDF text first and OCR only where needed."
            ),
        }

    async def _ingest_paper(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        await self._documents.ingest_document(document_id)
        result = self._inspect_paper({"document_id": document_id}, context)
        document = self._documents.get_document(document_id)
        if document is not None:
            self._record_paper_activity(context, document, "ingested")
        return result

    def _read_paper_pages(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        artifacts = self._documents.get_document_artifacts(document_id)
        chunk_count, _ = self._retrieval.chunk_stats(document_id)
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "extracted_manifest"),
            None,
        )
        if document.status != "ready" or manifest_artifact is None or not chunk_count:
            raise ValueError(
                "Paper has no readable extracted content. Inspect and ingest it before reading."
            )
        pages = manifest_pages(self._documents.artifact_content(manifest_artifact))
        start_page = int(arguments["start_page"])
        limit = int(arguments["limit"]) if arguments.get("limit") is not None else None
        available = [
            page for page in pages if int(page.get("page") or 0) >= start_page
        ]
        candidates = [
            {
                "page_number": int(page["page"]),
                "citation": str(page.get("citation") or f"p.{int(page['page'])}"),
                "text": str(page.get("text") or ""),
            }
            for page in available[:limit]
        ]
        selected, complete, next_offset = self._bounded_source_items(
            candidates, offset=int(arguments.get("offset") or 0),
            max_chars=arguments.get("_max_output_chars"),
        )
        if not selected:
            return {
                "document_id": document_id,
                "title": document.title,
                "page_count": len(pages),
                "has_more": False,
                "next_page": None,
                "pages": [],
                "warning": "The requested page is beyond the end of this paper.",
            }
        if any(item["text"].strip() for item in selected):
            self._record_paper_activity(
                context, document, "read",
                citations=sorted({str(item["citation"]) for item in selected if item["text"].strip()}),
            )
        next_page = int(selected[-1]["page_number"]) + (0 if next_offset else 1)
        has_more = complete < len(available) or bool(next_offset)
        return {
            "document_id": document_id,
            "title": document.title,
            "page_count": len(pages),
            "has_more": has_more,
            "next_page": next_page if has_more else None,
            "next_offset": next_offset,
            "pages": selected,
        }

    def _read_document_chunks(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        start = int(arguments.get("start") or 0)
        limit = int(arguments["limit"]) if arguments.get("limit") is not None else None
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        chunk_count, _ = self._retrieval.chunk_stats(document_id)
        if document.status != "ready" or not chunk_count:
            raise ValueError(
                "Paper has no readable extracted content. Inspect and ingest it before reading."
            )
        chunks = self._retrieval.fetch_document_chunks(document_id, start=start, limit=limit)
        candidates = [
            {
                "chunk_id": chunk.id, "chunk_index": chunk.chunk_index,
                "section_title": chunk.section_title, "citation": chunk.citation,
                "page_start": chunk.page_start, "page_end": chunk.page_end, "text": chunk.text,
            }
            for chunk in chunks
        ]
        selected, complete, next_offset = self._bounded_source_items(
            candidates, offset=int(arguments.get("offset") or 0),
            max_chars=arguments.get("_max_output_chars"),
        )
        if not selected:
            return {
                "document_id": document_id,
                "title": document.title,
                "chunk_count": chunk_count,
                "start": start,
                "has_more": False,
                "next_start": None,
                "chunks": [],
                "warning": "The requested chunk is beyond the end of this paper.",
            }
        if any(item["text"].strip() for item in selected):
            self._record_paper_activity(
                context, document, "read",
                citations=sorted({str(item["citation"]) for item in selected if item["text"].strip()}),
            )
        next_start = start + complete
        has_more = next_start < chunk_count
        return {
            "document_id": document_id,
            "title": document.title,
            "chunk_count": chunk_count,
            "start": start,
            "has_more": has_more,
            "next_start": next_start if has_more else None,
            "next_offset": next_offset,
            "chunks": selected,
        }

    def _bounded_source_items(
        self, items: list[dict[str, Any]], *, offset: int, max_chars: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        budget = max_chars
        if budget is not None and budget < 1:
            raise ValueError("max_chars must be positive.")
        selected: list[dict[str, Any]] = []
        complete = 0
        next_offset = 0
        for index, item in enumerate(items):
            start = max(0, offset) if index == 0 else 0
            text = str(item.get("text") or "")
            if start > len(text):
                raise ValueError("offset is beyond this source item's text.")
            result = {**item, "offset": start, "text": text[start:], "text_complete": True}
            if budget is None:
                selected.append(result)
                complete += 1
                continue
            size = len(json.dumps(result, ensure_ascii=False))
            if size > budget:
                if selected:
                    break
                lo, hi = 0, len(text) - start
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    result.update(text=text[start:start + mid], text_complete=False)
                    if len(json.dumps(result, ensure_ascii=False)) <= budget:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo == 0:
                    raise ValueError("Source metadata exceeds the output budget.")
                result.update(text=text[start:start + lo], text_complete=False)
                next_offset = start + lo
                selected.append(result)
                break
            selected.append(result)
            budget -= size
            complete += 1
        return selected, complete, next_offset

    def _keyword_search(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return self._retrieval.keyword_search(
            str(arguments["query"]),
            document_id=arguments.get("document_id"),
            top_k=int(arguments.get("top_k") or 5),
        )

    async def _download_paper(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        pdf_url = str(arguments["pdf_url"])
        title = str(arguments["title"]) if arguments.get("title") else None
        existing = self._source_downloads.find_document(pdf_url, title=title)
        document = await self._source_downloads.download_pdf(pdf_url, title=title)
        paper = self._workspace.ensure_paper_folder(document.id, document.title)
        marker = f"<!-- scholarweave-paper-acquired:{document.id} -->"
        notes = self._workspace.read_file(str(paper["notes_path"]))
        notes_content = notes.content if isinstance(notes.content, str) else ""
        if marker not in notes_content:
            notes = self._workspace.append_markdown(
                str(paper["notes_path"]),
                (
                    f"\n{marker}\n## Source acquired\n\n"
                    f"- URL: {str(arguments['pdf_url'])}\n"
                    f"- Document ID: `{document.id}`\n"
                ),
            )
            self._append_workspace_receipt(context, notes, "Updated")
        for created_path in paper["created"]:
            self._append_workspace_receipt(
                context,
                self._workspace.read_file(str(created_path)),
                "Created",
            )
        self._record_paper_activity(
            context,
            document,
            "acquired",
            url=str(arguments["pdf_url"]),
            summary_path=str(paper["summary_path"]),
            notes_path=str(paper["notes_path"]),
        )
        return {
            **self._inspect_paper({"document_id": document.id}, context),
            "already_in_library": existing is not None,
            "summary_path": paper["summary_path"],
            "notes_path": paper["notes_path"],
        }

    async def _download_web_page(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        requested_url = str(arguments["url"])
        try:
            source = await self._source_downloads.download_web_page(requested_url)
        except WebSourceUnavailable as exc:
            return {
                "status": "unavailable",
                "source_id": None,
                "title": None,
                "url": exc.url,
                "chunk_count": 0,
                "reason": exc.reason,
                "next_action": (
                    "Use the search-result snippet or try another result URL; "
                    "the download_web_page tool remains available."
                ),
            }
        return self._web_source_result(source)

    async def _read_web_page(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        source = await self._source_downloads.get_web_source(str(arguments["source_id"]))
        start = int(arguments["start"])
        limit = int(arguments["limit"])
        selected = source.chunks[start : start + limit]
        return {
            **self._web_source_result(source),
            "chunks": [
                {
                    "chunk_index": start + offset,
                    "citation": source.url,
                    "text": text,
                }
                for offset, text in enumerate(selected)
            ],
            "has_more": start + len(selected) < len(source.chunks),
            "next_start": start + len(selected),
        }

    async def _search_web_page(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return await self._source_downloads.search_web_source(
            str(arguments["source_id"]),
            str(arguments["query"]),
            top_k=int(arguments["top_k"]),
        )

    async def _search_web(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        query = " ".join(
            str(arguments["query"])
            .translate(str.maketrans("", "", "\"\u201c\u201d"))
            .split()
        )
        limit = int(arguments.get("limit", 10))
        if not query:
            raise ValueError("Search query cannot be empty.")
        if limit != 10:
            raise ValueError("Agent web searches must request exactly 10 results.")

        cache = context.metadata.setdefault(_WEB_SEARCH_CACHE_KEY, {})
        if not isinstance(cache, dict):
            raise ValueError("The web-search session cache is invalid.")
        cache_key = query.casefold()
        attempts = context.metadata.setdefault(_WEB_SEARCH_ATTEMPTS_KEY, [])
        if not isinstance(attempts, list) or any(
            not isinstance(item, str) for item in attempts
        ):
            raise ValueError("The web-search session attempt history is invalid.")
        cached = cache.get(cache_key)
        if cached is not None:
            if not isinstance(cached, dict):
                raise ValueError("The cached web-search result is invalid.")
            cached_limit = cached.get("limit")
            cached_result = cached.get("result")
            cached_results = (
                cached_result.get("results") if isinstance(cached_result, dict) else None
            )
            if (
                isinstance(cached_limit, int)
                and cached_limit >= limit
                and isinstance(cached_result, dict)
                and isinstance(cached_results, list)
            ):
                return self._web_search_result(
                    {
                        **cached_result,
                        "query": query,
                        "results": cached_results[:limit],
                    },
                    context,
                    cached=True,
                )
        if cache_key in attempts:
            raise ValueError(
                "This normalized web query was already attempted in the current session. "
                "Use its earlier results instead of retrying it with a different result limit."
            )

        used = self._web_search_requests_used(context)
        maximum = self._web_search_limit(context)
        if used >= maximum:
            raise ValueError(
                f"The web-search session limit was reached ({used}/{maximum}). "
                "Use the results already gathered instead of rephrasing or retrying searches."
            )
        context.metadata[_WEB_SEARCH_USAGE_KEY] = used + 1
        attempts.append(cache_key)
        try:
            result = await self._research_search.search_web(query, limit)
        except Exception:
            attempts.remove(cache_key)
            context.metadata[_WEB_SEARCH_USAGE_KEY] = used
            raise
        cache[cache_key] = {"limit": limit, "result": result}
        return self._web_search_result(result, context, cached=False)

    def _web_search_result(
        self,
        result: dict[str, Any],
        context: ScholarWeaveContext,
        *,
        cached: bool,
    ) -> dict[str, Any]:
        used = self._web_search_requests_used(context)
        maximum = self._web_search_limit(context)
        return {
            **result,
            "cached": cached,
            "search_budget": {
                "used": used,
                "limit": maximum,
                "remaining": max(0, maximum - used),
            },
        }

    @staticmethod
    def _web_search_requests_used(context: ScholarWeaveContext) -> int:
        value = context.metadata.get(_WEB_SEARCH_USAGE_KEY, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("The web-search session usage counter is invalid.")
        return value

    def _web_search_limit(self, context: ScholarWeaveContext) -> int:
        configured = self._settings.web_search_max_requests_per_session
        value = context.metadata.get("web_search_limit", configured)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("The web-search run limit is invalid.")
        return min(configured, value)

    @staticmethod
    def _web_source_result(source) -> dict[str, Any]:
        return {
            "status": "available",
            "source_id": source.id,
            "title": source.title,
            "url": source.url,
            "chunk_count": len(source.chunks),
            "created_at": source.created_at.isoformat(),
            "expires_at": source.expires_at.isoformat(),
        }

    def _read_workspace(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.read_file(str(arguments["path"]))
        return {
            "path": document.path,
            "media_type": document.media_type,
            "tags": list(document.tags),
            "content": document.content,
        }

    def _search_workspace(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> list[dict[str, Any]]:
        return [
            {
                **self._workspace_discovery_result(document),
                "score": document.score,
                "excerpt": document.excerpt,
            }
            for document in self._workspace.search(
                query=arguments.get("query"),
                kinds=list(arguments["kinds"]),
                tags=list(arguments["tags"]),
                limit=int(arguments["limit"]),
                offset=int(arguments["offset"]),
            )
        ]

    def _list_workspace(
        self, arguments: dict[str, Any], _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        collection = str(arguments["collection"])
        limit, offset = int(arguments["limit"]), int(arguments["offset"])
        if not 1 <= limit <= 50 or offset < 0:
            raise ValueError("Workspace listing requires limit 1 to 50 and a nonnegative offset.")
        if collection == "papers":
            results = [
                self._document_search_result(document)
                for document in self._documents.list_documents()[offset:offset + limit + 1]
            ]
        else:
            results = [
                self._workspace_discovery_result(document)
                for document in self._workspace.list_collection(
                    collection, limit=limit + 1, offset=offset,
                )
            ]
        return {
            "collection": collection,
            "results": results[:limit],
            "next_offset": offset + limit if len(results) > limit else None,
        }

    def _workspace_index(
        self, arguments: dict[str, Any], _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        action = arguments["action"]
        if action == "status":
            return self._workspace.index_status()
        if action == "refresh":
            return self._workspace.refresh_index()
        raise ValueError("Workspace index action must be status or refresh.")

    @staticmethod
    def _workspace_discovery_result(document: WorkspaceDocument) -> dict[str, Any]:
        return {
            "path": document.path,
            "name": document.note_name or document.paper_name or document.name,
            "kind": document.kind,
            "paper_id": document.paper_id,
            "note_id": document.note_id,
            "tags": list(document.tags),
            "modified_at": document.modified_at.isoformat(),
        }

    def _ensure_paper_workspace(
        self,
        arguments: dict[str, Any],
        _context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        return self._workspace.ensure_paper_folder(document.id, document.title)

    def _create_workspace_note(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        document = self._workspace.create_note(
            name=str(arguments["name"]),
            content=str(arguments["content"]),
            tags=list(arguments["tags"]),
        )
        self._append_workspace_receipt(context, document, "Created")
        return {
            **self._workspace_result(document),
            "note_id": document.note_id,
            "name": document.note_name,
            "kind": document.kind,
        }

    def _record_paper_activity(
        self,
        context: ScholarWeaveContext,
        document: Any,
        action: str,
        **details: Any,
    ) -> None:
        activity = context.metadata.setdefault("paper_activity", [])
        if not isinstance(activity, list):
            activity = []
            context.metadata["paper_activity"] = activity
        entry = {
            "action": action,
            "document_id": document.id,
            "title": document.title,
            **details,
        }
        if action == "read":
            entry["source_version"] = self._documents.source_revision(document.id)["source_version"]
        if entry not in activity:
            activity.append(entry)
            if self._runs is not None and self._runs.exists(context.run_id):
                self._runs.update_runtime_metadata(
                    context.run_id,
                    _persistable_tool_metadata(context.metadata),
                )

    def _require_paper_read(
        self,
        context: ScholarWeaveContext,
        document_id: str,
    ) -> None:
        activity = context.metadata.get("paper_activity")
        source_version = self._documents.source_revision(document_id)["source_version"]
        if isinstance(activity, list) and any(
            isinstance(item, dict)
            and item.get("document_id") == document_id
            and item.get("action") == "read"
            and item.get("source_version") == source_version
            for item in activity
        ):
            return
        raise ValueError(
            "Paper summaries and notes can only be saved after extracted pages or chunks "
            "from the current source version have been read in the active run."
        )

    @staticmethod
    def _workspace_result(document) -> dict[str, Any]:
        return {
            "path": document.path,
            "size_bytes": document.size_bytes,
            "sha256": document.sha256,
            "tags": list(document.tags),
        }

    @staticmethod
    def _append_workspace_receipt(
        context: ScholarWeaveContext,
        document,
        action: str,
    ) -> None:
        context.receipts.append(
            ToolReceipt(
                kind="file",
                title=f"{action} {document.path}",
                href=f"/workspace?path={document.path}",
                metadata={"sha256": document.sha256},
            )
        )


def _active_epoch_id(context: ScholarWeaveContext) -> str | None:
    value = context.metadata.get("active_epoch_id")
    return value if isinstance(value, str) else None
