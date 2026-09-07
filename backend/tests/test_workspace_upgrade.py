from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from backend.documents.models import Artifact, Document
from backend.documents.repository import DocumentRepository
from backend.persistence.database import create_session_factory
from backend.persistence.files import SafeStorage, StorageError
from backend.workspace.repository import WorkspaceRepository
from backend.workspace.service import WorkspaceService
from backend.workspace.upgrade import WorkspaceUpgrade


def seed_old_workspace(settings):
    sessions = create_session_factory(settings)
    storage = SafeStorage(settings)
    source = storage.write_document_bytes("paper-1/source/original.pdf", b"%PDF-1.4 original")
    with sessions() as session:
        session.add(Document(id="paper-1", title="A Study", source_filename="original.pdf"))
        session.add(Artifact(id="source-1", document_id="paper-1", owner_type="document", kind="source_pdf",
                             relative_path=source.relative_path, media_type="application/pdf",
                             size_bytes=source.size_bytes, sha256=source.sha256, metadata_json={"storage_area": "documents"}))
        session.commit()
    workspace = WorkspaceService(storage, WorkspaceRepository(sessions))
    workspace.write_file("papers/paper-1/notes.md", "Keep my notes", tags=["important"])
    workspace.write_file("papers/paper-1/summaries/version.md", "Immutable summary")
    workspace.write_file("notes/ideas.md", "My independent knowledge", tags=["learning"])
    sessions.kw["bind"].dispose()
    return storage


def test_upgrade_preview_is_read_only_and_apply_preserves_research(test_settings):
    storage = seed_old_workspace(test_settings)
    upgrade = WorkspaceUpgrade(test_settings)
    plan = upgrade.preview()
    assert len(plan["moves"]) == 4
    assert not upgrade.backup_dir.exists()
    assert not (test_settings.workspace_dir / "library").exists()
    assert upgrade.required()
    sessions = create_session_factory(test_settings)
    documents = DocumentRepository(sessions, test_settings, storage)
    revision = documents.source_revision("paper-1")
    result = upgrade.apply()
    assert documents.source_revision("paper-1") == revision
    sessions.kw["bind"].dispose()
    assert result["stage"] == "complete"
    assert not upgrade.required()
    assert upgrade.apply() == result
    assert storage.read_workspace_file("library/papers/a-study--paper-1/notes.md")[1] == "Keep my notes"
    assert storage.read_workspace_file("library/papers/a-study--paper-1/summaries/version.md")[1] == "Immutable summary"
    assert storage.read_workspace_file("knowledge/imported/ideas.md")[1] == "My independent knowledge"
    assert (upgrade.backup_dir / "metadata.sqlite3").is_file()
    assert (upgrade.backup_dir / "workspace/papers/paper-1/notes.md").is_file()
    sessions = create_session_factory(test_settings)
    workspace = WorkspaceService(storage, WorkspaceRepository(sessions))
    assert workspace.search(query="independent")[0].tags == ("learning",)
    assert workspace.search(query="notes")[0].paper_id == "paper-1"
    with sessions() as session:
        artifact = session.get(Artifact, "source-1")
        assert artifact.metadata_json["storage_area"] == "workspace"
        assert artifact.relative_path == "library/papers/a-study--paper-1/source.pdf"
        assert artifact.sha256 == storage.file_hash(test_settings.workspace_dir, artifact.relative_path)
    sessions.kw["bind"].dispose()


def test_upgrade_retires_orphaned_legacy_conversation_rows(test_settings):
    seed_old_workspace(test_settings)
    with closing(sqlite3.connect(test_settings.database_path)) as connection, connection:
        connection.execute(
            "CREATE TABLE code_conversations (conversation_id TEXT PRIMARY KEY "
            "REFERENCES conversations(id) ON DELETE CASCADE)"
        )
        connection.execute("INSERT INTO code_conversations VALUES ('retired-conversation')")
        assert len(connection.execute("PRAGMA foreign_key_check").fetchall()) == 1

    upgrade = WorkspaceUpgrade(test_settings)
    upgrade.apply()

    with closing(sqlite3.connect(test_settings.database_path)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM code_conversations").fetchone()[0] == 0
    with closing(sqlite3.connect(upgrade.backup_dir / "metadata.sqlite3")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_conversations").fetchone()[0] == 1


def test_upgrade_refuses_collision_without_touching_originals(test_settings):
    storage = seed_old_workspace(test_settings)
    storage.write_workspace_file("library/papers/a-study--paper-1/notes.md", "Different notes")
    with pytest.raises(StorageError, match="destination already exists"):
        WorkspaceUpgrade(test_settings).apply()
    assert storage.read_workspace_file("papers/paper-1/notes.md")[1] == "Keep my notes"


def test_upgrade_resumes_verified_copies_after_interruption(test_settings, monkeypatch):
    storage = seed_old_workspace(test_settings)
    upgrade = WorkspaceUpgrade(test_settings)
    relocate = upgrade.repository.relocate

    def interrupted(*args):
        raise RuntimeError("interrupted before metadata commit")

    monkeypatch.setattr(upgrade.repository, "relocate", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        upgrade.apply()
    assert storage.read_workspace_file("papers/paper-1/notes.md")[1] == "Keep my notes"
    monkeypatch.setattr(upgrade.repository, "relocate", relocate)
    assert upgrade.apply()["stage"] == "complete"
    assert not upgrade.required()


def test_upgrade_restore_preserves_both_original_and_new_research(test_settings):
    storage = seed_old_workspace(test_settings)
    upgrade = WorkspaceUpgrade(test_settings)
    upgrade.apply()
    storage.write_workspace_file("knowledge/new.md", "Keep work created after the upgrade")
    upgrade.restore()
    assert storage.read_workspace_file("papers/paper-1/notes.md")[1] == "Keep my notes"
    preserved = test_settings.workspace_dir.with_name("workspace.before-layout-restore")
    assert (preserved / "knowledge/new.md").read_text() == "Keep work created after the upgrade"
    assert (test_settings.documents_dir / "paper-1/source/original.pdf").is_file()
    assert upgrade.required()
    upgrade.restore()


def test_restore_refuses_corrupt_backup_before_touching_current_files(test_settings):
    storage = seed_old_workspace(test_settings)
    upgrade = WorkspaceUpgrade(test_settings)
    upgrade.apply()
    storage.write_text(upgrade.backup_dir / "workspace", "papers/paper-1/notes.md", "Corrupt backup")
    with pytest.raises(StorageError, match="checksum validation"):
        upgrade.restore()
    assert storage.read_workspace_file("library/papers/a-study--paper-1/notes.md")[1] == "Keep my notes"


def test_upgrade_preserves_standalone_note_identity_and_display_name(test_settings):
    storage = seed_old_workspace(test_settings)
    sessions = create_session_factory(test_settings)
    repository = WorkspaceRepository(sessions)
    entry = repository.get("notes/ideas.md")
    entry.note_id = "b47b892c-9ae4-4f67-82ca-bc41f78a77d1"
    entry.note_name = "Reusable Ideas"
    entry.display_name = entry.note_name
    repository.upsert(entry)
    WorkspaceUpgrade(test_settings).apply()
    workspace = WorkspaceService(storage, repository)
    note = workspace.search(query="independent")[0]
    assert note.path == f"knowledge/reusable-ideas--{entry.note_id}.md"
    assert (note.note_id, note.note_name, note.tags) == (entry.note_id, entry.note_name, ("learning",))
    workspace.refresh_index()
    assert workspace.search(query="independent")[0].note_id == entry.note_id
    sessions.kw["bind"].dispose()