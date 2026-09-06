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

from backend.agents.blueprint import AgentBlueprint
from backend.agents.compiler import AgentCompiler, CompiledAgent, with_reasoning_effort
from backend.agents.context import ScholarWeaveContext, ToolRuntime
from backend.agents.harness import (
    ConversationItem,
    MaxTurnsExceeded,
    RunInput,
    RunPolicyViolation,
    RunResult,
    item_text,
    run_streamed,
)
from backend.conversations.sessions import ConversationSessionFactory
from backend.conversations.steering import (
    SteeringInbox,
    SteeringMessage,
    emit_steering_applied,
)
from backend.core.config import Settings
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.prompting.registry import PromptRegistry
from backend.providers.inference import InferenceScheduler, inference_priority
from backend.providers.reasoning import ReasoningEffort
from backend.runs.events import (
    BufferedRunEventSink,
    EventBroker,
    PersistedRunEventSink,
)
from backend.runs.hooks import ScholarWeaveRunHooks
from backend.runs.logging import RunDetailLogger
from backend.runs.repository import (
    LeaseOwnershipError,
    RunLease,
    RunRepository,
)
from backend.tools.failures import restore_tool_failure_state_from_attempts
from backend.utils import merge_usage, to_jsonable, utcnow

logger = logging.getLogger(__name__)
STOP_AND_ANSWER_PROMPT = (
    "Stop all further research. Answer the user's request now using only the conversation "
    "history, tool results, and evidence already available. Give the most useful direct answer "
    "you can, clearly identify important uncertainty or missing evidence, and do not suggest or "
    "attempt additional tool calls."
)
STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION = "scholarweave:internal:stop-and-answer"


class RunLeaseLost(LeaseOwnershipError):
    pass


class RunBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseDeadline:
    lease: RunLease
    monotonic_expires_at: float


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


def _restore_work_plan(metadata: dict[str, Any], attempts: list[Any]) -> None:
    for attempt in attempts:
        if (
            attempt.status != "completed"
            or attempt.catalog_id not in {"work.plan.create", "work.plan.update", "work.plan.read"}
            or not isinstance(attempt.result_json, dict)
        ):
            continue
        items = attempt.result_json.get("items")
        if isinstance(items, list) and items and all(isinstance(item, dict) for item in items):
            metadata["work_plan"] = [dict(item) for item in items]


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
        sessions: ConversationSessionFactory,
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
        self._completion_validators = dict(completion_validators or {})
        self._owner_id = str(uuid.uuid4())
        self._closing = False
        self._active_runs: dict[str, Any] = {}
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
        """Restart interrupted runs from durable conversation and journal state."""
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
                if record.cancel_requested:
                    if self._repository.cancel_owned(lease_deadline.lease):
                        await self._event_sink(record.id).emit(
                            "run.cancelled",
                            {"reason": "user_requested"},
                        )
                    continue
                if record.conversation_id is not None:
                    self._steering_inboxes[record.id] = _restore_pending_steering(
                        self._repository.events_after(record.id)
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
                recovered = record.status != "pending"
                if recovered:
                    self._repository.abandon_incomplete_epochs_owned(lease_deadline.lease)
                if not recovered:
                    input_value = record.input_json
                elif (
                    record.conversation_id is not None
                    and compiled.blueprint.description
                    == STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION
                ):
                    session = self._sessions.get(
                        record.conversation_id,
                        compiled.blueprint.session,
                    )
                    stop_prompt = _stop_and_answer_prompt_from_compiled(compiled)
                    input_value = _stop_and_answer_input(
                        _stop_and_answer_source_items(record.input_json, stop_prompt),
                        await session.get_items(),
                        stop_prompt,
                    )
                else:
                    input_value = (
                        self._prompts.render("run-recovery", run_id=record.id)
                        if self._prompts is not None
                        else _recovery_instruction(record.id)
                    )
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
                        "recovered": recovered,
                    },
                )
                restore_tool_failure_state_from_attempts(
                    runtime_context.metadata,
                    record.tool_attempts,
                )
                _restore_work_plan(runtime_context.metadata, record.tool_attempts)
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
        compiled = with_reasoning_effort(compiled, reasoning_effort)
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
        input_value: RunInput,
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
        input_value: RunInput,
        *,
        conversation_id: str | None = None,
    ):
        _validate_completion_policy(compiled)
        record = self._repository.create(
            conversation_id=conversation_id,
            agent_name=compiled.blueprint.name,
            input_value=to_jsonable(input_value),
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
        active = self._active_runs.get(run_id)
        if active is not None:
            active.cancel()
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._wait_for_claim_cleanups(run_id)
        current = self._repository.get(run_id)
        lease = self._active_leases.get(run_id)
        if (
            current.status == "pending"
            and lease is None
            and isinstance(current.runtime_metadata_json, dict)
            and current.runtime_metadata_json.get("summary_batch_previous_run_id")
        ):
            await self._settle_waiting_summary(run_id)
            current = self._repository.get(run_id)
        if (
            current.status in {"pending", "running"}
            and lease is not None
            and self._repository.cancel_owned(lease)
        ):
            sink = self._event_sink(run_id)
            await sink.emit("run.cancelled", {"reason": "user_requested"})
        current = self._repository.get(run_id)
        if current.status in {"completed", "failed", "cancelled"}:
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
            session = self._sessions.get(
                record.conversation_id,
                compiled.blueprint.session,
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
        tasks = [self._tasks[record.id] for record in active if record.id in self._tasks]
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

    async def delete_conversation_runs(self, conversation_id: str) -> None:
        await self.cancel_conversation_runs(conversation_id)
        records = self._repository.list(conversation_id=conversation_id)
        if any(record.status not in {"completed", "failed", "cancelled"} for record in records):
            raise ConflictError("Conversation runs are still stopping. Retry deletion once they finish.")
        if self._run_logger is not None:
            self._run_logger.delete_conversation(conversation_id)
        if self._delete_history([record.id for record in records]) != len(records):
            raise ConflictError("Conversation runs are still active and cannot be deleted.")

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
        for active in tuple(self._active_runs.values()):
            active.cancel()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._wait_for_all_claim_cleanups()

    def _delete_history(self, run_ids: list[str]) -> int:
        active_ids = set(self._active_runs) | {
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
                if confirmed_at >= lease_deadline.monotonic_expires_at - safety_margin:
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
        claim_task: "asyncio.Task[RunLease | None]",
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
            if self._repository.cancel_requested(run_id) and self._repository.cancel_owned(
                lease
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
            raise RunLeaseLost(f"Run {current.lease.run_id} claim is no longer owned.")
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
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

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
        input_value: RunInput,
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        preclaimed: bool = False,
        lease_deadline: LeaseDeadline | None = None,
    ) -> None:
        record_metadata = self._repository.get(run_id).runtime_metadata_json
        if isinstance(record_metadata, dict) and record_metadata.get("summary_batch_previous_run_id"):
            # Recovery may preclaim all queued records. Release that claim before
            # waiting; queued work must not hold a lease.
            if preclaimed and lease_deadline is not None:
                try:
                    await self._release_claim(lease_deadline.lease)
                finally:
                    self._active_leases.pop(run_id, None)
                preclaimed = False
                lease_deadline = None
            try:
                await self._wait_for_summary_predecessor(run_id)
            except asyncio.CancelledError:
                if self._repository.cancel_requested(run_id):
                    await self._settle_waiting_summary(run_id)
                raise
            except Exception as exc:
                await self._settle_waiting_summary(run_id, error=f"{type(exc).__name__}: {exc}")
                return
        await self._execute_run(
            run_id,
            compiled,
            input_value,
            conversation_id=conversation_id,
            runtime_context=runtime_context,
            runtime_metadata=runtime_metadata,
            preclaimed=preclaimed,
            lease_deadline=lease_deadline,
        )

    def _pending_summary_predecessor(self, run_id: str) -> str | None:
        record = self._repository.get(run_id)
        seen = {run_id}
        pending = None
        while isinstance(record.runtime_metadata_json, dict):
            previous_id = record.runtime_metadata_json.get("summary_batch_previous_run_id")
            if not previous_id:
                break
            if not isinstance(previous_id, str) or previous_id in seen:
                raise RuntimeError("Invalid or cyclic summary batch dependency.")
            seen.add(previous_id)
            try:
                record = self._repository.get(previous_id)
            except NotFoundError:
                # Pruned terminal history is no longer a dependency.
                break
            if pending is None and record.status in {"pending", "running"}:
                pending = previous_id
        return pending

    async def _wait_for_summary_predecessor(self, run_id: str) -> None:
        while True:
            if self._repository.cancel_requested(run_id):
                raise asyncio.CancelledError
            previous_id = self._pending_summary_predecessor(run_id)
            if previous_id is None:
                return
            previous_task = self._tasks.get(previous_id)
            if previous_task is None or previous_task.done():
                await asyncio.sleep(0.1)
                continue
            try:
                await asyncio.shield(previous_task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                # The terminal database state, not the predecessor task outcome,
                # determines readiness. Cancelled intermediate jobs retain ancestry.
                pass

    async def _settle_waiting_summary(self, run_id: str, *, error: str | None = None) -> None:
        deadline = await self._acquire_claim(run_id)
        if deadline is None:
            return
        lease = deadline.lease
        self._active_leases[run_id] = lease
        try:
            sink = self._event_sink(run_id)
            if error is None:
                if self._repository.cancel_owned(lease):
                    await sink.emit("run.cancelled", {"reason": "user_requested"})
            else:
                await self._fail_or_cancel_owned(run_id, error, sink, {"error": error})
            self._log_terminal_run(run_id)
            await self._cleanup_standalone_session_if_terminal(run_id)
        finally:
            await self._release_claim(lease)
            if self._active_leases.get(run_id) == lease:
                self._active_leases.pop(run_id, None)

    async def _execute_run(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput,
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
        runtime_metadata: dict[str, Any] | None = None,
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
        input_value: RunInput,
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None,
        runtime_metadata: dict[str, Any] | None,
    ) -> None:
        await self._execute_claimed_run(
            run_id,
            compiled,
            input_value,
            conversation_id=conversation_id,
            runtime_context=runtime_context,
            runtime_metadata=runtime_metadata,
        )

    async def _execute_claimed_run(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput,
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        sink = self._event_sink(run_id)
        record_metadata = self._repository.get(run_id).runtime_metadata_json
        context = runtime_context or ScholarWeaveContext(
            run_id=run_id,
            conversation_id=conversation_id,
            tool_runtime=self._tool_runtime,
            event_sink=sink,
            metadata={
                **(dict(record_metadata) if isinstance(record_metadata, dict) else {}),
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
        session_id = conversation_id or _standalone_session_id(run_id)
        session = self._sessions.get(session_id, compiled.blueprint.session)
        steering_inbox = (
            self._steering_inboxes.setdefault(run_id, SteeringInbox())
            if conversation_id is not None
            else None
        )
        if steering_inbox is not None:
            steering_inbox.bind_session(session)
            context.metadata["_steering_inbox"] = steering_inbox
        initial_reasoning, initial_assistant = _persisted_stream_text(
            self._repository.get(run_id).events
        )
        self._repository.mark_running_owned(self._owned_lease(run_id))
        recovered = bool(context.metadata.get("recovered"))
        await sink.emit(
            "run.started",
            {"agent_name": compiled.blueprint.name, "recovered": recovered},
        )
        if recovered:
            await sink.emit(
                "run.recovered",
                {"reason": "process_interrupted", "strategy": "new_epoch"},
            )
        active_epoch_id: str | None = None
        deferred_steering: list[SteeringMessage] = []
        try:
            self._raise_if_cancelled(run_id)
            async with self._sessions.run_lock(session_id):
                priority = (
                    "background"
                    if context.metadata.get("autonomous_work")
                    or context.metadata.get("paper_summary_document_id")
                    else "interactive"
                )
                # Model tasks inherit priority without holding a model lease.
                with inference_priority(priority):
                    self._raise_if_cancelled(run_id)
                    session_checkpoint: int | None = None
                    epoch_input: RunInput = input_value
                    first_epoch = True
                    consumed_turns = self._repository.consumed_model_turns(run_id)
                    while True:
                        self._raise_if_cancelled(run_id)
                        remaining_turns = (
                            compiled.max_turns - consumed_turns
                            if compiled.max_turns is not None
                            else None
                        )
                        if remaining_turns is not None and remaining_turns <= 0:
                            raise RunBudgetExceeded(
                                f"Run exhausted its {compiled.max_turns}-turn budget."
                            )
                        epoch_turn_limit = self._settings.agent_epoch_max_turns
                        if remaining_turns is not None:
                            epoch_turn_limit = min(remaining_turns, epoch_turn_limit)
                        epoch = self._repository.begin_epoch_owned(
                            self._owned_lease(run_id),
                            to_jsonable(epoch_input),
                        )
                        active_epoch_id = epoch.id
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
                                "max_epochs": None,
                                "max_turns": epoch_turn_limit,
                                "remaining_run_turns": remaining_turns,
                            },
                        )
                        stream_sink = BufferedRunEventSink(
                            sink,
                            initial_reasoning=initial_reasoning if first_epoch else "",
                            initial_assistant=initial_assistant if first_epoch else "",
                        )
                        context.event_sink = stream_sink
                        if steering_inbox is not None:
                            steering_inbox.begin_epoch()
                        if deferred_steering:
                            await emit_steering_applied(context, deferred_steering)
                            deferred_steering = []
                        epoch_items = await _resolved_epoch_input(
                            session, epoch_input, internal=not first_epoch
                        )
                        context.metadata["_working_base_cursor"] = await session.checkpoint()
                        if session_checkpoint is None:
                            session_checkpoint = await session.checkpoint()
                        try:
                            handle = run_streamed(
                                compiled.entry_agent,
                                epoch_items,
                                context=context,
                                settings=compiled.run_settings,
                                max_turns=epoch_turn_limit,
                                hooks=hooks,
                                context_policy=compiled.context_policy,
                            )
                            self._active_runs[run_id] = handle
                            try:
                                result = await handle
                            finally:
                                await _flush_preserving_cancellation(stream_sink)
                        except MaxTurnsExceeded as exc:
                            usage = exc.run_data.usage.to_dict()
                            usage["performance"] = stream_sink.performance()
                            usage["model_turns"] = epoch_turn_limit
                            self._persist_items(run_id, exc.run_data.new_items)
                            await _commit_epoch_context(session, exc.run_data, context)
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
                            epoch_input = (
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
                            self._active_runs.pop(run_id, None)

                        epoch_usage = result.usage.to_dict()
                        epoch_usage["performance"] = stream_sink.performance()
                        epoch_usage["model_turns"] = _epoch_model_turns(
                            epoch_usage,
                            epoch_turn_limit,
                        )
                        pending_steering = (
                            steering_inbox.take_pending_or_close()
                            if steering_inbox is not None
                            else []
                        )
                        if pending_steering:
                            self._persist_items(run_id, result.new_items)
                            await _commit_epoch_context(session, result, context)
                            aggregate_usage = self._finish_epoch(
                                run_id,
                                epoch,
                                epoch_usage,
                                "steering_continuation",
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
                            epoch_input = []
                            deferred_steering = pending_steering
                            consumed_turns += int(epoch_usage["model_turns"])
                            first_epoch = False
                            active_epoch_id = None
                            continue
                        work_continuation = _work_continuation(context)
                        if work_continuation is not None:
                            self._persist_items(run_id, result.new_items)
                            await _commit_epoch_context(session, result, context)
                            aggregate_usage = self._finish_epoch(
                                run_id,
                                epoch,
                                epoch_usage,
                                "work_pending",
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
                            epoch_input = work_continuation
                            consumed_turns += int(epoch_usage["model_turns"])
                            first_epoch = False
                            active_epoch_id = None
                            continue
                        context.metadata["completion_output"] = _completion_output_text(
                            result.final_output
                        )
                        try:
                            completion_repair = _completion_repair_instruction(
                                compiled,
                                context,
                            )
                        finally:
                            context.metadata.pop("completion_output", None)
                        if completion_repair is not None:
                            self._persist_items(run_id, result.new_items)
                            await _commit_epoch_context(session, result, context)
                            aggregate_usage = self._finish_epoch(
                                run_id,
                                epoch,
                                epoch_usage,
                                "completion_rejected",
                            )
                            await sink.emit(
                                "run.epoch.completed",
                                {
                                    "epoch_id": epoch.id,
                                    "epoch_index": epoch.epoch_index,
                                    "terminal_reason": "completion_rejected",
                                    "usage": aggregate_usage,
                                },
                            )
                            epoch_input = completion_repair
                            consumed_turns += int(epoch_usage["model_turns"])
                            first_epoch = False
                            active_epoch_id = None
                            continue
                        await self._finish_result(
                            run_id,
                            result,
                            sink,
                            compiled=compiled,
                            context=context,
                            session=session,
                            session_checkpoint=session_checkpoint,
                            epoch_usage=epoch_usage,
                            completion_validated=True,
                        )
                        finished = self._repository.get(run_id)
                        aggregate_usage = self._finish_epoch(
                            run_id,
                            epoch,
                            epoch_usage,
                            "goal_completed",
                            status=finished.status,
                        )
                        await sink.emit(
                            "run.epoch.completed",
                            {
                                "epoch_id": epoch.id,
                                "epoch_index": epoch.epoch_index,
                                "terminal_reason": "goal_completed",
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
        except RunPolicyViolation as exc:
            await _cleanup_internal_prompt_if_needed(context, session)
            if run_id in self._lease_lost_runs:
                raise asyncio.CancelledError
            await sink.emit("run.policy.rejected", {"policy": exc.policy, **exc.detail})
            error = f"{type(exc).__name__}: {exc}"
            await self._fail_or_cancel_owned(run_id, error, sink, {"error": error})
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
            self._active_runs.pop(run_id, None)
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

    def _finish_epoch(
        self,
        run_id: str,
        epoch: Any,
        epoch_usage: dict[str, Any],
        terminal_reason: str,
        *,
        status: str = "completed",
    ) -> dict[str, Any]:
        self._repository.finish_epoch_owned(
            self._owned_lease(run_id),
            epoch.id,
            status=status,
            terminal_reason=terminal_reason,
            usage=epoch_usage,
        )
        aggregate_usage = self._repository.aggregate_epoch_usage(run_id)
        self._repository.update_usage_owned(self._owned_lease(run_id), aggregate_usage)
        return aggregate_usage

    def _persist_items(self, run_id: str, items: list[dict[str, Any]]) -> None:
        self._repository.add_items_owned(self._owned_lease(run_id), items)

    def _log_terminal_run(self, run_id: str) -> None:
        if self._run_logger is None:
            return
        record = self._repository.get(run_id)
        if record.status not in {"completed", "failed", "cancelled"}:
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
        if record.conversation_id is None and record.status in {
            "completed",
            "failed",
            "cancelled",
        }:
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
        result: RunResult,
        sink: PersistedRunEventSink,
        *,
        compiled: CompiledAgent,
        context: ScholarWeaveContext,
        session: Any,
        session_checkpoint: int,
        epoch_usage: dict[str, Any],
        completion_validated: bool = False,
    ) -> None:
        self._persist_items(run_id, result.new_items)
        await _commit_epoch_context(session, result, context)

        if self._repository.get(run_id).cancel_requested:
            await ScholarWeaveRunHooks().supersede_active(context, "run_cancelled")
            self._raise_if_lease_lost(run_id)
            if self._repository.cancel_owned(self._owned_lease(run_id)):
                await sink.emit("run.cancelled", {})
            return
        usage = merge_usage(
            self._repository.aggregate_epoch_usage(run_id),
            epoch_usage,
        )
        if compiled.completion_validator is not None and not completion_validated:
            try:
                context.metadata["completion_output"] = _completion_output_text(
                    result.final_output
                )
                compiled.completion_validator(context)
            except Exception:
                await session.rollback_to(session_checkpoint)
                raise
            finally:
                context.metadata.pop("completion_output", None)
        internal_prompt = context.metadata.get("internal_session_prompt")
        if isinstance(internal_prompt, str):
            await _remove_internal_session_prompt(session, internal_prompt)
            context.metadata["internal_session_prompt_removed"] = True
        self._raise_if_lease_lost(run_id)
        completed = self._repository.complete_owned(
            self._owned_lease(run_id),
            final_output=to_jsonable(result.final_output),
            last_agent_name=result.last_agent_name,
            usage=usage,
        )
        if not completed:
            if self._repository.cancel_owned(self._owned_lease(run_id)):
                await ScholarWeaveRunHooks().supersede_active(context, "run_cancelled")
                await sink.emit("run.cancelled", {})
            return
        await sink.emit(
            "run.completed",
            {
                "final_output": to_jsonable(result.final_output),
                "last_agent_name": result.last_agent_name,
                "usage": usage,
            },
        )


async def _resolved_epoch_input(
    session: Any,
    epoch_input: RunInput,
    *,
    internal: bool = False,
) -> list[ConversationItem]:
    """Build one epoch's model input from durable history plus the new items."""
    read_working = getattr(session, "get_working_items", session.get_items)
    history = await read_working()
    if isinstance(epoch_input, str):
        new_items: list[ConversationItem] = [{"role": "user", "content": epoch_input}]
        if internal:
            new_items[0]["_scholarweave_internal_continuation"] = True
    else:
        new_items = [dict(item) for item in epoch_input if isinstance(item, dict)]
    if new_items:
        await session.add_items(new_items)
    return [*history, *new_items]


async def _commit_epoch_context(
    session: Any, result: RunResult, context: ScholarWeaveContext
) -> None:
    commit = getattr(session, "commit_working_items", None)
    if not callable(commit):
        await session.add_items(result.generated_items)
        return
    working = result.working_snapshot_items
    covered = result.working_snapshot_generated_count
    if working is None:
        working = result.working_items
        covered = len(result.generated_items)
    internal_prompt = context.metadata.get("internal_session_prompt")
    if working is not None and context.metadata.get("internal_session_prompt_removed"):
        working = [
            item for item in working
            if not (item.get("role") == "user" and item.get("content") == internal_prompt)
        ]
    await commit(
        result.generated_items,
        working,
        base_cursor=int(context.metadata.get("_working_base_cursor", 0)),
        commit_id=str(context.metadata.get("active_epoch_id") or context.run_id),
        covered_item_count=covered,
    )


def _standalone_session_id(run_id: str) -> str:
    return f"run:{run_id}"


def _validate_completion_policy(compiled: CompiledAgent) -> None:
    if compiled.completion_validator is not None and compiled.completion_policy_id is None:
        raise ValueError("A completion validator must have a stable completion_policy_id.")


def _stop_and_answer_compiled(
    compiled: CompiledAgent,
    prompt: str = STOP_AND_ANSWER_PROMPT,
) -> CompiledAgent:
    entry_id = compiled.blueprint.entry_agent_id
    entry_spec = next(spec for spec in compiled.blueprint.agents if spec.id == entry_id)
    answer_blueprint = compiled.blueprint.model_copy(
        update={
            "description": STOP_AND_ANSWER_BLUEPRINT_DESCRIPTION,
            "agents": [
                entry_spec.model_copy(
                    update={"instructions": prompt, "output": None, "tool_ids": []}
                )
            ],
            "tools": [],
            "agent_tools": [],
            "run": compiled.blueprint.run.model_copy(update={"max_turns": 1}),
        }
    )
    answer_agent = replace(
        compiled.entry_agent,
        instructions=prompt,
        name=f"{compiled.entry_agent.name} Available Information Answer",
        tools=[],
        output_schema=None,
    )
    return CompiledAgent(
        blueprint=answer_blueprint,
        entry_agent=answer_agent,
        agents_by_id={entry_id: answer_agent},
        resolved_models={entry_id: compiled.resolved_models[entry_id]},
        run_settings=replace(compiled.run_settings, max_turns=1),
        max_turns=1,
        context_policy=compiled.context_policy,
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
                "effective_instructions": agent.instructions,
                "model": spec.model.model,
                "provider_profile_id": spec.model.provider_profile_id,
            }
        )
        for tool in agent.tools:
            key = (agent_id, tool.name)
            if key in seen_tools:
                continue
            seen_tools.add(key)
            tools.append(
                {
                    "agent_id": agent_id,
                    "name": tool.name,
                    "description": tool.description,
                    "parameters_schema": to_jsonable(tool.params_json_schema),
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
    session_items: list[ConversationItem],
    prompt: str = STOP_AND_ANSWER_PROMPT,
) -> list[ConversationItem]:
    answer_input: list[ConversationItem] = []
    current_turn_items = _current_turn_session_items(session_items)
    session_text = {
        text for item in current_turn_items if (text := item_text(item)) is not None
    }
    if isinstance(source_input, str):
        if source_input not in session_text:
            answer_input.append({"role": "user", "content": source_input})
    else:
        serialized_session_items = {
            json.dumps(to_jsonable(item), sort_keys=True) for item in current_turn_items
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
    entry_spec = next(spec for spec in compiled.blueprint.agents if spec.id == entry_id)
    return entry_spec.instructions


def _stop_and_answer_source_items(
    input_value: Any,
    prompt: str = STOP_AND_ANSWER_PROMPT,
) -> list[ConversationItem]:
    if not isinstance(input_value, list):
        return []
    return [item for item in input_value if item_text(item) != prompt]


def _current_turn_session_items(
    session_items: list[ConversationItem],
) -> list[ConversationItem]:
    last_assistant = -1
    for index, item in enumerate(session_items):
        raw = to_jsonable(item)
        if isinstance(raw, dict) and raw.get("role") == "assistant":
            last_assistant = index
    return session_items[last_assistant + 1 :]


async def _remove_internal_session_prompt(session: Any, prompt: str) -> None:
    popped: list[ConversationItem] = []
    while True:
        item = await session.pop_item()
        if item is None:
            break
        if item_text(item) == prompt:
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


def _persisted_stream_text(events: list[Any]) -> tuple[str, str]:
    reasoning = ""
    assistant = ""
    for event in events:
        if event.event_type == "model.retry":
            payload = event.payload_json
            discarded = payload.get("discarded_text_characters")
            if (
                payload.get("delegated") is not True
                and isinstance(discarded, int)
                and not isinstance(discarded, bool)
                and discarded > 0
            ):
                assistant = assistant[:-discarded]
            continue
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


def _persistable_runtime_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    ephemeral_keys = {
        "_steering_inbox",
        "active_epoch_id",
        "epoch_index",
        "goal_state",
        "completion_output",
        "active_model",
        "_working_base_cursor",
    }
    return {key: value for key, value in metadata.items() if key not in ephemeral_keys}


async def _flush_preserving_cancellation(stream_sink: BufferedRunEventSink) -> None:
    flush_task = asyncio.create_task(stream_sink.flush())
    try:
        await asyncio.shield(flush_task)
    except asyncio.CancelledError:
        await flush_task
        raise


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
        return None
    pending = [
        item
        for item in plan
        if isinstance(item, dict) and item.get("status") not in {"completed", "blocked"}
    ]
    if not pending:
        return None
    return (
        "Autonomous work is not finished. Continue with these open work items and "
        "update their statuses before giving the final answer:\n"
        f"{json.dumps(pending, ensure_ascii=False, separators=(',', ':'))}"
    )


def _epoch_model_turns(usage: dict[str, Any], limit: int) -> int:
    performance = usage.get("performance")
    if isinstance(performance, dict):
        model_calls = performance.get("main_model_calls", performance.get("model_calls"))
        if isinstance(model_calls, int) and not isinstance(model_calls, bool):
            return max(0, min(limit, model_calls))
    requests = usage.get("requests")
    if isinstance(requests, int) and not isinstance(requests, bool):
        return max(0, min(limit, requests))
    return limit


def _completion_output_text(output: Any) -> str:
    return output if isinstance(output, str) else json.dumps(to_jsonable(output), ensure_ascii=False)


def _completion_repair_instruction(
    compiled: CompiledAgent,
    context: ScholarWeaveContext,
) -> str | None:
    validator = compiled.completion_validator
    if validator is None:
        return None
    try:
        validator(context)
    except ValidationError as exc:
        issues = "\n".join(f"- {issue}" for issue in exc.issues)
        detail = f"\nMissing requirements:\n{issues}" if issues else ""
        return (
            "Your attempted final answer did not satisfy the run's completion requirements. "
            "Do not repeat the final answer yet. Use the available tools to complete every missing "
            "requirement below, then provide the final answer."
            f"\n\n{exc.message}{detail}"
        )
    return None
