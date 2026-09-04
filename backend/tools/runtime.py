from __future__ import annotations

import asyncio
import inspect
import json
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
from backend.runs.repository import LeaseOwnershipError, RunRepository
from backend.utils import to_jsonable
from backend.persistence.files import SafeStorage
from backend.prompting.registry import PromptRegistry
from backend.research.search import ResearchSearchService
from backend.research.sources import SourceDownloadService, WebSourceUnavailable
from backend.tools.catalog import APPLICATION_TOOL_HANDLERS
from backend.tools.failures import classify_tool_error
from backend.workspace.service import WorkspaceService


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
        raise ValueError("result_ref must be a safe relative run path.")
    normalized = re.sub(r"[\\/]+", "/", raw)
    parts = normalized.split("/")
    if (
        len(parts) < 3
        or parts[0] != "runs"
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        raise ValueError("result_ref must be a safe relative run path.")
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

    async def invoke(
        self,
        catalog_id: str,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
        *,
        tool_call_id: str | None = None,
    ) -> Any:
        """Dispatch one catalog tool and record its bounded result and activity metadata."""
        handler_name = APPLICATION_TOOL_HANDLERS.get(catalog_id)
        if handler_name is None:
            raise ValueError(f"Unknown application tool '{catalog_id}'.")
        handler = getattr(self, handler_name)
        journal = (
            self._runs
            if self._runs is not None and self._runs.exists(context.run_id)
            else None
        )
        safe_retry = _is_safe_read(catalog_id)
        attempts = self._settings.tool_read_retry_attempts if safe_retry else 1
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
            try:
                if inspect.iscoroutinefunction(handler):
                    result = await asyncio.wait_for(
                        handler(arguments, context),
                        timeout=self._settings.tool_call_timeout_seconds,
                    )
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(handler, arguments, context),
                        timeout=self._settings.tool_call_timeout_seconds,
                    )
                    if inspect.isawaitable(result):
                        result = await asyncio.wait_for(
                            result,
                            timeout=self._settings.tool_call_timeout_seconds,
                        )
                if journal is not None:
                    max_tokens = (
                        6_000
                        if catalog_id == "research.summary.checkpoint"
                        and isinstance(result, dict)
                        and result.get("final_checkpoint") is not None
                        else None
                    )
                    result = await self.bound_tool_result(
                        catalog_id,
                        result,
                        context,
                        max_tokens=max_tokens,
                    )
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
                    status = "cancelled" if safe_retry else "unknown_outcome"
                    self._finish_tool_attempt(
                        journal,
                        context,
                        attempt.id,
                        status=status,
                        failure_category=(
                            "cancelled" if safe_retry else "cancelled_after_dispatch"
                        ),
                        error=(
                            None
                            if safe_retry
                            else "Cancellation occurred after a write was dispatched."
                        ),
                    )
                raise
            except Exception as error:
                category, transient = classify_tool_error(error)
                retryable = safe_retry and transient and attempt_number < attempts
                status = "failed" if safe_retry else "unknown_outcome"
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
                    await asyncio.sleep(0.25 * attempt_number)
                    continue
                raise
        raise RuntimeError("Tool invocation ended without a result.")

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
        document_id = str(arguments["document_id"]).strip()
        content = str(arguments["content"]).strip()
        review_summary = str(arguments["review_summary"]).strip()
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        self._require_paper_read(context, document_id)
        citations = _paper_summary_citations(content)
        paper = self._workspace.ensure_paper_folder(document.id, document.title)
        revision = self._prompts.revision if self._prompts is not None else None
        created_at = datetime.now(timezone.utc).isoformat()
        version_id = context.run_id
        path = f"{paper['folder']}/summaries/{version_id}.md"
        metadata_path = f"{paper['folder']}/summaries/{version_id}.json"
        metadata = {
            "id": version_id,
            "document_id": document_id,
            "run_id": context.run_id,
            "path": path,
            "created_at": created_at,
            "prompt_revision": revision,
            "review_summary": review_summary,
            "citation_count": len(citations),
            "status": "reviewed",
        }
        saved = self._workspace.write_file(
            path,
            content + "\n",
            tags=["paper", f"paper:{document_id}", "summary-version"],
        )
        canonical = self._workspace.write_file(
            str(paper["summary_path"]),
            content + "\n",
            tags=["paper", f"paper:{document_id}", "summary"],
        )
        metadata["canonical_path"] = canonical.path
        self._workspace.write_file(
            metadata_path,
            metadata,
            tags=["paper", f"paper:{document_id}", "summary-version-metadata"],
        )
        self._append_workspace_receipt(context, saved, "Created")
        self._append_workspace_receipt(context, canonical, "Updated")
        self._record_paper_activity(
            context,
            document,
            "summary_saved",
            path=canonical.path,
            version_path=saved.path,
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
        relative_path = (
            f"runs/{context.run_id}/paper-summary/{document.id}/checkpoint.md"
        )
        action = str(arguments["action"])
        async with self._paper_summary_lock:
            try:
                checkpoint = self._storage.read_text(
                    self._settings.artifacts_dir,
                    relative_path,
                    allowed_suffixes={".md"},
                )
            except FileNotFoundError:
                checkpoint = ""

            if action == "append":
                self._require_paper_read(context, document_id)
                content = _nullable_tool_string(arguments.get("content"), "content")
                if content is None:
                    raise ValueError("content is required when appending a summary checkpoint.")
                separator = "\n\n" if checkpoint else ""
                updated = checkpoint.rstrip() + separator + content.strip() + "\n"
                saved = self._storage.write_text(
                    self._settings.artifacts_dir,
                    relative_path,
                    updated,
                )
                persisted = self._storage.read_text(
                    self._settings.artifacts_dir,
                    saved.relative_path,
                    allowed_suffixes={".md"},
                )
                if persisted != updated:
                    raise RuntimeError("Paper summary checkpoint verification failed.")
                state = self._paper_summary_state(context, document_id)
                pending = state.pop("pending_checkpoint", None)
                state["checkpoint_path"] = saved.relative_path
                state["checkpoint_size_characters"] = len(persisted)
                final_checkpoint = (
                    persisted
                    if isinstance(pending, dict)
                    and (
                        not pending.get("has_more")
                        or int(pending.get("batch") or 0) >= 5
                    )
                    else None
                )
                return {
                    "status": "appended",
                    "checkpoint_path": saved.relative_path,
                    "size_characters": len(persisted),
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
                    "final_checkpoint": final_checkpoint,
                    "instruction": (
                        "The checkpoint write was verified. Draft directly from final_checkpoint "
                        "without rereading it."
                        if final_checkpoint is not None
                        else "The checkpoint write was verified. The previous raw batch is now "
                        "discardable; read the next overlapping batch if more paper content remains."
                    ),
                }

            if action == "read":
                offset = int(arguments.get("offset") or 0)
                limit = int(arguments.get("limit") or 8000)
                content = checkpoint[offset : offset + limit]
                return {
                    "status": "available" if checkpoint else "empty",
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
                }

        raise ValueError(f"Unknown paper summary checkpoint action '{action}'.")

    async def bound_tool_result(
        self,
        catalog_id: str,
        result: Any,
        context: ScholarWeaveContext,
        *,
        max_tokens: int | None = None,
    ) -> Any:
        """Return a model-safe result, persisting oversized payloads behind a result reference."""
        if isinstance(result, dict) and result.get("result_ref") and result.get("truncated"):
            return result
        if catalog_id == "tool.results.read":
            return result
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        limit = (max_tokens or self._settings.tool_result_max_tokens) * 4
        if len(serialized) <= limit:
            return result
        relative_path = (
            f"runs/{context.run_id}/tool-results/{uuid.uuid4().hex}.json"
        )
        stored = self._storage.write_text(
            self._settings.artifacts_dir,
            relative_path,
            serialized,
        )
        await context.emit(
            "tool.result.stored",
            {
                "catalog_id": catalog_id,
                "result_ref": stored.relative_path,
                "size_bytes": stored.size_bytes,
            },
        )
        return {
            "truncated": True,
            "result_ref": stored.relative_path,
            "size_bytes": stored.size_bytes,
            "preview": serialized[:limit],
            "instruction": "Use read_tool_result with this result_ref for targeted slices.",
        }

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
        content = self._storage.read_artifact(result_ref).decode("utf-8")
        return {
            "result_ref": result_ref,
            "offset": offset,
            "content": content[offset : offset + limit],
            "has_more": offset + limit < len(content),
            "next_offset": min(len(content), offset + limit),
            "size_characters": len(content),
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
        start = arguments.get("start")
        limit = int(arguments["limit"])
        if action == "pages":
            return self._read_paper_pages(
                {
                    "document_id": document_id,
                    "start_page": max(1, int(start or 1)),
                    "limit": limit,
                },
                context,
            )
        if action == "chunks":
            return self._read_document_chunks(
                {
                    "document_id": document_id,
                    "start": max(0, int(start or 0)),
                    "limit": limit,
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
        batch_counts = context.metadata.setdefault("_paper_summary_read_batches", {})
        if not isinstance(batch_counts, dict):
            batch_counts = {}
            context.metadata["_paper_summary_read_batches"] = batch_counts
        completed = int(batch_counts.get(document_id, 0))
        delegated = {
            "document_id": arguments["document_id"],
            "action": action,
            "start": arguments.get("start"),
            "limit": 10 if completed == 0 or action in {"inspect", "prepare"} else 11,
        }
        if action in {"inspect", "prepare"}:
            return await self._read_research_paper(delegated, context)

        async with self._paper_summary_lock:
            state = self._paper_summary_state(context, document_id)
            pending = state.get("pending_checkpoint")
            if isinstance(pending, dict):
                raise ValueError(
                    "The previous paper-summary batch must be appended to "
                    f"{pending['checkpoint_path']} before another batch can be read."
                )
            if completed >= 5:
                raise ValueError(
                    "Paper summary reading is capped at five overlapping page or chunk batches."
                )
            result = await self._read_research_paper(delegated, context)
            completed += 1
            batch_counts[document_id] = completed
            if isinstance(result, dict):
                coverage = self._paper_summary_coverage(action, delegated, result)
                next_start = self._paper_summary_next_start(action, result)
                checkpoint_path = (
                    f"runs/{context.run_id}/paper-summary/{document_id}/checkpoint.md"
                )
                state["pending_checkpoint"] = {
                    "batch": completed,
                    "coverage": coverage,
                    "has_more": bool(result.get("has_more")),
                    "next_start": next_start,
                    "checkpoint_path": checkpoint_path,
                }
                return {
                    **result,
                    "next_start": next_start,
                    "summary_batch": completed,
                    "summary_batches_remaining": 5 - completed,
                    "checkpoint_required": True,
                    "checkpoint_path": checkpoint_path,
                    "coverage": coverage,
                    "instruction": (
                        "You must append your understanding of this batch to the checkpoint "
                        "before any next paper-summary batch. Parallel batch reads are rejected."
                    ),
                }
            return result

    @staticmethod
    def _paper_summary_next_start(action: str, result: dict[str, Any]) -> int | None:
        next_start = result.get("next_page", result.get("next_start"))
        if next_start is None:
            return None
        value = int(next_start)
        if action == "pages":
            return max(1, value - 1)
        if action == "chunks":
            return max(0, value - 1)
        return value

    @staticmethod
    def _paper_summary_state(
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
                "offset": 0,
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
        reusable = len(content.strip()) >= 200
        result["summary_check"] = {
            "status": "reusable" if reusable else "incomplete",
            "document_id": document_id,
            "needs_regeneration": not reusable,
            "reason": (
                "Canonical summary contains substantive content."
                if reusable
                else "Canonical summary is empty, still a template, or lacks substantive content."
            ),
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
        details = self._documents.get_document_details(document_id)
        if details is None:
            raise ValueError("Paper was not found.")
        document, artifacts, chunks = details
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
        chunk_chars = sum(len(chunk.text) for chunk in chunks)
        readable = (
            document.status == "ready"
            and manifest_artifact is not None
            and bool(chunks)
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
                "chunk_count": len(chunks),
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
        details = self._documents.get_document_details(document_id)
        if details is None:
            raise ValueError("Paper was not found.")
        document, artifacts, chunks = details
        manifest_artifact = next(
            (artifact for artifact in artifacts if artifact.kind == "extracted_manifest"),
            None,
        )
        if document.status != "ready" or manifest_artifact is None or not chunks:
            raise ValueError(
                "Paper has no readable extracted content. Inspect and ingest it before reading."
            )
        pages = manifest_pages(self._documents.artifact_content(manifest_artifact))
        start_page = int(arguments["start_page"])
        limit = int(arguments["limit"])
        available = [
            page for page in pages if int(page.get("page") or 0) >= start_page
        ]
        selected = available[:limit]
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
        self._record_paper_activity(context, document, "read")
        next_page = int(selected[-1]["page"]) + 1
        return {
            "document_id": document_id,
            "title": document.title,
            "page_count": len(pages),
            "has_more": len(available) > len(selected),
            "next_page": next_page,
            "pages": [
                {
                    "page_number": int(page["page"]),
                    "citation": str(page.get("citation") or f"p.{int(page['page'])}"),
                    "text": str(page.get("text") or ""),
                }
                for page in selected
            ],
        }

    def _read_document_chunks(
        self,
        arguments: dict[str, Any],
        context: ScholarWeaveContext,
    ) -> dict[str, Any]:
        start = int(arguments.get("start") or 0)
        limit = int(arguments.get("limit") or 20)
        document_id = str(arguments["document_id"])
        document = self._documents.get_document(document_id)
        if document is None:
            raise ValueError("Paper was not found.")
        chunks = self._retrieval.fetch_document_chunks(document_id)
        if document.status != "ready" or not chunks:
            raise ValueError(
                "Paper has no readable extracted content. Inspect and ingest it before reading."
            )
        selected = chunks[start : start + limit]
        if not selected:
            return {
                "document_id": document_id,
                "title": document.title,
                "chunk_count": len(chunks),
                "start": start,
                "has_more": False,
                "next_start": None,
                "chunks": [],
                "warning": "The requested chunk is beyond the end of this paper.",
            }
        self._record_paper_activity(context, document, "read")
        next_start = start + len(selected)
        return {
            "document_id": document_id,
            "title": document.title,
            "chunk_count": len(chunks),
            "start": start,
            "has_more": next_start < len(chunks),
            "next_start": next_start,
            "chunks": [
                {
                    "chunk_id": chunk.id,
                    "chunk_index": chunk.chunk_index,
                    "section_title": chunk.section_title,
                    "citation": chunk.citation,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "text": chunk.text,
                }
                for chunk in selected
            ],
        }

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
                "path": document.path,
                "name": document.note_name or document.paper_name or document.name,
                "kind": document.kind,
                "tags": list(document.tags),
                "modified_at": document.modified_at.isoformat(),
            }
            for document in self._workspace.search(
                query=arguments.get("query"),
                kinds=list(arguments["kinds"]),
                tags=list(arguments["tags"]),
                limit=int(arguments["limit"]),
                offset=int(arguments["offset"]),
            )
        ]

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
        if entry not in activity:
            activity.append(entry)
            if self._runs is not None and self._runs.exists(context.run_id):
                self._runs.update_runtime_metadata(
                    context.run_id,
                    _persistable_tool_metadata(context.metadata),
                )

    @staticmethod
    def _require_paper_read(
        context: ScholarWeaveContext,
        document_id: str,
    ) -> None:
        activity = context.metadata.get("paper_activity")
        if isinstance(activity, list) and any(
            isinstance(item, dict)
            and item.get("document_id") == document_id
            and item.get("action") == "read"
            for item in activity
        ):
            return
        raise ValueError(
            "Paper summaries and notes can only be saved after extracted pages or chunks "
            "have been read in the active run."
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


_SAFE_READ_PREFIXES = (
    "research.sources.search",
    "research.library.search",
    "research.paper.read",
    "research.web.read",
    "research.notes.search",
    "research.notes.read",
    "tool.results.read",
    "work.plan.read",
)


def _is_safe_read(catalog_id: str) -> bool:
    return any(catalog_id.startswith(prefix) for prefix in _SAFE_READ_PREFIXES)
