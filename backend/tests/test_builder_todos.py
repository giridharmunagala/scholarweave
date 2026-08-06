from __future__ import annotations

import pytest

from backend.builder.todos import (
    create_builder_todo_plan,
    finish_builder_run,
    update_builder_todo,
    validate_builder_completion,
)
from backend.runtime.context import ScholarWeaveContext, ToolReceipt


class Runtime:
    async def invoke(self, catalog_id, arguments, context):
        raise AssertionError("No application tool should be invoked.")


def context() -> ScholarWeaveContext:
    return ScholarWeaveContext(run_id="builder-run", tool_runtime=Runtime())


def test_builder_todos_advance_one_step_at_a_time() -> None:
    run_context = context()
    created = create_builder_todo_plan(
        {
            "tasks": [
                {"id": "draft", "title": "Draft blueprint"},
                {"id": "save", "title": "Save blueprint"},
            ]
        },
        run_context,
    )

    assert [todo["status"] for todo in created["todos"]] == ["in_progress", "pending"]
    with pytest.raises(ValueError, match="current TODO 'draft'"):
        update_builder_todo(
            {"id": "save", "status": "completed", "note": None},
            run_context,
        )

    advanced = update_builder_todo(
        {"id": "draft", "status": "completed", "note": "Drafted."},
        run_context,
    )
    assert [todo["status"] for todo in advanced["todos"]] == ["completed", "in_progress"]


def test_builder_completion_requires_completed_todos_and_save_receipt() -> None:
    run_context = context()
    create_builder_todo_plan(
        {
            "tasks": [
                {"id": "draft", "title": "Draft blueprint"},
                {"id": "save", "title": "Save blueprint"},
            ]
        },
        run_context,
    )
    with pytest.raises(RuntimeError, match="No successful builder completion"):
        validate_builder_completion(run_context)

    update_builder_todo(
        {"id": "draft", "status": "completed", "note": None},
        run_context,
    )
    run_context.receipts.append(ToolReceipt(kind="agent", title="Saved analyst"))
    update_builder_todo(
        {"id": "save", "status": "completed", "note": "Receipt received."},
        run_context,
    )
    with pytest.raises(RuntimeError, match="No successful builder completion"):
        validate_builder_completion(run_context)

    result = finish_builder_run(
        {"outcome": "saved", "summary": "Saved analyst."},
        run_context,
    )

    assert result["outcome"] == "saved"
    validate_builder_completion(run_context)


def test_builder_allows_informational_completion_without_build_activity() -> None:
    run_context = context()

    result = finish_builder_run(
        {"outcome": "informational", "summary": "Hello! What would you like to build?"},
        run_context,
    )

    assert result["outcome"] == "informational"
    validate_builder_completion(run_context)


def test_builder_cannot_escape_started_build_as_informational() -> None:
    run_context = context()
    create_builder_todo_plan(
        {
            "tasks": [
                {"id": "draft", "title": "Draft blueprint"},
                {"id": "save", "title": "Save blueprint"},
            ]
        },
        run_context,
    )

    with pytest.raises(ValueError, match="after starting or saving a build"):
        finish_builder_run(
            {"outcome": "informational", "summary": "Only described the requested agent."},
            run_context,
        )
    with pytest.raises(RuntimeError, match="No successful builder completion"):
        validate_builder_completion(run_context)
