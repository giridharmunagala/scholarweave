from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from backend.conversations.models import ConversationRecord
from backend.tracks.models import ResearchTrack, TrackConversation, TrackPaper
from backend.utils import utcnow


class TrackRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def list(self) -> list[ResearchTrack]:
        with self._sessions() as session:
            return list(session.scalars(select(ResearchTrack).order_by(ResearchTrack.created_at)))

    def get(self, track_id: str) -> ResearchTrack | None:
        with self._sessions() as session:
            return session.get(ResearchTrack, track_id)

    def create(self, track_id: str, title: str, folder_path: str) -> ResearchTrack:
        with self._sessions.begin() as session:
            record = ResearchTrack(id=track_id, title=title, folder_path=folder_path)
            session.add(record)
        return record

    def touch(self, track_id: str) -> None:
        with self._sessions.begin() as session:
            record = session.get(ResearchTrack, track_id)
            if record is not None:
                record.updated_at = utcnow()

    def paper_ids(self, track_id: str) -> list[str]:
        with self._sessions() as session:
            return list(session.scalars(
                select(TrackPaper.document_id).where(TrackPaper.track_id == track_id)
                .order_by(TrackPaper.document_id)
            ))

    def set_papers(self, track_id: str, paper_ids: list[str]) -> None:
        with self._sessions.begin() as session:
            session.execute(delete(TrackPaper).where(TrackPaper.track_id == track_id))
            session.add_all(TrackPaper(track_id=track_id, document_id=paper) for paper in paper_ids)

    def conversation_ids(self, track_id: str) -> list[str]:
        with self._sessions() as session:
            return list(session.scalars(
                select(TrackConversation.conversation_id)
                .where(TrackConversation.track_id == track_id)
                .order_by(TrackConversation.conversation_id)
            ))

    def conversation_exists(self, conversation_id: str) -> bool:
        with self._sessions() as session:
            return session.get(ConversationRecord, conversation_id) is not None

    def link_conversation(self, track_id: str, conversation_id: str) -> None:
        with self._sessions.begin() as session:
            record = session.get(TrackConversation, conversation_id)
            if record is None:
                session.add(TrackConversation(track_id=track_id, conversation_id=conversation_id))
            else:
                record.track_id = track_id

    def unlink_conversation(self, conversation_id: str) -> None:
        with self._sessions.begin() as session:
            session.execute(delete(TrackConversation).where(
                TrackConversation.conversation_id == conversation_id
            ))

    def for_conversation(self, conversation_id: str) -> str | None:
        with self._sessions() as session:
            record = session.get(TrackConversation, conversation_id)
            return record.track_id if record else None
