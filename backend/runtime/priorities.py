from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.runtime.context import ScholarWeaveContext, unwrap_scholar_context

PRIORITIES_KEY = "extended_work_priorities"
BUDGET_KEY = "extended_work_budget"
PROGRESS_REVISION_KEY = "extended_work_progress_revision"

NON_PROGRESS_TOOLS = {
    "tools.search",
    "extended.budget.status",
    "extended.priorities.list",
    "extended.plan.create",
    "extended.plan.update",
    "extended.notes.save",
    "extended.notes.list",
    "extended.notes.read",
}


class ResearchPriorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=2_000)
    trigger: Literal[
        "task_limit_exceeded",
        "source_limit_exceeded",
        "external_search_cap_reached",
    ]
    threshold: int = Field(ge=1, le=300)
    observed: int = Field(ge=0, le=10_000)
    limit_status: str = Field(min_length=1, max_length=2_000)
    current_state: str = Field(min_length=1, max_length=10_000)
    evidence_summary: str = Field(min_length=1, max_length=10_000)
    candidates: list[str] = Field(min_length=1, max_length=30)
    constraints: list[str] = Field(max_length=30)

    @model_validator(mode="after")
    def require_real_limit_failure(self) -> "ResearchPriorityRequest":
        if self.trigger == "external_search_cap_reached":
            if self.threshold != 300 or self.observed < self.threshold:
                raise ValueError(
                    "External-search reprioritization requires the 300-call cap to be reached."
                )
        elif self.observed <= self.threshold:
            raise ValueError(
                "Scope reprioritization requires observed work to exceed the selected threshold."
            )
        return self


def record_priority_decision(
    output: Any,
    context: ScholarWeaveContext,
) -> dict[str, Any] | None:
    if not isinstance(output, dict):
        return None
    summary = str(output.get("summary") or "").strip()
    recommended = output.get("recommended")
    deferred = output.get("deferred")
    stop_conditions = output.get("stop_conditions")
    if (
        not summary
        or not isinstance(recommended, list)
        or not isinstance(deferred, list)
        or not isinstance(stop_conditions, list)
    ):
        return None

    progress_revision = int(context.metadata.get(PROGRESS_REVISION_KEY, 0))
    decisions = context.metadata.setdefault(PRIORITIES_KEY, [])
    if not isinstance(decisions, list):
        decisions = []
        context.metadata[PRIORITIES_KEY] = decisions
    last_decision = decisions[-1] if decisions and isinstance(decisions[-1], dict) else None
    if (
        last_decision is not None
        and int(last_decision.get("progress_revision", -1)) >= progress_revision
    ):
        return None
    basis = {
        "summary": summary,
        "recommended": recommended,
        "deferred": deferred,
        "stop_conditions": stop_conditions,
    }
    fingerprint = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if last_decision is not None and last_decision.get("_fingerprint") == fingerprint:
        last_decision["progress_revision"] = progress_revision
        last_decision["confirmation_count"] = int(
            last_decision.get("confirmation_count", 1)
        ) + 1
        return _public_priority_decision(last_decision)
    decision = {
        "decision_id": f"priority-{len(decisions) + 1}",
        **basis,
        "progress_revision": progress_revision,
        "confirmation_count": 1,
        "_fingerprint": fingerprint,
    }
    decisions.append(decision)
    return _public_priority_decision(decision)


def list_priority_decisions(
    _arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    decisions = context.metadata.get(PRIORITIES_KEY)
    decisions = decisions if isinstance(decisions, list) else []
    return {
        "decisions": [
            _public_priority_decision(decision)
            for decision in decisions[-20:]
            if isinstance(decision, dict)
        ],
        "guidance": (
            "Deferred, merged, replaced, and stopped work is an intentional scope decision, not a "
            "failure or missing result. Do not delegate it again unless a later prioritization "
            "explicitly restores it or its stated stop condition materially changes. Reading or "
            "acknowledging this list is not new evidence and must not trigger reprioritization."
        ),
    }


def advance_priority_progress(context: ScholarWeaveContext) -> int:
    if not isinstance(context.metadata.get(BUDGET_KEY), dict):
        return 0
    revision = int(context.metadata.get(PROGRESS_REVISION_KEY, 0)) + 1
    context.metadata[PROGRESS_REVISION_KEY] = revision
    return revision


def mark_tool_progress(catalog_id: str, context: ScholarWeaveContext) -> None:
    if catalog_id not in NON_PROGRESS_TOOLS:
        advance_priority_progress(context)


def prioritizer_enabled(context: Any, _agent: Any) -> bool:
    scholar_context = unwrap_scholar_context(context)
    decisions = scholar_context.metadata.get(PRIORITIES_KEY)
    if not isinstance(decisions, list) or not decisions:
        return True
    if not isinstance(decisions[-1], dict):
        return True
    current_revision = int(scholar_context.metadata.get(PROGRESS_REVISION_KEY, 0))
    last_revision = int(decisions[-1].get("progress_revision", -1))
    return current_revision > last_revision


def _public_priority_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in decision.items() if not key.startswith("_")}
