from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from typing import Any

from agents import AgentsException, ModelBehaviorError, ModelSettings, ModelTracing
from agents.run_config import CallModelData, ModelInputData
from openai import OpenAIError

from backend.core.config import Settings
from backend.prompting.registry import PromptRegistry
from backend.agents.context import ScholarWeaveContext, unwrap_scholar_context
from backend.utils import to_jsonable
from backend.conversations.steering import (
    SteeringInbox,
    steering_message_ids,
    strip_steering_markers,
)

_CHECKPOINTS_KEY = "context_checkpoints"
_COMPACTION_STATES_KEY = "_context_compaction_states"
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


def create_context_budget_filter(
    settings: Settings,
    context_window_tokens_by_agent: dict[int, int] | None = None,
    agent_keys_by_agent: dict[int, str] | None = None,
    prompt_registry: PromptRegistry | None = None,
) -> Callable[[CallModelData[Any]], Any]:
    async def compact_if_needed(data: CallModelData[Any]) -> ModelInputData:
        context_window_tokens = (
            (context_window_tokens_by_agent or {}).get(id(data.agent))
            or settings.agent_context_window_tokens
        )
        high_water_tokens = int(
            context_window_tokens * settings.agent_context_high_water_ratio
        )
        high_water_chars = high_water_tokens * 4
        target_tokens = _adaptive_target_tokens(
            high_water_tokens,
            settings.agent_context_compaction_target_tokens,
        )
        target_chars = target_tokens * 4
        tool_result_tokens = min(
            settings.tool_result_max_tokens,
            max(512, context_window_tokens // 4),
        )
        context = _scholar_context(data.context)
        present_steering_ids = steering_message_ids(data.model_data.input)
        clean_input = strip_steering_markers(data.model_data.input)
        model_data = (
            ModelInputData(
                input=clean_input,
                instructions=data.model_data.instructions,
            )
            if clean_input is not data.model_data.input
            else data.model_data
        )
        source_input = model_data.input
        agent_key: str | None = None
        state_key: str | None = None
        if context is not None:
            agent_key = (
                (agent_keys_by_agent or {}).get(id(data.agent))
                or str(getattr(data.agent, "name", type(data.agent).__name__))
            )
            model_data, state_key = _restore_compacted_input(
                model_data,
                context,
                agent_key,
            )
            checkpointed_input = _replace_checkpointed_paper_reads(model_data.input)
            if checkpointed_input is not model_data.input:
                model_data = ModelInputData(
                    input=checkpointed_input,
                    instructions=model_data.instructions,
                )
        bounded_input = (
            await _bound_tool_output_items(
                model_data.input,
                context,
                tool_result_tokens,
            )
            if context is not None
            else model_data.input
        )
        if bounded_input is not model_data.input:
            model_data = ModelInputData(
                input=bounded_input,
                instructions=model_data.instructions,
            )
        input_chars = _serialized_characters(model_data.input)
        total_chars = input_chars + len(model_data.instructions or "")
        if total_chars < high_water_chars:
            return await _apply_steering(
                model_data,
                context,
                present_message_ids=present_steering_ids,
            )

        if context is None:
            return model_data
        checkpoint = _build_checkpoint(
            agent_name=data.agent.name,
            items=model_data.input,
            metadata=context.metadata,
            receipts=context.receipts,
            estimated_tokens=math.ceil(total_chars / 4),
        )
        summary_max_tokens = max(256, min(4_096, target_tokens // 4))
        checkpoint_overhead = _checkpoint_message_characters(checkpoint)
        remaining_chars = max(
            0,
            target_chars
            - len(model_data.instructions or "")
            - checkpoint_overhead
            - summary_max_tokens * 4,
        )
        discarded_items, recent_items, preserved_user_messages = (
            _split_with_verbatim_user_messages(model_data.input, remaining_chars)
        )
        await context.emit(
            "context.compaction_started",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "agent_name": data.agent.name,
                "estimated_tokens_before": checkpoint["estimated_tokens_before"],
                "context_window_tokens": context_window_tokens,
                "target_tokens": target_tokens,
                "discarded_item_count": (
                    len(discarded_items) - len(preserved_user_messages)
                ),
                "preserved_user_message_count": len(preserved_user_messages),
            },
        )
        model_summary, summary_method = await _summarize_discarded_history(
            data,
            discarded_items,
            checkpoint,
            max_tokens=summary_max_tokens,
            context=context,
            prompt_registry=prompt_registry,
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
        checkpoint_message = {
            "role": "user",
            "content": (
                "[ScholarWeave context checkpoint]\n"
                "Continue from this rolling summary and structured checkpoint. Treat omitted raw "
                "tool output as available only through its result_ref and the targeted result "
                "reader. Do not repeat completed work or invent omitted details.\n\n"
                + json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
            ),
        }
        compacted_input = [
            checkpoint_message,
            *preserved_user_messages,
            *recent_items,
        ]
        if agent_key is not None:
            _store_compaction_state(
                context,
                agent_key,
                state_key=state_key,
                source_input=source_input,
                compacted_input=compacted_input,
            )
        compacted_tokens = math.ceil(
            (
                len(model_data.instructions or "")
                + _serialized_characters(compacted_input)
            )
            / 4
        )
        await context.emit(
            "context.compacted",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "agent_name": data.agent.name,
                "estimated_tokens_before": checkpoint["estimated_tokens_before"],
                "estimated_tokens_after": compacted_tokens,
                "context_window_tokens": context_window_tokens,
                "target_tokens": target_tokens,
                "discarded_item_count": (
                    len(discarded_items) - len(preserved_user_messages)
                ),
                "preserved_user_message_count": len(preserved_user_messages),
                "summary_method": summary_method,
                "storage": checkpoint.get("storage"),
            },
        )
        return await _apply_steering(
            ModelInputData(
                input=compacted_input,
                instructions=model_data.instructions,
            ),
            context,
            present_message_ids=present_steering_ids,
        )

    return compact_if_needed


async def _apply_steering(
    model_data: ModelInputData,
    context: ScholarWeaveContext | None,
    *,
    present_message_ids: set[str],
) -> ModelInputData:
    if context is None:
        return model_data
    inbox = context.metadata.get("_steering_inbox")
    if not isinstance(inbox, SteeringInbox):
        return model_data
    return await inbox.apply(
        model_data,
        context,
        present_message_ids=present_message_ids,
    )


def _restore_compacted_input(
    model_data: ModelInputData,
    context: ScholarWeaveContext,
    agent_key: str,
) -> tuple[ModelInputData, str | None]:
    states = context.metadata.get(_COMPACTION_STATES_KEY)
    agent_states = states.get(agent_key) if isinstance(states, dict) else None
    if not isinstance(agent_states, list):
        return model_data, None
    current = model_data.input
    matches = [
        state
        for state in agent_states
        if isinstance(state, dict)
        and isinstance(state.get("source_input"), list)
        and len(current) >= len(state["source_input"])
        and current[: len(state["source_input"])] == state["source_input"]
    ]
    state = max(
        matches,
        key=lambda item: len(item["source_input"]),
        default=None,
    )
    compacted = state.get("compacted_input") if isinstance(state, dict) else None
    source = state.get("source_input") if isinstance(state, dict) else None
    if not isinstance(compacted, list) or not isinstance(source, list):
        return model_data, None
    new_items = current[len(source) :]
    return (
        ModelInputData(
            input=[*compacted, *new_items],
            instructions=model_data.instructions,
        ),
        state.get("state_id") if isinstance(state.get("state_id"), str) else None,
    )


def _store_compaction_state(
    context: ScholarWeaveContext,
    agent_key: str,
    *,
    state_key: str | None,
    source_input: list[Any],
    compacted_input: list[Any],
) -> None:
    states = context.metadata.setdefault(_COMPACTION_STATES_KEY, {})
    if not isinstance(states, dict):
        states = {}
        context.metadata[_COMPACTION_STATES_KEY] = states
    agent_states = states.setdefault(agent_key, [])
    if not isinstance(agent_states, list):
        agent_states = []
        states[agent_key] = agent_states
    previous = next(
        (
            state
            for state in agent_states
            if isinstance(state, dict) and state.get("state_id") == state_key
        ),
        None,
    )
    replacement = {
        "state_id": state_key or str(uuid.uuid4()),
        "source_input": source_input,
        "compacted_input": compacted_input,
        "compaction_count": (
            int(previous.get("compaction_count", 0)) + 1
            if isinstance(previous, dict)
            else 1
        ),
    }
    if previous is None:
        agent_states.append(replacement)
    else:
        agent_states[agent_states.index(previous)] = replacement
    del agent_states[:-8]


def _adaptive_target_tokens(high_water_tokens: int, configured_floor: int) -> int:
    headroom = max(256, high_water_tokens // 10)
    upper_bound = max(512, high_water_tokens - headroom)
    dynamic_target = int(high_water_tokens * 0.65)
    return min(
        upper_bound,
        max(512, min(configured_floor, upper_bound), dynamic_target),
    )


def _checkpoint_message_characters(checkpoint: dict[str, Any]) -> int:
    return _serialized_characters(
        [
            {
                "role": "user",
                "content": (
                    "[ScholarWeave context checkpoint]\n"
                    + json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
                ),
            }
        ]
    )


async def _summarize_discarded_history(
    data: CallModelData[Any],
    discarded_items: list[Any],
    checkpoint: dict[str, Any],
    *,
    max_tokens: int,
    context: ScholarWeaveContext,
    prompt_registry: PromptRegistry | None,
) -> tuple[str | None, str]:
    if not discarded_items:
        return None, "structured"
    model = getattr(data.agent, "model", None)
    get_response = getattr(model, "get_response", None)
    if not callable(get_response):
        return None, "structured"
    summary_input = {
        "history": to_jsonable(discarded_items),
        "structured_checkpoint": checkpoint,
    }
    try:
        response = await get_response(
            system_instructions=(
                prompt_registry.render("context-compaction")
                if prompt_registry is not None
                else (
                    "Summarize prior agent history for loss-minimized continuation. Preserve the "
                    "user objective, decisions, completed work, verified findings, citations and "
                    "result_ref values, unresolved questions, blockers, and the exact next action. "
                    "Omit verbose tool payloads and internal repetition. Return only the "
                    "continuation summary."
                )
            ),
            input=[
                {
                    "role": "user",
                    "content": json.dumps(
                        summary_input,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            ],
            model_settings=ModelSettings(
                max_tokens=max_tokens,
                include_usage=True,
            ),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    except (AgentsException, OpenAIError) as exc:
        await context.emit(
            "context.compaction_failed",
            {
                "agent_name": data.agent.name,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return None, "structured_fallback"
    summary = _model_response_text(response)
    if not summary:
        error = ModelBehaviorError("The context summarizer returned no text.")
        await context.emit(
            "context.compaction_failed",
            {
                "agent_name": data.agent.name,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        return None, "structured_fallback"
    return summary, "model"


def _model_response_text(response: Any) -> str:
    texts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "output_text" and isinstance(value.get("text"), str):
                texts.append(value["text"])
                return
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(to_jsonable(getattr(response, "output", response)))
    return "\n".join(text.strip() for text in texts if text.strip()).strip()


async def _bound_tool_output_items(
    items: list[Any],
    context: ScholarWeaveContext,
    max_tokens: int,
) -> list[Any]:
    max_characters = max_tokens * 4
    bound_result = getattr(context.tool_runtime, "bound_tool_result", None)
    if not callable(bound_result):
        return items
    changed = False
    bounded_items: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            bounded_items.append(item)
            continue
        field = _tool_output_field(item)
        output = item.get(field) if field else None
        if (
            field is None
            or _serialized_characters(output) <= max_characters
            or _is_bounded_result(output)
        ):
            bounded_items.append(item)
            continue
        catalog_id = str(item.get("name") or item.get("tool_name") or "sdk.tool_output")
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


def _replace_checkpointed_paper_reads(items: list[Any]) -> list[Any]:
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
            and _checkpoint_append_succeeded(item.get("output"))
        ):
            latest_successful_append = index
            latest_append_call_id = call_id
            latest_append_result = _tool_output_object(item.get("output"))

    if latest_successful_append < 0:
        return items

    changed = False
    replaced: list[Any] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or index > latest_successful_append:
            replaced.append(item)
            continue
        call_id = item.get("call_id")
        name, arguments = (
            calls.get(call_id, ("", {})) if isinstance(call_id, str) else ("", {})
        )
        if (
            item.get("type") == "function_call"
            and call_id == latest_append_call_id
        ):
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


def _checkpoint_append_succeeded(value: Any) -> bool:
    return _tool_output_object(value).get("status") == "appended"


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


def _scholar_context(value: Any) -> ScholarWeaveContext | None:
    if value is None:
        return None
    try:
        return unwrap_scholar_context(value)
    except TypeError:
        return None


def _build_checkpoint(
    *,
    agent_name: str,
    items: list[Any],
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
    items: list[Any],
    budget_chars: int,
) -> tuple[list[Any], list[Any], list[Any]]:
    adjusted_budget = budget_chars
    previous_discarded_count = -1
    discarded: list[Any] = []
    recent: list[Any] = []
    preserved: list[Any] = []
    while previous_discarded_count != len(discarded):
        previous_discarded_count = len(discarded)
        discarded, recent = _split_recent_history(items, adjusted_budget)
        preserved = _verbatim_user_messages(discarded)
        adjusted_budget = max(
            0,
            budget_chars - _serialized_characters(preserved),
        )
    return discarded, recent, preserved


def _verbatim_user_messages(items: list[Any]) -> list[Any]:
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("role") == "user"
        and not (
            isinstance(item.get("content"), str)
            and item["content"].startswith("[ScholarWeave context checkpoint]")
        )
    ]


def _split_recent_history(
    items: list[Any],
    budget_chars: int,
) -> tuple[list[Any], list[Any]]:
    chunks = _history_chunks(items)
    selected_chunks: list[list[Any]] = []
    used = 0
    selected_count = 0
    for chunk in reversed(chunks):
        chunk_chars = _serialized_characters(chunk)
        if used + chunk_chars > budget_chars:
            break
        selected_chunks.append(chunk)
        selected_count += len(chunk)
        used += chunk_chars
    selected_chunks.reverse()
    recent = [item for chunk in selected_chunks for item in chunk]
    return items[: len(items) - selected_count], recent


def _history_chunks(items: list[Any]) -> list[list[Any]]:
    chunks: list[list[Any]] = []
    current: list[Any] = []
    pending_calls: set[str] = set()
    for item in items:
        if not current:
            current = [item]
        else:
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
    item_type = str(item.get("type") or "")
    if item_type in {
        "function_call",
        "computer_call",
        "local_shell_call",
        "shell_call",
        "apply_patch_call",
        "custom_tool_call",
    }:
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
        if isinstance(call, dict)
        and isinstance((call_id := call.get("id")), str)
    }


def _completed_tool_call_ids(item: Any) -> set[str]:
    if not isinstance(item, dict):
        return set()
    item_type = str(item.get("type") or "")
    if item_type.endswith("_call_output"):
        call_id = item.get("call_id")
        return {call_id} if isinstance(call_id, str) else set()
    if item.get("role") == "tool":
        call_id = item.get("tool_call_id")
        return {call_id} if isinstance(call_id, str) else set()
    return set()


def _item_type(item: Any) -> str | None:
    if isinstance(item, dict):
        item_type = item.get("type")
    else:
        item_type = getattr(item, "type", None)
    return item_type if isinstance(item_type, str) else None


def _serialized_characters(value: Any) -> int:
    return len(json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":")))


def _excerpt(value: Any, limit: int = 320) -> Any:
    if not isinstance(value, str):
        return value
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)
