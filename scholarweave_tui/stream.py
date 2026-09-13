from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.runs.schemas import RunEventResponse, RunResponse
from scholarweave_tui.client import StreamEvent


@dataclass
class ProgressStep:
    key: str
    label: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None

    def seconds(self, now: datetime | None = None) -> float:
        finished = self.finished_at or now or datetime.now(UTC)
        return max(0.0, (finished - self.started_at).total_seconds())


@dataclass
class LiveRun:
    cursor: int = -1
    assistant: str = ""
    activity: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    goal_state: dict[str, Any] | None = None
    status: str = "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    context_tokens: int | None = None
    context_window_tokens: int | None = None
    progress: list[ProgressStep] = field(default_factory=list)
    _streamed: bool = field(default=False, init=False, repr=False)
    _messages: list[str] = field(default_factory=list, init=False, repr=False)
    _agent_name: str | None = field(default=None, init=False, repr=False)
    _active_steps: dict[str, ProgressStep] = field(default_factory=dict, init=False, repr=False)
    _active_model_steps: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def apply(self, event: StreamEvent | RunEventResponse) -> bool:
        if event.sequence <= self.cursor:
            return False
        self.cursor = event.sequence
        kind, payload = event.event_type, event.payload
        delegated = payload.get("delegated") is True
        at = event.created_at or datetime.now(UTC)
        self._track_progress(kind, payload, at)
        self._track_context(kind, payload)
        if kind == "model.stream":
            if not delegated and payload.get("raw_type") == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str):
                    self.assistant = delta if payload.get("snapshot") is True else self.assistant + delta
                    self._streamed = True
            return True
        if kind == "agent.stream":
            return True
        if kind == "model.retry" and not delegated:
            discarded = payload.get("discarded_text_characters")
            if type(discarded) is int and discarded > 0:
                self.assistant = self.assistant[:-discarded]
        if kind == "run.item":
            if not delegated and not self._streamed:
                self._remember_message(payload.get("item"))
            return True
        if kind == "agent.started" and not delegated:
            name = payload.get("agent_name")
            if isinstance(name, str):
                self._agent_name = name
        if not delegated:
            if kind in {"run.started", "run.recovered"}:
                self.status = "running"
            elif kind in {"run.completed", "run.failed", "run.cancelled"}:
                self.status = kind.removeprefix("run.")
                output = payload.get("final_output")
                if isinstance(output, str):
                    self.assistant = output
                    self._streamed = True
                self._update_usage(payload.get("usage"))
            if kind == "usage.updated":
                self._update_usage(payload.get("usage", payload))
            if kind in {"goal.plan.updated", "goal.blocked", "goal.completed"}:
                self._update_goal(payload)
            if kind == "tool.completed" and payload.get("tool_name") in {
                "create_work_plan", "read_work_plan", "update_work_item",
            }:
                result = payload.get("result")
                if isinstance(result, dict) and isinstance(result.get("items"), list):
                    self.goal_state = deepcopy(result)
        self._log_activity(kind, payload)
        return True

    def active_progress_label(self) -> str | None:
        active = [step for step in self.progress if step.status == "running"]
        return active[-1].label if active else None

    def _track_context(self, kind: str, payload: dict[str, Any]) -> None:
        if payload.get("delegated") is True:
            return
        if kind == "context.prepared":
            tokens = payload.get("estimated_input_tokens")
            window = payload.get("context_window_tokens")
            if type(tokens) is int and tokens >= 0:
                self.context_tokens = tokens
            if type(window) is int and window > 0:
                self.context_window_tokens = window
            return
        if kind != "model.telemetry" or payload.get("context_scope", "main") != "main":
            return
        usage = payload.get("usage")
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if type(tokens) is int and tokens >= 0:
            self.context_tokens = tokens

    def _track_progress(self, kind: str, payload: dict[str, Any], at: datetime) -> None:
        delegated = payload.get("delegated") is True
        if kind in {"run.started", "run.recovered"}:
            self.started_at = self.started_at or at
            self._start_step("context", "Preparing context", at)
            return
        if kind == "context.prepared" and not delegated:
            start = self.started_at or at
            self._start_step("context", "Preparing context", start)
            self._finish_step("context", at, label="Context prepared")
            return
        if kind == "model.phase" and not delegated:
            model_call_id = payload.get("model_call_id")
            phase = payload.get("phase")
            if not isinstance(model_call_id, str) or not isinstance(phase, str):
                return
            previous = self._active_model_steps.pop(model_call_id, None)
            if previous is not None:
                self._finish_step(previous, at)
            labels = {
                "queued": "Waiting in queue",
                "waiting": "Waiting for model",
                "processing": "Processing context",
                "thinking": "Thinking",
                "tool": "Preparing a tool",
                "writing": "Writing the answer",
            }
            label = labels.get(phase)
            if label:
                key = f"model:{model_call_id}:{self.cursor}"
                self._start_step(key, label, at)
                self._active_model_steps[model_call_id] = key
            return
        if kind == "tool.started":
            name = payload.get("tool_name")
            if not isinstance(name, str) or not name:
                return
            for model_call_id, step_key in list(self._active_model_steps.items()):
                self._finish_step(step_key, at)
                self._active_model_steps.pop(model_call_id, None)
            key = self._tool_key(payload, name)
            self._start_step(key, _humanize(name), at)
            return
        if kind in {"tool.completed", "tool.failed"}:
            name = payload.get("tool_name")
            if not isinstance(name, str) or not name:
                return
            key = self._tool_key(payload, name)
            self._finish_step(
                key,
                at,
                label=_humanize(name),
                status="failed" if kind == "tool.failed" else "completed",
            )
            return
        if kind == "agent.started" and delegated:
            name = payload.get("agent_name")
            if isinstance(name, str) and name:
                self._start_step(self._agent_key(payload, name), f"Delegated to {name}", at)
            return
        if kind in {"agent.completed", "agent.failed", "agent.superseded"} and delegated:
            name = payload.get("agent_name")
            if isinstance(name, str) and name:
                self._finish_step(
                    self._agent_key(payload, name),
                    at,
                    label=f"Delegated to {name}",
                    status="completed" if kind == "agent.completed" else "failed",
                )
            return
        if kind in {"run.completed", "run.failed", "run.cancelled"}:
            self.finished_at = at
            final_status = "completed" if kind == "run.completed" else "failed"
            for key in list(self._active_steps):
                self._finish_step(key, at, status=final_status)
            self._active_model_steps.clear()

    def _start_step(self, key: str, label: str, at: datetime) -> None:
        if key in self._active_steps:
            return
        step = ProgressStep(key=key, label=label, status="running", started_at=at)
        self._active_steps[key] = step
        self.progress.append(step)

    def _finish_step(
        self,
        key: str,
        at: datetime,
        *,
        label: str | None = None,
        status: str = "completed",
    ) -> None:
        step = self._active_steps.pop(key, None)
        if step is None:
            step = ProgressStep(key=key, label=label or key, status=status, started_at=at, finished_at=at)
            self.progress.append(step)
        else:
            step.status = status
            step.finished_at = at
            if label is not None:
                step.label = label

    def _tool_key(self, payload: dict[str, Any], name: str) -> str:
        call_id = payload.get("tool_call_id")
        return f"tool:{call_id}" if isinstance(call_id, str) and call_id else f"tool:{name}"

    def _agent_key(self, payload: dict[str, Any], name: str) -> str:
        invocation_id = payload.get("invocation_id")
        return (
            f"agent:{invocation_id}"
            if isinstance(invocation_id, str) and invocation_id
            else f"agent:{name}"
        )

    def _remember_message(self, item: Any) -> None:
        if not isinstance(item, dict) or item.get("type") != "message_output_item":
            return
        if item.get("delegated") is True:
            return
        if self._agent_name is not None and item.get("agent_name") != self._agent_name:
            return
        content = item.get("content")
        if isinstance(content, str) and content and content not in self._messages:
            self._messages.append(content)
            self.assistant = "\n\n".join(self._messages)

    def _update_usage(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        update = deepcopy(value)
        previous = self.usage.get("performance")
        candidate = update.get("performance")
        if isinstance(previous, dict) and isinstance(candidate, dict):
            old_calls, new_calls = previous.get("model_calls"), candidate.get("model_calls")
            if type(old_calls) is int and (type(new_calls) is not int or new_calls < old_calls):
                update.pop("performance")
        candidate = update.get("performance")
        if isinstance(candidate, dict):
            for key in ("input_tokens", "output_tokens"):
                if key not in update and key in candidate:
                    count = candidate[key]
                    if count is None or (type(count) is int and count >= 0):
                        update[key] = count
        self.usage.update(update)

    def _update_goal(self, payload: dict[str, Any]) -> None:
        candidate = payload.get("goal_state", payload)
        if not isinstance(candidate, dict):
            return
        version = candidate.get("version", payload.get("version"))
        previous = (self.goal_state or {}).get("version")
        if type(previous) is int and (type(version) is not int or version < previous):
            return
        self.goal_state = {**(self.goal_state or {}), **deepcopy(candidate)}
        if type(version) is int:
            self.goal_state["version"] = version

    def _log_activity(self, kind: str, payload: dict[str, Any]) -> None:
        if kind != "model.retry" and not kind.startswith(
            ("tool.", "agent.", "run.", "context.", "steering.", "goal."),
        ):
            return
        if kind.endswith((".delta", ".telemetry", ".stream")):
            return
        details = []
        for key in ("tool_name", "agent_name", "reason", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                details.append(" ".join(value.split())[:300])
        self.activity.append(kind + (": " + " / ".join(details) if details else ""))
        del self.activity[:-80]

    @classmethod
    def restore(cls, run: RunResponse) -> LiveRun:
        live = cls(
            usage=deepcopy(run.usage),
            goal_state=deepcopy(run.goal_state),
            started_at=run.started_at or run.created_at,
            finished_at=run.finished_at,
            context_window_tokens=run.context_window_tokens,
        )
        live._agent_name = run.agent_name
        for event in sorted(run.events, key=lambda item: item.sequence):
            live.apply(event)
        if isinstance(run.final_output, str):
            live.assistant = run.final_output
            live._streamed = True
        elif not live._streamed:
            for item in run.items:
                live._remember_message(item.model_dump())
        live.status = run.status
        # The durable record's totals are authoritative, while events can carry
        # newer cumulative performance and work-plan snapshots.
        performance = live.usage.get("performance")
        live.usage.update(deepcopy(run.usage))
        if isinstance(performance, dict):
            live._update_usage({
                "performance": performance,
                **{key: value for key, value in run.usage.items() if key != "performance"},
            })
        return live


def _humanize(value: str) -> str:
    return " ".join(value.replace(".", " ").replace("_", " ").split()).capitalize()
