from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.errors import ConflictError, NotFoundError
from backend.tools.models import FunctionToolRecord, FunctionToolRevision
from backend.core.time import utcnow


class FunctionToolRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def list(self, *, include_archived: bool = False) -> list[FunctionToolRecord]:
        with self._sessions() as session:
            statement = select(FunctionToolRecord).order_by(FunctionToolRecord.updated_at.desc())
            if not include_archived:
                statement = statement.where(FunctionToolRecord.archived.is_(False))
            records = list(session.scalars(statement))
            for record in records:
                _ = record.revisions
            return records

    def get(self, definition_id: str) -> FunctionToolRecord:
        with self._sessions() as session:
            record = session.get(FunctionToolRecord, definition_id)
            if record is None:
                raise NotFoundError("Function tool was not found.")
            _ = record.revisions
            return record

    def get_revision(self, revision_id: str) -> FunctionToolRevision:
        with self._sessions() as session:
            revision = session.get(FunctionToolRevision, revision_id)
            if revision is None:
                raise NotFoundError("Function tool revision was not found.")
            _ = revision.definition
            return revision

    def create(
        self,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        code: str,
        requires_approval: bool,
    ) -> FunctionToolRecord:
        with self._sessions() as session:
            if session.scalar(select(FunctionToolRecord.id).where(FunctionToolRecord.name == name)):
                raise ConflictError(f"A function tool named '{name}' already exists.")
            record = FunctionToolRecord(name=name, description=description)
            session.add(record)
            session.flush()
            session.add(
                FunctionToolRevision(
                    definition_id=record.id,
                    revision=1,
                    description=description,
                    parameters_schema_json=parameters_schema,
                    output_schema_json=output_schema,
                    code=code,
                    requires_approval=requires_approval,
                )
            )
            session.commit()
            session.refresh(record)
            _ = record.revisions
            return record

    def add_revision(
        self,
        definition_id: str,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        code: str,
        requires_approval: bool,
    ) -> FunctionToolRecord:
        with self._sessions() as session:
            record = session.get(FunctionToolRecord, definition_id)
            if record is None:
                raise NotFoundError("Function tool was not found.")
            conflict = session.scalar(
                select(FunctionToolRecord.id).where(
                    FunctionToolRecord.name == name,
                    FunctionToolRecord.id != definition_id,
                )
            )
            if conflict:
                raise ConflictError(f"A function tool named '{name}' already exists.")
            next_revision = (
                session.scalar(
                    select(func.max(FunctionToolRevision.revision)).where(
                        FunctionToolRevision.definition_id == definition_id
                    )
                )
                or 0
            ) + 1
            record.name = name
            record.description = description
            record.updated_at = utcnow()
            session.add(
                FunctionToolRevision(
                    definition_id=definition_id,
                    revision=next_revision,
                    description=description,
                    parameters_schema_json=parameters_schema,
                    output_schema_json=output_schema,
                    code=code,
                    requires_approval=requires_approval,
                )
            )
            session.commit()
            session.refresh(record)
            _ = record.revisions
            return record

    def archive(self, definition_id: str) -> FunctionToolRecord:
        with self._sessions() as session:
            record = session.get(FunctionToolRecord, definition_id)
            if record is None:
                raise NotFoundError("Function tool was not found.")
            record.archived = True
            record.updated_at = utcnow()
            session.commit()
            session.refresh(record)
            _ = record.revisions
            return record
