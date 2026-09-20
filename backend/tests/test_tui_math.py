from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult

from scholarweave_tui.markdown import Markdown, math_text, terminal_parser
from scholarweave_tui.widgets import ChatMessage


def text(source: str) -> str:
    return "\n".join(
        "".join(child.content for child in token.children or [])
        for token in terminal_parser().parse(source)
        if token.type == "inline"
    )


@pytest.mark.parametrize(("source", "expected"), [
    (r"\alpha + \beta \leq \infty", "α + β ≤ ∞"),
    (r"E=mc^2, x_{12}, a^{n+1}", "E=mc², x₁₂, aⁿ⁺¹"),
    (r"x_{query} + y^{ab}", "x_(query) + y^(ab)"),
    (r"\frac{1+\frac{x}{2}}{\sqrt{y}}", "(1+(x)/(2))/(√(y))"),
    (r"\sqrt[3]{x} \in \mathbb{R}", "root[3](x) ∈ ℝ"),
    (r"\mathbf{x} \cdot \mathrm{y} + \operatorname{rank}(A)", "x · y + rank(A)"),
    (r"\sum_{i=1}^{n} x_i", "∑ᵢ₌₁ⁿ xᵢ"),
    (r"\text{a_b * literal} \neq 0", "a_b * literal ≠ 0"),
    (r"A^\top + B^{\top} + C^{T} + D^{\mathrm{T}}", "Aᵀ + Bᵀ + Cᵀ + Dᵀ"),
    (r"x \perp y, \top, \bot", "x ⊥ y, ⊤, ⊥"),
])
def test_common_math_notation(source, expected):
    assert math_text(source) == expected


@pytest.mark.parametrize("source", [
    r"\unknown{x}", r"\frac{1}", r"\sqrt{", "x_{", r"\begin{matrix}a&b\end{matrix}",
    "{" * 100 + "x" + "}" * 100,
])
def test_unsupported_or_incomplete_math_is_lossless(source):
    assert math_text(source) == source


@pytest.mark.parametrize("opening,closing", [("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")])
def test_math_delimiters_in_prose(opening, closing):
    assert text(f"Before {opening}\\alpha_2{closing} after.") == "Before α₂ after."


@pytest.mark.parametrize(("source", "expected"), [
    (
        r"R(m\theta)^\top R(n\theta) = R(-m\theta)R(n\theta) = R((n-m)\theta).",
        "R(mθ)ᵀ R(nθ) = R(-mθ)R(nθ) = R((n-m)θ).",
    ),
    (r"(R(m\theta)q)^\top(R(n\theta)k),", "(R(mθ)q)ᵀ(R(nθ)k),"),
    (
        r"q^\top R(m\theta)^\top R(n\theta)k = q^\top R((n-m)\theta)k.",
        "qᵀ R(mθ)ᵀ R(nθ)k = qᵀ R((n-m)θ)k.",
    ),
])
@pytest.mark.parametrize("opening,closing", [("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")])
def test_rotary_position_equations_render_without_raw_commands(source, expected, opening, closing):
    assert text(f"{opening}{source}{closing}") == expected


def test_display_math_is_a_block_and_does_not_parse_markdown_operators():
    source = "Before\n\\[\n\\frac{x_*}{2}\n\\]\nAfter"
    assert text(source) == "Before\n(x_(*))/(2)\nAfter"
    assert text(r"$a * b * c$") == "a * b * c"


def test_code_currency_links_and_ordinary_markdown_are_preserved():
    source = r"""**Bold** and `$\alpha$` and `` `\(\beta\)` ``.

```latex
$$\gamma$$
```

    \[\delta\]

~~~text
$\epsilon$
~~~

Prices $5 and $10; escaped \$20. [A link](https://example.com/$value)
"""
    tokens = terminal_parser().parse(source)
    assert text(source).startswith(r"Bold and $\alpha$ and `\(\beta\)`.")
    assert [t.content for t in tokens if t.type in {"fence", "code_block"}] == [
        "$$\\gamma$$\n", "\\[\\delta\\]\n", "$\\epsilon$\n",
    ]
    assert "Prices $5 and $10; escaped $20." in text(source)
    link = next(child for t in tokens for child in t.children or [] if child.type == "link_open")
    assert link.attrGet("href") == "https://example.com/$value"


def test_math_in_tables_lists_and_blockquotes():
    assert "α" in text("| Symbol |\n| --- |\n| $\\alpha$ |")
    assert text("- $\\beta$\n\n> \\(\\gamma\\)") == "β\nγ"
    assert text("> $$\n> \\delta\n> $$") == "δ"


def test_chat_streaming_and_shared_previews_keep_original_source():
    class MathApp(App):
        def compose(self) -> ComposeResult:
            yield ChatMessage("assistant", r"Saved \(\alpha\)")
            yield Markdown(r"Preview $$\beta^2$$", id="preview")

    async def scenario():
        app = MathApp()
        async with app.run_test() as pilot:
            message = app.query_one(ChatMessage)
            assert "Saved α" in str(message.query_one("MarkdownParagraph").render())
            assert "Preview β²" in str(app.query_one("#preview").query_one("MarkdownParagraph").render())
            await message.set_content(r"Live \(\frac{1}")
            await pilot.pause()
            assert message.query_one("MarkdownParagraph").render()._renderable.plain == r"Live \(\frac{1}"
            final = r"Live \(\frac{1}{2}\), `$x^2$`."
            await message.set_content(final)
            await pilot.pause()
            assert message.content == final
            assert "Live (1)/(2), $x^2$." in str(message.query_one("MarkdownParagraph").render())
            await message.set_content(r"\[q^\top R((n-m)\theta)k.\]")
            await pilot.pause()
            assert message.query_one("MarkdownParagraph").render()._renderable.plain == "qᵀ R((n-m)θ)k."

    asyncio.run(scenario())


def test_content_updated_before_mount_uses_math_renderer():
    async def scenario():
        message = ChatMessage("assistant", "")
        await message.set_content(r"\[\Omega\]")

        class MathApp(App):
            def compose(self) -> ComposeResult:
                yield message

        async with MathApp().run_test():
            assert "Ω" in str(message.query_one("MarkdownParagraph").render())

    asyncio.run(scenario())
