from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agents import (
    InputGuardrailTripwireTriggered,
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

from backend.agents.compiler import CompiledAgent
from backend.runs.broker import EventBroker
from backend.runs.events import PersistedRunEventSink
from backend.runs.projector import project_run_item, project_stream_event, run_item_key
from backend.runs.repository import RunRepository
from backend.runtime.context import ScholarWeaveContext, ToolRuntime
from backend.runtime.compaction_events import observe_compaction
from backend.runtime.hooks import ScholarWeaveRunHooks
from backend.runtime.serialization import to_jsonable
from backend.runtime.sessions import SdkSessionFactory

RunInput = str | list[TResponseInputItem]
GuardrailTripwire = (
    InputGuardrailTripwireTriggered
    | OutputGuardrailTripwireTriggered
    | ToolInputGuardrailTripwireTriggered
    | ToolOutputGuardrailTripwireTriggered
)


class RunService:
    def __init__(
        self,
        repository: RunRepository,
        sessions: SdkSessionFactory,
        tool_runtime: ToolRuntime,
        event_broker: EventBroker,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._tool_runtime = tool_runtime
        self._broker = event_broker
        self._active_streams: dict[str, RunResultStreaming] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def list(self):
        return self._repository.list()

    def get(self, run_id: str):
        return self._repository.get(run_id)

    def create(
        self,
        compiled: CompiledAgent,
        input_value: RunInput,
        *,
        agent_revision_id: str | None,
        conversation_id: str | None,
    ):
        record = self._repository.create(
            agent_revision_id=agent_revision_id,
            conversation_id=conversation_id,
            agent_name=compiled.blueprint.name,
            input_value=to_jsonable(input_value),
            blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
        )
        task = asyncio.create_task(
            self._execute(
                record.id,
                compiled,
                input_value,
                conversation_id=conversation_id,
            )
        )
        self._tasks[record.id] = task
        task.add_done_callback(lambda _task, run_id=record.id: self._tasks.pop(run_id, None))
        return record

    async def run_now(
        self,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        agent_revision_id: str | None = None,
        conversation_id: str | None = None,
    ):
        record = self._repository.create(
            agent_revision_id=agent_revision_id,
            conversation_id=conversation_id,
            agent_name=compiled.blueprint.name,
            input_value=to_jsonable(input_value)
            if not isinstance(input_value, RunState)
            else {"type": "run_state"},
            blueprint=compiled.blueprint.model_dump(mode="json", by_alias=True),
        )
        await self._execute(
            record.id,
            compiled,
            input_value,
            conversation_id=conversation_id,
        )
        return self._repository.get(record.id)

    async def cancel(self, run_id: str):
        record = self._repository.get(run_id)
        if record.status not in {"pending", "running"}:
            return record
        self._repository.request_cancel(run_id)
        stream = self._active_streams.get(run_id)
        if stream is not None:
            stream.cancel("immediate")
        return self._repository.get(run_id)

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
        sink = PersistedRunEventSink(run_id, self._repository, self._broker)
        live_context = ScholarWeaveContext(
            run_id=run_id,
            conversation_id=record.conversation_id,
            tool_runtime=self._tool_runtime,
            event_sink=sink,
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
        self._repository.resolve_interruption(
            interruption_id,
            status=status,
            response=response,
        )
        task = asyncio.create_task(
            self._execute(
                run_id,
                compiled,
                state,
                conversation_id=record.conversation_id,
                runtime_context=live_context,
            )
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        return self._repository.get(run_id)

    def delete(self, run_id: str) -> None:
        if run_id in self._active_streams:
            raise ValueError("An active run cannot be deleted.")
        self._repository.delete(run_id)

    def events_after(self, run_id: str, sequence: int = -1):
        self._repository.get(run_id)
        return self._repository.events_after(run_id, sequence)

    async def close(self) -> None:
        for stream in tuple(self._active_streams.values()):
            stream.cancel("immediate")
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(
        self,
        run_id: str,
        compiled: CompiledAgent,
        input_value: RunInput | RunState[ScholarWeaveContext],
        *,
        conversation_id: str | None,
        runtime_context: ScholarWeaveContext | None = None,
    ) -> None:
        sink = PersistedRunEventSink(run_id, self._repository, self._broker)
        if isinstance(input_value, RunState) and runtime_context is None:
            raise ValueError("Resuming an SDK RunState requires restored live context.")
        context = runtime_context or ScholarWeaveContext(
                run_id=run_id,
                conversation_id=conversation_id,
                tool_runtime=self._tool_runtime,
                event_sink=sink,
        )
        context.event_sink = sink
        hooks = ScholarWeaveRunHooks()
        entry_model = compiled.resolved_models[compiled.blueprint.entry_agent_id]
        session = (
            self._sessions.get(conversation_id, compiled.blueprint.session, entry_model)
            if conversation_id is not None and not isinstance(input_value, RunState)
            else None
        )
        lock = (
            self._sessions.run_lock(conversation_id)
            if conversation_id is not None
            else _null_async_context()
        )
        persisted_item_count = len(self._repository.get(run_id).items)
        self._repository.mark_running(run_id)
        await sink.emit(
            "run.resumed" if isinstance(input_value, RunState) else "run.started",
            {"agent_name": compiled.blueprint.name},
        )
        try:
            async with lock, observe_compaction(sink.emit):
                stream = Runner.run_streamed(
                    compiled.entry_agent,
                    input_value,
                    context=context if not isinstance(input_value, RunState) else None,
                    max_turns=compiled.max_turns,
                    hooks=hooks,
                    run_config=compiled.run_config,
                    session=session,
                )
                self._active_streams[run_id] = stream
                async for event in stream.stream_events():
                    projected = project_stream_event(event)
                    if projected is not None:
                        await sink.emit(*projected)
                await self._finish_result(
                    run_id,
                    stream,
                    sink,
                    compiled=compiled,
                    context=context,
                    persisted_item_count=persisted_item_count,
                )
        except asyncio.CancelledError:
            self._repository.cancel(run_id)
            await sink.emit("run.cancelled", {})
            raise
        except (
            InputGuardrailTripwireTriggered,
            OutputGuardrailTripwireTriggered,
            ToolInputGuardrailTripwireTriggered,
            ToolOutputGuardrailTripwireTriggered,
        ) as exc:
            payload = _tripwire_payload(exc)
            await sink.emit("guardrail.tripwire", payload)
            error = f"{type(exc).__name__}: {exc}"
            self._repository.fail(run_id, error)
            await sink.emit("run.failed", {"error": error})
        except Exception as exc:
            if self._repository.get(run_id).cancel_requested:
                self._repository.cancel(run_id)
                await sink.emit("run.cancelled", {})
            else:
                self._repository.fail(run_id, f"{type(exc).__name__}: {exc}")
                await sink.emit(
                    "run.failed",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
        finally:
            self._active_streams.pop(run_id, None)

    async def _finish_result(
        self,
        run_id: str,
        result: RunResult | RunResultStreaming,
        sink: PersistedRunEventSink,
        *,
        compiled: CompiledAgent,
        context: ScholarWeaveContext,
        persisted_item_count: int,
    ) -> None:
        projected_items = [
            project_run_item(item)
            for item in result.new_items[persisted_item_count:]
        ]
        self._repository.add_items(run_id, projected_items)
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
        if result.interruptions:
            state = result.to_state().to_json(
                context_serializer=lambda context: {
                    "run_id": context.run_id,
                    "conversation_id": context.conversation_id,
                }
            )
            for item in result.interruptions:
                self._repository.add_interruption(
                    run_id,
                    item_key=run_item_key(item),
                    tool_name=item.tool_name,
                    item=project_run_item(item),
                )
            self._repository.pause(run_id, state=state)
            await sink.emit(
                "run.paused",
                {"interruptions": [project_run_item(item) for item in result.interruptions]},
            )
            return
        if self._repository.get(run_id).cancel_requested:
            self._repository.cancel(run_id)
            await sink.emit("run.cancelled", {})
            return
        usage = to_jsonable(result.context_wrapper.usage)
        if compiled.completion_validator is not None:
            compiled.completion_validator(context)
        self._repository.complete(
            run_id,
            final_output=to_jsonable(result.final_output),
            last_agent_name=result.last_agent.name,
            usage=usage,
        )
        await sink.emit(
            "run.completed",
            {
                "final_output": to_jsonable(result.final_output),
                "last_agent_name": result.last_agent.name,
                "usage": usage,
            },
        )


class _null_async_context:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


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
