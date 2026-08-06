from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agents.blueprint import AgentBlueprint
from backend.agents.compiler import AgentCompiler, CompiledAgent
from backend.agents.models import AgentRecord, AgentRevision
from backend.agents.repository import AgentRepository
from backend.core.errors import ValidationError


@dataclass(frozen=True, slots=True)
class AgentDocument:
    record: AgentRecord
    latest_revision: AgentRevision


class AgentService:
    def __init__(self, repository: AgentRepository, compiler: AgentCompiler) -> None:
        self._repository = repository
        self._compiler = compiler

    def list(self) -> list[AgentDocument]:
        return [self._document(record) for record in self._repository.list()]

    def get(self, agent_id: str) -> AgentDocument:
        return self._document(self._repository.get(agent_id))

    def get_revision(self, revision_id: str) -> tuple[AgentRevision, AgentBlueprint]:
        revision = self._repository.get_revision(revision_id)
        return revision, AgentBlueprint.model_validate(revision.blueprint_json)

    def list_revisions(self, agent_id: str) -> list[AgentRevision]:
        return self._repository.list_revisions(agent_id)

    def compile_revision(self, revision_id: str) -> CompiledAgent:
        _, blueprint = self.get_revision(revision_id)
        return self._compiler.compile(blueprint)

    def validate(self, blueprint: AgentBlueprint) -> tuple[str, ...]:
        return self._compiler.validate(blueprint)

    def create(
        self,
        blueprint: AgentBlueprint,
        *,
        presentation: dict[str, Any] | None = None,
        is_template: bool = False,
    ) -> AgentDocument:
        self._ensure_valid(blueprint)
        record = self._repository.create(
            name=blueprint.name,
            description=blueprint.description,
            is_template=is_template,
            blueprint=blueprint.model_dump(mode="json", by_alias=True),
            presentation=presentation or {},
            sdk_version=blueprint.sdk_version,
        )
        return self._document(record)

    def update(
        self,
        agent_id: str,
        blueprint: AgentBlueprint,
        *,
        presentation: dict[str, Any] | None = None,
    ) -> AgentDocument:
        self._ensure_valid(blueprint)
        record = self._repository.add_revision(
            agent_id,
            name=blueprint.name,
            description=blueprint.description,
            blueprint=blueprint.model_dump(mode="json", by_alias=True),
            presentation=presentation or {},
            sdk_version=blueprint.sdk_version,
        )
        return self._document(record)

    def delete(self, agent_id: str) -> None:
        self._repository.delete(agent_id)

    def _ensure_valid(self, blueprint: AgentBlueprint) -> None:
        issues = self.validate(blueprint)
        if issues:
            raise ValidationError("Agent blueprint is invalid.", issues=issues)

    @staticmethod
    def _document(record: AgentRecord) -> AgentDocument:
        if not record.revisions:
            raise RuntimeError(f"Agent '{record.id}' has no revisions.")
        return AgentDocument(record=record, latest_revision=record.revisions[-1])
