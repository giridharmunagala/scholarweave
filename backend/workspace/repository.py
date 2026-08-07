from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.workspace.models import WorkspaceEntry

_SEARCH_TOKEN = re.compile(r"[\w-]+", re.UNICODE)


class WorkspaceRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        engine = session_factory.kw["bind"]
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS workspace_entries_fts
                USING fts5(
                    path UNINDEXED,
                    name,
                    display_name,
                    content,
                    tags,
                    tokenize = 'unicode61'
                )
                """
            )
            entry_count = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM workspace_entries"
            ).scalar_one()
            search_count = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM workspace_entries_fts"
            ).scalar_one()
            if entry_count != search_count:
                connection.exec_driver_sql("DELETE FROM workspace_entries_fts")
                connection.exec_driver_sql(
                    """
                    INSERT INTO workspace_entries_fts
                        (path, name, display_name, content, tags)
                    SELECT
                        path,
                        name,
                        COALESCE(display_name, ''),
                        search_content,
                        tags_text
                    FROM workspace_entries
                    """
                )

    def count(self) -> int:
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(WorkspaceEntry)) or 0)

    def upsert(self, entry: WorkspaceEntry) -> WorkspaceEntry:
        values = {
            column.name: getattr(entry, column.name)
            for column in WorkspaceEntry.__table__.columns
        }
        with self._session_factory() as session:
            statement = sqlite_insert(WorkspaceEntry).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[WorkspaceEntry.path],
                set_={key: value for key, value in values.items() if key != "path"},
            )
            session.execute(statement)
            session.execute(
                text("DELETE FROM workspace_entries_fts WHERE path = :path"),
                {"path": entry.path},
            )
            session.execute(
                text(
                    """
                    INSERT INTO workspace_entries_fts
                        (path, name, display_name, content, tags)
                    VALUES
                        (:path, :name, :display_name, :content, :tags)
                    """
                ),
                {
                    "path": entry.path,
                    "name": entry.name,
                    "display_name": entry.display_name or "",
                    "content": entry.search_content,
                    "tags": " ".join(entry.tags_json or []),
                },
            )
            session.commit()
            stored = session.get(WorkspaceEntry, entry.path)
            assert stored is not None
            return stored

    def get(self, path: str) -> WorkspaceEntry | None:
        with self._session_factory() as session:
            return session.get(WorkspaceEntry, path)

    def list(self) -> list[WorkspaceEntry]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(WorkspaceEntry).order_by(WorkspaceEntry.path.collate("NOCASE"))
                )
            )

    def list_prefix(self, prefix: str) -> list[WorkspaceEntry]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(WorkspaceEntry)
                    .where(
                        WorkspaceEntry.path.like(
                            f"{escaped}/%",
                            escape="\\",
                        )
                    )
                    .order_by(WorkspaceEntry.path.collate("NOCASE"))
                )
            )

    def search(
        self,
        *,
        query: str | None,
        kinds: Sequence[str],
        tags: Sequence[str],
        limit: int,
        offset: int,
    ) -> list[WorkspaceEntry]:
        with self._session_factory() as session:
            statement = select(WorkspaceEntry)
            tokens = _SEARCH_TOKEN.findall(query or "")
            if tokens:
                match_query = " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
                paths = (
                    select(text("path"))
                    .select_from(text("workspace_entries_fts"))
                    .where(text("workspace_entries_fts MATCH :query"))
                )
                statement = statement.where(WorkspaceEntry.path.in_(paths)).params(
                    query=match_query
                )
            if kinds:
                statement = statement.where(WorkspaceEntry.kind.in_(kinds))
            for tag in tags:
                statement = statement.where(
                    func.instr(WorkspaceEntry.tags_text, f"\n{tag.casefold()}\n") > 0
                )
            statement = statement.order_by(WorkspaceEntry.modified_at.desc())
            statement = statement.offset(offset).limit(limit)
            return list(session.scalars(statement))

    def delete_path(self, path: str) -> None:
        with self._session_factory() as session:
            session.execute(delete(WorkspaceEntry).where(WorkspaceEntry.path == path))
            session.execute(
                text("DELETE FROM workspace_entries_fts WHERE path = :path"),
                {"path": path},
            )
            session.commit()

    def delete_prefix(self, prefix: str) -> None:
        paths = [entry.path for entry in self.list_prefix(prefix)]
        with self._session_factory() as session:
            if paths:
                session.execute(
                    delete(WorkspaceEntry).where(WorkspaceEntry.path.in_(paths))
                )
                for path in paths:
                    session.execute(
                        text("DELETE FROM workspace_entries_fts WHERE path = :path"),
                        {"path": path},
                    )
            session.commit()
