from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import column, delete, func, literal, select, table, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from backend.workspace.models import WorkspaceEntry

_SEARCH_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_FTS_INSERT = """
    INSERT INTO workspace_entries_fts (path, name, display_name, content, tags)
    SELECT path, name, COALESCE(display_name, ''), search_content,
        COALESCE((SELECT group_concat(value, ' ') FROM json_each(tags_json)), '')
    FROM workspace_entries
"""
_BM25 = "bm25(workspace_entries_fts, 0, 2, 5, 1, 2)"


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
                connection.exec_driver_sql(_FTS_INSERT)

    def count(self) -> int:
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(WorkspaceEntry)) or 0)

    def rebuild(self, entries: Sequence[WorkspaceEntry]) -> None:
        """Replace both indexes atomically after all source files have been read."""
        with self._session_factory() as session:
            session.execute(delete(WorkspaceEntry))
            session.add_all(entries)
            session.flush()
            session.execute(text("DELETE FROM workspace_entries_fts"))
            session.execute(text(_FTS_INSERT))
            session.commit()

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
                text(_FTS_INSERT + " WHERE path = :path"),
                {"path": entry.path},
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
    ) -> list[tuple[WorkspaceEntry, float | None, str | None]]:
        with self._session_factory() as session:
            tokens = list(dict.fromkeys(_SEARCH_TOKEN.findall(query or "")))
            if query and not tokens:
                return []
            statement = select(WorkspaceEntry, literal(None), literal(None))
            if tokens:
                fts = table("workspace_entries_fts", column("path"))
                statement = (
                    select(
                        WorkspaceEntry,
                        text(f"-{_BM25}"),
                        text("snippet(workspace_entries_fts, 3, '', '', ' ... ', 32)"),
                    )
                    .join(fts, WorkspaceEntry.path == fts.c.path)
                    .where(text("workspace_entries_fts MATCH :query"))
                    .params(query=" OR ".join(f'"{token}"' for token in tokens))
                )
            if kinds:
                statement = statement.where(WorkspaceEntry.kind.in_(kinds))
            for tag in tags:
                statement = statement.where(
                    func.instr(WorkspaceEntry.tags_text, f"\n{tag.casefold()}\n") > 0
                )
            if tokens:
                statement = statement.order_by(text(_BM25))
            statement = statement.order_by(WorkspaceEntry.modified_at.desc(), WorkspaceEntry.path)
            statement = statement.offset(offset).limit(limit)
            return [(entry, score, excerpt) for entry, score, excerpt in session.execute(statement)]

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
