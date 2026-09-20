"""Terminal math as readable text, without rewriting stored Markdown or code."""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline
from textual.widgets import Markdown as TextualMarkdown


_SYMBOLS = dict(zip(
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega".split(),
    "αβγδεζηθικλμνξοπρστυφχψω",
))
_SYMBOLS.update(dict(zip(
    "Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega".split(),
    "ΓΔΘΛΞΠΣΥΦΨΩ",
)))
_SYMBOLS.update({
    "varepsilon": "ε", "vartheta": "ϑ", "varphi": "ϕ", "varpi": "ϖ",
    "varrho": "ϱ", "varsigma": "ς", "infty": "∞", "partial": "∂",
    "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬",
    "oint": "∮", "pm": "±", "mp": "∓", "times": "×", "cdot": "·",
    "div": "÷", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "ne": "≠", "neq": "≠", "approx": "≈", "equiv": "≡", "sim": "∼",
    "propto": "∝", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "neg": "¬", "land": "∧", "lor": "∨", "to": "→", "rightarrow": "→",
    "leftarrow": "←", "leftrightarrow": "↔", "Rightarrow": "⇒",
    "Leftarrow": "⇐", "Leftrightarrow": "⇔", "implies": "⇒", "iff": "⇔",
    "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "langle": "⟨", "rangle": "⟩", "lvert": "|", "rvert": "|",
    "lVert": "‖", "rVert": "‖", "ell": "ℓ", "hbar": "ℏ",
    "top": "⊤", "bot": "⊥", "perp": "⊥",
})
_FUNCTIONS = set("sin cos tan arcsin arccos arctan sinh cosh tanh log ln exp lim min max arg det gcd sup inf".split())
_STYLES = set("mathrm mathbf mathit mathsf mathtt mathcal mathscr boldsymbol operatorname".split())
_SUPER = dict(zip("0123456789+-=()inT⊤", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿᵀᵀ"))
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
_PAIRS = (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$"))


def math_text(source: str) -> str:
    """Convert common TeX notation; preserve unsupported or malformed expressions."""
    pos = 0

    def argument(depth: int) -> str:
        nonlocal pos
        while pos < len(source) and source[pos].isspace():
            pos += 1
        if pos >= len(source):
            raise ValueError("Missing argument")
        if source[pos] == "{":
            pos += 1
            return parse(depth + 1, grouped=True)
        return atom(depth + 1)

    def atom(depth: int) -> str:
        nonlocal pos
        if depth > 40:
            raise ValueError("Excessive nesting")
        char = source[pos]
        pos += 1
        if char == "{":
            return "{" + parse(depth + 1, grouped=True) + "}"
        if char in "^_":
            value = argument(depth)
            mapping = _SUPER if char == "^" else _SUB
            return "".join(mapping[c] for c in value) if value and all(c in mapping for c in value) else char + "(" + value + ")"
        if char != "\\":
            return char
        match = re.match(r"[A-Za-z]+|.", source[pos:], re.DOTALL)
        if match is None:
            raise ValueError("Incomplete command")
        command = match.group()
        pos += len(command)
        if command in _SYMBOLS:
            return _SYMBOLS[command]
        if command in _FUNCTIONS:
            return command + " "
        if command in {"text", "textrm", "textbf"}:
            while pos < len(source) and source[pos].isspace():
                pos += 1
            if pos >= len(source) or source[pos] != "{":
                raise ValueError("Missing text group")
            pos += 1
            start, nesting = pos, 1
            while pos < len(source):
                if source[pos] == "\\":
                    pos += 2
                    continue
                if source[pos] == "{":
                    nesting += 1
                elif source[pos] == "}":
                    nesting -= 1
                    if nesting == 0:
                        value = source[start:pos]
                        pos += 1
                        return re.sub(r"\\([{}_%#$&])", r"\1", value)
                pos += 1
            raise ValueError("Unclosed text group")
        if command in _STYLES:
            return argument(depth)
        if command == "mathbb":
            value = argument(depth)
            return {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ"}.get(value, value)
        if command in {"frac", "dfrac", "tfrac"}:
            numerator, denominator = argument(depth), argument(depth)
            return f"({numerator})/({denominator})"
        if command == "sqrt":
            degree = ""
            if pos < len(source) and source[pos] == "[":
                end = source.find("]", pos + 1)
                if end < 0:
                    raise ValueError("Incomplete root")
                degree = source[pos + 1:end]
                pos = end + 1
            value = argument(depth)
            return f"root[{degree}]({value})" if degree else f"√({value})"
        if command in {"left", "right", "limits", "nolimits", "displaystyle", "textstyle"}:
            if command in {"left", "right"} and pos < len(source) and source[pos] == ".":
                pos += 1
            return ""
        if command in {",", ";", ":", " ", "quad", "qquad", "enspace", "thinspace"}:
            return " "
        if command == "!":
            return ""
        if command in {"{", "}", "_", "%", "#", "$", "&", "|"}:
            return command
        if command == "\\":
            return "; "
        # Unknown commands must not silently lose their meaning or arguments.
        raise ValueError("Unsupported command")

    def parse(depth: int, *, grouped: bool = False) -> str:
        nonlocal pos
        result = []
        while pos < len(source):
            if source[pos] == "}":
                if not grouped:
                    raise ValueError("Unexpected closing brace")
                pos += 1
                return "".join(result)
            result.append(atom(depth))
        if grouped:
            raise ValueError("Unclosed group")
        return "".join(result)

    try:
        return re.sub(r"\s+", " ", parse(0)).strip()
    except (ValueError, RecursionError):
        return source.strip()


def _closing(source: str, delimiter: str, start: int) -> int:
    while (end := source.find(delimiter, start)) >= 0:
        slashes = len(source[:end]) - len(source[:end].rstrip("\\"))
        if slashes % 2 == 0:
            return end
        start = end + len(delimiter)
    return -1


def _inline_math(state: StateInline, silent: bool) -> bool:
    start = state.pos
    source = state.src
    for opening, closing in _PAIRS:
        if not source.startswith(opening, start):
            continue
        body_start = start + len(opening)
        end = _closing(source, closing, body_start)
        if end < 0:
            # Keep incomplete streamed TeX delimiters visible instead of Markdown
            # consuming their backslashes. The next update reparses the full source.
            if opening.startswith("\\"):
                if not silent:
                    state.push("text", "", 0).content = source[start:state.posMax]
                state.pos = state.posMax
                return True
            return False
        body = source[body_start:end]
        if opening == "$" and (
            not body or body[0].isspace() or body[-1].isspace()
            or "\n" in body or (end + 1 < len(source) and source[end + 1].isalnum())
        ):
            return False
        if not silent:
            state.push("text", "", 0).content = math_text(body)
        state.pos = end + len(closing)
        return True
    return False


def _block_math(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    if state.is_code_block(start):
        return False
    offset = state.bMarks[start] + state.tShift[start]
    first = state.src[offset:state.eMarks[start]]
    pair = next(((a, b) for a, b in _PAIRS[:2] if first.startswith(a)), None)
    if pair is None:
        return False
    opening, closing = pair
    lines = []
    for line in range(start, end):
        if line > start and state.sCount[line] < state.blkIndent:
            break
        lines.append(state.src[state.bMarks[line] + state.tShift[line]:state.eMarks[line]])
        if _closing(lines[-1], closing, len(opening) if line == start else 0) < 0:
            continue
        content = "\n".join(lines)
        close = _closing(content, closing, len(opening))
        if close < 0:
            continue
        if content[close + len(closing):].strip():
            return False
        if silent:
            return True
        state.push("paragraph_open", "p", 1).map = [start, line + 1]
        token = state.push("inline", "", 0)
        token.content = content
        token.map = [start, line + 1]
        token.children = []
        state.push("paragraph_close", "p", -1)
        state.line = line + 1
        return True
    return False


def terminal_parser() -> MarkdownIt:
    parser = MarkdownIt("gfm-like")
    parser.inline.ruler.before("escape", "terminal_math", _inline_math)
    parser.block.ruler.before("fence", "terminal_math", _block_math, {"alt": ["paragraph", "reference", "blockquote", "list"]})
    return parser


class Markdown(TextualMarkdown):
    """Shared renderer for chat, plans, papers, notes, and skill previews."""

    def __init__(self, markdown: str | None = None, **kwargs) -> None:
        super().__init__(markdown, parser_factory=terminal_parser, **kwargs)
