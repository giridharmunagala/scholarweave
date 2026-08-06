from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.agents.models import AgentRecord, AgentRevision
from backend.core.errors import ConflictError, NotFoundError
from backend.core.time import utcnow


class AgentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def list(self, *, include_templates: bool = True) -> list[AgentRecord]:
        with self._sessions() as session:
            statement = select(AgentRecord).order_by(AgentRecord.updated_at.desc())
            if not include_templates:
                statement = statement.where(AgentRecord.is_template.is_(False))
            records = list(session.scalars(statement))
            for record in records:
                _ = record.revisions
            return records

    def get(self, agent_id: str) -> AgentRecord:
        with self._sessions() as session:
            record = session.get(AgentRecord, agent_id)
            if record is None:
                raise NotFoundError("Agent was not found.")
            _ = record.revisions
            return record

    def get_revision(self, revision_id: str) -> AgentRevision:
        with self._sessions() as session:
            revision = session.get(AgentRevision, revision_id)
            if revision is None:
                raise NotFoundError("Agent revision was not found.")
            return revision

    def list_revisions(self, agent_id: str) -> list[AgentRevision]:
        with self._sessions() as session:
            exists = session.get(AgentRecord, agent_id)
            if exists is None:
                raise NotFoundError("Agent was not found.")
            return list(
                session.scalars(
                    select(AgentRevision)
                    .where(AgentRevision.agent_id == agent_id)
                    .order_by(AgentRevision.revision.desc())
                )
            )

    def create(
        self,
        *,
        name: str,
        description: str | None,
        is_template: bool,
        blueprint: dict[str, Any],
        presentation: dict[str, Any],
        sdk_version: str,
    ) -> AgentRecord:
        with self._sessions() as session:
            if session.scalar(select(AgentRecord.id).where(AgentRecord.name == name)):
                raise ConflictError(f"An agent named '{name}' already exists.")
            record = AgentRecord(
                name=name,
                description=description,
                is_template=is_template,
            )
            session.add(record)
            session.flush()
            session.add(
                AgentRevision(
                    agent_id=record.id,
                    revision=1,
                    blueprint_json=blueprint,
                    presentation_json=presentation,
                    sdk_version=sdk_version,
                )
            )
            session.commit()
            session.refresh(record)
            _ = record.revisions
            return record

    def add_revision(
        self,
        agent_id: str,
        *,
        name: str,
        description: str | None,
        blueprint: dict[str, Any],
        presentation: dict[str, Any],
        sdk_version: str,
    ) -> AgentRecord:
        with self._sessions() as session:
            record = session.get(AgentRecord, agent_id)
            if record is None:
                raise NotFoundError("Agent was not found.")
            conflicting = session.scalar(
                select(AgentRecord.id).where(
                    AgentRecord.name == name,
                    AgentRecord.id != agent_id,
                )
            )
            if conflicting:
                raise ConflictError(f"An agent named '{name}' already exists.")
            next_revision = (
                session.scalar(
                    select(func.max(AgentRevision.revision)).where(
                        AgentRevision.agent_id == agent_id
                    )
                )
                or 0
            ) + 1
            record.name = name
            record.description = description
            record.updated_at = utcnow()
            session.add(
                AgentRevision(
                    agent_id=agent_id,
                    revision=next_revision,
                    blueprint_json=blueprint,
                    presentation_json=presentation,
                    sdk_version=sdk_version,
                )
            )
            session.commit()
            session.refresh(record)
            _ = record.revisions
            return record

    def delete(self, agent_id: str) -> None:
        with self._sessions() as session:
            record = session.get(AgentRecord, agent_id)
            if record is None:
                raise NotFoundError("Agent was not found.")
            session.delete(record)
            session.commit()
