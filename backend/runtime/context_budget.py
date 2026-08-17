from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from typing import Any

from agents.run_config import CallModelData, ModelInputData

from backend.core.config import Settings
from backend.runtime.context import ScholarWeaveContext, unwrap_scholar_context
from backend.runtime.lifecycle import finish_agent_invocation, start_agent_invocation
from backend.runtime.serialization import to_jsonable

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


def create_context_budget_filter(
    settings: Settings,
    context_window_tokens_by_agent: dict[int, int] | None = None,
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
        target_tokens = min(
            settings.agent_context_compaction_target_tokens,
            max(512, high_water_tokens // 2),
        )
        target_chars = target_tokens * 4
        tool_result_tokens = min(
            settings.tool_result_max_tokens,
            max(512, context_window_tokens // 4),
        )
        model_data = data.model_data
        context = _scholar_context(data.context)
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
            return model_data

        if context is None:
            return model_data
        checkpoint = _build_checkpoint(
            agent_name=data.agent.name,
            items=model_data.input,
            metadata=context.metadata,
            estimated_tokens=math.ceil(total_chars / 4),
        )
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
                "Continue from this structured checkpoint. Treat omitted raw tool output as "
                "available only through its result_ref and the targeted result reader. Do not "
                "invent omitted details.\n\n"
                + json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"))
            ),
        }
        remaining_chars = max(
            0,
            target_chars
            - len(model_data.instructions or "")
            - _serialized_characters([checkpoint_message]),
        )
        recent_items = _recent_items(model_data.input, remaining_chars)
        compacted_input = [checkpoint_message, *recent_items]
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
                "discarded_item_count": len(model_data.input) - len(recent_items),
                "storage": checkpoint.get("storage"),
            },
        )
        previous_id = await finish_agent_invocation(
            context,
            data.agent.name,
            "superseded",
            reason="context_high_water",
        )
        if previous_id is not None:
            await start_agent_invocation(
                context,
                data.agent.name,
                reason="context_checkpoint_resume",
            )
        return ModelInputData(
            input=compacted_input,
            instructions=model_data.instructions,
        )

    return compact_if_needed


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
        return value.get("truncated") is True and isinstance(value.get("result_ref"), str)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(parsed, dict) and _is_bounded_result(parsed)
    return False


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
    plan = metadata.get("extended_work_plan")
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
    notes = metadata.get("extended_work_notes")
    if isinstance(notes, dict):
        state["saved_notes"] = [
            {
                "note_id": note.get("id"),
                "task_id": note.get("task_id"),
                "title": note.get("title"),
                "summary": _excerpt(note.get("summary")),
                "sources": list(note.get("sources", []))[:20]
                if isinstance(note.get("sources"), list)
                else [],
            }
            for note in notes.values()
            if isinstance(note, dict)
        ][:20]
    priorities = metadata.get("extended_work_priorities")
    if isinstance(priorities, list):
        state["priority_decisions"] = to_jsonable(priorities[-10:])
    return state


def _recent_items(items: list[Any], budget_chars: int) -> list[Any]:
    selected: list[Any] = []
    used = 0
    for item in reversed(items):
        item_chars = _serialized_characters([item])
        if selected and used + item_chars > budget_chars:
            break
        if item_chars > budget_chars:
            continue
        selected.append(item)
        used += item_chars
    selected.reverse()
    while selected and _item_type(selected[0]) in {
        "function_call_output",
        "computer_call_output",
        "local_shell_call_output",
    }:
        selected.pop(0)
    return selected


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
