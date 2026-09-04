from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


from backend.prompting.registry import default_prompt_registry

GLOBAL_AGENT_INSTRUCTIONS = default_prompt_registry().render("global")


def current_system_information(
    at: datetime | None = None,
    *,
    timezone_name: str | None = None,
    user_profile: str | None = None,
) -> str:
    current = at or datetime.now().astimezone()
    if current.tzinfo is None:
        raise ValueError("System information requires a timezone-aware datetime.")
    if timezone_name:
        current = current.astimezone(ZoneInfo(timezone_name))
    offset = current.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}"
    timezone_name = current.tzname() or "local"
    information = (
        "System information:\n"
        f"Current date: {current.date().isoformat()}\n"
        f"Current time: {current.strftime('%H:%M:%S')} "
        f"{timezone_name} (UTC{formatted_offset})"
    )
    if user_profile:
        information += f"\nUser context: {user_profile.strip()}"
    return information


def with_global_agent_instructions(
    instructions: str,
    *,
    global_instructions: str = GLOBAL_AGENT_INSTRUCTIONS,
    at: datetime | None = None,
    timezone_name: str | None = None,
    user_profile: str | None = None,
) -> str:
    combined = instructions.rstrip()
    global_instructions = global_instructions.strip()
    if global_instructions and global_instructions not in combined:
        combined = f"{combined}\n\n{global_instructions}"
    return (
        f"{combined}\n\n"
        f"{current_system_information(at, timezone_name=timezone_name, user_profile=user_profile)}"
    )
