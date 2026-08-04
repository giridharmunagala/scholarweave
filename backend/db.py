from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from backend.config import Settings
from backend.utils import dumps_json, loads_json


class Base(DeclarativeBase):
    pass


class JSONText(TypeDecorator[Any]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return dumps_json(value)

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        return loads_json(value)


#: Columns added after a table first shipped. ``create_all`` only creates missing
#: tables, so existing databases need these applied by hand.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "runs": {"owner_pid": "INTEGER"},
}


def _apply_additive_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    for table, columns in _ADDED_COLUMNS.items():
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        missing = {name: kind for name, kind in columns.items() if name not in existing}
        if not missing:
            continue
        with engine.begin() as connection:
            for name, kind in missing.items():
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = create_engine(
        f"sqlite:///{settings.database_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    _apply_additive_migrations(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
