from __future__ import annotations

from typing import Any

from backend.runtime.context import ScholarWeaveContext

TODO_METADATA_KEY = "builder_todos"
FINISHED_METADATA_KEY = "builder_finished"
SAVED_RECEIPT_KINDS = {"agent", "function_tool"}


def create_builder_todo_plan(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    if context.metadata.get(TODO_METADATA_KEY):
        raise ValueError("This builder run already has a TODO plan.")
    raw_tasks = arguments.get("tasks")
    if not isinstance(raw_tasks, list) or not 2 <= len(raw_tasks) <= 12:
        raise ValueError("A builder TODO plan must contain between 2 and 12 tasks.")

    todos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            raise ValueError("Each builder TODO must be an object.")
        task_id = str(raw_task.get("id") or "").strip()
        title = str(raw_task.get("title") or "").strip()
        if not task_id or not title:
            raise ValueError("Each builder TODO requires a non-empty id and title.")
        if task_id in seen:
            raise ValueError(f"Duplicate builder TODO id '{task_id}'.")
        seen.add(task_id)
        todos.append(
            {
                "id": task_id,
                "title": title,
                "status": "in_progress" if index == 0 else "pending",
                "note": None,
            }
        )

    context.metadata[TODO_METADATA_KEY] = todos
    return _snapshot(todos)


def update_builder_todo(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    todos = _todos(context)
    task_id = str(arguments.get("id") or "").strip()
    status = str(arguments.get("status") or "").strip()
    note = arguments.get("note")
    if status not in {"completed", "blocked"}:
        raise ValueError("A builder TODO can only be marked completed or blocked.")

    current = next((todo for todo in todos if todo["status"] == "in_progress"), None)
    if current is None:
        raise ValueError("The builder TODO plan has no task in progress.")
    if current["id"] != task_id:
        raise ValueError(
            f"Complete the current TODO '{current['id']}' before updating '{task_id}'."
        )

    current["status"] = status
    current["note"] = str(note).strip() if note is not None else None
    if status == "completed":
        next_todo = next((todo for todo in todos if todo["status"] == "pending"), None)
        if next_todo is not None:
            next_todo["status"] = "in_progress"
    return _snapshot(todos)


def finish_builder_run(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    outcome = str(arguments.get("outcome") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    if not summary:
        raise ValueError("A builder completion summary is required.")

    todos = context.metadata.get(TODO_METADATA_KEY)
    saved = any(receipt.kind in SAVED_RECEIPT_KINDS for receipt in context.receipts)
    if outcome == "informational":
        if saved or todos:
            raise ValueError(
                "A builder run cannot finish as informational after starting or saving a build."
            )
        result = {"outcome": outcome, "summary": summary}
        context.metadata[FINISHED_METADATA_KEY] = result
        return result
    if outcome != "saved":
        raise ValueError("Builder outcome must be 'saved' or 'informational'.")
    if not saved:
        raise ValueError("The builder cannot finish as saved without a save receipt.")
    if not isinstance(todos, list) or not todos:
        raise ValueError("The builder cannot finish a build without a TODO plan.")
    incomplete = [todo["id"] for todo in todos if todo["status"] != "completed"]
    if incomplete:
        raise ValueError(
            "Complete every builder TODO before finishing: " + ", ".join(incomplete)
        )

    result = {"outcome": outcome, "summary": summary}
    context.metadata[FINISHED_METADATA_KEY] = result
    return result


def validate_builder_completion(context: ScholarWeaveContext) -> None:
    finished = context.metadata.get(FINISHED_METADATA_KEY)
    todos = context.metadata.get(TODO_METADATA_KEY)
    saved = any(receipt.kind in SAVED_RECEIPT_KINDS for receipt in context.receipts)
    if (
        isinstance(finished, dict)
        and finished.get("outcome") == "informational"
        and not saved
        and not todos
    ):
        return
    if (
        isinstance(finished, dict)
        and finished.get("outcome") == "saved"
        and saved
        and isinstance(todos, list)
        and todos
        and all(todo["status"] == "completed" for todo in todos)
    ):
        return
    raise RuntimeError(
        "Builder stopped before completing its TODO plan and saving the requested asset. "
        "No successful builder completion was recorded."
    )


def _todos(context: ScholarWeaveContext) -> list[dict[str, Any]]:
    todos = context.metadata.get(TODO_METADATA_KEY)
    if not isinstance(todos, list) or not todos:
        raise ValueError("Create a builder TODO plan before updating it.")
    return todos


def _snapshot(todos: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "todos": [dict(todo) for todo in todos],
        "completed": all(todo["status"] == "completed" for todo in todos),
    }
