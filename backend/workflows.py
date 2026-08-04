from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.config import Settings
from backend.conditions import condition_predicates
from backend.ports import coercion_for, kinds_compatible
from backend.registry import NodeRegistry, PortDefinition
from backend.schemas import (
    WorkflowDefinition,
    WorkflowPort,
    WorkflowSignature,
    WorkflowValidationResult,
)


@dataclass(frozen=True, slots=True)
class OutputBinding:
    """Connects a declared interface key to the node (and result key) that produces it."""

    key: str
    node_id: str
    value_port: str


@dataclass(slots=True)
class CompiledWorkflow:
    definition: WorkflowDefinition
    node_lookup: dict[str, Any]
    outgoing: dict[str, list[Any]]
    incoming: dict[str, list[Any]]
    order: list[str]
    sinks: list[str]
    outputs: list[OutputBinding] = field(default_factory=list)
    # Edge key -> the conversion its value needs, resolved once at compile time.
    coercions: dict[tuple[str, str, str, str], str] = field(default_factory=dict)


class WorkflowValidator:
    def __init__(self, registry: NodeRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    def validate(
        self,
        workflow: WorkflowDefinition,
        *,
        visiting: tuple[str, ...] = (),
        depth: int = 0,
    ) -> WorkflowValidationResult:
        errors: list[str] = []
        node_lookup = {node.id: node for node in workflow.nodes}
        declared_workflow_inputs = {port.key for port in self._collect_interface(workflow)[0].inputs}
        if len(node_lookup) != len(workflow.nodes):
            errors.append("Node ids must be unique")
        outgoing: dict[str, list[Any]] = {node.id: [] for node in workflow.nodes}
        incoming: dict[str, list[Any]] = {node.id: [] for node in workflow.nodes}
        for node in workflow.nodes:
            node_executor = None
            validated_config = None
            try:
                node_executor = self.registry.get(node.type)
            except KeyError as exc:
                errors.append(str(exc).strip("'"))
                continue
            try:
                validated_config = node_executor.validate_config(node.config)
            except Exception as exc:
                errors.append(f"Invalid config for {node.id}: {exc}")
            if node.run_when is not None:
                errors.extend(
                    self._validate_condition_paths(node.id, node.run_when, node_executor, declared_workflow_inputs)
                )
            configured_condition = getattr(validated_config, "condition", None)
            if configured_condition is not None:
                errors.extend(
                    self._validate_condition_paths(node.id, configured_condition, node_executor, declared_workflow_inputs)
                )
            errors.extend(self._validate_subflow(node, visiting=visiting, depth=depth))
            errors.extend(self._validate_reference(node, node_executor, visiting=visiting, depth=depth))
        for edge in workflow.edges:
            if edge.source_node_id not in node_lookup:
                errors.append(f"Unknown source node: {edge.source_node_id}")
                continue
            if edge.target_node_id not in node_lookup:
                errors.append(f"Unknown target node: {edge.target_node_id}")
                continue
            try:
                source_def = self.registry.get(node_lookup[edge.source_node_id].type)
                target_def = self.registry.get(node_lookup[edge.target_node_id].type)
            except KeyError:
                continue
            source_port = self._find_port(source_def.outputs, edge.source_port)
            target_port = self._find_port(target_def.inputs, edge.target_port)
            if not source_port:
                errors.append(f"Unknown output port {edge.source_port} on node {edge.source_node_id}")
                continue
            if not target_port:
                errors.append(f"Unknown input port {edge.target_port} on node {edge.target_node_id}")
                continue
            if not self._ports_compatible(source_port, target_port):
                errors.append(
                    f"Incompatible connection {edge.source_node_id}.{edge.source_port} -> {edge.target_node_id}.{edge.target_port}"
                )
                continue
            outgoing[edge.source_node_id].append(edge)
            incoming[edge.target_node_id].append(edge)
        for node in workflow.nodes:
            try:
                node_def = self.registry.get(node.type)
            except KeyError:
                continue
            incoming_ports = {edge.target_port for edge in incoming[node.id]}
            consumed_ports = {edge.source_port for edge in outgoing[node.id]}
            # A node wired only into an agent's tools port is never run by the graph —
            # the agent supplies its arguments when it decides to call it — so its
            # required inputs are not the author's to provide here.
            if consumed_ports and consumed_ports <= {"tool"} and node_def.tool_spec is not None:
                continue
            for port in node_def.inputs:
                if port.required and port.name not in incoming_ports and port.name not in node.static_inputs:
                    errors.append(f"Missing required input {node.id}.{port.name}")
        order, cycle_error = self._topological_order(workflow, outgoing)
        if cycle_error:
            errors.append(cycle_error)
        signature, interface_errors = self._collect_interface(workflow)
        errors.extend(interface_errors)
        return WorkflowValidationResult(
            valid=not errors,
            errors=errors,
            order=order if not errors else [],
            signature=signature,
        )

    @staticmethod
    def _validate_condition_paths(
        node_id: str,
        condition: Any,
        node_executor: Any,
        declared_workflow_inputs: set[str],
    ) -> list[str]:
        """Checks top-level source names while allowing dynamic nested object shapes."""
        errors: list[str] = []
        input_ports = {port.name for port in node_executor.inputs}
        for predicate in condition_predicates(condition):
            root = predicate.path.split(".", 1)[0]
            if predicate.source == "inputs" and root not in input_ports:
                errors.append(
                    f"Condition on {node_id} references unknown node input '{root}' "
                    f"(use one of: {', '.join(sorted(input_ports)) or 'none'})"
                )
            elif predicate.source == "workflow" and declared_workflow_inputs and root not in declared_workflow_inputs:
                errors.append(
                    f"Condition on {node_id} references unknown workflow input '{root}' "
                    f"(use one of: {', '.join(sorted(declared_workflow_inputs))})"
                )
        return errors

    def signature(self, workflow: WorkflowDefinition) -> WorkflowSignature:
        """Derives the workflow's public interface from its input and output nodes."""
        return self._collect_interface(workflow)[0]

    def _collect_interface(self, workflow: WorkflowDefinition) -> tuple[WorkflowSignature, list[str]]:
        errors: list[str] = []
        inputs: list[WorkflowPort] = []
        outputs: list[WorkflowPort] = []
        for node in workflow.nodes:
            try:
                executor = self.registry.get(node.type)
            except KeyError:
                continue
            if not executor.interface_role:
                continue
            try:
                port = executor.interface_port(node.id, executor.validate_config(node.config))
            except Exception:  # noqa: BLE001 - config errors are already reported by validate()
                continue
            if port is None:
                continue
            (inputs if executor.interface_role == "input" else outputs).append(port)

        # A value needed in several places is declared once and entered once, so repeated input
        # keys merge into a single interface port. Every declaring node still reads the same key
        # at run time, so this is presentation only. Conflicting kinds remain a real error, and
        # duplicate *output* keys stay an error because they are genuinely ambiguous.
        inputs, merge_errors = self._merge_input_ports(inputs)
        errors.extend(merge_errors)
        seen: dict[str, str] = {}
        for port in outputs:
            port.node_ids = [port.node_id]
            if port.key in seen:
                errors.append(
                    f"Duplicate workflow output key '{port.key}' declared by {seen[port.key]} and {port.node_id}"
                )
            else:
                seen[port.key] = port.node_id
        inputs.sort(key=lambda port: port.key)
        outputs.sort(key=lambda port: port.key)
        return WorkflowSignature(inputs=inputs, outputs=outputs), errors

    @staticmethod
    def _merge_input_ports(ports: list[WorkflowPort]) -> tuple[list[WorkflowPort], list[str]]:
        errors: list[str] = []
        merged: dict[str, WorkflowPort] = {}
        for port in ports:
            existing = merged.get(port.key)
            if existing is None:
                merged[port.key] = port.model_copy(update={"node_ids": [port.node_id]})
                continue
            if existing.kind != port.kind:
                errors.append(
                    f"Workflow input '{port.key}' is declared as '{existing.kind}' by {existing.node_id} "
                    f"and as '{port.kind}' by {port.node_id}"
                )
                continue
            existing.node_ids.append(port.node_id)
            # The first declaration wins for presentation; later ones only fill in the blanks.
            existing.label = existing.label or port.label
            existing.description = existing.description or port.description
            if existing.default is None:
                existing.default = port.default
            # If any node needs the value the run cannot proceed without it.
            existing.required = existing.required or port.required
        for port in merged.values():
            if port.default is not None:
                port.required = False
        return list(merged.values()), errors

    def compile(self, workflow: WorkflowDefinition) -> CompiledWorkflow:
        result = self.validate(workflow)
        if not result.valid:
            raise ValueError("; ".join(result.errors))
        node_lookup = {node.id: node for node in workflow.nodes}
        outgoing = {node.id: [] for node in workflow.nodes}
        incoming = {node.id: [] for node in workflow.nodes}
        for edge in workflow.edges:
            outgoing[edge.source_node_id].append(edge)
            incoming[edge.target_node_id].append(edge)
        sinks = [node.id for node in workflow.nodes if not outgoing[node.id]]
        return CompiledWorkflow(
            workflow,
            node_lookup,
            outgoing,
            incoming,
            result.order,
            sinks,
            self._output_bindings(workflow),
            self.edge_coercions(workflow),
        )

    def edge_coercions(self, workflow: WorkflowDefinition) -> dict[tuple[str, str, str, str], str]:
        """Resolves, per edge, the conversion its value needs to enter the target port."""
        coercions: dict[tuple[str, str, str, str], str] = {}
        for edge in workflow.edges:
            source_node = next((node for node in workflow.nodes if node.id == edge.source_node_id), None)
            target_node = next((node for node in workflow.nodes if node.id == edge.target_node_id), None)
            if source_node is None or target_node is None:
                continue
            try:
                source_ports = self.registry.get(source_node.type).outputs
                target_ports = self.registry.get(target_node.type).inputs
            except KeyError:
                continue
            source_port = self._find_port(source_ports, edge.source_port)
            target_port = self._find_port(target_ports, edge.target_port)
            if source_port is None or target_port is None:
                continue
            coercion = coercion_for(source_port.kind, target_port.kind)
            if coercion:
                coercions[(edge.source_node_id, edge.source_port, edge.target_node_id, edge.target_port)] = coercion
        return coercions

    def _output_bindings(self, workflow: WorkflowDefinition) -> list[OutputBinding]:
        bindings: list[OutputBinding] = []
        for node in workflow.nodes:
            try:
                executor = self.registry.get(node.type)
            except KeyError:
                continue
            if executor.interface_role != "output":
                continue
            port = executor.interface_port(node.id, executor.validate_config(node.config))
            if port is None:
                continue
            bindings.append(OutputBinding(key=port.key, node_id=node.id, value_port=executor.interface_value_port))
        bindings.sort(key=lambda binding: binding.key)
        return bindings

    def _validate_reference(
        self,
        node: Any,
        executor: Any,
        *,
        visiting: tuple[str, ...],
        depth: int,
    ) -> list[str]:
        reference = executor.referenced_workflow()
        if reference is None:
            return []
        workflow_id, definition = reference
        if workflow_id in visiting:
            chain = " → ".join([*visiting, workflow_id])
            return [f"{node.id} creates a workflow reference cycle: {chain}"]
        if depth + 1 > self.settings.max_subworkflow_depth:
            return [f"{node.id} nests workflows deeper than the limit of {self.settings.max_subworkflow_depth}"]
        nested = self.validate(definition, visiting=(*visiting, workflow_id), depth=depth + 1)
        return [f"{node.id} → {error}" for error in nested.errors]

    def _validate_subflow(self, node: Any, *, visiting: tuple[str, ...] = (), depth: int = 0) -> list[str]:
        errors: list[str] = []
        if node.type == "map_subflow":
            try:
                max_items = int(node.config.get("max_items", self.settings.max_map_items))
                if max_items > self.settings.max_map_items:
                    errors.append(
                        f"map_subflow {node.id} allows {max_items} items, above the limit of "
                        f"{self.settings.max_map_items} set in Settings"
                    )
            except (TypeError, ValueError):
                errors.append(f"map_subflow {node.id} has an invalid max_items value")
        if node.type == "repeat_subflow":
            try:
                max_iterations = int(node.config.get("max_iterations", self.settings.max_repeat_iterations))
                if max_iterations > self.settings.max_repeat_iterations:
                    errors.append(f"repeat_subflow {node.id} exceeds max_iterations limit")
            except (TypeError, ValueError):
                errors.append(f"repeat_subflow {node.id} has an invalid max_iterations value")
        if node.type in {"map_subflow", "repeat_subflow"}:
            if "subflow" not in node.config:
                errors.append(f"{node.id} is missing a subflow definition")
            else:
                try:
                    subflow = WorkflowDefinition.model_validate(node.config["subflow"])
                except Exception as exc:
                    errors.append(f"{node.id} has an invalid subflow: {exc}")
                else:
                    nested = self.validate(subflow, visiting=visiting, depth=depth)
                    errors.extend(f"{node.id}: {error}" for error in nested.errors)
        return errors

    @staticmethod
    def _find_port(ports: list[PortDefinition], name: str) -> PortDefinition | None:
        for port in ports:
            if port.name == name:
                return port
        return None

    @staticmethod
    def _ports_compatible(source: PortDefinition, target: PortDefinition) -> bool:
        if source.kind == target.kind == "list":
            return not target.item_kind or not source.item_kind or source.item_kind == target.item_kind
        return kinds_compatible(source.kind, target.kind)

    @staticmethod
    def _topological_order(workflow: WorkflowDefinition, outgoing: dict[str, list[Any]]) -> tuple[list[str], str | None]:
        incoming_count = {node.id: 0 for node in workflow.nodes}
        for edges in outgoing.values():
            for edge in edges:
                incoming_count[edge.target_node_id] += 1
        ready = sorted(node_id for node_id, count in incoming_count.items() if count == 0)
        order: list[str] = []
        while ready:
            node_id = ready.pop(0)
            order.append(node_id)
            for edge in outgoing[node_id]:
                incoming_count[edge.target_node_id] -= 1
                if incoming_count[edge.target_node_id] == 0:
                    ready.append(edge.target_node_id)
                    ready.sort()
        if len(order) != len(workflow.nodes):
            return order, "Workflow graph contains a cycle"
        return order, None
