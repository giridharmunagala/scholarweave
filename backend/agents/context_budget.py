"""Deterministic context compaction for the native agent harness.

The input budget follows the model window and reserves response capacity. Older
tool payloads are archived before narrative is summarized, and recent complete
rounds and exact constraints are retained even when they exceed the soft target.

The policy also replaces superseded paper-summary batch reads with their checkpoint
receipt and appends queued steering messages. Fresh tool output remains exact unless
hard model-window pressure requires a readable archive and a fitting preview.
"""

from __future__ import annotations

import json
import math
import uuid
from copy import deepcopy
from typing import Any

from openai import OpenAIError

from backend.agents.context import ScholarWeaveContext
from backend.agents.harness import (
    AgentDefinition,
    HarnessError,
    ModelBehaviorError,
    ModelBinding,
    PreparedInput,
    RunPolicyViolation,
    RunInputItems,
    request_parameters,
    serialized_characters,
    to_chat_messages,
    tool_payload,
    usage_from_payload,
    Usage,
)
from backend.conversations.steering import (
    SteeringInbox,
    steering_message_ids,
    strip_steering_markers,
)
from backend.core.config import Settings
from backend.prompting.registry import PromptRegistry
from backend.providers.inference import inference_priority
from backend.providers.reasoning import REASONING_EFFORTS, infer_reasoning_efforts
from backend.utils import to_jsonable

CHECKPOINT_MESSAGE_PREFIX = "[ScholarWeave context checkpoint]"
_CHECKPOINTS_KEY = "context_checkpoints"
_MAX_INLINE_HISTORY_ARCHIVES = 8
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
    "checkpoint_path",
    "next_start",
    "next_offset",
    "complete",
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
_REFERENCE_LIFETIME_DESCRIPTION = (
    "Cache references under conversations/ survive run cleanup until their conversation is "
    "deleted. Legacy or standalone references under runs/, including checkpoint artifacts, "
    "may expire during run cleanup. Determine lifetime from the exact result_ref scope."
)
_REFERENCE_LIFETIME_INSTRUCTIONS = (
    _REFERENCE_LIFETIME_DESCRIPTION + " Read cached tool results and exact history archives "
    "with read_tool_result. "
    "If an archive contains older user instructions, re-read the relevant instructions before "
    "acting on earlier requirements; absence from the checkpoint does not revoke a constraint. "
    "An archive_manifest contains paginated archive references and indexes, not raw history; "
    "use it to locate older evidence without rereading full transcripts. "
    "Only retained text, citations, and readable durable paper/workspace evidence are known facts. "
    "If a reference is unavailable, use permitted durable/source reads or report missing evidence; "
    "never reconstruct omitted details."
)


class _SummaryInputLimitError(HarnessError):
    pass


def model_context_budget(window: int, response_tokens: int | None = None) -> tuple[int, int, int]:
    """Reserve safety first, then output; input includes instructions and tool schemas."""
    safety = math.ceil(window * 0.10)
    response = min(response_tokens or math.ceil(window * 0.25), window - safety - 1)
    return window - safety - response, response, safety


class ContextBudgetPolicy:
    """Prepares each model request: ages old evidence, compacts, applies steering."""

    def __init__(
        self,
        settings: Settings,
        *,
        context_window_tokens_by_agent: dict[str, int] | None = None,
        prompt_registry: PromptRegistry | None = None,
        compaction_model: ModelBinding | None = None,
        compaction_model_error: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._windows = dict(context_window_tokens_by_agent or {})
        self._prompts = prompt_registry
        self._compaction_model = compaction_model
        self._compaction_model_error = compaction_model_error

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
        instructions = "\n\n".join(filter(None, (instructions, _REFERENCE_LIFETIME_INSTRUCTIONS)))
        context_window_tokens = self.context_window_tokens(agent)
        token_ratio = _token_estimate_ratio(agent, context)
        summary_job = bool(context.metadata.get("paper_summary_document_id"))
        safety_tokens = 0
        response_tokens = agent.model_settings.max_tokens or self._settings.agent_context_response_reserve_tokens
        if summary_job:
            _, response_tokens, safety_tokens = model_context_budget(
                context_window_tokens, agent.model_settings.max_tokens,
            )
        present_steering_ids = steering_message_ids(items)
        last_user_index = max(
            (index for index, item in enumerate(items)
             if item.get("role") == "user" and not _is_checkpoint(item)),
            default=-1,
        )
        prepared = [
            item for index, item in enumerate(items)
            if item.get("_scholarweave_internal_continuation") is not True
            or index == last_user_index
        ]
        prepared = _replace_checkpointed_paper_reads(prepared)

        # Apply pending guidance before measuring; it must never escape the budget.
        model_items = await _apply_steering(prepared, context, present_steering_ids)
        if not summary_job:
            protected_tokens = _request_tokens(
                agent, _protected_history_items(model_items), instructions, context,
            )
            available_response_tokens = context_window_tokens - protected_tokens - 256
            response_tokens = min(
                response_tokens,
                available_response_tokens if available_response_tokens > 0
                else self._settings.agent_context_response_reserve_tokens,
            )
        request_limit = context_window_tokens - response_tokens - safety_tokens
        working_limit = request_limit
        high_water_tokens = (
            request_limit if summary_job
            else int(request_limit * self._settings.agent_context_high_water_ratio)
        )
        target_tokens = (
            request_limit if summary_job
            else min(int(request_limit * 0.7), int(high_water_tokens * 0.8))
        )
        unbounded_tokens = _request_tokens(agent, model_items, instructions, context)
        model_items, hard_limit_evictions = await self._page_protected_tool_outputs(
            agent, model_items, instructions, context, request_limit=request_limit,
        )
        prepared = model_items[:len(prepared)]
        steering_delta = model_items[len(prepared):]
        required = _protected_history_items(model_items)
        mandatory_tokens = _request_tokens(agent, required, instructions, context)
        archived_user_ids: set[int] = set()
        if mandatory_tokens > request_limit and _can_read_history(agent, context) and callable(
            getattr(context.tool_runtime, "store_context_history", None)
        ):
            first_user = next(
                (item for item in model_items if item.get("role") == "user" and not _is_checkpoint(item)),
                None,
            )
            latest_user = next(
                (item for item in reversed(model_items)
                 if item.get("role") == "user" and not _is_checkpoint(item)),
                None,
            )
            older_users = [
                item for item in prepared[:_recent_round_start(prepared)]
                if item.get("role") == "user" and not _is_checkpoint(item)
                and item is not first_user and item is not latest_user
                and not steering_message_ids([item])
            ]
            # Only genuine hard-limit pressure permits archiving older user turns.
            # Prefer long historical inputs over short, still-useful requirements.
            for item in sorted(older_users, key=_estimated_tokens, reverse=True):
                archived_user_ids.add(id(item))
                required = _protected_history_items(model_items, archived_user_ids)
                mandatory_tokens = _request_tokens(agent, required, instructions, context)
                if mandatory_tokens <= target_tokens:
                    break
        if mandatory_tokens > request_limit:
            _budget_failure(mandatory_tokens, request_limit, response_tokens)
        before_tokens = _request_tokens(agent, model_items, instructions, context)
        compacted = prepared
        did_compact = False
        tool_payload_evictions = hard_limit_evictions
        needs_reduction = before_tokens > high_water_tokens and any(
            not _is_verbatim_constraint(item)
            for item in prepared
        )
        if needs_reduction:
            fixed_tokens = _request_tokens(agent, steering_delta, instructions, context)
            compacted, older_evictions, did_compact = await self._compact(
                agent, prepared, "", context,
                context_window_tokens=context_window_tokens,
                target_tokens=max(0, int((target_tokens - fixed_tokens) / token_ratio)),
                total_chars=before_tokens * 4,
                archived_user_ids=archived_user_ids,
            )
            tool_payload_evictions += older_evictions
        final_items = strip_steering_markers([*compacted, *steering_delta])
        after_tokens = _request_tokens(agent, final_items, instructions, context)
        if after_tokens > request_limit:
            _budget_failure(after_tokens, request_limit, response_tokens)
        retry_response_tokens = max(0, context_window_tokens - after_tokens - (safety_tokens or 256))
        await context.emit(
            "context.prepared",
            {
                "agent_name": agent.name,
                "turn_index": turn_index,
                "estimated_tokens_before": unbounded_tokens,
                "estimated_input_tokens": after_tokens,
                "token_estimate_ratio": token_ratio,
                "tool_schema_tokens": _estimated_tokens([
                    tool_payload(tool) for tool in agent.enabled_tools(context)
                ]),
                "instruction_tokens": _estimated_tokens(instructions),
                "request_overhead_tokens": _request_tokens(agent, [], instructions, context),
                "response_headroom_tokens": response_tokens,
                "safety_headroom_tokens": safety_tokens,
                "response_retry_max_tokens": retry_response_tokens,
                "working_context_tokens": working_limit,
                "input_budget_tokens": request_limit,
                "context_window_tokens": context_window_tokens,
                "compacted": did_compact,
                "tool_payload_evictions": tool_payload_evictions,
                "item_count": len(final_items),
            },
        )
        return PreparedInput(
            items=final_items,
            instructions=instructions,
            working_items=compacted,
            response_max_tokens=response_tokens,
            response_retry_max_tokens=retry_response_tokens,
            estimated_input_tokens=after_tokens,
            context_window_tokens=context_window_tokens,
            safety_headroom_tokens=safety_tokens or 256,
        )

    async def _page_protected_tool_outputs(
        self,
        agent: AgentDefinition,
        items: RunInputItems,
        instructions: str,
        context: ScholarWeaveContext,
        *,
        request_limit: int,
    ) -> tuple[RunInputItems, int]:
        protected = _protected_history_items(items)
        if _request_tokens(agent, protected, instructions, context) <= request_limit:
            return items, 0
        store = getattr(context.tool_runtime, "store_context_history", None)
        can_read_archive = _can_read_history(agent, context)
        constrain = getattr(context.tool_runtime, "constrain_paper_summary_batch", None)
        if not callable(store) or (not can_read_archive and not callable(constrain)):
            return items, 0
        protected_ids = {id(item) for item in protected}
        candidates = [
            index for index, item in enumerate(items)
            if id(item) in protected_ids and _tool_output_field(item) is not None
        ]
        candidates.sort(key=lambda index: _estimated_tokens(items[index]), reverse=True)
        paged = list(items)
        count = 0
        for index in candidates:
            if _request_tokens(
                agent, _protected_history_items(paged), instructions, context,
            ) <= request_limit:
                break
            item = items[index]
            field = _tool_output_field(item)
            assert field is not None
            original = _tool_output_object(item[field])
            if not can_read_archive and original.get("checkpoint_required") is not True:
                continue
            archive = store([item], context)
            if (
                not isinstance(archive, dict)
                or not isinstance(archive.get("result_ref"), str)
                or not archive["result_ref"]
            ):
                raise HarnessError("Context history storage returned no readable result_ref.")
            receipt: dict[str, Any] = {
                "status": "archived_context_tool_output",
                "result_ref": archive["result_ref"],
                "history_item_index": 0,
                "instruction": (
                    "The complete tool output is archived, not discarded. Use read_tool_result "
                    "for exact targeted slices before acting on omitted evidence."
                ),
            }
            if original.get("checkpoint_required") is True:
                receipt["checkpoint_required"] = True
                receipt["checkpoint_available"] = False
                receipt["instruction"] += (
                    " This archived source is not checkpointable. Read a smaller paper batch "
                    "before appending evidence or saving a summary."
                )
            location = next(
                (entry for entry in archive.get("index", []) if entry.get("item") == 0), None,
            )
            if location is not None:
                receipt.update(offset=location["offset"], length=location["length"])
            elif archive.get("index_ref"):
                receipt["index_ref"] = archive["index_ref"]
            preview = item[field] if isinstance(item[field], str) else json.dumps(
                item[field], ensure_ascii=False, separators=(",", ":"),
            )

            def encode_output(payload: dict[str, Any]) -> dict[str, Any]:
                return {
                    **item,
                    field: (
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                        if isinstance(item[field], str) else payload
                    ),
                }

            def replacement(length: int) -> dict[str, Any]:
                return encode_output({**receipt, "preview": preview[:length]} if length else receipt)

            constrained = False
            pending_result = original
            if original.get("checkpoint_required") is True and callable(constrain):
                paged[index] = {**item, field: ""}
                available_chars = max(0, int((
                    request_limit - 256 - _request_tokens(agent, paged, instructions, context)
                ) * 4 / _token_estimate_ratio(agent, context)))
                while available_chars > 0:
                    try:
                        reduced = constrain(pending_result, context, max_chars=available_chars)
                    except ValueError:
                        break
                    if not isinstance(reduced, dict):
                        break
                    pending_result = reduced
                    reduced = {
                        **reduced,
                        "context_archive_ref": archive["result_ref"],
                        "context_archive_instruction": (
                            "Only the returned batch coverage may be checkpointed. The archive "
                            "contains additional source that has not been read in this batch."
                        ),
                    }
                    paged[index] = encode_output(reduced)
                    if _request_tokens(agent, paged, instructions, context) <= request_limit - 256:
                        constrained = True
                        break
                    available_chars //= 2
            if constrained:
                count += 1
                await context.emit("tool.result.stored", {
                    "agent_name": agent.name,
                    "tool_call_id": _item_call_id(item),
                    "result_ref": archive["result_ref"],
                    "reason": "context_sized_paper_batch",
                })
                continue
            if not can_read_archive:
                mark_unavailable = getattr(
                    context.tool_runtime, "mark_paper_summary_batch_unavailable", None,
                )
                if callable(mark_unavailable):
                    mark_unavailable(pending_result, context)
                paged[index] = item
                continue
            paged[index] = replacement(0)
            if _estimated_tokens(paged[index]) >= _estimated_tokens(item):
                paged[index] = item
                continue
            low, high = 0, min(len(preview), max(0, request_limit) * 4)
            while low < high:
                middle = (low + high + 1) // 2
                paged[index] = replacement(middle)
                if _request_tokens(agent, paged, instructions, context) <= request_limit - 256:
                    low = middle
                else:
                    high = middle - 1
            paged[index] = replacement(low)
            if original.get("checkpoint_required") is True:
                mark_unavailable = getattr(
                    context.tool_runtime, "mark_paper_summary_batch_unavailable", None,
                )
                if callable(mark_unavailable):
                    mark_unavailable(pending_result, context)
            count += 1
            await context.emit("tool.result.stored", {
                "agent_name": agent.name,
                "tool_call_id": _item_call_id(item),
                "result_ref": archive["result_ref"],
                "reason": "context_limit",
            })
        return paged, count

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
        archived_user_ids: set[int] | None = None,
    ) -> tuple[RunInputItems, int, bool]:
        recent_start = _recent_round_start(items)
        older = items[:recent_start]
        if not _can_read_history(agent, context):
            return items, 0, False
        required_ids = {
            id(item) for item in _required_history_items(items, archived_user_ids)
        }
        if not any(id(item) not in required_ids and not _is_checkpoint(item) for item in older):
            return items, 0, False
        store_history = getattr(context.tool_runtime, "store_context_history", None)
        if not callable(store_history):
            # No readable archive means no permission to discard exact history.
            return items, 0, False
        archive = store_history(older, context)
        if (
            not isinstance(archive, dict)
            or not isinstance(archive.get("result_ref"), str)
            or not archive["result_ref"]
        ):
            raise HarnessError("Context history storage returned no readable result_ref.")
        archives = _history_archives(older)
        if archived_user_ids:
            archive["contains_user_instructions"] = True
        archives.append(deepcopy(archive))
        folded_refs: set[str] = set()
        if len(archives) > _MAX_INLINE_HISTORY_ARCHIVES:
            folded = archives[:-4]
            manifest = store_history(folded, context)
            if (
                not isinstance(manifest, dict)
                or not isinstance(manifest.get("result_ref"), str)
                or not manifest["result_ref"]
            ):
                raise HarnessError("Context archive manifest storage returned no readable result_ref.")
            folded_refs = {
                ref for entry in folded for key in ("result_ref", "index_ref")
                if isinstance(ref := entry.get(key), str)
            }
            manifest = {
                **manifest,
                "kind": "archive_manifest",
                "archive_count": sum(entry.get("archive_count", 1) for entry in folded),
                "contains_user_instructions": any(
                    entry.get("contains_user_instructions") for entry in folded
                ),
            }
            archives = [manifest, *archives[-4:]]
        checkpoint: dict[str, Any] = {
            "checkpoint_id": str(uuid.uuid4()),
            "agent_name": agent.name,
            "estimated_tokens_before": math.ceil(total_chars / 4),
            "history_archives": archives,
            "references": [entry["result_ref"] for entry in archives],
            "reference_lifetime": _REFERENCE_LIFETIME_DESCRIPTION,
        }
        prior_checkpoints = [item for item in older if _is_checkpoint(item)]
        if prior_checkpoints:
            prior = _build_checkpoint(
                agent_name=agent.name, items=prior_checkpoints, metadata={},
                receipts=[], estimated_tokens=math.ceil(total_chars / 4),
            )
            checkpoint["prior_summary"] = prior["prior_summary"]
            checkpoint["evidence"] = prior["evidence"]
            for ref in prior["references"]:
                _append_unique(checkpoint["references"], ref)
        compacted = list(items)
        calls = _tool_names(items)
        candidates = [
            index for index, item in enumerate(older)
            if id(item) not in required_ids
            and (field := _tool_output_field(item)) is not None
            and _estimated_tokens(item[field]) > 256
        ]
        candidates.sort(key=lambda index: (
            not calls.get(_item_call_id(items[index]), "").startswith(
                ("read", "search", "fetch", "browse", "get")
            ),
            index,
        ))
        evicted = 0
        for index in candidates:
            if _estimated_tokens(compacted) <= target_tokens:
                break
            item = items[index]
            field = _tool_output_field(item)
            assert field is not None
            receipt: dict[str, Any] = {
                "status": "archived_context_tool_output",
                "result_ref": archive["result_ref"],
                "history_item_index": index,
                "instruction": "Read the exact original output using read_tool_result.",
            }
            location = next(
                (entry for entry in archive.get("index", []) if entry.get("item") == index),
                None,
            )
            if location is not None:
                receipt["offset"] = location["offset"]
                receipt["length"] = location["length"]
            elif archive.get("index_ref"):
                receipt["index_ref"] = archive["index_ref"]
            replacement = dict(item)
            replacement[field] = (
                json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
                if isinstance(item[field], str) else receipt
            )
            compacted[index] = replacement
            evicted += 1

        discarded: RunInputItems = []
        retained = compacted
        summary_method = "tool_eviction"
        summary_max_tokens = max(256, min(2_048, target_tokens // 10))
        if _estimated_tokens(compacted) > target_tokens:
            # Remove only an older prefix, in complete call/output groups. Reserve
            # checkpoint space, but never spend the last two rounds on that target.
            removed_count = 0
            for chunk in _history_chunks(compacted[:recent_start]):
                removed_count += len(chunk)
                discarded = older[:removed_count]
                preserved = _required_history_items(discarded, archived_user_ids)
                retained = [*preserved, *compacted[removed_count:]]
                if _estimated_tokens(retained) + summary_max_tokens + 768 <= target_tokens:
                    break
            checkpoint = _build_checkpoint(
                agent_name=agent.name, items=discarded, metadata=context.metadata,
                receipts=context.receipts, estimated_tokens=math.ceil(total_chars / 4),
            )
            checkpoint["history_archives"] = archives
            for entry in archives:
                _append_unique(checkpoint["references"], entry["result_ref"])
            checkpoint["reference_lifetime"] = _REFERENCE_LIFETIME_DESCRIPTION
        if not discarded and not evicted:
            return items, 0, False
        checkpoint["references"] = [
            ref for ref in checkpoint["references"] if ref not in folded_refs
        ]
        if discarded and _estimated_tokens([
            _checkpoint_item(checkpoint), *(item for item in retained if not _is_checkpoint(item)),
        ]) >= _estimated_tokens(items):
            return items, 0, False
        if discarded:
            summary_binding = self._compaction_model or agent.binding
            await context.emit(
                "context.compaction_started",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "agent_name": agent.name,
                    "estimated_tokens_before": checkpoint["estimated_tokens_before"],
                    "context_window_tokens": context_window_tokens,
                    "model_name": (
                        self._compaction_model_error["model_name"]
                        if self._compaction_model_error else summary_binding.model_name
                    ),
                    "provider_kind": (
                        None if self._compaction_model_error else summary_binding.provider_kind
                    ),
                    "summary_model_role": (
                        "helper" if self._compaction_model or self._compaction_model_error else "main"
                    ),
                    "target_tokens": target_tokens,
                    "discarded_item_count": len(discarded) - len(
                        _required_history_items(discarded, archived_user_ids)
                    ),
                    "preserved_user_message_count": len(_verbatim_user_messages(
                        _required_history_items(discarded, archived_user_ids)
                    )),
                },
            )
            model_summary, summary_method = await self._summarize_discarded_history(
                agent, discarded, checkpoint, max_tokens=summary_max_tokens, context=context,
            )
            if model_summary:
                checkpoint["model_summary"] = model_summary
            else:
                checkpoint["summary_limitations"] = (
                    "Bounded excerpts may omit narrative understanding. Read archived "
                    "history or durable evidence rather than infer omitted findings."
                )
        checkpoint["summary_method"] = summary_method
        compacted = [
            _checkpoint_item(checkpoint),
            *(item for item in retained if not _is_checkpoint(item)),
        ]
        # Checkpoint metadata, not recent interactions, pays for a tight target.
        for entry in archives:
            if _estimated_tokens(compacted) <= target_tokens:
                break
            entry.pop("index", None)
            compacted[0] = _checkpoint_item(checkpoint)
        for key in ("activity", "evidence", "caveats", "unresolved_questions"):
            while _estimated_tokens(compacted) > target_tokens and checkpoint.get(key):
                if key == "evidence" and len(checkpoint[key]) == 1:
                    # Keep a self-contained excerpt, not just an external reference.
                    break
                checkpoint[key].pop(0 if key == "activity" else -1)
                compacted[0] = _checkpoint_item(checkpoint)
        store = getattr(context.tool_runtime, "store_context_checkpoint", None)
        if callable(store):
            checkpoint["storage"] = store(checkpoint, context)
        compacted[0] = _checkpoint_item(checkpoint)
        checkpoints = context.metadata.setdefault(_CHECKPOINTS_KEY, [])
        if not isinstance(checkpoints, list):
            checkpoints = []
            context.metadata[_CHECKPOINTS_KEY] = checkpoints
        checkpoints.append(deepcopy(checkpoint))
        del checkpoints[:-20]
        # The target bounds optional memory, not immutable instructions. The caller
        # enforces the hard request ceiling after reattaching schemas and steering.
        if discarded:
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
                    "discarded_item_count": len(discarded) - len(
                        _required_history_items(discarded, archived_user_ids)
                    ),
                    "preserved_user_message_count": len(_verbatim_user_messages(
                        _required_history_items(discarded, archived_user_ids)
                    )),
                    "summary_method": summary_method,
                    "model_name": checkpoint.get("summary_model_name"),
                    "summary_model_role": checkpoint.get("summary_model_role"),
                    "summary_input_truncated": bool(checkpoint.get("summary_input_truncated")),
                    "storage": checkpoint.get("storage"),
                    "history_archives": archives,
                },
            )
        return compacted, evicted, bool(discarded)

    async def _summarize_discarded_history(
        self,
        agent: AgentDefinition,
        discarded: RunInputItems,
        checkpoint: dict[str, Any],
        *,
        max_tokens: int,
        context: ScholarWeaveContext,
    ) -> tuple[str | None, str]:
        if not discarded or not self._settings.agent_context_model_summary_enabled:
            return None, "structured"
        if all(_is_verbatim_constraint(item) for item in discarded):
            return None, "structured"
        bindings = [agent.binding]
        if self._compaction_model is not None and (
            self._compaction_model.client is not agent.binding.client
            or self._compaction_model.model_name != agent.binding.model_name
        ):
            bindings.insert(0, self._compaction_model)
        attempts: list[dict[str, Any]] = []
        if len(bindings) > 1 or self._compaction_model_error:
            checkpoint["summary_attempts"] = attempts
        if self._compaction_model_error:
            failure = {**self._compaction_model_error, "summary_model_role": "helper"}
            attempts.append({**failure, "status": "failed"})
            await self._summary_failure(agent, checkpoint, context, failure, retry=True)
        for index, binding in enumerate(bindings):
            is_helper = len(bindings) > 1 and index == 0
            window = (
                binding.context_window_tokens
                if is_helper else self.context_window_tokens(agent)
            )
            details = {
                "model_name": binding.model_name,
                "provider_kind": binding.provider_kind,
                "summary_model_role": "helper" if is_helper else "main",
                "summary_context_window_tokens": window,
            }
            if index > 0 or self._compaction_model_error:
                await context.emit("context.compaction_started", {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "agent_name": agent.name,
                    **details,
                    "is_fallback": True,
                })
            checkpoint["summary_model_name"] = binding.model_name
            checkpoint["summary_model_role"] = details["summary_model_role"]
            checkpoint["summary_context_window_tokens"] = window
            try:
                if window is None:
                    raise _SummaryInputLimitError(
                        f"Model '{binding.model_name}' has no known context window. "
                        "The helper cannot safely receive exact history without its own input "
                        "budget. No sampled summary was requested."
                    )
                summary = await self._request_summary(
                    agent, binding, discarded, checkpoint,
                    context_window_tokens=window, max_tokens=max_tokens, context=context,
                )
            except (OpenAIError, HarnessError) as exc:
                failure = {**details, "error_type": type(exc).__name__, "error": str(exc)}
                attempts.append({**failure, "status": "failed"})
                await self._summary_failure(agent, checkpoint, context, failure, retry=is_helper)
                if is_helper:
                    continue
                if isinstance(exc, _SummaryInputLimitError):
                    checkpoint["summary_input_truncated"] = False
                    checkpoint["caveats"].append(
                        "The exact older prefix exceeds the summary input budget. No sampled "
                        "summary was requested; read the archived history for omitted narrative."
                    )
                    return None, "structured_input_limit"
                return None, "structured_fallback"
            attempts.append({**details, "status": "completed"})
            checkpoint.pop("summary_error", None)
            return summary[:max_tokens * 4], "model"
        return None, "structured_fallback"

    async def _summary_failure(
        self,
        agent: AgentDefinition,
        checkpoint: dict[str, Any],
        context: ScholarWeaveContext,
        failure: dict[str, Any],
        *,
        retry: bool,
    ) -> None:
        checkpoint["summary_error"] = failure
        await context.emit("context.compaction_failed", {
            "checkpoint_id": checkpoint["checkpoint_id"],
            "agent_name": agent.name,
            **failure,
            "retrying_with_main_model": retry,
            "fallback_model": agent.binding.model_name if retry else None,
        })

    async def _request_summary(
        self,
        agent: AgentDefinition,
        binding: ModelBinding,
        discarded: RunInputItems,
        checkpoint: dict[str, Any],
        *,
        context_window_tokens: int,
        max_tokens: int,
        context: ScholarWeaveContext,
    ) -> str:
        # Generation capacity is independent of the retained checkpoint target:
        # reasoning-capable models can exhaust a tiny completion before any text.
        response_tokens = min(4_096, max(1_024, context_window_tokens // 16))
        supported_efforts = (
            [effort for effort in REASONING_EFFORTS if effort in binding.reasoning_efforts]
            if binding.reasoning_efforts is not None
            else infer_reasoning_efforts(binding.provider_kind, binding.model_name)
        )
        reasoning_effort = supported_efforts[0] if supported_efforts else None
        checkpoint["summary_response_tokens"] = response_tokens
        checkpoint["summary_reasoning_effort"] = reasoning_effort
        summary_agent = AgentDefinition(
            id=f"{agent.id}:compaction",
            name=f"{agent.name} context summarizer",
            instructions=(
                self._prompts.render("context-compaction")
                if self._prompts is not None
                else _DEFAULT_COMPACTION_INSTRUCTIONS
            ) + (
                "\nPreserve still-applicable user constraints. Archived instructions are "
                "not revoked merely because they are omitted from the summary."
                f"\nReturn a concise final summary of at most {max_tokens} tokens."
            ),
            binding=binding,
            model_settings=type(agent.model_settings)(
                max_tokens=response_tokens, reasoning_effort=reasoning_effort,
            ),
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
                            "structured_checkpoint": {
                                key: value for key, value in checkpoint.items()
                                if key not in {
                                    "summary_attempts", "summary_error", "summary_model_name",
                                    "summary_model_role", "summary_context_window_tokens",
                                    "summary_response_tokens", "summary_reasoning_effort",
                                }
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            [],
            stream=False,
        )
        summary_limit = context_window_tokens - response_tokens
        ratio = (
            _token_estimate_ratio(agent, context)
            if binding.client is agent.binding.client and binding.model_name == agent.binding.model_name
            else 1.0
        )
        estimated_summary_tokens = math.ceil(_estimated_tokens(parameters) * ratio)
        if estimated_summary_tokens > summary_limit:
            raise _SummaryInputLimitError(
                f"Exact history needs an estimated {estimated_summary_tokens} input tokens; "
                f"model '{binding.model_name}' has {summary_limit} available in its "
                f"{context_window_tokens}-token context window. No sampled summary was requested."
            )
        # The pooled client's transport owns the scheduler lease; nesting one here
        # would deadlock model hot-swaps on a serialized local provider.
        usage = Usage(requests=1)
        usage_complete = False
        timings: dict[str, float] = {}
        completed = False
        model_call_id = str(uuid.uuid4())
        try:
            with inference_priority("background"):
                response = await binding.client.chat.completions.create(**parameters)
            completed = True
            raw = response.model_dump() if hasattr(response, "model_dump") else dict(response)
            raw_usage = raw.get("usage") or {}
            usage = usage_from_payload(raw_usage)
            usage_complete = isinstance(raw_usage, dict) and all(
                type(value) is int and value >= 0
                for value in (
                    raw_usage.get("prompt_tokens", raw_usage.get("input_tokens")),
                    raw_usage.get("completion_tokens", raw_usage.get("output_tokens")),
                )
            )
            if isinstance(raw.get("timings"), dict):
                timings = {
                    key: value for key, value in raw["timings"].items()
                    if key in {"prompt_n", "prompt_ms", "predicted_n", "predicted_ms"}
                    and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0
                }
        finally:
            await context.emit("model.telemetry", {
                "model_call_id": model_call_id,
                "agent_id": summary_agent.id,
                "agent_name": summary_agent.name,
                "model": binding.model_name,
                "context_scope": "compaction",
                "usage": usage.to_dict(),
                "usage_complete": usage_complete,
                "timings": timings,
                "completed": completed,
            })
        summary = _response_text(response)
        if not summary:
            raise ModelBehaviorError(
                "The context summarizer returned no final text. Reasoning-only output "
                "is not a summary; exact history remains in the archive."
            )
        raw = response.model_dump() if hasattr(response, "model_dump") else response
        if any(
            choice.get("finish_reason") == "length"
            for choice in raw.get("choices") or [] if isinstance(choice, dict)
        ):
            raise ModelBehaviorError(
                "The context summarizer returned truncated output (finish_reason=length); "
                "exact history remains in the archive."
            )
        return summary


def _response_text(response: Any) -> str:
    raw = response.model_dump() if hasattr(response, "model_dump") else response
    if not isinstance(raw, dict):
        return ""
    texts = [
        content.strip()
        for choice in raw.get("choices") or []
        if isinstance(choice, dict)
        and isinstance(content := choice.get("message", {}).get("content"), str)
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


def _estimated_tokens(value: Any) -> int:
    """Byte-based sizing heuristic; provider tokenizers are not available here."""
    return math.ceil(len(json.dumps(to_jsonable(value), ensure_ascii=False).encode("utf-8")) / 4)


def _token_estimate_ratio(agent: AgentDefinition, context: ScholarWeaveContext) -> float:
    ratios = context.metadata.get("_context_token_ratios")
    ratio = ratios.get(agent.id, 1.0) if isinstance(ratios, dict) else 1.0
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not math.isfinite(ratio):
        return 1.0
    return max(1.0, float(ratio))


def _request_tokens(
    agent: AgentDefinition, items: RunInputItems, instructions: str, context: ScholarWeaveContext
) -> int:
    messages = to_chat_messages(
        strip_steering_markers(items), instructions,
        preserve_thinking=agent.binding.preserve_thinking, model_name=agent.binding.model_name,
    )
    parameters = request_parameters(agent, messages, agent.enabled_tools(context), stream=False)
    # Output limits are not prompt tokens; counting them changes the calibrated
    # input estimate when a truncated response is retried with a larger limit.
    parameters.pop("max_tokens", None)
    return math.ceil(
        (_estimated_tokens(parameters) + 8 * len(messages)) * _token_estimate_ratio(agent, context)
    )


def _budget_failure(required: int, available: int, response: int) -> None:
    raise RunPolicyViolation(
        "The configured model context budget cannot fit preserved constraints, recent reasoning, "
        "archive references, instructions, tool schemas and response headroom. Shorten the "
        "request/tool surface or increase the model's configured context window.",
        policy="working_context",
        detail={"estimated_required_tokens": required, "available_input_tokens": available,
                "response_headroom_tokens": response},
    )


def _can_read_history(agent: AgentDefinition, context: ScholarWeaveContext) -> bool:
    return any(tool.name == "read_tool_result" for tool in agent.enabled_tools(context))


def _is_checkpoint(item: dict[str, Any]) -> bool:
    return item.get("_scholarweave_context_checkpoint") is True


def _is_verbatim_constraint(item: dict[str, Any]) -> bool:
    return (
        item.get("role") in {"user", "system", "developer"}
        and not _is_checkpoint(item)
    )


def _checkpoint_item(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "user",
        "_scholarweave_context_checkpoint": True,
        "_scholarweave_context_policy_version": 2,
        "content": CHECKPOINT_MESSAGE_PREFIX + "\n\n" + json.dumps(
            checkpoint, ensure_ascii=False, separators=(",", ":")
        ),
    }


def _tool_output_field(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("type") or "")
    if item_type.endswith("_output") and "output" in item:
        return "output"
    if item.get("role") == "tool" and "content" in item:
        return "content"
    return None


def _replace_checkpointed_paper_reads(items: RunInputItems) -> RunInputItems:
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    successful_appends: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
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
            and _tool_output_object(item.get("output")).get("status") in {"appended", "reconciled"}
        ):
            successful_appends.append((index, call_id, arguments, _tool_output_object(item.get("output"))))

    if not successful_appends:
        return items

    append_ids = {call_id for _, call_id, _, _ in successful_appends}
    changed = False
    replaced: RunInputItems = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            replaced.append(item)
            continue
        call_id = item.get("call_id")
        name, arguments = (
            calls.get(call_id, ("", {})) if isinstance(call_id, str) else ("", {})
        )
        if item.get("type") == "function_call" and call_id in append_ids:
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
        output = _tool_output_object(item.get(field))
        document_id = arguments.get("document_id") or output.get("document_id")
        latest_append_result = None
        for append_index, _, append_arguments, append_result in successful_appends:
            if (
                append_index <= index
                or not document_id
                or document_id != append_arguments.get("document_id")
            ):
                continue
            path = output.get("checkpoint_path")
            if path is not None and path != append_result.get("checkpoint_path"):
                continue
            if (
                output.get("coverage") is not None
                and output["coverage"] != append_result.get("coverage")
            ):
                continue
            batch = output.get("summary_batch", output.get("batch"))
            batch_id = output.get("batch_id")
            if batch_id is not None:
                if batch_id != append_result.get("batch_id"):
                    continue
            elif batch is not None:
                if batch != append_result.get("checkpointed_batch"):
                    continue
            elif name == "read_paper_summary_batch":
                coverage = append_result.get("coverage") or {}
                if output.get("coverage") is not None:
                    if output["coverage"] != coverage:
                        continue
                elif arguments.get("start") != coverage.get("start"):
                    continue
            elif name == "read_tool_result":
                # A generic retained-result read is not evidence for this paper
                # merely because some later checkpoint happened to succeed.
                continue
            latest_append_result = append_result
            break
        if latest_append_result is None:
            replaced.append(item)
            continue
        receipt = {
            "status": "replaced_by_summary_checkpoint",
            "document_id": document_id,
            "checkpoint_path": latest_append_result.get("checkpoint_path"),
            "batch_id": latest_append_result.get("batch_id"),
            "checkpointed_batch": latest_append_result.get("checkpointed_batch"),
            "coverage": latest_append_result.get("coverage"),
            "next_start": latest_append_result.get("next_start"),
            "next_offset": latest_append_result.get("next_offset"),
            "has_more_paper": latest_append_result.get("has_more_paper"),
            "complete": latest_append_result.get("complete"),
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
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
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
    previous = next(
        (
            _tool_output_object(str(item.get("content", "")).split("\n\n")[-1])
            for item in reversed(items)
            if _is_checkpoint(item)
        ),
        {},
    )
    for item in reversed(to_jsonable(items)):
        if isinstance(item, dict) and _is_checkpoint(item):
            try:
                prior = json.loads(str(item.get("content", "")).split("\n\n")[-1])
            except (json.JSONDecodeError, IndexError):
                prior = {}
            _collect_checkpoint_values(
                prior, evidence=evidence, references=references, caveats=caveats,
                unresolved_questions=unresolved_questions, seen=seen,
            )
            if isinstance(prior, dict):
                for reference in prior.get("references", []):
                    if isinstance(reference, str):
                        _append_unique(references, reference)
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
        "reference_lifetime": _REFERENCE_LIFETIME_DESCRIPTION,
        "prior_summary": previous.get("model_summary") or previous.get("prior_summary"),
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
                selected[str(key)] = excerpt
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
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return
        _collect_checkpoint_values(
            parsed, evidence=evidence, references=references, caveats=caveats,
            unresolved_questions=unresolved_questions, seen=seen,
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


def _verbatim_user_messages(items: RunInputItems) -> RunInputItems:
    return [
        item
        for item in items
        if isinstance(item, dict)
        and _is_verbatim_constraint(item)
    ]


def _required_history_items(
    items: RunInputItems, archived_user_ids: set[int] | None = None,
) -> RunInputItems:
    pending_item_ids = {
        id(item)
        for chunk in _history_chunks(items)
        if any(
            (output := _tool_output_object(
                candidate.get(_tool_output_field(candidate) or "output")
            )).get("checkpoint_required") is True
            and output.get("checkpoint_available") is not False
            for candidate in chunk
        )
        for item in chunk
    }
    return [
        item for item in items
        if (
            _is_verbatim_constraint(item) and id(item) not in (archived_user_ids or ())
        ) or id(item) in pending_item_ids
    ]


def _recent_round_start(items: RunInputItems) -> int:
    """Protect two model responses, including their user input and tool outputs."""
    rounds: list[int] = []
    offset = 0
    pending_user: int | None = None
    previous_narrative = False
    previous_reasoning = False
    for chunk in _history_chunks(items):
        first = chunk[0]
        if _is_verbatim_constraint(first):
            if pending_user is None:
                pending_user = offset
            previous_narrative = False
            previous_reasoning = False
        elif not _is_checkpoint(first):
            is_call = bool(_started_tool_call_ids(first))
            if not (
                pending_user is None
                and (previous_reasoning or (is_call and previous_narrative))
            ):
                rounds.append(pending_user if pending_user is not None else offset)
            pending_user = None
            previous_reasoning = first.get("type") == "reasoning"
            previous_narrative = (
                first.get("role") == "assistant"
                and not any(_started_tool_call_ids(item) for item in chunk)
            )
        offset += len(chunk)
    return rounds[-2] if len(rounds) >= 2 else (rounds[0] if rounds else len(items))


def _protected_history_items(
    items: RunInputItems, archived_user_ids: set[int] | None = None,
) -> RunInputItems:
    protected_ids = {
        id(item) for item in _required_history_items(items, archived_user_ids)
    }
    protected_ids.update(id(item) for item in items[_recent_round_start(items):])
    return [item for item in items if id(item) in protected_ids]


def _item_call_id(item: dict[str, Any]) -> str:
    return str(item.get("call_id") or item.get("tool_call_id") or item.get("id") or "")


def _tool_names(items: RunInputItems) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in items:
        if item.get("type") == "function_call":
            names[_item_call_id(item)] = str(item.get("name") or "")
        for call in item.get("tool_calls") or []:
            names[str(call.get("id") or "")] = str(call.get("function", {}).get("name") or "")
    return names


def _history_archives(items: RunInputItems) -> list[dict[str, Any]]:
    archives: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not _is_checkpoint(item):
            continue
        checkpoint = _tool_output_object(str(item.get("content", "")).split("\n\n")[-1])
        for archive in checkpoint.get("history_archives", []):
            if isinstance(archive, dict) and isinstance(ref := archive.get("result_ref"), str):
                if ref not in seen:
                    archives.append(archive)
                    seen.add(ref)
    return archives


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
