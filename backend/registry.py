from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel

from backend.schemas import (
    NodeDefinitionResponse,
    PortDefinitionResponse,
    WorkflowDefinition,
    WorkflowPort,
)


@dataclass(frozen=True, slots=True)
class PortDefinition:
    name: str
    kind: str
    item_kind: str | None = None
    description: str = ""
    required: bool = True
    #: Accepts several incoming edges, collecting their values into a list. Used by
    #: agent ports such as ``tools`` and ``handoffs`` where one target has many sources.
    fan_in: bool = False


@dataclass(frozen=True, slots=True)
class ToolParameter:
    """One argument an agent supplies when it calls a node as a tool."""

    port: str
    json_type: str = "string"
    description: str = ""
    required: bool = True


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Declares how a node exposes itself to agents as a callable tool.

    A node with a spec keeps working as an ordinary DAG step; the spec only adds the
    ``tool`` output port that can be wired into an agent, so the same capability is
    available whether the author wants a fixed pipeline or an agent that decides.
    """

    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = ()
    #: Which entry of the node's ``execute`` result is handed back to the model.
    result_port: str = "text"


#: Appended to any node that declares a ``tool_spec``.
TOOL_OUTPUT_PORT = PortDefinition(
    "tool",
    "tool",
    description="Connect to an agent's tools port to let the agent call this node.",
    required=False,
)


@dataclass(slots=True)
class NodeExecutionContext:
    services: Any
    run_id: str
    node_path: str
    node_run_id: str
    run_inputs: dict[str, Any]
    emit: Callable[[str, dict[str, Any]], Any]
    check_cancelled: Callable[[], None]
    execute_subflow: Callable[[WorkflowDefinition, dict[str, Any], str], Any]
    workflow_model_defaults: Any = None
    depth: int = 0
    #: Output ports of this node that something downstream actually reads. Empty means
    #: the node is a sink. Lets an agent node skip its own LLM call when it is only
    #: being referenced as a handoff target or as another agent's tool.
    consumed_ports: frozenset[str] = frozenset()
    #: Target port -> the source port names feeding it, in arrival order. Lets a node
    #: label incoming values after where they came from.
    input_origins: dict[str, list[str]] = field(default_factory=dict)
    #: Input ports connected in the graph, including branches that are inactive in this run.
    connected_inputs: frozenset[str] = frozenset()


class BaseNode(ABC):
    type_name: str
    label: str
    description: str
    category: str
    tags: list[str] = []
    inputs: list[PortDefinition] = []
    outputs: list[PortDefinition] = []
    config_model: type[BaseModel] | None = None

    #: Set on nodes that declare part of a workflow's public interface. Input nodes
    #: pull a named value out of the run inputs; output nodes publish a named result.
    interface_role: Literal["input", "output"] | None = None
    #: Which entry of the node's ``execute`` result carries the interface value.
    interface_value_port: str = "value"
    #: Present on nodes an agent may call directly. See :class:`ToolSpec`.
    tool_spec: ToolSpec | None = None

    def validate_config(self, config: dict[str, Any]) -> BaseModel | dict[str, Any]:
        if self.config_model is None:
            return config
        return self.config_model.model_validate(config)

    def interface_port(self, node_id: str, config: Any) -> WorkflowPort | None:
        """Describes the interface entry this node contributes, if any."""
        return None

    def referenced_workflow(self) -> tuple[str, WorkflowDefinition] | None:
        """Returns the saved workflow this node delegates to, for recursion checks."""
        return None

    def catalog_entry(self) -> NodeDefinitionResponse:
        return NodeDefinitionResponse(
            type=self.type_name,
            label=self.label,
            description=self.description,
            category=self.category,
            tags=list(self.tags),
            inputs=[PortDefinitionResponse(**asdict(port)) for port in self.inputs],
            outputs=[PortDefinitionResponse(**asdict(port)) for port in self.outputs],
            config_schema=self.config_model.model_json_schema() if self.config_model else {},
            interface_role=self.interface_role,
        )

    @abstractmethod
    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: Any) -> dict[str, Any]:
        raise NotImplementedError


class NodeProvider(ABC):
    """Supplies node types that are not known until runtime, such as saved workflows."""

    @abstractmethod
    def resolve(self, type_name: str) -> BaseNode | None:
        raise NotImplementedError

    @abstractmethod
    def catalog(self) -> list[NodeDefinitionResponse]:
        raise NotImplementedError

    def describe_missing(self, type_name: str) -> str | None:
        """Returns a provider-specific error message for a type this provider owns."""
        return None


class NodeRegistry:
    def __init__(self) -> None:
        self._nodes: dict[str, BaseNode] = {}
        self._providers: list[NodeProvider] = []

    def register(self, node: BaseNode) -> None:
        # A node that can be called by an agent gains its ``tool`` port here rather than
        # repeating the same port declaration on every such class.
        if node.tool_spec and not any(port.name == TOOL_OUTPUT_PORT.name for port in node.outputs):
            node.outputs = [*node.outputs, TOOL_OUTPUT_PORT]
        self._nodes[node.type_name] = node

    def add_provider(self, provider: NodeProvider) -> None:
        self._providers.append(provider)

    def get(self, type_name: str) -> BaseNode:
        node = self._nodes.get(type_name)
        if node is not None:
            return node
        for provider in self._providers:
            resolved = provider.resolve(type_name)
            if resolved is not None:
                return resolved
        for provider in self._providers:
            message = provider.describe_missing(type_name)
            if message:
                raise KeyError(message)
        raise KeyError(f"Unknown node type: {type_name}")

    def all(self) -> list[BaseNode]:
        return list(self._nodes.values())

    def catalog(self) -> list[NodeDefinitionResponse]:
        entries = [node.catalog_entry() for node in self._nodes.values()]
        for provider in self._providers:
            entries.extend(provider.catalog())
        return entries
