from __future__ import annotations

from typing import Any

from backend.runtime.context import ScholarWeaveContext

PLAN_KEY = "extended_work_plan"
NOTES_KEY = "extended_work_notes"


def create_work_plan(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    if context.metadata.get(PLAN_KEY):
        raise ValueError("This extended run already has a work plan.")
    raw_tasks = arguments.get("tasks")
    if not isinstance(raw_tasks, list) or not 2 <= len(raw_tasks) <= 10:
        raise ValueError("An extended work plan must contain between 2 and 10 tasks.")

    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, dict):
            raise ValueError("Each work item must be an object.")
        task_id = str(raw_task.get("id") or "").strip()
        title = str(raw_task.get("title") or "").strip()
        if not task_id or not title:
            raise ValueError("Each work item requires a non-empty id and title.")
        if task_id in seen:
            raise ValueError(f"Duplicate work item id '{task_id}'.")
        seen.add(task_id)
        tasks.append(
            {
                "id": task_id,
                "title": title,
                "status": "in_progress" if index == 0 else "pending",
                "summary": None,
            }
        )
    context.metadata[PLAN_KEY] = tasks
    context.metadata[NOTES_KEY] = {}
    return work_snapshot(context)


def update_work_item(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    tasks = _tasks(context)
    task_id = str(arguments.get("id") or "").strip()
    status = str(arguments.get("status") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    if status not in {"completed", "blocked"}:
        raise ValueError("A work item can only be marked completed or blocked.")
    if not summary:
        raise ValueError("A work-item update requires a summary.")

    current = next((task for task in tasks if task["status"] == "in_progress"), None)
    if current is None:
        raise ValueError("The extended work plan has no task in progress.")
    if current["id"] != task_id:
        raise ValueError(
            f"Complete the current work item '{current['id']}' before updating '{task_id}'."
        )
    current["status"] = status
    current["summary"] = summary
    if status == "completed":
        next_task = next((task for task in tasks if task["status"] == "pending"), None)
        if next_task is not None:
            next_task["status"] = "in_progress"
    return work_snapshot(context)


def save_work_note(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    tasks = _tasks(context)
    task_id = str(arguments.get("task_id") or "").strip()
    task = next((item for item in tasks if item["id"] == task_id), None)
    if task is None:
        raise ValueError(f"Unknown extended work item '{task_id}'.")
    title = str(arguments.get("title") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    content = str(arguments.get("content") or "").strip()
    if not title or not summary or not content:
        raise ValueError("A work note requires a title, summary, and content.")
    sources = arguments.get("sources")
    if not isinstance(sources, list) or any(not isinstance(source, str) for source in sources):
        raise ValueError("Work-note sources must be a list of strings.")
    if len(sources) > 100:
        raise ValueError("A work note cannot contain more than 100 sources.")
    if any(not source.strip() or len(source) > 2_000 for source in sources):
        raise ValueError("Each work-note source must contain between 1 and 2,000 characters.")

    notes = context.metadata.setdefault(NOTES_KEY, {})
    if not isinstance(notes, dict):
        raise ValueError("Extended work-note storage is invalid.")
    note_id = f"{task_id}-{len(notes) + 1}"
    notes[note_id] = {
        "id": note_id,
        "task_id": task_id,
        "title": title,
        "summary": summary,
        "content": content,
        "sources": sources,
    }
    return {
        "note_id": note_id,
        "task_id": task_id,
        "title": title,
        "summary": summary,
        "content": content,
        "sources": list(sources),
        "source_count": len(sources),
    }


def list_work_notes(
    _arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    notes = _notes(context)
    return {
        "notes": [
            {
                "note_id": note["id"],
                "task_id": note["task_id"],
                "title": note["title"],
                "summary": note["summary"],
                "source_count": len(note["sources"]),
            }
            for note in notes.values()
        ]
    }


def read_work_note(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    note_id = str(arguments.get("note_id") or "").strip()
    note = _notes(context).get(note_id)
    if note is None:
        raise ValueError(f"Unknown extended work note '{note_id}'.")
    return dict(note)


def work_snapshot(context: ScholarWeaveContext) -> dict[str, Any]:
    tasks = _tasks(context)
    return {
        "tasks": [dict(task) for task in tasks],
        "completed": all(task["status"] in {"completed", "blocked"} for task in tasks),
    }


def _tasks(context: ScholarWeaveContext) -> list[dict[str, Any]]:
    tasks = context.metadata.get(PLAN_KEY)
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Create an extended work plan first.")
    return tasks


def _notes(context: ScholarWeaveContext) -> dict[str, dict[str, Any]]:
    notes = context.metadata.get(NOTES_KEY)
    if not isinstance(notes, dict):
        raise ValueError("Create an extended work plan before using work notes.")
    return notes
