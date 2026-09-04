from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.errors import ConflictError, NotFoundError
from backend.providers.models import ProviderProfile
from backend.utils import utcnow


class ProviderRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def list(self, *, include_archived: bool = False) -> list[ProviderProfile]:
        with self._sessions() as session:
            statement = select(ProviderProfile).order_by(ProviderProfile.created_at.asc())
            if not include_archived:
                statement = statement.where(ProviderProfile.state == "active")
            return list(session.scalars(statement))

    def get(self, profile_id: str, *, include_archived: bool = False) -> ProviderProfile:
        with self._sessions() as session:
            record = session.get(ProviderProfile, profile_id)
            if record is None or (record.state != "active" and not include_archived):
                raise NotFoundError("Provider profile was not found.")
            return record

    def create(self, **values) -> ProviderProfile:
        with self._sessions() as session:
            if session.scalar(
                select(ProviderProfile.id).where(ProviderProfile.name == values["name"])
            ):
                raise ConflictError(f"A provider named '{values['name']}' already exists.")
            record = ProviderProfile(**values)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def update(self, profile_id: str, **values) -> ProviderProfile:
        with self._sessions() as session:
            record = session.get(ProviderProfile, profile_id)
            if record is None:
                raise NotFoundError("Provider profile was not found.")
            name = values.get("name")
            if name and session.scalar(
                select(ProviderProfile.id).where(
                    ProviderProfile.name == name,
                    ProviderProfile.id != profile_id,
                )
            ):
                raise ConflictError(f"A provider named '{name}' already exists.")
            for key, value in values.items():
                setattr(record, key, value)
            record.updated_at = utcnow()
            session.commit()
            session.refresh(record)
            return record

    def archive(self, profile_id: str) -> ProviderProfile:
        return self.update(profile_id, state="archived")

    def ensure_default_ollama(self, *, base_url: str) -> ProviderProfile:
        with self._sessions() as session:
            record = session.scalar(
                select(ProviderProfile).where(ProviderProfile.name == "Default Ollama")
            )
            if record is not None:
                return record
            record = ProviderProfile(
                name="Default Ollama",
                kind="ollama",
                base_url=base_url,
                state="active",
                models_json=[],
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record
