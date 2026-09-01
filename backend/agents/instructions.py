from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


GLOBAL_AGENT_INSTRUCTIONS = (
    "When rendering mathematical notation, always use display math with double-dollar delimiters "
    "in the form $$<math>$$. Put the opening and closing $$ on their own lines. Do not use single "
    "dollar signs, \\(...\\), \\[...\\], or bare square brackets as math delimiters. "
    "Do not loop on an unavailable source or failing tool. If one information tool repeatedly "
    "fails, stop using that tool and continue with the remaining tools or another source. Answer "
    "from the evidence already available only when alternatives are exhausted, clearly stating "
    "limitations."
)


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
    at: datetime | None = None,
) -> str:
    combined = instructions.rstrip()
    if GLOBAL_AGENT_INSTRUCTIONS not in combined:
        combined = f"{combined}\n\n{GLOBAL_AGENT_INSTRUCTIONS}"
    return f"{combined}\n\n{current_system_information(at)}"
