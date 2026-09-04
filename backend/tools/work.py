from __future__ import annotations

from typing import Any

from backend.agents.context import ScholarWeaveContext

PLAN_KEY = "work_plan"


def create_work_plan(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    if context.metadata.get(PLAN_KEY):
        raise ValueError("This run already has a work plan.")
    raw_items = arguments.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 10:
        raise ValueError("A work plan must contain between 1 and 10 items.")

    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Each work item must be an object.")
        item_id = str(raw_item.get("id") or "").strip()
        title = str(raw_item.get("title") or "").strip()
        if not item_id or not title:
            raise ValueError("Each work item requires a non-empty id and title.")
        if item_id in seen:
            raise ValueError(f"Duplicate work item id '{item_id}'.")
        seen.add(item_id)
        items.append(
            {
                "id": item_id,
                "title": title,
                "status": "pending",
                "summary": "",
            }
        )
    context.metadata[PLAN_KEY] = items
    return work_plan(context)


def update_work_item(
    arguments: dict[str, Any],
    context: ScholarWeaveContext,
) -> dict[str, Any]:
    items = _items(context)
    item_id = str(arguments.get("id") or "").strip()
    status = str(arguments.get("status") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    if status not in {"in_progress", "completed", "blocked"}:
        raise ValueError("Work item status must be in_progress, completed, or blocked.")
    item = next((candidate for candidate in items if candidate["id"] == item_id), None)
    if item is None:
        raise ValueError(f"Unknown work item '{item_id}'.")
    if status in {"completed", "blocked"} and not summary:
        raise ValueError("Completed or blocked work items require a summary.")
    item["status"] = status
    item["summary"] = summary
    return work_plan(context)


def work_plan(context: ScholarWeaveContext) -> dict[str, Any]:
    items = _items(context)
    pending = [
        dict(item)
        for item in items
        if item["status"] not in {"completed", "blocked"}
    ]
    return {
        "items": [dict(item) for item in items],
        "pending": pending,
        "complete": not pending,
    }


def _items(context: ScholarWeaveContext) -> list[dict[str, str]]:
    items = context.metadata.get(PLAN_KEY)
    if not isinstance(items, list) or not items:
        raise ValueError("Create a work plan before reading or updating it.")
    return items
