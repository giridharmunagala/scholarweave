from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Workflow, WorkflowVersion
from backend.registry import BaseNode, NodeExecutionContext, NodeProvider, PortDefinition
from backend.schemas import (
    NodeDefinitionResponse,
    PortDefinitionResponse,
    WorkflowDefinition,
    WorkflowSignature,
)

WORKFLOW_NODE_PREFIX = "workflow:"
WORKFLOW_NODE_CATEGORY = "workflows"


def workflow_node_type(workflow_id: str) -> str:
    return f"{WORKFLOW_NODE_PREFIX}{workflow_id}"


class SubWorkflowNode(BaseNode):
    """A saved workflow exposed as a node, with ports taken from its declared interface."""

    category = WORKFLOW_NODE_CATEGORY
    config_model = None

    def __init__(
        self,
        workflow_id: str,
        name: str,
        description: str | None,
        definition: WorkflowDefinition,
        signature: WorkflowSignature,
    ) -> None:
        self.workflow_id = workflow_id
        self.definition = definition
        self.signature = signature
        self.type_name = workflow_node_type(workflow_id)
        self.label = name
        self.description = description or f"Runs the saved workflow “{name}”."
        self.tags = ["workflow", "reusable"]
        self.inputs = [
            PortDefinition(
                name=port.key,
                kind=port.kind,
                description=port.description or port.label,
                required=port.required,
            )
            for port in signature.inputs
        ]
        self.outputs = [
            PortDefinition(name=port.key, kind=port.kind, description=port.description or port.label, required=False)
            for port in signature.outputs
        ]

    def referenced_workflow(self) -> tuple[str, WorkflowDefinition] | None:
        return self.workflow_id, self.definition

    def catalog_entry(self) -> NodeDefinitionResponse:
        return NodeDefinitionResponse(
            type=self.type_name,
            label=self.label,
            description=self.description,
            category=self.category,
            tags=list(self.tags),
            inputs=[PortDefinitionResponse(**asdict(port)) for port in self.inputs],
            outputs=[PortDefinitionResponse(**asdict(port)) for port in self.outputs],
            config_schema={},
        )

    async def execute(self, context: NodeExecutionContext, inputs: dict[str, Any], config: Any) -> dict[str, Any]:
        child_inputs = {port.key: inputs[port.key] for port in self.signature.inputs if port.key in inputs}
        outputs = await context.execute_subflow(self.definition, child_inputs, f"::{self.workflow_id[:8]}")
        if not isinstance(outputs, dict):
            outputs = {"result": outputs}
        return {port.key: outputs.get(port.key) for port in self.signature.outputs}


class WorkflowNodeProvider(NodeProvider):
    """Publishes every saved workflow as a callable node, refreshed when a new version is saved."""

    def __init__(self, session_factory: Callable[[], Session], validator: Any) -> None:
        self.session_factory = session_factory
        self.validator = validator
        self._cache: dict[str, tuple[str, SubWorkflowNode]] = {}
        self._building: set[str] = set()

    def resolve(self, type_name: str) -> BaseNode | None:
        if not type_name.startswith(WORKFLOW_NODE_PREFIX):
            return None
        workflow_id = type_name[len(WORKFLOW_NODE_PREFIX) :]
        with self.session_factory() as session:
            workflow = session.get(Workflow, workflow_id)
            if workflow is None:
                return None
            version = self._latest_version(session, workflow_id)
            if version is None:
                return None
            return self._build(workflow.name, workflow.description, workflow_id, version)

    def describe_missing(self, type_name: str) -> str | None:
        if not type_name.startswith(WORKFLOW_NODE_PREFIX):
            return None
        return f"Referenced workflow {type_name[len(WORKFLOW_NODE_PREFIX):]} no longer exists"

    def catalog(self) -> list[NodeDefinitionResponse]:
        entries: list[NodeDefinitionResponse] = []
        with self.session_factory() as session:
            workflows = list(session.scalars(select(Workflow).order_by(Workflow.name)))
            for workflow in workflows:
                version = self._latest_version(session, workflow.id)
                if version is None:
                    continue
                try:
                    node = self._build(workflow.name, workflow.description, workflow.id, version)
                except Exception:  # noqa: BLE001 - a broken saved workflow must not break the palette
                    continue
                entries.append(node.catalog_entry())
        return entries

    @staticmethod
    def _latest_version(session: Session, workflow_id: str) -> WorkflowVersion | None:
        return session.scalars(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        ).first()

    def _build(self, name: str, description: str | None, workflow_id: str, version: WorkflowVersion) -> SubWorkflowNode:
        cached = self._cache.get(workflow_id)
        if cached and cached[0] == version.id:
            return cached[1]
        definition = WorkflowDefinition.model_validate(version.definition_json)
        if workflow_id in self._building:
            # Re-entered while deriving our own signature, so this workflow references
            # itself. Hand back a portless placeholder and let the validator report it.
            return SubWorkflowNode(workflow_id, name, description, definition, WorkflowSignature())
        self._building.add(workflow_id)
        try:
            node = SubWorkflowNode(
                workflow_id=workflow_id,
                name=name,
                description=description,
                definition=definition,
                signature=self.validator.signature(definition),
            )
        finally:
            self._building.discard(workflow_id)
        self._cache[workflow_id] = (version.id, node)
        return node
