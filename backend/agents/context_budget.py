"""Deterministic context compaction for the native agent harness.

Compaction runs before a model call once the prepared request crosses the
configured high-water mark (a ratio of the model's context window).  Because the
harness only re-enters this code after a tool-call round, compaction naturally
happens at tool-call boundaries and never mid-response.

The policy also bounds oversized tool results, replaces superseded paper-summary
batch reads with their checkpoint receipt, and appends queued steering messages.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from openai import OpenAIError

from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import (
    AgentDefinition,
    HarnessError,
    ModelBehaviorError,
    PreparedInput,
    RunInputItems,
    request_parameters,
    serialized_characters,
)
from backend.conversations.steering import (
    SteeringInbox,
    steering_message_ids,
    strip_steering_markers,
)
from backend.core.config import Settings
from backend.prompting.registry import PromptRegistry
from backend.utils import to_jsonable

CHECKPOINT_MESSAGE_PREFIX = "[ScholarWeave context checkpoint]"
_CHECKPOINTS_KEY = "context_checkpoints"
_IMPORTANT_KEYS = {
    "id",
    "document_id",
    "source_id",
    "result_ref",
    "artifact_id",
    "url",
    "citation",
    "score",
    "rank",
    "title",
    "name",
    "status",
    "summary",
    "query",
}
_TEXT_KEYS = {
    "text",
    "content",
    "summary",
    "abstract",
    "snippet",
    "description",
    "caveat",
    "limitation",
    "error",
}
_DEFAULT_COMPACTION_INSTRUCTIONS = (
    "Summarize prior agent history for loss-minimized continuation. Preserve the user "
    "objective, decisions, completed work, verified findings, citations and result_ref "
    "values, unresolved questions, blockers, and the exact next action. Omit verbose tool "
    "payloads and internal repetition. Return only the continuation summary."
)


class ContextBudgetPolicy:
    """Prepares each model request: bounds tool output, compacts, applies steering."""

    def __init__(
        self,
        settings: Settings,
        *,
        context_window_tokens_by_agent: dict[str, int] | None = None,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._windows = dict(context_window_tokens_by_agent or {})
        self._prompts = prompt_registry

    def context_window_tokens(self, agent: AgentDefinition) -> int:
        return (
            self._windows.get(agent.id)
            or agent.binding.context_window_tokens
            or self._settings.agent_context_window_tokens
        )

    async def prepare(
        self,
        agent: AgentDefinition,
        items: RunInputItems,
        instructions: str,
        context: ScholarWeaveContext,
        *,
        turn_index: int,
    ) -> PreparedInput:
        context_window_tokens = self.context_window_tokens(agent)
        high_water_tokens = int(
            context_window_tokens * self._settings.agent_context_high_water_ratio
        )
        target_tokens = _adaptive_target_tokens(
            high_water_tokens,
            self._settings.agent_context_compaction_target_tokens,
        )
        tool_result_tokens = min(
            self._settings.tool_result_max_tokens,
            max(512, context_window_tokens // 4),
        )

        present_steering_ids = steering_message_ids(items)
        prepared = strip_steering_markers(items)
        prepared = _replace_checkpointed_paper_reads(prepared)
        prepared = await _bound_tool_output_items(prepared, context, tool_result_tokens)

        total_chars = serialized_characters(prepared) + len(instructions or "")
        if total_chars < high_water_tokens * 4:
            return PreparedInput(
                items=await _apply_steering(prepared, context, present_steering_ids),
                instructions=instructions,
                working_items=prepared,
            )

        compacted = await self._compact(
            agent,
            prepared,
            instructions,
            context,
            context_window_tokens=context_window_tokens,
            target_tokens=target_tokens,
            total_chars=total_chars,
        )
        return PreparedInput(
            items=await _apply_steering(compacted, context, present_steering_ids),
            instructions=instructions,
            working_items=compacted,
        )

    async def _compact(
        self,
        agent: AgentDefinition,
        items: RunInputItems,
        instructions: str,
        context: ScholarWeaveContext,
        *,
        context_window_tokens: int,
        target_tokens: int,
        total_chars: int,
    ) -> RunInputItems:
        checkpoint = _build_checkpoint(
            agent_name=agent.name,
            items=items,
            metadata=context.metadata,
            receipts=context.receipts,
            estimated_tokens=math.ceil(total_chars / 4),
        )
        summary_max_tokens = max(256, min(4_096, target_tokens // 4))
        remaining_chars = max(
            0,
            target_tokens * 4
            - len(instructions or "")
            - _checkpoint_message_characters(checkpoint)
            - summary_max_tokens * 4,
        )
        discarded, recent, preserved_user_messages = _split_with_verbatim_user_messages(
            items,
            remaining_chars,
        )
        await context.emit(
            "context.compaction_started",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "agent_name": agent.name,
                "estimated_tokens_before": checkpoint["estimated_tokens_before"],
                "context_window_tokens": context_window_tokens,
                "target_tokens": target_tokens,
                "discarded_item_count": len(discarded) - len(preserved_user_messages),
                "preserved_user_message_count": len(preserved_user_messages),
            },
        )
        model_summary, summary_method = await self._summarize_discarded_history(
            agent,
            discarded,
            checkpoint,
            max_tokens=summary_max_tokens,
            context=context,
        )
        if model_summary:
            checkpoint["model_summary"] = model_summary
        checkpoint["summary_method"] = summary_method
        store = getattr(context.tool_runtime, "store_context_checkpoint", None)
        if callable(store):
            checkpoint["storage"] = store(checkpoint, context)
        checkpoints = context.metadata.setdefault(_CHECKPOINTS_KEY, [])
        if not isinstance(checkpoints, list):
            checkpoints = []
            context.metadata[_CHECKPOINTS_KEY] = checkpoints
        checkpoints.append(checkpoint)
        del checkpoints[:-20]

        compacted: RunInputItems = [
            {
                "role": "user",
                "content": (
                    f"{CHECKPOINT_MESSAGE_PREFIX}\n"
                    "Continue from this rolling summary and structured checkpoint. Treat "
                    "omitted raw tool output as available only through its result_ref and "
                    "the targeted result reader. Do not repeat completed work or invent "
                    "omitted details.\n\n"
                    + json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
                ),
            },
            *preserved_user_messages,
            *recent,
        ]
        await context.emit(
            "context.compacted",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "agent_name": agent.name,
                "estimated_tokens_before": checkpoint["estimated_tokens_before"],
                "estimated_tokens_after": math.ceil(
                    (len(instructions or "") + serialized_characters(compacted)) / 4
                ),
                "context_window_tokens": context_window_tokens,
                "target_tokens": target_tokens,
                "discarded_item_count": len(discarded) - len(preserved_user_messages),
                "preserved_user_message_count": len(preserved_user_messages),
                "summary_method": summary_method,
                "storage": checkpoint.get("storage"),
            },
        )
        return compacted

    async def _summarize_discarded_history(
        self,
        agent: AgentDefinition,
        discarded: RunInputItems,
        checkpoint: dict[str, Any],
        *,
        max_tokens: int,
        context: ScholarWeaveContext,
    ) -> tuple[str | None, str]:
        if not discarded:
            return None, "structured"
        summary_agent = AgentDefinition(
            id=f"{agent.id}:compaction",
            name=f"{agent.name} context summarizer",
            instructions=(
                self._prompts.render("context-compaction")
                if self._prompts is not None
                else _DEFAULT_COMPACTION_INSTRUCTIONS
            ),
            binding=agent.binding,
            model_settings=type(agent.model_settings)(max_tokens=max_tokens),
        )
        parameters = request_parameters(
            summary_agent,
            [
                {"role": "system", "content": summary_agent.instructions},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "history": to_jsonable(discarded),
                            "structured_checkpoint": checkpoint,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            [],
            stream=False,
        )
        try:
            response = await agent.binding.client.chat.completions.create(**parameters)
            summary = _response_text(response)
        except (OpenAIError, HarnessError) as exc:
            await context.emit(
                "context.compaction_failed",
                {
                    "agent_name": agent.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return None, "structured_fallback"
        if not summary:
            error = ModelBehaviorError("The context summarizer returned no text.")
            await context.emit(
                "context.compaction_failed",
                {
                    "agent_name": agent.name,
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
            return None, "structured_fallback"
        return summary, "model"


def _response_text(response: Any) -> str:
    raw = response.model_dump() if hasattr(response, "model_dump") else response
    if not isinstance(raw, dict):
        return ""
    texts = [
        str(choice.get("message", {}).get("content") or "").strip()
        for choice in raw.get("choices") or []
        if isinstance(choice, dict)
    ]
    return "\n".join(text for text in texts if text).strip()


async def _apply_steering(
    items: RunInputItems,
    context: ScholarWeaveContext,
    present_message_ids: set[str],
) -> RunInputItems:
    inbox = context.metadata.get("_steering_inbox")
    if not isinstance(inbox, SteeringInbox):
        return items
    return await inbox.apply(items, context, present_message_ids=present_message_ids)


def _adaptive_target_tokens(high_water_tokens: int, configured_floor: int) -> int:
    headroom = max(256, high_water_tokens // 10)
    upper_bound = max(512, high_water_tokens - headroom)
    dynamic_target = int(high_water_tokens * 0.65)
    return min(
        upper_bound,
        max(512, min(configured_floor, upper_bound), dynamic_target),
    )


def _checkpoint_message_characters(checkpoint: dict[str, Any]) -> int:
    return serialized_characters(
        [
            {
                "role": "user",
                "content": (
                    f"{CHECKPOINT_MESSAGE_PREFIX}\n"
                    + json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
                ),
            }
        ]
    )


async def _bound_tool_output_items(
    items: RunInputItems,
    context: ScholarWeaveContext,
    max_tokens: int,
) -> RunInputItems:
    max_characters = max_tokens * 4
    bound_result = getattr(context.tool_runtime, "bound_tool_result", None)
    if not callable(bound_result):
        return items
    changed = False
    bounded_items: RunInputItems = []
    for item in items:
        if not isinstance(item, dict):
            bounded_items.append(item)
            continue
        field = _tool_output_field(item)
        output = item.get(field) if field else None
        if (
            field is None
            or serialized_characters(output) <= max_characters
            or _is_bounded_result(output)
        ):
            bounded_items.append(item)
            continue
        catalog_id = str(item.get("name") or item.get("tool_name") or "tool_output")
        compacted = await bound_result(
            catalog_id,
            output,
            context,
            max_tokens=max_tokens,
        )
        replacement = dict(item)
        replacement[field] = (
            json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
            if isinstance(output, str)
            else compacted
        )
        bounded_items.append(replacement)
        changed = True
    return bounded_items if changed else items


def _tool_output_field(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("type") or "")
    if item_type.endswith("_output") and "output" in item:
        return "output"
    if item.get("role") == "tool" and "content" in item:
        return "content"
    return None


def _is_bounded_result(value: Any) -> bool:
    if isinstance(value, dict):
        has_result_ref = isinstance(value.get("result_ref"), str)
        return has_result_ref and (
            value.get("truncated") is True
            or (
                isinstance(value.get("content"), str)
                and isinstance(value.get("offset"), int)
                and isinstance(value.get("has_more"), bool)
            )
        )
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(parsed, dict) and _is_bounded_result(parsed)
    return False


def _replace_checkpointed_paper_reads(items: RunInputItems) -> RunInputItems:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    latest_successful_append = -1
    latest_append_call_id: str | None = None
    latest_append_result: dict[str, Any] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        call_id = item.get("call_id")
        if item_type == "function_call" and isinstance(call_id, str):
            calls[call_id] = (
                str(item.get("name") or ""),
                _tool_arguments(item.get("arguments")),
            )
            continue
        if item_type != "function_call_output" or not isinstance(call_id, str):
            continue
        name, arguments = calls.get(call_id, ("", {}))
        if (
            name == "paper_summary_checkpoint"
            and arguments.get("action") == "append"
            and _tool_output_object(item.get("output")).get("status") == "appended"
        ):
            latest_successful_append = index
            latest_append_call_id = call_id
            latest_append_result = _tool_output_object(item.get("output"))

    if latest_successful_append < 0:
        return items

    changed = False
    replaced: RunInputItems = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or index > latest_successful_append:
            replaced.append(item)
            continue
        call_id = item.get("call_id")
        name, arguments = (
            calls.get(call_id, ("", {})) if isinstance(call_id, str) else ("", {})
        )
        if item.get("type") == "function_call" and call_id == latest_append_call_id:
            replacement = dict(item)
            replacement["arguments"] = json.dumps(
                {
                    "document_id": arguments.get("document_id"),
                    "action": "append",
                    "content": None,
                    "offset": None,
                    "limit": None,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            replaced.append(replacement)
            changed = True
            continue
        field = _tool_output_field(item)
        replaceable_read = name in {"read_paper_summary_batch", "read_tool_result"} or (
            name == "paper_summary_checkpoint" and arguments.get("action") == "read"
        )
        if field is None or not replaceable_read:
            replaced.append(item)
            continue
        receipt = {
            "status": "replaced_by_summary_checkpoint",
            "checkpoint_path": latest_append_result.get("checkpoint_path"),
            "checkpointed_batch": latest_append_result.get("checkpointed_batch"),
            "coverage": latest_append_result.get("coverage"),
            "instruction": (
                "Raw batch content was removed after its understanding was appended to the "
                "paper-summary checkpoint."
            ),
        }
        replacement = dict(item)
        replacement[field] = (
            json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
            if isinstance(item.get(field), str)
            else receipt
        )
        replaced.append(replacement)
        changed = True
    return replaced if changed else items


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_output_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_checkpoint(
    *,
    agent_name: str,
    items: RunInputItems,
    metadata: dict[str, Any],
    receipts: list[Any],
    estimated_tokens: int,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    references: list[str] = []
    caveats: list[str] = []
    unresolved_questions: list[str] = []
    seen: set[str] = set()
    for item in to_jsonable(items):
        _collect_checkpoint_values(
            item,
            evidence=evidence,
            references=references,
            caveats=caveats,
            unresolved_questions=unresolved_questions,
            seen=seen,
        )
        if len(evidence) >= 40:
            break
    return {
        "checkpoint_id": str(uuid.uuid4()),
        "agent_name": agent_name,
        "estimated_tokens_before": estimated_tokens,
        "evidence": evidence[:40],
        "references": references[:60],
        "caveats": caveats[:20],
        "unresolved_questions": unresolved_questions[:20],
        "activity": [
            {
                "kind": receipt.kind,
                "title": receipt.title,
                "description": receipt.description,
                "href": receipt.href,
                "metadata": to_jsonable(receipt.metadata),
            }
            for receipt in receipts[-50:]
        ],
        "work_state": _work_state(metadata),
    }


def _collect_checkpoint_values(
    value: Any,
    *,
    evidence: list[dict[str, Any]],
    references: list[str],
    caveats: list[str],
    unresolved_questions: list[str],
    seen: set[str],
) -> None:
    if isinstance(value, dict):
        selected: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _IMPORTANT_KEYS and isinstance(item, (str, int, float, bool)):
                selected[str(key)] = _excerpt(item)
            if normalized in {"result_ref", "artifact_id", "url", "citation"} and isinstance(
                item, str
            ):
                _append_unique(references, item)
            if normalized in _TEXT_KEYS and isinstance(item, str):
                excerpt = _excerpt(item)
                if normalized in {"caveat", "limitation", "error"}:
                    _append_unique(caveats, excerpt)
                for line in item.splitlines():
                    if line.strip().endswith("?"):
                        _append_unique(unresolved_questions, _excerpt(line))
        if selected:
            fingerprint = json.dumps(selected, sort_keys=True, ensure_ascii=False)
            if fingerprint not in seen:
                seen.add(fingerprint)
                evidence.append(selected)
        for item in value.values():
            if len(evidence) >= 40:
                return
            _collect_checkpoint_values(
                item,
                evidence=evidence,
                references=references,
                caveats=caveats,
                unresolved_questions=unresolved_questions,
                seen=seen,
            )
    elif isinstance(value, list):
        for item in value:
            if len(evidence) >= 40:
                return
            _collect_checkpoint_values(
                item,
                evidence=evidence,
                references=references,
                caveats=caveats,
                unresolved_questions=unresolved_questions,
                seen=seen,
            )


def _work_state(metadata: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    plan = metadata.get("work_plan")
    if isinstance(plan, list):
        state["plan"] = [
            {
                key: item.get(key)
                for key in ("id", "title", "status", "summary")
                if item.get(key) is not None
            }
            for item in plan
            if isinstance(item, dict)
        ][:10]
    paper_activity = metadata.get("paper_activity")
    if isinstance(paper_activity, list):
        state["paper_activity"] = [
            to_jsonable(item)
            for item in paper_activity[-50:]
            if isinstance(item, dict)
        ]
    return state


def _split_with_verbatim_user_messages(
    items: RunInputItems,
    budget_chars: int,
) -> tuple[RunInputItems, RunInputItems, RunInputItems]:
    adjusted_budget = budget_chars
    previous_discarded_count = -1
    discarded: RunInputItems = []
    recent: RunInputItems = []
    preserved: RunInputItems = []
    while previous_discarded_count != len(discarded):
        previous_discarded_count = len(discarded)
        discarded, recent = _split_recent_history(items, adjusted_budget)
        preserved = _verbatim_user_messages(discarded)
        adjusted_budget = max(0, budget_chars - serialized_characters(preserved))
    return discarded, recent, preserved


def _verbatim_user_messages(items: RunInputItems) -> RunInputItems:
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("role") == "user"
        and not (
            isinstance(item.get("content"), str)
            and item["content"].startswith(CHECKPOINT_MESSAGE_PREFIX)
        )
    ]


def _split_recent_history(
    items: RunInputItems,
    budget_chars: int,
) -> tuple[RunInputItems, RunInputItems]:
    chunks = _history_chunks(items)
    selected_chunks: list[RunInputItems] = []
    used = 0
    selected_count = 0
    for chunk in reversed(chunks):
        chunk_chars = serialized_characters(chunk)
        if used + chunk_chars > budget_chars:
            break
        selected_chunks.append(chunk)
        selected_count += len(chunk)
        used += chunk_chars
    selected_chunks.reverse()
    recent = [item for chunk in selected_chunks for item in chunk]
    return items[: len(items) - selected_count], recent


def _history_chunks(items: RunInputItems) -> list[RunInputItems]:
    """Group items so a tool call is never separated from its output."""
    chunks: list[RunInputItems] = []
    current: RunInputItems = []
    pending_calls: set[str] = set()
    for item in items:
        current.append(item)
        pending_calls.update(_started_tool_call_ids(item))
        pending_calls.difference_update(_completed_tool_call_ids(item))
        if not pending_calls:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def _started_tool_call_ids(item: Any) -> set[str]:
    if not isinstance(item, dict):
        return set()
    if item.get("type") == "function_call":
        call_id = item.get("call_id") or item.get("id")
        return {call_id} if isinstance(call_id, str) else set()
    if item.get("role") != "assistant":
        return set()
    tool_calls = item.get("tool_calls")
    if not isinstance(tool_calls, list):
        return set()
    return {
        call_id
        for call in tool_calls
        if isinstance(call, dict) and isinstance((call_id := call.get("id")), str)
    }


def _completed_tool_call_ids(item: Any) -> set[str]:
    if not isinstance(item, dict):
        return set()
    if str(item.get("type") or "").endswith("_call_output"):
        call_id = item.get("call_id")
        return {call_id} if isinstance(call_id, str) else set()
    if item.get("role") == "tool":
        call_id = item.get("tool_call_id")
        return {call_id} if isinstance(call_id, str) else set()
    return set()


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _excerpt(value: Any, limit: int = 320) -> Any:
    if not isinstance(value, str):
        return value
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"
