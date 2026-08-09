from __future__ import annotations

from datetime import datetime


GLOBAL_AGENT_INSTRUCTIONS = (
    "When rendering mathematical notation, always use display math with double-dollar delimiters "
    "in the form $$<math>$$. Put the opening and closing $$ on their own lines. Do not use single "
    "dollar signs, \\(...\\), \\[...\\], or bare square brackets as math delimiters."
)


def current_system_information(at: datetime | None = None) -> str:
    current = at or datetime.now().astimezone()
    if current.tzinfo is None:
        raise ValueError("System information requires a timezone-aware datetime.")
    offset = current.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}"
    timezone_name = current.tzname() or "local"
    return (
        "System information:\n"
        f"Current date: {current.date().isoformat()}\n"
        f"Current time: {current.strftime('%H:%M:%S')} "
        f"{timezone_name} (UTC{formatted_offset})"
    )


def with_global_agent_instructions(
    instructions: str,
    *,
    at: datetime | None = None,
) -> str:
    combined = instructions.rstrip()
    if GLOBAL_AGENT_INSTRUCTIONS not in combined:
        combined = f"{combined}\n\n{GLOBAL_AGENT_INSTRUCTIONS}"
    return f"{combined}\n\n{current_system_information(at)}"
