from __future__ import annotations


GLOBAL_AGENT_INSTRUCTIONS = (
    "When rendering mathematical notation, always use display math with double-dollar delimiters "
    "in the form $$<math>$$. Put the opening and closing $$ on their own lines. Do not use single "
    "dollar signs, \\(...\\), \\[...\\], or bare square brackets as math delimiters."
)


def with_global_agent_instructions(instructions: str) -> str:
    if GLOBAL_AGENT_INSTRUCTIONS in instructions:
        return instructions
    return f"{instructions.rstrip()}\n\n{GLOBAL_AGENT_INSTRUCTIONS}"
