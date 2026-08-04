from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.agent_tools import node_as_tool
from backend.conditions import evaluate_condition
from backend.events import EventBroker
from backend.models import NodeRun, Run, RunEvent, WorkflowVersion
from backend.ports import apply_coercion
from backend.registry import NodeExecutionContext
from backend.schemas import WorkflowDefinition
from backend.utils import truncate_text, utcnow
from backend.workflows import CompiledWorkflow, WorkflowValidator


class RunCancelledError(RuntimeError):
    pass


def collapse_outputs(outputs: dict[str, Any]) -> Any:
    """Unwraps a single unnamed ``result`` so simple workflows keep returning a bare value."""
    if set(outputs) == {"result"}:
        return outputs["result"]
    return outputs


def json_safe(value: Any) -> Any:
    """Replaces values that cannot be stored in a JSON column with a short descriptor.

    Agent and tool ports carry live SDK objects between nodes. Run history still needs
    to record what flowed where, so those become a readable placeholder rather than
    breaking the write.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    name = getattr(value, "name", None)
    label = f"{type(value).__name__}" + (f" '{name}'" if isinstance(name, str) else "")
    return f"<{label}>"


def _fan_in_ports(validator: WorkflowValidator, node: Any) -> set[str]:
    try:
        executor = validator.registry.get(node.type)
    except KeyError:
        return set()
    return {port.name for port in executor.inputs if port.fan_in}


class WorkflowExecutor:
    def __init__(self, services: Any, event_broker: EventBroker) -> None:
        self.services = services
        self.event_broker = event_broker
        self.validator = WorkflowValidator(services.registry, services.settings)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}

    async def start_run(self, workflow: WorkflowDefinition, inputs: dict[str, Any], workflow_version_id: str | None = None) -> Run:
        with self.services.session_factory() as session:
            workflow_name = workflow.name
            run = Run(
                workflow_version_id=workflow_version_id,
                workflow_name=workflow_name,
                status="pending",
                input_json=inputs,
                workflow_json=workflow.model_dump(mode="json"),
                owner_pid=os.getpid(),
            )
            session.add(run)
            session.commit()
            session.refresh(run)
        cancel_flag = asyncio.Event()
        self._cancel_flags[run.id] = cancel_flag
        task = asyncio.create_task(self._run_workflow(run.id, workflow, inputs), name=f"workflow-run-{run.id}")
        self._tasks[run.id] = task
        task.add_done_callback(lambda _task, run_id=run.id: self._tasks.pop(run_id, None))
        return run

    async def cancel_run(self, run_id: str) -> None:
        with self.services.session_factory() as session:
            run = session.get(Run, run_id)
            if not run:
                raise ValueError("Run not found")
            run.cancel_requested = True
            if run.status in {"completed", "failed", "cancelled"}:
                session.commit()
                return
            if run.status == "pending":
                run.status = "cancelled"
                run.finished_at = utcnow()
            session.commit()
        if run_id in self._cancel_flags:
            self._cancel_flags[run_id].set()
        await self._record_event(run_id, "run.cancel_requested", {"run_id": run_id})

    def is_cancelled(self, run_id: str) -> bool:
        flag = self._cancel_flags.get(run_id)
        return bool(flag and flag.is_set())

    async def _run_workflow(self, run_id: str, workflow: WorkflowDefinition, inputs: dict[str, Any]) -> None:
        with self.services.session_factory() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.status = "running"
            run.started_at = utcnow()
            session.commit()
        await self._record_event(run_id, "run.started", {"run_id": run_id, "workflow": workflow.name})
        try:
            # Compiling inside the try matters: a graph that fails validation used to
            # leave the run stuck at "pending" with no explanation, because the error
            # escaped into a background task nobody awaits.
            compiled = self.validator.compile(workflow)
            final_output = collapse_outputs(await self._execute_compiled(run_id, compiled, inputs, namespace=""))
        except RunCancelledError:
            with self.services.session_factory() as session:
                run = session.get(Run, run_id)
                assert run is not None
                run.status = "cancelled"
                run.finished_at = utcnow()
                session.commit()
            await self._record_event(run_id, "run.cancelled", {"run_id": run_id})
        except Exception as exc:  # noqa: BLE001
            with self.services.session_factory() as session:
                run = session.get(Run, run_id)
                assert run is not None
                run.status = "failed"
                run.error = str(exc)
                run.finished_at = utcnow()
                session.commit()
            await self._record_event(run_id, "run.failed", {"run_id": run_id, "error": str(exc)})
        else:
            with self.services.session_factory() as session:
                run = session.get(Run, run_id)
                assert run is not None
                run.status = "completed"
                run.output_json = final_output
                run.finished_at = utcnow()
                session.commit()
            await self._record_event(run_id, "run.completed", {"run_id": run_id, "output": final_output})
        finally:
            self._cancel_flags.pop(run_id, None)

    async def _execute_compiled(
        self,
        run_id: str,
        compiled: CompiledWorkflow,
        inputs: dict[str, Any],
        *,
        namespace: str,
        parent_node_run_id: str | None = None,
        depth: int = 0,
    ) -> dict[str, Any]:
        values: dict[str, dict[str, Any]] = {}
        skipped_nodes: set[str] = set()
        remaining = {node.id: len(compiled.incoming[node.id]) for node in compiled.definition.nodes}
        ready = sorted([node.id for node in compiled.definition.nodes if remaining[node.id] == 0])
        pending: dict[asyncio.Task[tuple[str, dict[str, Any], bool]], str] = {}
        failures: list[Exception] = []

        def release(node_id: str) -> None:
            for edge in compiled.outgoing[node_id]:
                remaining[edge.target_node_id] -= 1
                if remaining[edge.target_node_id] == 0:
                    ready.append(edge.target_node_id)
            ready.sort()

        while ready or pending:
            while ready and len(pending) < self.services.settings.max_concurrent_nodes:
                node_id = ready.pop(0)
                node = compiled.node_lookup[node_id]
                node_path = f"{namespace}.{node_id}" if namespace else node_id
                node_inputs, origins, skip_reason = self._resolve_node_inputs(compiled, node, values)
                if skip_reason:
                    self._check_cancelled(run_id)
                    await self._record_skipped_node(
                        run_id, node_path, node, node_inputs, parent_node_run_id, skip_reason
                    )
                    values[node_id] = {}
                    skipped_nodes.add(node_id)
                    release(node_id)
                    continue
                consumed = frozenset(edge.source_port for edge in compiled.outgoing[node_id])
                connected_inputs = frozenset(edge.target_port for edge in compiled.incoming[node_id])
                task = asyncio.create_task(
                    self._execute_node(
                        run_id, node_path, node, node_inputs, inputs, parent_node_run_id, depth, consumed, origins,
                        compiled.definition.model_defaults, connected_inputs,
                    ),
                    name=f"node-{run_id}-{node_path}",
                )
                pending[task] = node_id
            if not pending:
                break
            done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                node_id = pending.pop(task)
                try:
                    finished_node_id, output, was_skipped = await task
                except RunCancelledError as exc:
                    failures.append(exc)
                except Exception as exc:  # noqa: BLE001
                    failures.append(exc)
                else:
                    values[finished_node_id] = output
                    if was_skipped:
                        skipped_nodes.add(finished_node_id)
                    release(finished_node_id)
            if failures:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending.keys(), return_exceptions=True)
                raise failures[0]

        if compiled.outputs:
            return {
                binding.key: values[binding.node_id][binding.value_port]
                for binding in compiled.outputs
                if binding.node_id in values
                and binding.node_id not in skipped_nodes
                and binding.value_port in values[binding.node_id]
            }
        # Graphs without a declared interface fall back to the terminal node's raw output.
        sink_outputs = [
            values[node_id] for node_id in compiled.sinks if node_id in values and node_id not in skipped_nodes
        ]
        return sink_outputs[-1] if sink_outputs else {}

    def _resolve_node_inputs(
        self,
        compiled: CompiledWorkflow,
        node: Any,
        values: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, list[str]], str | None]:
        """Combines only active edge values and decides whether a node is unreachable.

        A node with graph inputs but no active incoming outputs is skipped unless it
        declares static inputs. This makes static inputs an explicit independent-entry
        rule while keeping source nodes and legacy optional-port graphs unchanged.
        """
        node_inputs = dict(node.static_inputs)
        fan_in_ports = _fan_in_ports(self.validator, node)
        origins: dict[str, list[str]] = {}
        active_edges = 0
        for edge in compiled.incoming[node.id]:
            source_output = values.get(edge.source_node_id, {})
            if edge.source_port not in source_output:
                continue
            active_edges += 1
            value = source_output[edge.source_port]
            coercion = compiled.coercions.get(
                (edge.source_node_id, edge.source_port, edge.target_node_id, edge.target_port)
            )
            value = apply_coercion(value, coercion, f"{edge.target_node_id}.{edge.target_port}")
            origins.setdefault(edge.target_port, []).append(edge.source_port)
            if edge.target_port in fan_in_ports:
                node_inputs.setdefault(edge.target_port, []).append(value)
            else:
                node_inputs[edge.target_port] = value

        executor = self.services.registry.get(node.type)
        incoming = compiled.incoming[node.id]
        static_port_inputs = {port.name for port in executor.inputs} & set(node.static_inputs)
        if incoming and not active_edges and not static_port_inputs:
            return node_inputs, origins, "All incoming graph paths were inactive"
        for port in executor.inputs:
            if port.required and port.name not in node_inputs:
                inactive_edges = [edge for edge in incoming if edge.target_port == port.name]
                if inactive_edges:
                    return node_inputs, origins, f"Required input '{port.name}' was unavailable because its branch was inactive"
        return node_inputs, origins, None

    async def _execute_node(
        self,
        run_id: str,
        node_path: str,
        node: Any,
        node_inputs: dict[str, Any],
        run_inputs: dict[str, Any],
        parent_node_run_id: str | None,
        depth: int = 0,
        consumed_ports: frozenset[str] = frozenset(),
        input_origins: dict[str, list[str]] | None = None,
        workflow_model_defaults: Any = None,
        connected_inputs: frozenset[str] = frozenset(),
    ) -> tuple[str, dict[str, Any], bool]:
        self._check_cancelled(run_id)
        executor = self.services.registry.get(node.type)
        config = executor.validate_config(node.config)
        child_depth = depth + 1 if executor.referenced_workflow() is not None else depth
        if child_depth > self.services.settings.max_subworkflow_depth:
            raise ValueError(
                f"{node_path} nests workflows deeper than the limit of {self.services.settings.max_subworkflow_depth}"
            )
        if node.run_when is not None and not evaluate_condition(node.run_when, workflow=run_inputs, inputs=node_inputs):
            await self._record_skipped_node(
                run_id,
                node_path,
                node,
                node_inputs,
                parent_node_run_id,
                "run_when condition evaluated to false",
            )
            return node.id, {}, True
        with self.services.session_factory() as session:
            node_run = NodeRun(
                run_id=run_id,
                node_path=node_path,
                node_id=node.id,
                node_type=node.type,
                parent_node_run_id=parent_node_run_id,
                status="running",
                input_json=json_safe(node_inputs),
                started_at=utcnow(),
            )
            session.add(node_run)
            session.commit()
            session.refresh(node_run)
        await self._record_event(run_id, "node.started", {"node_path": node_path, "node_type": node.type, "input": json_safe(node_inputs)})

        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            await self._record_event(run_id, f"node.{event_type}", payload)

        async def execute_subflow(definition: WorkflowDefinition, child_inputs: dict[str, Any], suffix: str) -> Any:
            merged_inputs = {**run_inputs, **child_inputs}
            compiled = self.validator.compile(definition)
            return await self._execute_compiled(
                run_id,
                compiled,
                merged_inputs,
                namespace=f"{node_path}{suffix}",
                parent_node_run_id=node_run.id,
                depth=child_depth,
            )

        context = NodeExecutionContext(
            services=self.services,
            run_id=run_id,
            node_path=node_path,
            node_run_id=node_run.id,
            run_inputs=run_inputs,
            emit=emit,
            check_cancelled=lambda: self._check_cancelled(run_id),
            execute_subflow=execute_subflow,
            workflow_model_defaults=workflow_model_defaults,
            depth=child_depth,
            consumed_ports=consumed_ports,
            input_origins=input_origins or {},
            connected_inputs=connected_inputs,
        )
        try:
            output = await self._produce(executor, context, node_inputs, config, consumed_ports)
        except Exception as exc:  # noqa: BLE001
            with self.services.session_factory() as session:
                row = session.get(NodeRun, node_run.id)
                assert row is not None
                row.status = "cancelled" if isinstance(exc, RunCancelledError) else "failed"
                row.error = str(exc)
                row.finished_at = utcnow()
                session.commit()
            await self._record_event(
                run_id,
                "node.failed",
                {"node_path": node_path, "node_type": node.type, "error": str(exc)},
            )
            raise
        with self.services.session_factory() as session:
            row = session.get(NodeRun, node_run.id)
            assert row is not None
            row.status = "completed"
            row.output_json = json_safe(output)
            row.finished_at = utcnow()
            session.commit()
        await self._record_event(
            run_id,
            "node.completed",
            {"node_path": node_path, "node_type": node.type, "output_summary": truncate_text(str(output), 500)},
        )
        return node.id, output, False

    async def _record_skipped_node(
        self,
        run_id: str,
        node_path: str,
        node: Any,
        node_inputs: dict[str, Any],
        parent_node_run_id: str | None,
        reason: str,
    ) -> None:
        finished_at = utcnow()
        with self.services.session_factory() as session:
            node_run = NodeRun(
                run_id=run_id,
                node_path=node_path,
                node_id=node.id,
                node_type=node.type,
                parent_node_run_id=parent_node_run_id,
                status="skipped",
                input_json=json_safe(node_inputs),
                error=reason,
                started_at=finished_at,
                finished_at=finished_at,
            )
            session.add(node_run)
            session.commit()
        await self._record_event(
            run_id,
            "node.skipped",
            {
                "node_path": node_path,
                "node_type": node.type,
                "input": json_safe(node_inputs),
                "reason": reason,
            },
        )

    async def _produce(
        self,
        executor: Any,
        context: NodeExecutionContext,
        node_inputs: dict[str, Any],
        config: Any,
        consumed_ports: frozenset[str],
    ) -> dict[str, Any]:
        """Runs a node, and hands an agent-callable tool to whoever wired the ``tool`` port.

        A node wired *only* into an agent's tools is not run here: it has no arguments
        yet, because the agent supplies them when it decides to call. Values wired on the
        canvas are pinned, so the agent fills in the rest rather than overriding the graph.
        """
        spec = getattr(executor, "tool_spec", None)
        wants_tool = spec is not None and (not consumed_ports or "tool" in consumed_ports)
        tool_only = spec is not None and bool(consumed_ports) and set(consumed_ports) <= {"tool"}

        def build_tool() -> Any:
            # A node may rename itself per instance, so two Python steps wired into the
            # same agent are distinguishable rather than both called "run_python_step".
            return node_as_tool(
                executor,
                config,
                context,
                static_inputs=node_inputs,
                name_override=getattr(config, "tool_name", None) or None,
                description_override=getattr(config, "tool_description", None) or None,
            )

        if tool_only:
            return {"tool": build_tool()}
        output = await executor.execute(context, node_inputs, config)
        if wants_tool and isinstance(output, dict) and "tool" not in output:
            output["tool"] = build_tool()
        return output

    def _check_cancelled(self, run_id: str) -> None:
        if self.is_cancelled(run_id):
            raise RunCancelledError("Run was cancelled")

    async def _record_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> RunEvent:
        with self.services.session_factory() as session:
            event = RunEvent(run_id=run_id, event_type=event_type, payload_json=payload)
            session.add(event)
            session.commit()
            session.refresh(event)
        await self.event_broker.publish(
            run_id,
            {"id": event.id, "run_id": event.run_id, "event_type": event.event_type, "payload": event.payload_json, "created_at": event.created_at.isoformat()},
        )
        return event

    def load_run(self, run_id: str) -> Run | None:
        with self.services.session_factory() as session:
            return session.get(Run, run_id)

    def list_runs(self) -> list[Run]:
        with self.services.session_factory() as session:
            return list(session.scalars(select(Run).order_by(Run.created_at.desc())))

    def load_node_runs(self, run_id: str) -> list[NodeRun]:
        with self.services.session_factory() as session:
            return list(session.scalars(select(NodeRun).where(NodeRun.run_id == run_id).order_by(NodeRun.started_at.asc(), NodeRun.node_path.asc())))

    def load_events(self, run_id: str, after_id: int | None = None) -> list[RunEvent]:
        with self.services.session_factory() as session:
            stmt = select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id.asc())
            if after_id is not None:
                stmt = stmt.where(RunEvent.id > after_id)
            return list(session.scalars(stmt))
