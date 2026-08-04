"""Renders a workflow as a runnable Agents SDK script.

The export is meant to be read as much as run: it shows exactly which agents exist, what
tools they hold and how the graph is orchestrated, so a workflow built on the canvas can
be taken away, version-controlled and edited as ordinary Python.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.registry import BaseNode, NodeRegistry
from backend.schemas import WorkflowDefinition

_HEADER = '''"""{name}

{description}

Generated from a workflow in the local research workbench. Run it with:

    pip install openai-agents
    python {slug}.py

Set OPENAI_BASE_URL and OPENAI_API_KEY to point at a different provider; the defaults
target a local Ollama through its OpenAI-compatible endpoint.
"""

from __future__ import annotations

import asyncio
import os

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.interface import Model, ModelProvider
from openai import AsyncOpenAI

BASE_URL = os.environ.get("OPENAI_BASE_URL", "{base_url}")
API_KEY = os.environ.get("OPENAI_API_KEY", "ollama")
MODEL = os.environ.get("AGENT_MODEL", "{model}")

client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)


class LocalProvider(ModelProvider):
    """Ollama only speaks Chat Completions, so every model is resolved through it."""

    def get_model(self, model_name: str | None) -> Model:
        return OpenAIChatCompletionsModel(model=model_name or MODEL, openai_client=client)


RUN_CONFIG = RunConfig(model_provider=LocalProvider(), tracing_disabled=True)


def fill(agent: Agent, **values: str) -> Agent:
    """Substitutes the {{name}} placeholders the canvas uses in an agent's instructions."""
    text = agent.instructions or ""
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return agent.clone(instructions=text)
'''


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "workflow"


def _identifier(node_id: str, prefix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", node_id).strip("_") or "node"
    if cleaned[0].isdigit():
        cleaned = f"n_{cleaned}"
    return f"{prefix}_{cleaned}"


def _literal(value: Any) -> str:
    if isinstance(value, str) and "\n" in value:
        body = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        return f'"""{body}"""'
    return json.dumps(value, ensure_ascii=False)


def _indent(text: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def _tool_stub(node: Any, definition: BaseNode) -> str:
    """Emits a placeholder for a tool that reaches into this app's local database."""
    spec = definition.tool_spec
    assert spec is not None
    args = ", ".join(
        f"{parameter.port}: str" if parameter.required else f'{parameter.port}: str = ""'
        for parameter in spec.parameters
    )
    lines = [
        "@function_tool",
        f"def {spec.name}({args}) -> str:",
        f'    """{spec.description}"""',
        "    # This tool reads the workbench's local library, which this script has no",
        f"    # access to. Replace the body with your own {definition.type_name} lookup.",
        f'    raise NotImplementedError("Connect {spec.name} to your own data source.")',
    ]
    return "\n".join(lines)


def _python_tool(node: Any) -> str:
    config = node.config or {}
    code = str(config.get("code", "")).rstrip()
    entrypoint = config.get("entrypoint", "transform")
    tool_name = config.get("tool_name") or "run_python_step"
    description = config.get("tool_description") or "Runs a custom Python transform."
    lines = [
        code,
        "",
        "",
        "@function_tool",
        f"def {tool_name}(inputs: str) -> str:",
        f'    """{description}"""',
        "    import json",
        "",
        "    payload = json.loads(inputs) if inputs else {}",
        f"    return json.dumps({entrypoint}(payload), default=str)",
    ]
    return "\n".join(lines)


def _custom_node_stub(node: Any, definition: BaseNode) -> str:
    """Keep exports parseable without weakening the application's sandbox boundary."""
    name = _identifier(node.id, "custom_node")
    return "\n".join(
        [
            f"def {name}(inputs: dict, config: dict) -> dict:",
            f'    """Pinned custom node {definition.type_name}; port and config validation is app-managed."""',
            "    raise NotImplementedError(",
            f'        "Custom node {definition.type_name} runs only in the workbench sandbox. "',
            '        "Copy its reviewed implementation into this function if you need a standalone runtime."',
            "    )",
        ]
    )


def _agent_block(node: Any, name: str, tools: list[str], handoffs: list[str]) -> str:
    config = node.config or {}
    instructions = config.get("instructions", "You are a helpful assistant.")
    reference = config.get("model_reference")
    referenced_model = reference.get("model") if isinstance(reference, dict) else None
    configured_model = config.get("model") or referenced_model
    lines = [f"{name} = Agent("]
    lines.append(f'    name={_literal(config.get("name", node.id))},')
    lines.append(f"    instructions={_literal(instructions)},")
    if configured_model:
        lines.append(f'    model={_literal(configured_model)},')
    else:
        lines.append("    model=MODEL,")
    if config.get("temperature") is not None:
        lines.append(f'    model_settings=ModelSettings(temperature={config["temperature"]}),')
    if config.get("handoff_description"):
        lines.append(f'    handoff_description={_literal(config["handoff_description"])},')
    lines.append(f"    tools=[{', '.join(tools)}],")
    if handoffs:
        lines.append(f"    handoffs=[{', '.join(handoffs)}],")
    lines.append(")")
    return "\n".join(lines)


def export_workflow(workflow: WorkflowDefinition, registry: NodeRegistry, *, base_url: str, model: str) -> str:
    """Turns a workflow definition into a standalone Agents SDK script."""
    nodes = {node.id: node for node in workflow.nodes}
    order = _topological_order(workflow)

    tool_names: dict[tuple[str, str], str] = {}
    sections: list[str] = []
    agent_names: dict[str, str] = {}

    # Tools first: an agent's definition references them by name.
    for node_id in order:
        node = nodes[node_id]
        try:
            definition = registry.get(node.type)
        except KeyError:
            continue
        if node.type == "python_code":
            sections.append(_python_tool(node))
            tool_names[(node_id, "tool")] = (node.config or {}).get("tool_name") or "run_python_step"
        elif definition.type_name.startswith("custom:"):
            sections.append(_custom_node_stub(node, definition))
        elif definition.tool_spec is not None and _feeds_tool_port(workflow, node_id):
            sections.append(_tool_stub(node, definition))
            tool_names[(node_id, "tool")] = definition.tool_spec.name

    # Names are assigned up front so an agent can reference one that is defined later,
    # then the blocks are emitted in reverse order so referenced agents come first.
    for node_id in order:
        if nodes[node_id].type == "agent":
            agent_names[node_id] = _identifier(node_id, "agent")

    agent_blocks: list[str] = []
    for node_id in reversed(order):
        node = nodes[node_id]
        if node.type != "agent":
            continue
        name = agent_names[node_id]
        wired_tools = [
            tool_names[(edge.source_node_id, edge.source_port)]
            for edge in workflow.edges
            if edge.target_node_id == node_id
            and edge.target_port == "tools"
            and (edge.source_node_id, edge.source_port) in tool_names
        ]
        wired_tools += [
            f'{agent_names[edge.source_node_id]}.as_tool(tool_name="{_slug(edge.source_node_id)}", tool_description="Delegate to this agent.")'
            for edge in workflow.edges
            if edge.target_node_id == node_id
            and edge.target_port == "tools"
            and edge.source_node_id in agent_names
        ]
        wired_handoffs = [
            agent_names[edge.source_node_id]
            for edge in workflow.edges
            if edge.target_node_id == node_id and edge.target_port == "handoffs" and edge.source_node_id in agent_names
        ]
        agent_blocks.append(_agent_block(node, name, wired_tools, wired_handoffs))
    sections.extend(reversed(agent_blocks))

    body = _main_block(workflow, order, nodes, agent_names)
    header = _HEADER.format(
        name=workflow.name,
        description=workflow.description or "No description.",
        slug=_slug(workflow.name),
        base_url=base_url,
        model=model or "gemma4:e2b",
    )
    return "\n\n\n".join([header.rstrip(), *sections, body]) + "\n"


def _feeds_tool_port(workflow: WorkflowDefinition, node_id: str) -> bool:
    return any(
        edge.source_node_id == node_id and edge.source_port == "tool" for edge in workflow.edges
    )


def _main_block(
    workflow: WorkflowDefinition,
    order: list[str],
    nodes: dict[str, Any],
    agent_names: dict[str, str],
) -> str:
    inputs = [
        (node.config or {}).get("key", node.id)
        for node in workflow.nodes
        if node.type == "workflow_input"
    ]
    lines = ["async def main() -> None:"]
    if inputs:
        lines.append("    # Fill these in — they are the workflow's declared inputs.")
        for key in inputs:
            lines.append(f'    {_identifier(key, "input")} = ""')
        lines.append("")

    conditional_nodes = [
        node
        for node in workflow.nodes
        if node.run_when is not None or node.type == "if_else"
    ]
    if conditional_nodes:
        lines.append("    # Conditional data-node routing is enforced by the workbench runtime.")
        lines.append("    # Recreate these declarative rules in your standalone data pipeline before running agents:")
        for node in conditional_nodes:
            condition = node.run_when
            label = "run_when"
            if node.type == "if_else":
                condition = (node.config or {}).get("condition")
                label = "if_else"
            serialized = json.dumps(
                condition.model_dump(mode="json") if hasattr(condition, "model_dump") else condition,
                ensure_ascii=False,
            )
            lines.append(f"    # {node.id} {label}: {serialized}")
        lines.append("")

    runnable = [node_id for node_id in order if node_id in agent_names]
    if not runnable:
        lines.append("    # This workflow has no agents; its steps are data operations only.")
        lines.append("    print('Nothing to run.')")
    else:
        previous: str | None = None
        for node_id in runnable:
            if not _is_run_directly(workflow, node_id):
                continue
            name = agent_names[node_id]
            source = _prompt_source(workflow, node_id, inputs, previous)
            filled = ", ".join(f'{key}={_identifier(key, "input")}' for key in inputs)
            prepared = f"fill({name}, input={source}, {filled})" if filled else f"fill({name}, input={source})"
            lines.append(f"    result_{name} = await Runner.run({prepared}, {source}, run_config=RUN_CONFIG)")
            lines.append(f"    print(result_{name}.final_output)")
            previous = f"result_{name}.final_output"
        if previous is None:
            lines.append("    print('Every agent in this workflow is referenced by another one.')")

    lines.append("")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append("    asyncio.run(main())")
    return "\n".join(lines)


def _is_run_directly(workflow: WorkflowDefinition, node_id: str) -> bool:
    """An agent used only as another agent's tool or handoff target is not run on its own."""
    consumed = {edge.source_port for edge in workflow.edges if edge.source_node_id == node_id}
    return not consumed or bool(consumed - {"agent", "tool"})


def _prompt_source(workflow: WorkflowDefinition, node_id: str, inputs: list[str], previous: str | None) -> str:
    for edge in workflow.edges:
        if edge.target_node_id == node_id and edge.target_port == "input":
            source_node = next((n for n in workflow.nodes if n.id == edge.source_node_id), None)
            if source_node is not None and source_node.type == "workflow_input":
                return _identifier((source_node.config or {}).get("key", source_node.id), "input")
    return previous or '"Carry out your instructions."'


def _topological_order(workflow: WorkflowDefinition) -> list[str]:
    remaining = {node.id: {edge.source_node_id for edge in workflow.edges if edge.target_node_id == node.id} for node in workflow.nodes}
    order: list[str] = []
    while remaining:
        ready = sorted(node_id for node_id, deps in remaining.items() if not deps - set(order))
        if not ready:
            order.extend(sorted(remaining))
            break
        for node_id in ready:
            order.append(node_id)
            remaining.pop(node_id)
    return order
