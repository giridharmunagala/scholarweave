from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from agents import (
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelSettings,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    TResponseInputItem,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
)

from backend.agents.blueprint import ReasoningEffort, ReasoningSpec
from backend.agents.compiler import CompiledAgent
from backend.agents.blueprint import AgentBlueprint
from backend.agents.compiler import AgentCompiler
from backend.core.config import Settings
from backend.core.errors import ConflictError, NotFoundError
from backend.utils import to_jsonable, utcnow
from backend.runs.broker import EventBroker
from backend.runs.events import BufferedRunEventSink, PersistedRunEventSink
from backend.runs.projector import project_run_item, project_stream_event, run_item_key
from backend.runs.repository import (
    LeaseOwnershipError,
    RunLease,
    RunRepository,
)
from backend.agents.context import ScholarWeaveContext, ToolReceipt, ToolRuntime
from backend.runs.hooks import ScholarWeaveRunHooks
from backend.providers.inference import InferenceScheduler
from backend.conversations.sessions import SdkSessionFactory
from backend.conversations.steering import (
    SteeringInbox,
    SteeringMessage,
    emit_steering_applied,
)
from backend.prompting.registry import PromptRegistry
from backend.runs.logging import RunDetailLogger
from backend.tools.failures import (
    restore_tool_failure_state,
    restore_tool_failure_state_from_attempts,
    serialize_tool_failure_state,
)

RunInput = str | list[TResponseInputItem]
logger = logging.getLogger(__name__)
STOP_AND_ANSWER_PROMPT = (
    "Stop all further research. Answer the user's request now using only the conversation "
    "history, tool results, and evidence already available. Give the most useful direct answer "
    "you can, clearly identify important uncertainty or missing evidence, and do not suggest or "
    "attempt additional tool calls."
)
STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION = "scholarweave:internal:stop-and-answer"
GuardrailTripwire = (
    InputGuardrailTripwireTriggered
    | OutputGuardrailTripwireTriggered
    | ToolInputGuardrailTripwireTriggered
    | ToolOutputGuardrailTripwireTriggered
)


class RunLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseDeadline:
    lease: RunLease
    monotonic_expires_at: float


def _with_reasoning_effort(
    compiled: CompiledAgent,
    reasoning_effort: ReasoningEffort | None,
) -> CompiledAgent:
    if reasoning_effort is None:
        return compiled

    reasoning = {"effort": reasoning_effort}
    inherited = compiled.run_config.model_settings or ModelSettings()
    run_config = replace(
        compiled.run_config,
        model_settings=inherited.resolve(ModelSettings(reasoning=reasoning)),
    )
    blueprint = compiled.blueprint.model_copy(deep=True)
    for agent in blueprint.agents:
        agent.model_settings.reasoning = ReasoningSpec(effort=reasoning_effort)
    return replace(compiled, blueprint=blueprint, run_config=run_config)


async def _persist_result_to_session_if_needed(
    result: RunResult | RunResultStreaming,
    *,
    runner_session: Any,
    conversation_session: Any,
) -> None:
    if runner_session is not None or conversation_session is None:
        return
    original_items = (
        [{"role": "user", "content": result.input}]
        if isinstance(result.input, str)
        else list(result.input)
    )
    continuation_items = result.to_input_list(mode="normalized")
    await conversation_session.add_items(continuation_items[len(original_items) :])


def _restore_pending_steering(events: list[Any]) -> SteeringInbox:
    queued: dict[str, str] = {}
    for event in events:
        payload = event.payload_json
        message_id = payload.get("message_id")
        if not isinstance(message_id, str):
            continue
        if event.event_type == "steering.queued":
            content = payload.get("content")
            if isinstance(content, str):
                queued[message_id] = content
        elif event.event_type == "steering.applied":
            queued.pop(message_id, None)
    inbox = SteeringInbox()
    for message_id, content in queued.items():
        inbox.restore(message_id, content)
    return inbox


class RunService:
    _CLEANUP_INTERVAL_SECONDS = 60 * 60
    _CLAIM_LEASE_SECONDS = 300.0
    _CLAIM_HEARTBEAT_SECONDS = 60.0
    _CLAIM_RETRY_SECONDS = 1.0
    _CLAIM_SAFETY_MARGIN_SECONDS = 5.0
    _RECOVERY_RETRY_SECONDS = 5.0

    def __init__(
        self,
        repository: RunRepository,
        sessions: SdkSessionFactory,
        tool_runtime: ToolRuntime,
        event_broker: EventBroker,
        *,
        settings: Settings | None = None,
        retention_days: int | None = None,
        delete_run_artifacts: Callable[[str], None] | None = None,
        prompts: PromptRegistry | None = None,
        run_log_dir: Path | None = None,
        inference_scheduler: InferenceScheduler | None = None,
        completion_validators: Mapping[
            str, Callable[[ScholarWeaveContext], None]
        ] | None = None,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._tool_runtime = tool_runtime
        self._broker = event_broker
        self._settings = settings or Settings()
        self._retention = timedelta(
            days=retention_days or self._settings.run_retention_days
        )
        self._delete_run_artifacts = delete_run_artifacts
        self._prompts = prompts
        self._run_logger = RunDetailLogger(run_log_dir) if run_log_dir is not None else None
        self._inference_scheduler = inference_scheduler
        self._completion_validators = dict(completion_validators or {})
        self._owner_id = str(uuid.uuid4())
        self._closing = False
        self._active_streams: dict[str, RunResultStreaming] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._compiled_runs: dict[str, CompiledAgent] = {}
        self._event_sinks: dict[str, PersistedRunEventSink] = {}
        self._steering_inboxes: dict[str, SteeringInbox] = {}
        self._stop_and_answer_in_progress: set[str] = set()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._recovery_compiler: AgentCompiler | None = None
        self._lease_lost_runs: set[str] = set()
        self._active_leases: dict[str, RunLease] = {}
        self._claim_cleanup_tasks: dict[str, set[asyncio.Task[None]]] = {}

    async def recover_incomplete(self, compiler: AgentCompiler) -> None:
        self._recovery_compiler = compiler
        await self._recover_incomplete_once(compiler)
        if self._closing or self._recovery_task is not None:
            return
        self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def _recover_incomplete_once(self, compiler: AgentCompiler) -> None:
        for record in self._repository.incomplete():
            try:
                lease_deadline = await self._acquire_claim(record.id)
            except Exception:
                logger.exception("Could not claim incomplete run %s for recovery.", record.id)
                continue
            if lease_deadline is None:
                continue
            self._active_leases[record.id] = lease_deadline.lease
            claim_transferred = False
            try:
                record = self._repository.get(record.id)
                if record.status not in {"pending", "running"}:
                    continue
                runtime_context: ScholarWeaveContext | None = None
                if record.cancel_requested:
                    if self._repository.cancel_owned(lease_deadline.lease):
                        await self._event_sink(record.id).emit(
                            "run.cancelled",
                            {"reason": "user_requested"},
                        )
                    continue
                if record.conversation_id is not None:
                    self._steering_inboxes[record.id] = (
                        _restore_pending_steering(
                            self._repository.events_after(record.id)
                        )
                    )
                compiled = compiler.compile(
                    AgentBlueprint.model_validate(record.blueprint_json),
                    context_window_tokens=record.context_window_tokens,
                )
                if record.completion_policy_id is not None:
                    validator = self._completion_validators.get(
                        record.completion_policy_id
                    )
                    if validator is None:
                        raise RuntimeError(
                            "Unknown persisted completion policy "
                            f"{record.completion_policy_id!r}."
                        )
                    compiled = replace(
                        compiled,
                        completion_validator=validator,
                        completion_policy_id=record.completion_policy_id,
                    )
                if record.status == "pending":
                    if record.state_json:
                        sink = self._event_sink(record.id)
                        serialized_context = record.state_json.get(
                            "context", {}
                        ).get("context", {})
                        serialized_metadata = (
                            serialized_context.get("metadata")
                            if isinstance(serialized_context, dict)
                            else None
                        )
                        runtime_context = ScholarWeaveContext(
                            run_id=record.id,
                            conversation_id=record.conversation_id,
                            tool_runtime=self._tool_runtime,
                            event_sink=sink,
                            metadata={
                                **(
                                    dict(record.runtime_metadata_json)
                                    if isinstance(
                                        record.runtime_metadata_json, dict
                                    )
                                    else {}
                                ),
                                **(
                                    dict(serialized_metadata)
                                    if isinstance(serialized_metadata, dict)
                                    else {}
                                ),
                            },
                        )
                        _restore_tool_failure_state_from_run_state(
                            runtime_context,
                            record.state_json,
                        )
                        input_value = await RunState.from_json(
                            compiled.entry_agent,
                            record.state_json,
                            context_override=RunContextWrapper(runtime_context),
                        )
                    else:
                        input_value = record.input_json
                    recovered = False
                elif (
                    record.conversation_id is not None
                    and compiled.blueprint.description
                    == STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION
                ):
                    self._repository.abandon_incomplete_epochs_owned(
                        lease_deadline.lease
                    )
                    entry_model = compiled.resolved_models[
                        compiled.blueprint.entry_agent_id
                    ]
                    session = self._sessions.get(
                        record.conversation_id,
                        compiled.blueprint.session,
                        entry_model,
                    )
                    stop_prompt = _stop_and_answer_prompt_from_compiled(compiled)
                    input_value = _stop_and_answer_input(
                        _stop_and_answer_source_items(record.input_json, stop_prompt),
                        await session.get_items(),
                        stop_prompt,
                    )
                    recovered = True
                else:
                    self._repository.abandon_incomplete_epochs_owned(
                        lease_deadline.lease
                    )
                    input_value = (
                        self._prompts.render("run-recovery", run_id=record.id)
                        if self._prompts is not None
                        else _recovery_instruction(record.id)
                    )
                    recovered = True
                if recovered:
                    runtime_context = ScholarWeaveContext(
                        run_id=record.id,
                        conversation_id=record.conversation_id,
                        tool_runtime=self._tool_runtime,
                        event_sink=self._event_sink(record.id),
                        metadata={
                            **(
                                dict(record.runtime_metadata_json)
                                if isinstance(record.runtime_metadata_json, dict)
                                else {}
                            ),
                            "recovered": True,
                        },
                    )
                    restore_tool_failure_state_from_attempts(
                        runtime_context.metadata,
                        self._repository.get(record.id).tool_attempts,
                    )
                lease_deadline = await self._renew_claim(lease_deadline)
                self._schedule(
                    record.id,
                    compiled,
                    input_value,
                    conversation_id=record.conversation_id,
                    runtime_context=runtime_context,
                    runtime_metadata={
                        **(
                            dict(record.runtime_metadata_json)
                            if isinstance(record.runtime_metadata_json, dict)
                            else {}
                        ),
                        "recovered": recovered,
                    },
                    preclaimed=True,
                    lease_deadline=lease_deadline,
                )
                claim_transferred = True
            except asyncio.CancelledError:
                if (
                    record.id not in self._lease_lost_runs
                    and lease_deadline is not None
                    and self._repository.cancel_requested(record.id)
                    and self._repository.cancel_owned(lease_deadline.lease)
                ):
                    await self._event_sink(record.id).emit(
                        "run.cancelled",
                        {"reason": "user_requested"},
                    )
                raise
            except Exception as exc:
                error = f"Recovery failed: {type(exc).__name__}: {exc}"
                try:
                    lease_deadline = await self._renew_claim(lease_deadline)
                    still_owned = True
                except Exception:
                    logger.exception(
                        "Could not confirm ownership after recovery failed for run %s.",
                        record.id,
                    )
                    still_owned = False
                if still_owned and record.id not in self._lease_lost_runs:
                    if self._repository.fail_owned(lease_deadline.lease, error):
                        await self._event_sink(record.id).emit(
                            "run.failed",
                            {"error": error, "terminal_reason": "recovery_failed"},
                        )
                        self._log_terminal_run(record.id)
            finally:
                if not claim_transferred:
                    try:
                        await self._release_claim(lease_deadline.lease)
                    except Exception:
                        logger.exception(
                            "Could not release recovery claim for run %s.", record.id
                        )
                    self._active_leases.pop(record.id, None)
                    lease_lost = record.id in self._lease_lost_runs
                    self._lease_lost_runs.discard(record.id)
                    if not lease_lost:
                        await self._cleanup_standalone_session_if_terminal(record.id)

    def list(self, *, conversation_id: str | None = None):
        return self._repository.list(conversation_id=conversation_id)

    def get(self, run_id: str):
        return self._repository.get(run_id)

    def create(
        self,
        compiled: CompiledAgent,
        input_value: RunInput,
        *,
        conversation_id: str | None,
        reasoning_effort: ReasoningEffort | None = None,
        runtime_metadata: dict[str, Any] | None = None,
    ):
        compiled = _with_reasoning_effort(compiled, reasoning_effort)
        _validate_completion_policy(compiled)
        record = self._repository.create(
            conversation_id=conversation_id,
            agent_name=compiled.blueprint.name,
            input_value=to_jsonable(input_value),
            blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
            context_window_tokens=compiled.context_window_tokens,
            runtime_metadata=to_jsonable(
                _persistable_runtime_metadata(runtime_metadata or {})
            ),
            completion_policy_id=compiled.completion_policy_id,
        )
        if conversation_id is not None:
            self._steering_inboxes[record.id] = SteeringInbox()
        self._schedule(
            record.id,
            compiled,
            input_value,
            conversation_id=conversation_id,
            runtime_metadata=runtime_metadata,
        )
        return record

    def prompt_snapshot(self, run_id: str) -> dict[str, Any]:
        self.get(run_id)
        events = self._repository.events_after(run_id, -1)
        snapshot = next(
            (
                dict(event.payload_json)
                for event in events
                if event.event_type == "prompt.snapshot"
            ),
            None,
        )
        if snapshot is None:
            raise NotFoundError("A prompt snapshot is not available for this run.")
        snapshot["activated_skills"] = [
            dict(event.payload_json)
            for event in events
            if event.event_type == "skill.activated"
        ]
        return snapshot

    def _schedule(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        preclaimed: bool = False,
        lease_deadline: LeaseDeadline | None = None,
    ) -> None:
        previous = self._tasks.get(run_id)
        self._compiled_runs[run_id] = compiled
        if conversation_id is not None:
            self._steering_inboxes.setdefault(run_id, SteeringInbox())

        async def execute_after_previous() -> None:
            nonlocal execution_started
            if previous is not None and not previous.done():
                await asyncio.gather(previous, return_exceptions=True)
            execution_started = True
            await self._execute(
                run_id,
                compiled,
                input_value,
                conversation_id=conversation_id,
                runtime_context=runtime_context,
                runtime_metadata=runtime_metadata,
                preclaimed=preclaimed,
                lease_deadline=lease_deadline,
            )

        execution_started = False
        task = asyncio.create_task(execute_after_previous())
        self._tasks[run_id] = task

        def remove_if_current(completed: asyncio.Task[None]) -> None:
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass
            if preclaimed and not execution_started and lease_deadline is not None:
                self._track_claim_cleanup(
                    run_id,
                    self._cleanup_unadopted_claim(lease_deadline.lease),
                )
            if self._tasks.get(run_id) is completed:
                self._tasks.pop(run_id, None)
                self._compiled_runs.pop(run_id, None)
                self._event_sinks.pop(run_id, None)
                inbox = self._steering_inboxes.pop(run_id, None)
                if inbox is not None:
                    inbox.close()

        task.add_done_callback(remove_if_current)

    async def run_now(
        self,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        conversation_id: str | None = None,
    ):
        _validate_completion_policy(compiled)
        record = self._repository.create(
            conversation_id=conversation_id,
            agent_name=compiled.blueprint.name,
            input_value=to_jsonable(input_value)
            if not isinstance(input_value, RunState)
            else {"type": "run_state"},
            blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
            context_window_tokens=compiled.context_window_tokens,
            completion_policy_id=compiled.completion_policy_id,
        )
        if conversation_id is not None:
            self._steering_inboxes[record.id] = SteeringInbox()
        await self._execute(
            record.id,
            compiled,
            input_value,
            conversation_id=conversation_id,
        )
        return self._repository.get(record.id)

    async def steer(self, run_id: str, content: str) -> SteeringMessage:
        record = self._repository.get(run_id)
        if record.status not in {"pending", "running"}:
            raise ConflictError("Only an active run can accept steering messages.")
        if record.conversation_id is None:
            raise ConflictError("Steering requires a run associated with a conversation.")
        inbox = self._steering_inboxes.get(run_id)
        if inbox is None:
            raise ConflictError("The active run is not owned by this process.")
        message = inbox.queue(content)
        sink = self._event_sink(run_id)
        await sink.emit(
            "steering.queued",
            {"message_id": message.id, "content": message.content},
        )
        return message

    async def cancel(self, run_id: str):
        record = self._repository.get(run_id)
        if record.status not in {"pending", "running"}:
            return record
        if not self._repository.request_cancel(run_id):
            return self._repository.get(run_id)
        stream = self._active_streams.get(run_id)
        if stream is not None:
            stream.cancel("immediate")
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._wait_for_claim_cleanups(run_id)
        current = self._repository.get(run_id)
        lease = self._active_leases.get(run_id)
        if (
            current.status in {"pending", "running"}
            and lease is not None
            and self._repository.cancel_owned(lease)
        ):
            sink = self._event_sink(run_id)
            await sink.emit("run.cancelled", {"reason": "user_requested"})
        current = self._repository.get(run_id)
        if current.status in {"completed", "failed", "cancelled", "paused"}:
            self._log_terminal_run(run_id)
        return self._repository.get(run_id)

    async def stop_and_answer(self, run_id: str):
        if run_id in self._stop_and_answer_in_progress:
            raise ConflictError("Stop and answer is already in progress for this run.")
        self._stop_and_answer_in_progress.add(run_id)
        try:
            record = self._repository.get(run_id)
            if record.status not in {"pending", "running"}:
                raise ConflictError("Only an active run can be stopped and answered.")
            if record.conversation_id is None:
                raise ConflictError(
                    "Stop and answer requires a run associated with a conversation."
                )
            compiled = self._compiled_runs.get(run_id)
            if compiled is None:
                raise ConflictError("The active run is not owned by this process.")
            stopped = await self.cancel(run_id)
            stop_prompt = (
                self._prompts.render("stop-and-answer")
                if self._prompts is not None
                else STOP_AND_ANSWER_PROMPT
            )
            answer_compiled = _stop_and_answer_compiled(compiled, stop_prompt)
            entry_model = compiled.resolved_models[compiled.blueprint.entry_agent_id]
            session = self._sessions.get(
                record.conversation_id,
                compiled.blueprint.session,
                entry_model,
            )
            answer_input = _stop_and_answer_input(
                record.input_json,
                await session.get_items(),
                stop_prompt,
            )
            answer = self.create(
                answer_compiled,
                answer_input,
                conversation_id=record.conversation_id,
                runtime_metadata={
                    "internal_session_prompt": stop_prompt,
                    "stop_and_answer_source_run_id": run_id,
                },
            )
            return stopped, answer
        finally:
            self._stop_and_answer_in_progress.discard(run_id)

    async def cancel_conversation_runs(self, conversation_id: str) -> None:
        active = [
            record
            for record in self._repository.list()
            if record.conversation_id == conversation_id
            and record.status in {"pending", "running"}
        ]
        for record in active:
            await self.cancel(record.id)
        tasks = [
            self._tasks[record.id]
            for record in active
            if record.id in self._tasks
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._wait_for_all_claim_cleanups()

    async def _wait_for_all_claim_cleanups(self) -> None:
        while self._claim_cleanup_tasks:
            pending = tuple(
                task
                for cleanup_tasks in self._claim_cleanup_tasks.values()
                for task in cleanup_tasks
            )
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)

    async def resolve_interruption(
        self,
        compiled: CompiledAgent,
        *,
        run_id: str,
        interruption_id: str,
        approved: bool,
        rejection_message: str | None = None,
    ):
        record = self._repository.get(run_id)
        if record.status != "paused" or not record.state_json:
            raise ValueError("Only a paused run can resolve an interruption.")
        interruption = self._repository.get_interruption(interruption_id)
        if interruption.run_id != run_id or interruption.status != "pending":
            raise ValueError("The interruption is not pending for this run.")
        sink = self._event_sink(run_id)
        serialized_context = record.state_json.get("context", {}).get("context", {})
        serialized_context = serialized_context if isinstance(serialized_context, dict) else {}
        raw_metadata = serialized_context.get("metadata")
        raw_receipts = serialized_context.get("receipts")
        live_context = ScholarWeaveContext(
            run_id=run_id,
            conversation_id=record.conversation_id,
            tool_runtime=self._tool_runtime,
            event_sink=sink,
            metadata=dict(raw_metadata) if isinstance(raw_metadata, dict) else {},
            receipts=[
                ToolReceipt(
                    kind=str(receipt.get("kind") or ""),
                    title=str(receipt.get("title") or ""),
                    description=str(receipt.get("description") or ""),
                    href=receipt.get("href")
                    if isinstance(receipt.get("href"), str)
                    else None,
                    metadata=dict(receipt.get("metadata") or {}),
                )
                for receipt in raw_receipts or []
                if isinstance(receipt, dict)
            ],
        )
        _restore_tool_failure_state_from_run_state(
            live_context,
            record.state_json,
        )
        state = await RunState.from_json(
            compiled.entry_agent,
            record.state_json,
            context_override=RunContextWrapper(live_context),
        )
        approval_item = next(
            (
                item
                for item in state.get_interruptions()
                if run_item_key(item) == interruption.item_key
            ),
            None,
        )
        if approval_item is None:
            raise ValueError("The SDK run state no longer contains this interruption.")
        if approved:
            state.approve(approval_item)
            status = "approved"
            response = {"approved": True}
        else:
            state.reject(
                approval_item,
                rejection_message=rejection_message,
            )
            status = "rejected"
            response = {
                "approved": False,
                "message": rejection_message,
            }
        persisted_state = state.to_json(
            context_serializer=lambda context: {
                "run_id": context.run_id,
                "conversation_id": context.conversation_id,
                "metadata": to_jsonable(
                    _persistable_runtime_metadata(context.metadata)
                ),
                "receipts": [
                    {
                        "kind": receipt.kind,
                        "title": receipt.title,
                        "description": receipt.description,
                        "href": receipt.href,
                        "metadata": to_jsonable(receipt.metadata),
                    }
                    for receipt in context.receipts
                ],
                "tool_failure_state": serialize_tool_failure_state(
                    context.metadata
                ),
            }
        )
        self._repository.resolve_interruption(
            interruption_id,
            run_id=run_id,
            status=status,
            response=response,
            state=persisted_state,
        )
        self._schedule(
            run_id,
            compiled,
            state,
            conversation_id=record.conversation_id,
            runtime_context=live_context,
        )
        return self._repository.get(run_id)

    def delete(self, run_id: str) -> None:
        record = self._repository.get(run_id)
        if record.status not in {"completed", "failed", "cancelled"}:
            raise ConflictError("Only a finished run can be deleted.")
        if self._delete_history([run_id]) != 1:
            raise ConflictError("The run is still active and cannot be deleted.")

    def events_after(self, run_id: str, sequence: int = -1):
        self._repository.get(run_id)
        return self._repository.events_after(run_id, sequence)

    async def start(self) -> None:
        if self._cleanup_task is not None:
            return
        await asyncio.to_thread(self.prune_expired)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def prune_expired(self) -> int:
        cutoff = utcnow() - self._retention
        return self._delete_history(self._repository.ids_created_before(cutoff))

    def clear_history(self) -> int:
        return self._delete_history(self._repository.ids_created_before())

    async def close(self) -> None:
        self._closing = True
        recovery_task = self._recovery_task
        self._recovery_task = None
        if recovery_task is not None:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        for stream in tuple(self._active_streams.values()):
            stream.cancel("immediate")
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._wait_for_all_claim_cleanups()

    def _delete_history(self, run_ids: list[str]) -> int:
        active_ids = set(self._active_streams) | {
            run_id for run_id, task in self._tasks.items() if not task.done()
        }
        deletable = [run_id for run_id in run_ids if run_id not in active_ids]
        if self._delete_run_artifacts is not None:
            for run_id in deletable:
                self._delete_run_artifacts(run_id)
        for run_id in deletable:
            self._event_sinks.pop(run_id, None)
            if self._repository.get(run_id).conversation_id is None:
                self._sessions.delete_persisted(_standalone_session_id(run_id))
        self._repository.delete_many(deletable)
        return len(deletable)

    def _event_sink(self, run_id: str) -> PersistedRunEventSink:
        sink = self._event_sinks.get(run_id)
        if sink is None:
            sink = PersistedRunEventSink(
                run_id,
                self._repository,
                self._broker,
                lease=lambda: self._active_leases.get(run_id),
            )
            self._event_sinks[run_id] = sink
        return sink

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._CLEANUP_INTERVAL_SECONDS)
            await asyncio.to_thread(self.prune_expired)

    async def _recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self._RECOVERY_RETRY_SECONDS)
            compiler = self._recovery_compiler
            if compiler is not None:
                try:
                    await self._recover_incomplete_once(compiler)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Periodic incomplete-run recovery failed.")

    async def _heartbeat_claim(
        self,
        run_id: str,
        lease_deadline: LeaseDeadline,
    ) -> None:
        while True:
            safety_margin = self._claim_safety_margin()
            remaining = (
                lease_deadline.monotonic_expires_at
                - safety_margin
                - asyncio.get_running_loop().time()
            )
            if remaining <= 0:
                self._lease_lost_runs.add(run_id)
                raise RunLeaseLost(f"Run {run_id} claim renewal deadline expired.")
            await asyncio.sleep(min(self._CLAIM_HEARTBEAT_SECONDS, remaining))
            while True:
                if (
                    asyncio.get_running_loop().time()
                    >= lease_deadline.monotonic_expires_at - safety_margin
                ):
                    self._lease_lost_runs.add(run_id)
                    raise RunLeaseLost(f"Run {run_id} claim renewal deadline expired.")
                try:
                    lease_deadline = await self._renew_claim(lease_deadline)
                except asyncio.CancelledError:
                    raise
                except RunLeaseLost:
                    raise
                except Exception:
                    logger.exception("Run %s claim renewal failed; retrying.", run_id)
                    remaining = (
                        lease_deadline.monotonic_expires_at
                        - safety_margin
                        - asyncio.get_running_loop().time()
                    )
                    if remaining <= 0:
                        self._lease_lost_runs.add(run_id)
                        raise RunLeaseLost(
                            f"Run {run_id} claim could not be renewed safely."
                        )
                    await asyncio.sleep(min(self._CLAIM_RETRY_SECONDS, remaining))
                    continue
                confirmed_at = asyncio.get_running_loop().time()
                if (
                    confirmed_at
                    >= lease_deadline.monotonic_expires_at - safety_margin
                ):
                    self._lease_lost_runs.add(run_id)
                    raise RunLeaseLost(
                        f"Run {run_id} claim was not renewed before its safety margin."
                    )
                break

    async def _acquire_claim(self, run_id: str) -> LeaseDeadline | None:
        started_at = asyncio.get_running_loop().time()
        task = asyncio.create_task(
            asyncio.to_thread(
                self._repository.claim,
                run_id,
                self._owner_id,
                lease_seconds=self._CLAIM_LEASE_SECONDS,
            )
        )
        try:
            lease = await asyncio.shield(task)
        except asyncio.CancelledError:
            self._track_claim_cleanup(
                run_id,
                self._cleanup_cancelled_acquisition(task),
            )
            raise
        if lease is None:
            return None
        return LeaseDeadline(
            lease=lease,
            monotonic_expires_at=started_at + lease.duration_seconds,
        )

    async def _cleanup_cancelled_acquisition(
        self,
        claim_task: asyncio.Task[RunLease | None],
    ) -> None:
        try:
            lease = await asyncio.shield(claim_task)
        except Exception:
            logger.exception("Cancelled run claim acquisition failed.")
            return
        if lease is not None:
            await self._cleanup_unadopted_claim(lease)

    async def _cleanup_unadopted_claim(self, lease: RunLease) -> None:
        run_id = lease.run_id
        if self._active_leases.get(run_id) is None:
            self._active_leases[run_id] = lease
        try:
            if (
                self._repository.cancel_requested(run_id)
                and self._repository.cancel_owned(lease)
            ):
                await self._event_sink(run_id).emit(
                    "run.cancelled",
                    {"reason": "user_requested"},
                )
            await self._release_claim(lease)
        finally:
            if self._active_leases.get(run_id) == lease:
                self._active_leases.pop(run_id, None)
        await self._cleanup_standalone_session_if_terminal(run_id)

    def _track_claim_cleanup(
        self,
        run_id: str,
        cleanup: Awaitable[None],
    ) -> None:
        task = asyncio.create_task(cleanup)
        self._claim_cleanup_tasks.setdefault(run_id, set()).add(task)

        def finished(completed: asyncio.Task[None]) -> None:
            tasks = self._claim_cleanup_tasks.get(run_id)
            if tasks is not None:
                tasks.discard(completed)
                if not tasks:
                    self._claim_cleanup_tasks.pop(run_id, None)
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Could not clean up unadopted run claim %s.", run_id)

        task.add_done_callback(finished)

    async def _wait_for_claim_cleanups(self, run_id: str) -> None:
        while cleanup_tasks := tuple(self._claim_cleanup_tasks.get(run_id, ())):
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    async def _renew_claim(self, current: LeaseDeadline) -> LeaseDeadline:
        started_at = asyncio.get_running_loop().time()
        lease = await asyncio.to_thread(
            self._repository.renew_claim,
            current.lease.run_id,
            current.lease.owner_id,
            generation=current.lease.generation,
            token=current.lease.token,
            lease_seconds=self._CLAIM_LEASE_SECONDS,
        )
        if lease is None:
            self._lease_lost_runs.add(current.lease.run_id)
            raise RunLeaseLost(
                f"Run {current.lease.run_id} claim is no longer owned."
            )
        self._active_leases[current.lease.run_id] = lease
        return LeaseDeadline(
            lease=lease,
            monotonic_expires_at=started_at + lease.duration_seconds,
        )

    async def _release_claim(self, lease: RunLease) -> None:
        task = asyncio.create_task(
            asyncio.to_thread(
                self._repository.release_claim,
                lease.run_id,
                lease.owner_id,
                generation=lease.generation,
                token=lease.token,
            )
        )
        await asyncio.shield(task)

    async def _validate_preclaimed_deadline(
        self,
        lease_deadline: LeaseDeadline,
    ) -> None:
        run_id = lease_deadline.lease.run_id
        if (
            asyncio.get_running_loop().time()
            >= lease_deadline.monotonic_expires_at - self._claim_safety_margin()
            or not await asyncio.to_thread(
                self._repository.validate_claim,
                lease_deadline.lease,
            )
        ):
            self._lease_lost_runs.add(run_id)
            raise RunLeaseLost(f"Run {run_id} preclaimed lease is no longer safe.")

    def _claim_safety_margin(self) -> float:
        return min(
            self._CLAIM_SAFETY_MARGIN_SECONDS,
            self._CLAIM_LEASE_SECONDS / 5,
        )

    async def _execute(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        preclaimed: bool = False,
        lease_deadline: LeaseDeadline | None = None,
    ) -> None:
        deadline = (
            asyncio.get_running_loop().time()
            + self._settings.agent_run_timeout_seconds
        )
        await self._execute_run(
            run_id,
            compiled,
            input_value,
            conversation_id=conversation_id,
            runtime_context=runtime_context,
            runtime_metadata=runtime_metadata,
            deadline=deadline,
            preclaimed=preclaimed,
            lease_deadline=lease_deadline,
        )

    async def _execute_run(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        deadline: float,
        preclaimed: bool = False,
        lease_deadline: LeaseDeadline | None = None,
    ) -> None:
        if not preclaimed:
            lease_deadline = await self._acquire_claim(run_id)
            if lease_deadline is None:
                return
            self._active_leases[run_id] = lease_deadline.lease
        elif lease_deadline is None:
            raise ValueError("A preclaimed run requires its lease expiration deadline.")
        heartbeat: asyncio.Task[None] | None = None
        execution: asyncio.Task[None] | None = None
        try:
            await self._validate_preclaimed_deadline(lease_deadline)
            async with asyncio.timeout_at(deadline):
                heartbeat = asyncio.create_task(
                    self._heartbeat_claim(run_id, lease_deadline)
                )
                execution = asyncio.create_task(
                    self._execute_owned_run(
                        run_id,
                        compiled,
                        input_value,
                        conversation_id=conversation_id,
                        runtime_context=runtime_context,
                        runtime_metadata=runtime_metadata,
                        deadline=deadline,
                    )
                )
                done, _ = await asyncio.wait(
                    {execution, heartbeat},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat in done:
                    await heartbeat
                    raise RuntimeError("Run claim heartbeat stopped unexpectedly.")
                await execution
        except asyncio.CancelledError:
            if (
                run_id not in self._lease_lost_runs
                and lease_deadline is not None
                and self._repository.cancel_requested(run_id)
                and self._repository.cancel_owned(lease_deadline.lease)
            ):
                await self._event_sink(run_id).emit(
                    "run.cancelled",
                    {"reason": "user_requested"},
                )
            raise
        except (RunLeaseLost, LeaseOwnershipError) as exc:
            self._lease_lost_runs.add(run_id)
            logger.warning("%s Execution was cancelled.", exc)
            if execution is not None:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
        except TimeoutError:
            if execution is not None and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            if run_id not in self._lease_lost_runs:
                record = self._repository.get(run_id)
                if record.status in {"pending", "running"}:
                    error = (
                        f"Run exceeded its {self._settings.agent_run_timeout_seconds:g}-second "
                        "deadline."
                    )
                    await self._fail_or_cancel_owned(
                        run_id,
                        error,
                        self._event_sink(run_id),
                        {"error": error, "terminal_reason": "deadline_exceeded"},
                    )
                    self._log_terminal_run(run_id)
        except Exception as exc:
            record = self._repository.get(run_id)
            if (
                run_id not in self._lease_lost_runs
                and record.status in {"pending", "running"}
            ):
                error = f"{type(exc).__name__}: {exc}"
                await self._fail_or_cancel_owned(
                    run_id,
                    error,
                    self._event_sink(run_id),
                    {"error": error, "terminal_reason": "setup_error"},
                )
                self._log_terminal_run(run_id)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if execution is not None:
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            lease_lost = run_id in self._lease_lost_runs
            if not lease_lost:
                await self._release_claim(lease_deadline.lease)
            self._active_leases.pop(run_id, None)
            self._lease_lost_runs.discard(run_id)
            if not lease_lost:
                await self._cleanup_standalone_session_if_terminal(run_id)

    async def _execute_owned_run(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None,
        runtime_metadata: dict[str, Any] | None,
        deadline: float,
    ) -> None:
        if (
            self._inference_scheduler is not None
            and compiled.blueprint.run.exclusive_inference
            and _uses_local_inference(compiled)
        ):
            async with self._inference_scheduler.exclusive():
                await self._execute_claimed_run(
                    run_id,
                    compiled,
                    input_value,
                    conversation_id=conversation_id,
                    runtime_context=runtime_context,
                    runtime_metadata=runtime_metadata,
                    deadline=deadline,
                )
            return
        await self._execute_claimed_run(
            run_id,
            compiled,
            input_value,
            conversation_id=conversation_id,
            runtime_context=runtime_context,
            runtime_metadata=runtime_metadata,
            deadline=deadline,
        )

    async def _execute_claimed_run(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        deadline: float,
    ) -> None:
        sink = self._event_sink(run_id)
        if isinstance(input_value, RunState) and runtime_context is None:
            raise ValueError("Resuming an SDK RunState requires restored live context.")
        record_metadata = self._repository.get(run_id).runtime_metadata_json
        context = runtime_context or ScholarWeaveContext(
            run_id=run_id,
            conversation_id=conversation_id,
            tool_runtime=self._tool_runtime,
            event_sink=sink,
            metadata={
                **(
                    dict(record_metadata)
                    if isinstance(record_metadata, dict)
                    else {}
                ),
                **dict(runtime_metadata or {}),
            },
        )
        if compiled.blueprint.description == STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION:
            context.metadata.setdefault(
                "internal_session_prompt",
                (
                    self._prompts.render("stop-and-answer")
                    if self._prompts is not None
                    else STOP_AND_ANSWER_PROMPT
                ),
            )
        if not any(
            event.event_type == "prompt.snapshot"
            for event in self._repository.events_after(run_id, -1)
        ):
            await sink.emit(
                "prompt.snapshot",
                _compiled_prompt_snapshot(
                    run_id,
                    compiled,
                    prompt_revision=(
                        self._prompts.revision if self._prompts is not None else None
                    ),
                ),
            )
        context.event_sink = sink
        hooks = ScholarWeaveRunHooks()
        sdk_session_id = conversation_id or _standalone_session_id(run_id)
        conversation_session = self._sessions.get(
            sdk_session_id,
            compiled.blueprint.session,
        )
        session = (
            conversation_session if not isinstance(input_value, RunState) else None
        )
        steering_inbox = (
            self._steering_inboxes.setdefault(run_id, SteeringInbox())
            if conversation_id is not None
            else None
        )
        if steering_inbox is not None:
            steering_inbox.bind_session(conversation_session)
            context.metadata["_steering_inbox"] = steering_inbox
        lock = (
            self._sessions.run_lock(sdk_session_id)
        )
        existing_record = self._repository.get(run_id)
        persisted_item_count = (
            len(existing_record.items) if isinstance(input_value, RunState) else 0
        )
        initial_reasoning, initial_assistant = _persisted_stream_text(
            existing_record.events
        )
        self._repository.mark_running_owned(self._owned_lease(run_id))
        recovered = bool(context.metadata.get("recovered"))
        await sink.emit(
            "run.resumed" if isinstance(input_value, RunState) else "run.started",
            {
                "agent_name": compiled.blueprint.name,
                "recovered": recovered,
            },
        )
        if recovered:
            await sink.emit(
                "run.recovered",
                {"reason": "process_interrupted", "strategy": "new_epoch"},
            )
        active_epoch_id: str | None = None
        deferred_steering: list[SteeringMessage] = []
        try:
            async with asyncio.timeout_at(deadline):
                self._raise_if_cancelled(run_id)
                async with lock:
                    self._raise_if_cancelled(run_id)
                    session_snapshot = (
                        await session.get_items() if session is not None else None
                    )
                    epoch_input = input_value
                    first_epoch = True
                    consumed_turns = self._repository.consumed_model_turns(run_id)
                    while True:
                        self._raise_if_cancelled(run_id)
                        remaining_turns = compiled.max_turns - consumed_turns
                        if remaining_turns <= 0:
                            raise RunBudgetExceeded(
                                f"Run exhausted its {compiled.max_turns}-turn budget."
                            )
                        epoch_turn_limit = min(
                            remaining_turns,
                            self._settings.agent_epoch_max_turns,
                        )
                        epoch = self._repository.begin_epoch_owned(
                            self._owned_lease(run_id),
                            to_jsonable(epoch_input)
                            if not isinstance(epoch_input, RunState)
                            else {"type": "approval_resume"},
                        )
                        active_epoch_id = epoch.id
                        if epoch.epoch_index >= self._settings.agent_max_epochs:
                            self._repository.finish_epoch_owned(
                                self._owned_lease(run_id),
                                epoch.id,
                                status="failed",
                                terminal_reason="budget_exhausted",
                                error="Maximum epoch budget reached.",
                            )
                            raise RunBudgetExceeded(
                                f"Run exhausted {self._settings.agent_max_epochs} epochs."
                            )
                        context.metadata["active_epoch_id"] = epoch.id
                        context.metadata["epoch_index"] = epoch.epoch_index
                        context.metadata["goal_state"] = self._repository.get_goal_state(
                            run_id
                        )
                        await sink.emit(
                            "run.epoch.started",
                            {
                                "epoch_id": epoch.id,
                                "epoch_index": epoch.epoch_index,
                                "max_epochs": self._settings.agent_max_epochs,
                                "max_turns": epoch_turn_limit,
                                "remaining_run_turns": remaining_turns,
                            },
                        )
                        stream_sink = BufferedRunEventSink(
                            sink,
                            initial_reasoning=initial_reasoning
                            if first_epoch
                            else "",
                            initial_assistant=initial_assistant
                            if first_epoch
                            else "",
                        )
                        context.event_sink = stream_sink
                        if steering_inbox is not None:
                            steering_inbox.begin_epoch()
                        if deferred_steering:
                            await emit_steering_applied(context, deferred_steering)
                            deferred_steering = []
                        try:
                            stream = Runner.run_streamed(
                                compiled.entry_agent,
                                epoch_input,
                                context=context
                                if not isinstance(epoch_input, RunState)
                                else None,
                                max_turns=epoch_turn_limit,
                                hooks=hooks,
                                run_config=compiled.run_config,
                                session=session,
                            )
                            self._active_streams[run_id] = stream
                            try:
                                async for event in stream.stream_events():
                                    projected = project_stream_event(event)
                                    if projected is not None:
                                        await stream_sink.emit(*projected)
                            finally:
                                await _flush_preserving_cancellation(stream_sink)
                        except MaxTurnsExceeded as exc:
                            run_data = exc.run_data
                            _persist_new_items(
                                self._repository,
                                self._owned_lease(run_id),
                                getattr(run_data, "new_items", []),
                                persisted_item_count if first_epoch else 0,
                            )
                            await _persist_result_to_session_if_needed(
                                run_data,
                                runner_session=session,
                                conversation_session=conversation_session,
                            )
                            usage = to_jsonable(
                                getattr(
                                    getattr(run_data, "context_wrapper", None),
                                    "usage",
                                    {},
                                )
                            )
                            usage["model_turns"] = epoch_turn_limit
                            self._repository.finish_epoch_owned(
                                self._owned_lease(run_id),
                                epoch.id,
                                status="completed",
                                terminal_reason="turn_boundary",
                                usage=usage,
                            )
                            await sink.emit(
                                "run.epoch.completed",
                                {
                                    "epoch_id": epoch.id,
                                    "epoch_index": epoch.epoch_index,
                                    "terminal_reason": "turn_boundary",
                                    "usage": usage,
                                },
                            )
                            next_epoch = epoch.epoch_index + 1
                            goal_state = self._repository.get_goal_state(run_id)
                            continuation = (
                                self._prompts.render(
                                    "run-continuation",
                                    run_id=run_id,
                                    epoch_index=next_epoch,
                                    goal_state=json.dumps(
                                        goal_state,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                )
                                if self._prompts is not None
                                else _continuation_instruction(
                                    run_id,
                                    next_epoch,
                                    goal_state,
                                )
                            )
                            if session is None:
                                epoch_input = [
                                    *run_data.to_input_list(mode="normalized"),
                                    {"role": "user", "content": continuation},
                                ]
                            else:
                                epoch_input = continuation
                            persisted_item_count = 0
                            consumed_turns += int(usage["model_turns"])
                            first_epoch = False
                            active_epoch_id = None
                            continue
                        except Exception as exc:
                            self._repository.finish_epoch_owned(
                                self._owned_lease(run_id),
                                epoch.id,
                                status="failed",
                                terminal_reason="error",
                                error=f"{type(exc).__name__}: {exc}",
                            )
                            raise
                        finally:
                            await _cleanup_internal_prompt_if_needed(context, session)
                            self._active_streams.pop(run_id, None)

                        epoch_usage = to_jsonable(stream.context_wrapper.usage)
                        epoch_usage["performance"] = stream_sink.performance()
                        epoch_usage["model_turns"] = _epoch_model_turns(
                            epoch_usage,
                            epoch_turn_limit,
                            consumed_turns,
                            resumed=isinstance(epoch_input, RunState),
                        )
                        pending_steering = (
                            steering_inbox.take_pending_or_close()
                            if steering_inbox is not None
                            else []
                        )
                        if pending_steering:
                            await self._persist_result_activity(
                                run_id,
                                stream,
                                sink,
                                persisted_item_count=persisted_item_count
                                if first_epoch
                                else 0,
                            )
                            await _persist_result_to_session_if_needed(
                                stream,
                                runner_session=session,
                                conversation_session=conversation_session,
                            )
                            self._repository.finish_epoch_owned(
                                self._owned_lease(run_id),
                                epoch.id,
                                status="completed",
                                terminal_reason="steering_continuation",
                                usage=epoch_usage,
                            )
                            aggregate_usage = self._repository.aggregate_epoch_usage(
                                run_id
                            )
                            self._repository.update_usage_owned(
                                self._owned_lease(run_id),
                                aggregate_usage,
                            )
                            await sink.emit(
                                "run.epoch.completed",
                                {
                                    "epoch_id": epoch.id,
                                    "epoch_index": epoch.epoch_index,
                                    "terminal_reason": "steering_continuation",
                                    "usage": aggregate_usage,
                                },
                            )
                            await steering_inbox.persist(pending_steering)
                            if session is None:
                                epoch_input = [
                                    *stream.to_input_list(mode="normalized"),
                                    *(
                                        message.input_item()
                                        for message in pending_steering
                                    ),
                                ]
                            else:
                                epoch_input = []
                            deferred_steering = pending_steering
                            persisted_item_count = 0
                            consumed_turns += int(epoch_usage["model_turns"])
                            first_epoch = False
                            active_epoch_id = None
                            continue
                        work_continuation = _work_continuation(context)
                        if work_continuation is not None:
                            await self._persist_result_activity(
                                run_id,
                                stream,
                                sink,
                                persisted_item_count=persisted_item_count
                                if first_epoch
                                else 0,
                            )
                            await _persist_result_to_session_if_needed(
                                stream,
                                runner_session=session,
                                conversation_session=conversation_session,
                            )
                            self._repository.finish_epoch_owned(
                                self._owned_lease(run_id),
                                epoch.id,
                                status="completed",
                                terminal_reason="work_pending",
                                usage=epoch_usage,
                            )
                            aggregate_usage = self._repository.aggregate_epoch_usage(
                                run_id
                            )
                            self._repository.update_usage_owned(
                                self._owned_lease(run_id),
                                aggregate_usage,
                            )
                            await sink.emit(
                                "run.epoch.completed",
                                {
                                    "epoch_id": epoch.id,
                                    "epoch_index": epoch.epoch_index,
                                    "terminal_reason": "work_pending",
                                    "usage": aggregate_usage,
                                },
                            )
                            if session is None:
                                epoch_input = [
                                    *stream.to_input_list(mode="normalized"),
                                    {"role": "user", "content": work_continuation},
                                ]
                            else:
                                epoch_input = work_continuation
                            persisted_item_count = 0
                            consumed_turns += int(epoch_usage["model_turns"])
                            first_epoch = False
                            active_epoch_id = None
                            continue
                        await self._finish_result(
                            run_id,
                            stream,
                            sink,
                            performance=stream_sink.performance(),
                            compiled=compiled,
                            context=context,
                            persisted_item_count=persisted_item_count
                            if first_epoch
                            else 0,
                            session=session,
                            conversation_session=conversation_session,
                            session_snapshot=session_snapshot,
                            epoch_usage=epoch_usage,
                        )
                        finished = self._repository.get(run_id)
                        terminal_reason = (
                            "approval_required"
                            if finished.status == "paused"
                            else "goal_completed"
                        )
                        self._repository.finish_epoch_owned(
                            self._owned_lease(run_id),
                            epoch.id,
                            status=finished.status,
                            terminal_reason=terminal_reason,
                            usage=epoch_usage,
                        )
                        aggregate_usage = self._repository.aggregate_epoch_usage(run_id)
                        self._repository.update_usage_owned(
                            self._owned_lease(run_id),
                            aggregate_usage,
                        )
                        await sink.emit(
                            "run.epoch.completed",
                            {
                                "epoch_id": epoch.id,
                                "epoch_index": epoch.epoch_index,
                                "terminal_reason": terminal_reason,
                                "usage": aggregate_usage,
                            },
                        )
                        active_epoch_id = None
                        return
        except asyncio.CancelledError:
            await _cleanup_internal_prompt_if_needed(context, session)
            if run_id in self._lease_lost_runs:
                pass
            elif self._closing:
                self._repository.abandon_incomplete_epochs_owned(
                    self._owned_lease(run_id)
                )
                await sink.emit(
                    "run.interrupted",
                    {"reason": "process_shutdown", "recoverable": True},
                )
            else:
                await hooks.supersede_active(context, "run_cancelled")
                if self._repository.cancel_owned(self._owned_lease(run_id)):
                    await sink.emit("run.cancelled", {"reason": "user_requested"})
            raise
        except TimeoutError:
            await _cleanup_internal_prompt_if_needed(context, session)
            if run_id in self._lease_lost_runs:
                raise asyncio.CancelledError
            error = (
                f"Run exceeded its {self._settings.agent_run_timeout_seconds:g}-second "
                "deadline."
            )
            await self._fail_or_cancel_owned(
                run_id,
                error,
                sink,
                {"error": error, "terminal_reason": "deadline_exceeded"},
            )
        except (
            InputGuardrailTripwireTriggered,
            OutputGuardrailTripwireTriggered,
            ToolInputGuardrailTripwireTriggered,
            ToolOutputGuardrailTripwireTriggered,
        ) as exc:
            await _cleanup_internal_prompt_if_needed(context, session)
            if run_id in self._lease_lost_runs:
                raise asyncio.CancelledError
            payload = _tripwire_payload(exc)
            await sink.emit("guardrail.tripwire", payload)
            error = f"{type(exc).__name__}: {exc}"
            await self._fail_or_cancel_owned(
                run_id,
                error,
                sink,
                {"error": error},
            )
        except LeaseOwnershipError:
            self._lease_lost_runs.add(run_id)
            raise asyncio.CancelledError
        except Exception as exc:
            await _cleanup_internal_prompt_if_needed(context, session)
            if run_id in self._lease_lost_runs:
                raise asyncio.CancelledError
            if self._repository.get(run_id).cancel_requested:
                await hooks.supersede_active(context, "run_cancelled")
                if self._repository.cancel_owned(self._owned_lease(run_id)):
                    await sink.emit("run.cancelled", {})
            else:
                await hooks.fail_active(context, exc)
                error = f"{type(exc).__name__}: {exc}"
                await self._fail_or_cancel_owned(
                    run_id,
                    error,
                    sink,
                    {
                        "error": error,
                        "terminal_reason": (
                            "budget_exhausted"
                            if isinstance(exc, RunBudgetExceeded)
                            else "error"
                        ),
                    },
                )
        finally:
            self._active_streams.pop(run_id, None)
            if run_id not in self._tasks:
                self._event_sinks.pop(run_id, None)
            inbox = self._steering_inboxes.pop(run_id, None)
            if inbox is not None:
                inbox.close()
            if active_epoch_id is not None and run_id not in self._lease_lost_runs:
                self._repository.abandon_incomplete_epochs_owned(
                    self._owned_lease(run_id)
                )
            if run_id not in self._lease_lost_runs:
                self._log_terminal_run(run_id)

    def _log_terminal_run(self, run_id: str) -> None:
        if self._run_logger is None:
            return
        record = self._repository.get(run_id)
        if record.status not in {"completed", "failed", "cancelled", "paused"}:
            return
        try:
            self._run_logger.write(record)
        except OSError:
            logger.exception("Could not write durable run details for run %s.", run_id)

    def _owned_lease(self, run_id: str) -> RunLease:
        lease = self._active_leases.get(run_id)
        if lease is None:
            raise LeaseOwnershipError(f"Run {run_id} has no active local lease.")
        return lease

    async def _fail_or_cancel_owned(
        self,
        run_id: str,
        error: str,
        sink: PersistedRunEventSink,
        failure_payload: dict[str, Any],
    ) -> None:
        lease = self._owned_lease(run_id)
        if self._repository.fail_owned(lease, error):
            await sink.emit("run.failed", failure_payload)
        elif self._repository.cancel_owned(lease):
            await sink.emit("run.cancelled", {"reason": "user_requested"})

    async def _cleanup_standalone_session_if_terminal(self, run_id: str) -> None:
        record = self._repository.get(run_id)
        if (
            record.conversation_id is None
            and record.status in {"completed", "failed", "cancelled"}
        ):
            await self._sessions.evict(_standalone_session_id(run_id), clear=True)

    def _raise_if_cancelled(self, run_id: str) -> None:
        if self._repository.cancel_requested(run_id):
            raise asyncio.CancelledError

    def _raise_if_lease_lost(self, run_id: str) -> None:
        if run_id in self._lease_lost_runs:
            raise asyncio.CancelledError

    async def _finish_result(
        self,
        run_id: str,
        result: RunResult | RunResultStreaming,
        sink: PersistedRunEventSink,
        *,
        performance: dict[str, Any],
        compiled: CompiledAgent,
        context: ScholarWeaveContext,
        persisted_item_count: int,
        session: Any,
        conversation_session: Any,
        session_snapshot: list[TResponseInputItem] | None,
        epoch_usage: dict[str, Any],
    ) -> None:
        await self._persist_result_activity(
            run_id,
            result,
            sink,
            persisted_item_count=persisted_item_count,
        )
        await _persist_result_to_session_if_needed(
            result,
            runner_session=session,
            conversation_session=conversation_session,
        )

        if result.interruptions:
            await ScholarWeaveRunHooks().supersede_active(context, "run_paused")
            self._raise_if_lease_lost(run_id)
            state = result.to_state().to_json(
                context_serializer=lambda context: {
                    "run_id": context.run_id,
                    "conversation_id": context.conversation_id,
                    "metadata": to_jsonable(
                        _persistable_runtime_metadata(context.metadata)
                    ),
                    "receipts": [
                        {
                            "kind": receipt.kind,
                            "title": receipt.title,
                            "description": receipt.description,
                            "href": receipt.href,
                            "metadata": to_jsonable(receipt.metadata),
                        }
                        for receipt in context.receipts
                    ],
                    "tool_failure_state": serialize_tool_failure_state(
                        context.metadata
                    ),
                }
            )
            for item in result.interruptions:
                self._repository.add_interruption(
                    run_id,
                    item_key=run_item_key(item),
                    tool_name=item.tool_name,
                    item=project_run_item(item),
                )
            if not self._repository.pause_owned(
                self._owned_lease(run_id),
                state=state,
            ):
                if self._repository.cancel_owned(self._owned_lease(run_id)):
                    await sink.emit("run.cancelled", {})
                return
            await sink.emit(
                "run.paused",
                {"interruptions": [project_run_item(item) for item in result.interruptions]},
            )
            return
        if self._repository.get(run_id).cancel_requested:
            await ScholarWeaveRunHooks().supersede_active(context, "run_cancelled")
            self._raise_if_lease_lost(run_id)
            if self._repository.cancel_owned(self._owned_lease(run_id)):
                await sink.emit("run.cancelled", {})
            return
        usage = _merge_usage(
            self._repository.aggregate_epoch_usage(run_id),
            epoch_usage,
        )
        if compiled.completion_validator is not None:
            try:
                compiled.completion_validator(context)
            except Exception:
                if session is not None and session_snapshot is not None:
                    await session.clear_session()
                    if session_snapshot:
                        await session.add_items(session_snapshot)
                raise
        internal_prompt = context.metadata.get("internal_session_prompt")
        if session is not None and isinstance(internal_prompt, str):
            await _remove_internal_session_prompt(session, internal_prompt)
            context.metadata["internal_session_prompt_removed"] = True
        self._raise_if_lease_lost(run_id)
        completed = self._repository.complete_owned(
            self._owned_lease(run_id),
            final_output=to_jsonable(result.final_output),
            last_agent_name=result.last_agent.name,
            usage=usage,
        )
        if not completed:
            if self._repository.cancel_owned(self._owned_lease(run_id)):
                await ScholarWeaveRunHooks().supersede_active(
                    context,
                    "run_cancelled",
                )
                await sink.emit("run.cancelled", {})
            return
        await sink.emit(
            "run.completed",
            {
                "final_output": to_jsonable(result.final_output),
                "last_agent_name": result.last_agent.name,
                "usage": usage,
            },
        )

    async def _persist_result_activity(
        self,
        run_id: str,
        result: RunResult | RunResultStreaming,
        sink: PersistedRunEventSink,
        *,
        persisted_item_count: int,
    ) -> None:
        projected_items = [
            project_run_item(item)
            for item in result.new_items[persisted_item_count:]
        ]
        self._repository.add_items_owned(
            self._owned_lease(run_id),
            projected_items,
        )
        for kind, guardrail_results in (
            ("input", result.input_guardrail_results),
            ("output", result.output_guardrail_results),
            ("tool_input", result.tool_input_guardrail_results),
            ("tool_output", result.tool_output_guardrail_results),
        ):
            for guardrail_result in guardrail_results:
                await sink.emit(
                    "guardrail.result",
                    _guardrail_result_payload(kind, guardrail_result),
                )


def _standalone_session_id(run_id: str) -> str:
    return f"run:{run_id}"


def _validate_completion_policy(compiled: CompiledAgent) -> None:
    if (
        compiled.completion_validator is not None
        and compiled.completion_policy_id is None
    ):
        raise ValueError(
            "A completion validator must have a stable completion_policy_id."
        )


def _uses_local_inference(compiled: CompiledAgent) -> bool:
    return any(
        model.local_inference
        for model in compiled.resolved_models.values()
    )


def _stop_and_answer_compiled(
    compiled: CompiledAgent,
    prompt: str = STOP_AND_ANSWER_PROMPT,
) -> CompiledAgent:
    entry_id = compiled.blueprint.entry_agent_id
    entry_spec = next(
        spec for spec in compiled.blueprint.agents if spec.id == entry_id
    )
    answer_blueprint = compiled.blueprint.model_copy(
        update={
            "description": STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION,
            "agents": [
                entry_spec.model_copy(
                    update={
                        "instructions": prompt,
                        "output": None,
                        "tool_ids": [],
                        "input_guardrail_ids": [],
                        "output_guardrail_ids": [],
                    }
                )
            ],
            "tools": [],
            "handoffs": [],
            "agent_tools": [],
            "guardrails": [],
            "run": compiled.blueprint.run.model_copy(update={"max_turns": 1}),
        }
    )
    answer_agent = compiled.entry_agent.clone(
        name=f"{compiled.entry_agent.name} Available Information Answer",
        instructions=prompt,
        tools=[],
        handoffs=[],
        mcp_servers=[],
        output_type=None,
        input_guardrails=[],
        output_guardrails=[],
    )
    return CompiledAgent(
        blueprint=answer_blueprint,
        entry_agent=answer_agent,
        agents_by_id={entry_id: answer_agent},
        resolved_models={entry_id: compiled.resolved_models[entry_id]},
        run_config=compiled.run_config,
        max_turns=1,
        completion_validator=None,
        context_window_tokens=compiled.context_window_tokens,
    )


def _compiled_prompt_snapshot(
    run_id: str,
    compiled: CompiledAgent,
    *,
    prompt_revision: str | None,
) -> dict[str, Any]:
    specifications = {spec.id: spec for spec in compiled.blueprint.agents}
    agents: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    seen_tools: set[tuple[str, str]] = set()
    for agent_id, agent in compiled.agents_by_id.items():
        spec = specifications[agent_id]
        agents.append(
            {
                "id": agent_id,
                "name": agent.name,
                "source_instructions": spec.instructions,
                "effective_instructions": str(agent.instructions or ""),
                "model": spec.model.model,
                "provider_profile_id": spec.model.provider_profile_id,
            }
        )
        for tool in agent.tools:
            name = str(getattr(tool, "name", type(tool).__name__))
            key = (agent_id, name)
            if key in seen_tools:
                continue
            seen_tools.add(key)
            tools.append(
                {
                    "agent_id": agent_id,
                    "name": name,
                    "description": getattr(tool, "description", None),
                    "parameters_schema": to_jsonable(
                        getattr(tool, "params_json_schema", None)
                    ),
                }
            )
    return {
        "run_id": run_id,
        "prompt_revision": prompt_revision,
        "agents": agents,
        "tools": tools,
        "activated_skills": [],
    }


def _stop_and_answer_input(
    source_input: RunInput,
    session_items: list[TResponseInputItem],
    prompt: str = STOP_AND_ANSWER_PROMPT,
) -> list[TResponseInputItem]:
    answer_input: list[TResponseInputItem] = []
    current_turn_items = _current_turn_session_items(session_items)
    session_text = {
        text
        for item in current_turn_items
        if (text := _session_item_text(item)) is not None
    }
    if isinstance(source_input, str):
        if source_input not in session_text:
            answer_input.append({"role": "user", "content": source_input})
    else:
        serialized_session_items = {
            json.dumps(to_jsonable(item), sort_keys=True)
            for item in current_turn_items
        }
        answer_input.extend(
            item
            for item in source_input
            if json.dumps(to_jsonable(item), sort_keys=True)
            not in serialized_session_items
        )
    answer_input.append({"role": "user", "content": prompt})
    return answer_input


def _stop_and_answer_prompt_from_compiled(compiled: CompiledAgent) -> str:
    entry_id = compiled.blueprint.entry_agent_id
    entry_spec = next(
        spec for spec in compiled.blueprint.agents if spec.id == entry_id
    )
    return entry_spec.instructions


def _stop_and_answer_source_items(
    input_value: Any,
    prompt: str = STOP_AND_ANSWER_PROMPT,
) -> list[TResponseInputItem]:
    if not isinstance(input_value, list):
        return []
    return [
        item
        for item in input_value
        if _session_item_text(item) != prompt
    ]


def _current_turn_session_items(
    session_items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    last_assistant = -1
    for index, item in enumerate(session_items):
        raw = to_jsonable(item)
        if isinstance(raw, dict) and raw.get("role") == "assistant":
            last_assistant = index
    return session_items[last_assistant + 1 :]


async def _remove_internal_session_prompt(session: Any, prompt: str) -> None:
    popped: list[TResponseInputItem] = []
    while True:
        item = await session.pop_item()
        if item is None:
            break
        if _session_item_text(item) == prompt:
            continue
        popped.append(item)
    if popped:
        await session.add_items(list(reversed(popped)))


async def _cleanup_internal_prompt_if_needed(
    context: ScholarWeaveContext,
    session: Any,
) -> None:
    internal_prompt = context.metadata.get("internal_session_prompt")
    if (
        session is None
        or not isinstance(internal_prompt, str)
        or context.metadata.get("internal_session_prompt_removed")
    ):
        return
    await _remove_internal_session_prompt(session, internal_prompt)
    context.metadata["internal_session_prompt_removed"] = True


def _session_item_text(item: TResponseInputItem) -> str | None:
    raw = to_jsonable(item)
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [
        part.get("text")
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts) if parts else None


def _restore_tool_failure_state_from_run_state(
    context: ScholarWeaveContext,
    state: Any,
) -> None:
    if not isinstance(state, dict):
        return
    context_entry = state.get("context")
    if not isinstance(context_entry, dict):
        return
    serialized_context = context_entry.get("context")
    if not isinstance(serialized_context, dict):
        return
    restore_tool_failure_state(
        context.metadata,
        serialized_context.get("tool_failure_state"),
    )

def _guardrail_result_payload(kind: str, result: Any) -> dict[str, Any]:
    guardrail = result.guardrail
    output = result.output
    payload = {
        "kind": kind,
        "guardrail_name": guardrail.get_name(),
        "output_info": to_jsonable(output.output_info),
    }
    tripwire = getattr(output, "tripwire_triggered", None)
    if tripwire is not None:
        payload["tripwire_triggered"] = bool(tripwire)
    behavior = getattr(output, "behavior", None)
    if behavior is not None:
        payload["behavior"] = to_jsonable(behavior)
    return payload


def _tripwire_payload(exc: GuardrailTripwire) -> dict[str, Any]:
    result = getattr(exc, "guardrail_result", None)
    if result is not None:
        kind = (
            "input"
            if isinstance(exc, InputGuardrailTripwireTriggered)
            else "output"
        )
        return _guardrail_result_payload(kind, result)
    guardrail = exc.guardrail
    output = exc.output
    kind = (
        "tool_input"
        if isinstance(exc, ToolInputGuardrailTripwireTriggered)
        else "tool_output"
    )
    return {
        "kind": kind,
        "guardrail_name": guardrail.get_name(),
        "output_info": to_jsonable(output.output_info),
        "behavior": to_jsonable(output.behavior),
    }


def _persisted_stream_text(events: list[Any]) -> tuple[str, str]:
    reasoning = ""
    assistant = ""
    for event in events:
        if event.event_type != "model.stream":
            continue
        payload = event.payload_json
        raw_type = str(payload.get("raw_type") or "")
        delta = payload.get("delta")
        if not isinstance(delta, str):
            continue
        snapshot = payload.get("snapshot") is True
        if raw_type in {
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
        }:
            reasoning = delta if snapshot else reasoning + delta
        elif raw_type == "response.output_text.delta":
            assistant = delta if snapshot else assistant + delta
    return reasoning, assistant


class RunBudgetExceeded(RuntimeError):
    pass


def _persistable_runtime_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    ephemeral_keys = {
        "_steering_inbox",
        "active_epoch_id",
        "epoch_index",
        "goal_state",
    }
    return {key: value for key, value in metadata.items() if key not in ephemeral_keys}


async def _flush_preserving_cancellation(stream_sink: BufferedRunEventSink) -> None:
    flush_task = asyncio.create_task(stream_sink.flush())
    try:
        await asyncio.shield(flush_task)
    except asyncio.CancelledError:
        await flush_task
        raise


def _persist_new_items(
    repository: RunRepository,
    lease: RunLease,
    items: list[Any],
    skip: int,
) -> None:
    repository.add_items_owned(
        lease,
        [project_run_item(item) for item in items[skip:]],
    )


def _continuation_instruction(
    run_id: str,
    epoch_index: int,
    goal_state: dict[str, Any],
) -> str:
    state = json.dumps(goal_state, ensure_ascii=False, separators=(",", ":"))
    return (
        f"[ScholarWeave supervisor epoch {epoch_index} for run {run_id}] "
        "Continue the original request from durable conversation history. Do not repeat "
        "completed research or writes. Finish with the evidence already gathered unless "
        "a specific missing fact still requires another tool call. "
        f"Current durable goal state: {state}"
    )


def _recovery_instruction(run_id: str) -> str:
    return (
        f"[ScholarWeave recovery for run {run_id}] The process stopped during the "
        "previous epoch. Continue from durable conversation history and saved notes. "
        "Do not replay writes unless their outcome has been verified. Resume only "
        "unfinished research."
    )


def _work_continuation(context: ScholarWeaveContext) -> str | None:
    if context.metadata.get("autonomous_work") is not True:
        return None
    plan = context.metadata.get("work_plan")
    if not isinstance(plan, list) or not plan:
        return (
            "Autonomous work is not finished because no work plan exists. Call "
            "create_work_plan now, complete each tracked item, and update every item "
            "to completed or blocked before giving the final answer."
        )
    pending = [
        item
        for item in plan
        if isinstance(item, dict)
        and item.get("status") not in {"completed", "blocked"}
    ]
    if not pending:
        return None
    return (
        "Autonomous work is not finished. Continue with these open work items and "
        "update their statuses before giving the final answer:\n"
        f"{json.dumps(pending, ensure_ascii=False, separators=(',', ':'))}"
    )


def _epoch_model_turns(
    usage: dict[str, Any],
    limit: int,
    consumed_turns: int,
    *,
    resumed: bool,
) -> int:
    performance = usage.get("performance")
    if isinstance(performance, dict):
        model_calls = performance.get("model_calls")
        if isinstance(model_calls, int) and not isinstance(model_calls, bool):
            return max(0, min(limit, model_calls))
    requests = usage.get("requests")
    if isinstance(requests, int) and not isinstance(requests, bool):
        if resumed:
            return max(0, min(limit, requests - consumed_turns))
        return max(0, min(limit, requests))
    return limit


def _merge_usage(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        current = merged.get(key)
        if (
            isinstance(current, (int, float))
            and not isinstance(current, bool)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            merged[key] = current + value
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_usage(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = [*current, *value][-100:]
        else:
            merged[key] = value
    return merged
