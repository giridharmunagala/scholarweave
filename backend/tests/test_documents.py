from __future__ import annotations

import base64
import shutil

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app
from backend.bootstrap import create_services
from backend.documents import DocumentProcessingError
from backend.documents.formatting import DocumentFormatter
from backend.documents.formatting import build_paper_manifest
from backend.documents.models import Artifact, Document, DocumentChunk
from backend.documents.service import _opening_abstract


@pytest.mark.anyio
async def test_ingestion_persists_repaired_native_unicode_consistently(test_settings, monkeypatch, caplog):
    source_text = "Readable scientific evidence. " * 4 + "\ud835\udc00 and broken \ud835\ud835 \udc00."
    expected = source_text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")

    class Page:
        def extract_text(self):
            return source_text

    class Reader:
        pages = [Page()]

    monkeypatch.setattr("backend.documents.ocr.PdfReader", lambda _: Reader())
    services = create_services(test_settings)
    try:
        source = b"%PDF-1.4\n%%EOF"
        document = services.documents.create_document_from_bytes(source, filename="unicode.pdf", title="Unicode paper")
        await services.documents.ingest_document(document.id, enhance_with_llm=False)
        assert services.documents.get_document(document.id).status == "ready"
        artifacts = {artifact.kind: artifact for artifact in services.documents.get_document_artifacts(document.id)}
        assert services.documents.artifact_content(artifacts["extracted_markdown"]) == expected
        manifest = services.documents.artifact_content(artifacts["extracted_manifest"])
        assert manifest["pages"][0]["text"] == expected
        assert manifest["pages"][0]["raw_text"] == expected
        assert manifest["pages"][0]["char_count"] == len(expected)
        with services.session_factory() as session:
            chunks = list(session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document.id)))
        assert chunks and all(not any(0xD800 <= ord(char) <= 0xDFFF for char in chunk.text) for chunk in chunks)
        assert services.documents.artifact_bytes(artifacts["source_pdf"]) == source
        assert "PDF page 1" in caplog.text
    finally:
        await services.close()


def test_paper_manifest_has_stable_reading_structure() -> None:
    manifest = build_paper_manifest(
        document_id="paper-1",
        title="Structured Paper",
        source_filename="paper.pdf",
        content_type="application/pdf",
        pages=[
            {"page": 1, "text": "Abstract text", "ocr_used": False},
            {"page": 2, "text": "Method text", "ocr_used": True},
        ],
        chunks=[
            {
                "section_title": "Method",
                "page_start": 2,
                "page_end": 2,
                "citation": "p.2",
                "text": "Method text",
            }
        ],
    )

    assert manifest["schema"] == "scholarweave.paper"
    assert manifest["schema_version"] == 1
    assert manifest["paper"]["page_count"] == 2
    assert manifest["pages"][1]["citation"] == "p.2"
    assert manifest["sections"] == [
        {
            "section_index": 0,
            "title": "Method",
            "citation": "p.2",
            "page_start": 2,
            "page_end": 2,
            "chunk_index": 0,
        }
    ]
    assert manifest["content"] == {
        "char_count": 24,
        "nonempty_page_count": 2,
        "chunk_count": 1,
    }


def test_paper_folders_organize_documents(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        document = app.state.services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF",
            filename="organized.pdf",
            title="Organized Paper",
        )
        created = client.post("/api/paper-folders", json={"name": "Methods"})
        assert created.status_code == 201, created.text
        folder = created.json()

        assigned = client.put(
            f"/api/documents/{document.id}/folder",
            json={"folder_id": folder["id"]},
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["metadata"]["folder_id"] == folder["id"]
        assert client.get("/api/paper-folders").json()[0]["name"] == "Methods"

        duplicate = client.post("/api/paper-folders", json={"name": " methods "})
        assert duplicate.status_code == 409

        deleted = client.delete(f"/api/paper-folders/{folder['id']}")
        assert deleted.status_code == 204, deleted.text
        assert client.get("/api/paper-folders").json() == []
        unfiled = client.get(f"/api/documents/{document.id}")
        assert "folder_id" not in unfiled.json()["metadata"]
        assert client.delete(f"/api/paper-folders/{folder['id']}").status_code == 404


def test_paper_usefulness_persists_separately_from_progress_and_files(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        services = app.state.services
        document = services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF", filename="rated.pdf", title="Rated paper",
            metadata={"source_url": "https://example.org/paper"},
        )
        folder = services.documents.create_folder("Methods")
        services.documents.assign_folder(document.id, folder.id)
        services.documents.repository.mark_ready(document.id, page_count=3, metadata={})
        url = f"/api/documents/{document.id}"
        original = client.get(url).json()
        assert original["usefulness"] == "unrated"
        for value in ("useful", "maybe_useful", "not_useful", "unrated", "useful"):
            response = client.put(f"{url}/usefulness", json={"usefulness": value})
            assert response.status_code == 200, response.text
            updated = response.json()
            assert updated["usefulness"] == value
            assert updated["metadata"].get("usefulness", "unrated") == value
            for field in ("status", "page_count", "artifacts", "chunks"):
                assert updated[field] == original[field]
            for key in ("folder_id", "source_url", "ingestion"):
                assert updated["metadata"][key] == original["metadata"][key]
            assert client.get("/api/documents").json()[0]["usefulness"] == value
        services.documents.repository.mark_processing(document.id)
        services.documents.repository.update_ingestion_progress(document.id, {"completed_pages": 1})
        services.documents.repository.mark_ready(document.id, page_count=3, metadata={})
        assert client.get(url).json()["usefulness"] == "useful"
        assert client.delete(f"/api/paper-folders/{folder.id}").status_code == 204
        assert client.get(url).json()["usefulness"] == "useful"
        for payload in ({}, {"usefulness": None}, {"usefulness": "essential"}, {"usefulness": "useful", "status": "ready"}):
            assert client.put(f"{url}/usefulness", json=payload).status_code == 422
        assert client.get(url).json()["usefulness"] == "useful"
        assert client.put("/api/documents/missing/usefulness", json={"usefulness": "useful"}).status_code == 404
    with TestClient(create_app(test_settings)) as client:
        assert client.get(url).json()["usefulness"] == "useful"
        assert client.get("/api/documents").json()[0]["usefulness"] == "useful"


def test_library_search_filters_usefulness_before_pagination(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        services = app.state.services
        folder = services.documents.create_folder("Rated papers")
        expected = {value: [] for value in ("unrated", "useful", "maybe_useful", "not_useful")}
        for value in expected:
            for index in range(2):
                document = services.documents.create_document_from_bytes(
                    b"%PDF-1.4\n%%EOF", filename=f"{value}-{index}.pdf", title="Quasar study",
                )
                expected[value].append(document.id)
                services.documents.assign_folder(document.id, folder.id)
                if value != "unrated":
                    response = client.put(f"/api/documents/{document.id}/usefulness", json={"usefulness": value})
                    assert response.status_code == 200, response.text
        assert len(client.get("/api/documents/search", params={"query": "quasar"}).json()) == 8
        for value, ids in expected.items():
            params = {"query": "quasar", "usefulness": value, "folder_id": folder.id, "paper_status": "uploaded"}
            hits = client.get("/api/documents/search", params=params).json()
            assert {hit["id"] for hit in hits} == set(ids)
            assert all(hit["usefulness"] == value for hit in hits)
            for offset, hit in enumerate(hits):
                page = client.get("/api/documents/search", params={**params, "limit": 1, "offset": offset})
                assert [item["id"] for item in page.json()] == [hit["id"]]
            assert client.get("/api/documents/search", params={**params, "unfiled": True}).json() == []
            assert client.get("/api/documents/search", params={**params, "paper_status": "ready"}).json() == []
        assert client.get("/api/documents/search", params={"query": "quasar", "usefulness": "invalid"}).status_code == 422


def test_library_bm25_search_titles_content_filters_and_pagination(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        services = app.state.services
        title_paper, content_paper, unrelated = [
            services.documents.create_document_from_bytes(
                b"%PDF-1.4\n%%EOF", filename=f"{index}.pdf", title=title,
            )
            for index, title in enumerate(("Quasar discovery", "Observational astronomy", "Unrelated research"))
        ]
        services.retrieval.replace_document_chunks(content_paper.id, [
            {"text": "We describe quasar observations and stellar measurements.", "page_start": index + 1}
            for index in range(30)
        ])
        folder = services.documents.create_folder("Astronomy")
        services.documents.assign_folder(content_paper.id, folder.id)
        response = client.get("/api/documents/search", params={"query": "quasar"})
        assert response.status_code == 200, response.text
        hits = response.json()
        assert [hit["id"] for hit in hits] == [title_paper.id, content_paper.id]
        assert hits[0]["score"] > hits[1]["score"] > 0
        assert hits[0]["citation"] is None
        assert hits[1]["citation"] == "p.1"
        assert hits[1]["page_start"] == 1
        assert "quasar" in hits[1]["excerpt"]
        assert "chunks" not in hits[1]
        assert client.get("/api/documents/search", params={
            "query": "quasar", "offset": 1, "limit": 1,
        }).json()[0]["id"] == content_paper.id
        assert [hit["id"] for hit in client.get("/api/documents/search", params={
            "query": "quasar", "folder_id": folder.id,
        }).json()] == [content_paper.id]
        assert [hit["id"] for hit in client.get("/api/documents/search", params={
            "query": "quasar", "unfiled": True, "paper_status": "uploaded",
        }).json()] == [title_paper.id]
        assert client.get("/api/documents/search", params={"query": "quasar", "paper_status": "ready"}).json() == []
        assert client.get("/api/documents/search", params={"query": "*** : ()"}).json() == []
        assert client.get("/api/documents/search", params={"query": 'quasar" OR *'}).status_code == 200
        assert client.get("/api/documents/search", params={"query": "quasar", "limit": 101}).status_code == 422
        assert client.get("/api/documents/search", params={"query": "quasar", "offset": -1}).status_code == 422
        services.retrieval.replace_document_chunks(content_paper.id, [{"text": "Revised stellar measurements."}])
        assert len(client.get("/api/documents/search", params={"query": "quasar"}).json()) == 1
        with services.documents.repository.session_factory() as session:
            session.get(Document, unrelated.id).title = "Quasar follow-up"
            session.commit()
        assert len(client.get("/api/documents/search", params={"query": "quasar"}).json()) == 2
        assert client.delete(f"/api/documents/{title_paper.id}").status_code == 204
        assert client.get("/api/documents/search", params={"query": "quasar"}).json()[0]["id"] == unrelated.id
        assert services.retrieval.keyword_search("follow-up") == []
    restarted = create_app(test_settings)
    with TestClient(restarted) as client:
        assert client.get("/api/documents/search", params={"query": "quasar"}).json()[0]["id"] == unrelated.id


def test_library_search_upgrades_existing_chunk_index_without_changing_documents(test_settings) -> None:
    from backend.documents.retrieval import RetrievalService

    services = create_app(test_settings).state.services
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="legacy.pdf", title="Legacy quasar paper",
    )
    services.retrieval.replace_document_chunks(document.id, [{"text": "Existing stellar content."}])
    with services.retrieval.session_factory.kw["bind"].begin() as connection:
        for prefix in ("document_chunks", "document_titles"):
            for operation in ("insert", "delete", "update"):
                connection.exec_driver_sql(f"DROP TRIGGER {prefix}_search_{operation}")
        connection.exec_driver_sql("DROP TABLE document_chunks_fts")
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE document_chunks_fts USING fts5(chunk_id UNINDEXED, document_id UNINDEXED, content)"
        )
        connection.exec_driver_sql("INSERT INTO document_chunks_fts SELECT id, document_id, text FROM document_chunks")
    retrieval = RetrievalService(services.retrieval.session_factory, test_settings)
    assert retrieval.keyword_search("stellar")[0]["document_id"] == document.id
    assert services.documents.search_documents("quasar")[0][0].id == document.id


def test_deleting_paper_removes_managed_files_and_workspace_notes(test_settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        document = app.state.services.documents.create_document_from_bytes(
            b"%PDF-1.4\n%%EOF",
            filename="delete-me.pdf",
            title="Delete Me",
        )
        paper = app.state.services.workspace.ensure_paper_folder(document.id, document.title)
        assert (test_settings.workspace_dir / paper["summary_path"]).exists()
        assert (test_settings.workspace_dir / paper["folder"] / "source.pdf").is_file()

        deleted = client.delete(f"/api/documents/{document.id}")

        assert deleted.status_code == 204, deleted.text
        assert client.get(f"/api/documents/{document.id}").status_code == 404
        assert not (test_settings.workspace_dir / paper["folder"]).exists()
        assert not (test_settings.documents_dir / document.id).exists()
        assert all(
            not entry.path.startswith(f"{paper['folder']}/")
            for entry in app.state.services.workspace.list_files()
        )


def test_extracted_markdown_contains_only_paper_text(test_settings) -> None:
    pages = [
        {
            "page": 1,
            "text": "The title printed in the PDF\n\nAbstract text",
            "ocr_used": False,
        },
        {
            "page": 2,
            "text": "Methods text",
            "ocr_used": False,
            "llm_enhancement_note": "Processing metadata must not enter the paper text.",
        },
    ]

    markdown = DocumentFormatter(test_settings).build_markdown(pages)

    assert markdown == "The title printed in the PDF\n\nAbstract text\n\nMethods text"


def test_artifact_records_are_idempotent_by_owned_path(test_settings) -> None:
    services = create_services(test_settings)
    path = "workspace/result.md"
    first_file = services.storage.write_text(test_settings.artifacts_dir, path, "first")
    first = services.documents.create_artifact_record(
        owner_type="workspace",
        kind="markdown",
        relative_path=path,
        media_type="text/markdown",
        stored=first_file,
    )

    updated_file = services.storage.write_text(test_settings.artifacts_dir, path, "updated")
    updated = services.documents.create_artifact_record(
        owner_type="workspace",
        kind="markdown",
        relative_path=path,
        media_type="text/markdown",
        stored=updated_file,
        metadata={"revision": 2},
    )

    assert updated.id == first.id
    assert updated.sha256 == updated_file.sha256
    assert updated.metadata_json == {"revision": 2, "storage_area": "artifacts"}
    with services.session_factory() as session:
        assert len(list(session.scalars(select(Artifact).where(Artifact.relative_path == path)))) == 1

    with pytest.raises(ValueError, match="already owned by another resource"):
        services.documents.create_artifact_record(
            owner_type="different-owner",
            kind="markdown",
            relative_path=path,
            media_type="text/markdown",
            stored=updated_file,
        )


def test_delete_run_artifacts_removes_records_and_files(test_settings) -> None:
    services = create_services(test_settings)
    run_id = "run-to-delete"
    path = f"runs/{run_id}/result.md"
    stored = services.storage.write_text(
        test_settings.artifacts_dir,
        path,
        "generated result",
    )
    artifact = services.documents.create_artifact_record(
        owner_type="agent_run",
        kind="generated",
        relative_path=path,
        media_type="text/markdown",
        stored=stored,
        metadata={"agent_run_id": run_id},
    )

    services.documents.delete_run_artifacts(run_id)

    assert not stored.absolute_path.exists()
    assert services.documents.get_artifact(artifact.id) is None


def test_document_list_returns_summaries_without_loading_details(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(test_settings)
    with app.state.services.session_factory() as session:
        session.add(
            Document(
                id="summary-only",
                title="Lightweight list item",
                source_filename="paper.pdf",
                content_type="application/pdf",
                status="ready",
                page_count=12,
                metadata_json={},
            )
        )
        session.commit()

    def fail_if_details_are_loaded(_document_id: str):
        raise AssertionError("Document details should not be loaded for the list.")

    monkeypatch.setattr(
        app.state.services.documents,
        "get_document_details",
        fail_if_details_are_loaded,
    )

    response = TestClient(app).get("/api/documents")

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Lightweight list item"
    assert "artifacts" not in response.json()[0]
    assert "chunks" not in response.json()[0]


def test_document_abstracts_reuse_metadata_and_bounded_indexed_sections(test_settings):
    app = create_app(test_settings)
    with TestClient(app) as client:
        with app.state.services.session_factory() as session:
            for identifier, metadata, status in [
                ("metadata", {"abstract": "Publisher abstract."}, "ready"),
                ("indexed", {}, "ready"),
                ("missing", {}, "ready"),
                ("processing", {}, "processing"),
            ]:
                session.add(Document(
                    id=identifier, title=f"AbstractTest {identifier}", source_filename="paper.pdf",
                    status=status, metadata_json=metadata,
                ))
            session.flush()
            for identifier in ("metadata", "indexed", "processing"):
                for index, (section, content) in enumerate([
                    ("Abstract", "We compare calibration methods."),
                    ("Abstract", "The best method reduces error."),
                    ("1 Introduction", "This must not appear in the abstract."),
                ]):
                    session.add(DocumentChunk(
                        document_id=identifier, chunk_index=index, section_title=section,
                        page_start=1, page_end=1, citation="p.1", text=content,
                    ))
            session.add(DocumentChunk(
                document_id="missing", chunk_index=0, section_title="Overview",
                page_start=1, page_end=1, citation="p.1", text="Opening text is not an abstract.",
            ))
            session.commit()
        expected = {
            "metadata": "Publisher abstract.",
            "indexed": "We compare calibration methods. The best method reduces error.",
            "missing": None, "processing": None,
        }
        for path in ("/api/documents", "/api/documents/search?query=AbstractTest"):
            response = client.get(path)
            assert response.status_code == 200
            assert {row["id"]: row["abstract"] for row in response.json()} == expected
        detail = client.get("/api/documents/indexed")
        assert detail.status_code == 200
        assert detail.json()["abstract"] == expected["indexed"]


@pytest.mark.parametrize(("text", "expected"), [
    ("## Abstract\n\nFirst paragraph.\n\nSecond paragraph.\n\n## Motivation\nBody.",
     "First paragraph. Second paragraph."),
    ("**Abstract**\nWe compare methods.\n\nRelated Work\nBody.",
     "We compare methods."),
    ("Authors and affiliations. Abstract: We compare methods. 1 Introduction Body.",
     "We compare methods."),
    ("## ABSTRACT\nABSTRACT\nWe compare methods.\nKeywords: calibration", "We compare methods."),
    ("Jane Doe\nUniversity of Science\n\n" + "We study calibration under distribution shift. " * 8
     + "\n\nLater paragraph.", ("We study calibration under distribution shift. " * 8).strip()),
    ("Jane Doe\nUniversity of Science\njane@example.org", None),
    ("Abstract\n", None),
    ("Abstract\n" + "Evidence " * 700, ("Evidence " * 444).strip() + "..."),
], ids=[
    "markdown", "bold", "inline", "duplicate-label", "opening-prose", "front-matter-only", "empty", "bounded",
])
def test_document_abstract_heuristics_from_existing_text(text, expected):
    assert _opening_abstract(text) == expected


def test_text_layer_inspection_recommends_an_ingestion_mode(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)

    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        pages = [
            FakePage("Embedded text " * 10),
            FakePage("Embedded text " * 10),
            FakePage(""),
        ]

    monkeypatch.setattr("backend.documents.ocr.PdfReader", lambda _: FakeReader())

    summary = services.document_ocr.inspect_text_layer(test_settings.data_dir / "paper.pdf")

    assert summary == {
        "total_pages": 3,
        "embedded_text_pages": 2,
        "embedded_text_ratio": pytest.approx(2 / 3),
        "recommended_mode": "ocr",
    }


@pytest.mark.anyio
async def test_ingestion_persists_progress_and_completion(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "progress-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Progress",
                source_filename="progress.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    async def complete_ingestion(_document_id: str, **kwargs: object) -> dict[str, object]:
        assert services.documents.get_document(document_id).status == "processing"
        progress = kwargs["progress"]
        assert callable(progress)
        await progress(
            {
                "phase": "ocr",
                "phase_label": "Rendering and OCR",
                "completed_pages": 1,
                "total_pages": 2,
            }
        )
        processing = services.documents.get_document(document_id)
        assert processing is not None
        assert processing.metadata_json["ingestion"]["completed_pages"] == 1
        services.document_repository.mark_ready(
            document_id,
            page_count=2,
            metadata={"figure_count": 0},
        )
        return {"document_id": document_id}

    monkeypatch.setattr(services.document_ingestion, "ingest", complete_ingestion)

    await services.documents.ingest_document(document_id)

    completed = services.documents.get_document(document_id)
    assert completed is not None
    assert completed.status == "ready"
    assert completed.metadata_json["ingestion"] == {
        "phase": "complete",
        "phase_label": "Ingestion complete",
        "completed_pages": 2,
        "total_pages": 2,
    }


@pytest.mark.anyio
async def test_forced_ocr_is_forwarded_to_ingestion(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "forced-ocr-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Forced OCR",
                source_filename="forced.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    async def complete_ingestion(_document_id: str, **kwargs: object) -> dict[str, object]:
        assert kwargs["force_ocr"] is True
        services.document_repository.mark_ready(
            document_id,
            page_count=1,
            metadata={"extraction_mode": "ocr"},
        )
        return {"document_id": document_id}

    monkeypatch.setattr(services.document_ocr, "available", lambda: True)
    monkeypatch.setattr(services.document_ingestion, "ingest", complete_ingestion)

    await services.documents.ingest_document(document_id, force_ocr=True)

    completed = services.documents.get_document(document_id)
    assert completed is not None
    assert completed.metadata_json["extraction_mode"] == "ocr"


@pytest.mark.anyio
async def test_forced_ocr_clears_stale_generated_outputs(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "ocr-cleanup-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="OCR Cleanup",
                source_filename="cleanup.pdf",
                content_type="application/pdf",
                status="ready",
                page_count=1,
                metadata_json={},
            )
        )
        session.add(
            DocumentChunk(
                document_id=document_id,
                chunk_index=0,
                section_title="Old",
                page_start=1,
                page_end=1,
                citation="p.1",
                text="stale text",
                metadata_json={},
            )
        )
        session.commit()
    source_file = services.storage.write_text(
        test_settings.documents_dir,
        f"{document_id}/source.pdf",
        "source",
    )
    source_artifact = services.documents.create_artifact_record(
        owner_type="document",
        kind="source_pdf",
        document_id=document_id,
        relative_path=source_file.relative_path,
        media_type="application/pdf",
        stored=source_file,
        storage_area="documents",
    )
    stale_file = services.storage.write_text(
        test_settings.artifacts_dir,
        f"documents/{document_id}/extracted.md",
        "old markdown",
    )
    stale_artifact = services.documents.create_artifact_record(
        owner_type="document",
        kind="extracted_markdown",
        document_id=document_id,
        relative_path=stale_file.relative_path,
        media_type="text/markdown",
        stored=stale_file,
    )
    orphan_file = services.storage.write_text(
        test_settings.artifacts_dir,
        f"documents/{document_id}/orphan-note.md",
        "orphan",
    )

    async def complete_ingestion(_document_id: str, **kwargs: object) -> dict[str, object]:
        assert kwargs["force_ocr"] is True
        details = services.documents.get_document_details(document_id)
        assert details is not None
        _, artifacts, chunks = details
        assert [artifact.kind for artifact in artifacts] == ["source_pdf"]
        assert chunks == []
        assert source_file.absolute_path.exists()
        assert not stale_file.absolute_path.exists()
        assert not orphan_file.absolute_path.exists()
        services.document_repository.mark_ready(
            document_id,
            page_count=1,
            metadata={"extraction_mode": "ocr"},
        )
        return {"document_id": document_id}

    monkeypatch.setattr(services.document_ocr, "available", lambda: True)
    monkeypatch.setattr(services.document_ingestion, "ingest", complete_ingestion)

    await services.documents.ingest_document(document_id, force_ocr=True)

    assert services.documents.get_artifact(source_artifact.id) is not None
    assert services.documents.get_artifact(stale_artifact.id) is None


@pytest.mark.anyio
async def test_failed_ingestion_does_not_leave_document_processing(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "failed-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Failure",
                source_filename="failure.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    async def fail_ingestion(_document_id: str, **_kwargs: object) -> dict[str, object]:
        raise DocumentProcessingError("PDF extraction failed")

    monkeypatch.setattr(services.document_ingestion, "ingest", fail_ingestion)

    with pytest.raises(DocumentProcessingError, match="PDF extraction failed"):
        await services.documents.ingest_document(document_id)

    failed = services.documents.get_document(document_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.metadata_json["ingestion"]["phase"] == "failed"


@pytest.mark.anyio
async def test_running_ingestion_can_be_stopped(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "stoppable-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Stoppable",
                source_filename="stoppable.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    started = anyio.Event()

    async def blocked_ingestion(
        _document_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError("cancelled ingestion resumed unexpectedly")

    monkeypatch.setattr(services.document_ingestion, "ingest", blocked_ingestion)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(services.documents.ingest_document, document_id)
        await started.wait()
        stopped = services.documents.stop_ingestion(document_id)
        assert stopped.status == "uploaded"

    document = services.documents.get_document(document_id)
    assert document is not None
    assert document.status == "uploaded"
    assert document.metadata_json["ingestion"]["phase"] == "stopped"
    assert document.metadata_json["ingestion"]["phase_label"] == "Ingestion stopped"


@pytest.mark.anyio
async def test_server_shutdown_stops_running_ingestions(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)
    document_id = "shutdown-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Shutdown",
                source_filename="shutdown.pdf",
                content_type="application/pdf",
                status="uploaded",
                metadata_json={},
            )
        )
        session.commit()

    started = anyio.Event()

    async def blocked_ingestion(
        _document_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError("cancelled ingestion resumed unexpectedly")

    monkeypatch.setattr(services.document_ingestion, "ingest", blocked_ingestion)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(services.documents.ingest_document, document_id)
        await started.wait()
        await services.close()

    document = services.documents.get_document(document_id)
    assert document is not None
    assert document.status == "uploaded"
    assert document.metadata_json["ingestion"]["phase"] == "stopped"
    assert "server shutdown" in document.metadata_json["ingestion"]["phase_label"]


def test_stale_ingestion_is_recovered_for_retry(test_settings) -> None:
    services = create_services(test_settings)
    document_id = "stale-document"
    with services.session_factory() as session:
        session.add(
            Document(
                id=document_id,
                title="Stale",
                source_filename="stale.pdf",
                content_type="application/pdf",
                status="processing",
                page_count=4,
                metadata_json={
                    "ingestion": {
                        "phase": "ocr",
                        "previous_status": "ready",
                    }
                },
            )
        )
        session.commit()

    assert services.document_repository.recover_stale_ingestions() == [document_id]

    document = services.documents.get_document(document_id)
    assert document is not None
    assert document.status == "ready"
    assert document.metadata_json["ingestion"]["phase"] == "stopped"
    assert "restart" in document.metadata_json["ingestion"]["phase_label"]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_image_only_pdf_uses_ocr(test_settings, tmp_path, monkeypatch) -> None:
    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    pytest.importorskip("pypdfium2")
    pytest.importorskip("pytesseract")

    image = image_module.new("RGB", (1400, 500), "white")
    draw = draw_module.Draw(image)
    draw.text((80, 180), "Local OCR research paper verification", fill="black", font_size=48)
    pdf_path = tmp_path / "scanned.pdf"
    image.save(pdf_path, "PDF", resolution=150)

    app = create_app(test_settings)
    client = TestClient(app)
    with pdf_path.open("rb") as pdf:
        upload = client.post(
            "/api/documents",
            files={"file": ("scanned.pdf", pdf, "application/pdf")},
        )
    assert upload.status_code == 201

    document_id = upload.json()["id"]
    ingested = client.post(f"/api/documents/{document_id}/ingest")

    assert ingested.status_code == 200, ingested.text
    payload = ingested.json()
    assert payload["status"] == "ready"
    assert payload["metadata"]["ocr_pages"] == [1]
    extracted = "\n".join(chunk["text"] for chunk in payload["chunks"]).lower()
    assert "local ocr research paper" in extracted

    async def unexpected_ocr(*_args, **_kwargs):
        pytest.fail("Automatic retry must reuse already OCRed pages")

    monkeypatch.setattr(app.state.services.document_ocr, "ocr_image", unexpected_ocr)
    reingested = client.post(f"/api/documents/{document_id}/ingest")
    assert reingested.status_code == 200
    assert [(chunk["text"], chunk["citation"]) for chunk in reingested.json()["chunks"]] == [
        (chunk["text"], chunk["citation"]) for chunk in payload["chunks"]
    ]
    assert {artifact["id"] for artifact in reingested.json()["artifacts"]} == {
        artifact["id"] for artifact in payload["artifacts"]
    }


@pytest.mark.anyio
async def test_good_ocr_skips_the_rewrite_model(test_settings) -> None:
    services = create_services(test_settings)

    class FakeOllama:
        profile_id = services.document_vision.ollama.profile_id

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append({"model": model, "prompt": prompt, **kwargs})
            return {"response": '{"quality":"good","issues":[]}'}

    fake_ollama = FakeOllama()
    services.document_vision.ollama = fake_ollama  # type: ignore[assignment]
    image = b"rendered-page"
    pages = [
        {
            "page": 1,
            "raw_text": "A B\n1 2",
            "text": "A B\n1 2",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": image,
        }
    ]

    progress_events: list[dict[str, object]] = []

    async def collect_progress(payload: dict[str, object]) -> None:
        progress_events.append(payload)

    await services.document_vision.enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
        progress=collect_progress,
    )

    assert len(fake_ollama.calls) == 1
    assert fake_ollama.calls[0]["model"] == "small-vision-model"
    assert fake_ollama.calls[0]["format_"]["properties"]["quality"]["enum"] == [
        "good",
        "average",
        "poor",
    ]
    assert fake_ollama.calls[0]["images"] == [base64.b64encode(image).decode("ascii")]
    assert "A B\n1 2" in str(fake_ollama.calls[0]["prompt"])
    assert pages[0]["ocr_quality"] == "good"
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["text"].startswith("A B\n1 2")
    assert "llm_validation_status" not in pages[0]
    assert progress_events[0] == {
        "phase": "ocr_triage",
        "phase_label": "Checking OCR quality",
        "completed_pages": 0,
        "total_pages": 1,
    }
    assert progress_events[-1]["phase"] == "ocr_triage"
    assert progress_events[-1]["page_quality"] == "good"
    assert progress_events[-1]["pages_needing_repair"] == 0


@pytest.mark.anyio
async def test_poor_ocr_is_rewritten_and_revalidated(test_settings) -> None:
    services = create_services(test_settings)

    class FakeOllama:
        profile_id = services.document_vision.ollama.profile_id

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            self.calls.append({"model": model, "prompt": prompt, **kwargs})
            if model == "small-vision-model":
                if "CANDIDATE MARKDOWN" in prompt:
                    return {"response": '{"quality":"good","issues":[]}'}
                return {"response": '{"quality":"poor","issues":["Columns are interleaved."]}'}
            return {"response": "## Results\n\n| A | B |\n|---|---|\n| 1 | 2 |"}

    fake_ollama = FakeOllama()
    services.document_vision.ollama = fake_ollama  # type: ignore[assignment]
    image = b"rendered-page"
    pages = [
        {
            "page": 1,
            "raw_text": "A B\n1 2",
            "text": "A B\n1 2",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": image,
        }
    ]

    progress_events: list[dict[str, object]] = []

    async def collect_progress(payload: dict[str, object]) -> None:
        progress_events.append(payload)

    await services.document_vision.enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
        progress=collect_progress,
    )

    assert [call["model"] for call in fake_ollama.calls] == [
        "small-vision-model",
        "big-vision-model",
        "small-vision-model",
    ]
    assert "Columns are interleaved." in str(fake_ollama.calls[1]["prompt"])
    assert "format_" not in fake_ollama.calls[1]
    assert fake_ollama.calls[1]["images"] == [base64.b64encode(image).decode("ascii")]
    assert pages[0]["ocr_quality"] == "poor"
    assert pages[0]["llm_enhanced"] is True
    assert pages[0]["llm_validation_status"] == "passed"
    assert "| A | B |" in pages[0]["text"]
    chunks = services.document_formatter.chunk_pages(pages, title="Test")
    assert any("| A | B |\n|---|---|\n| 1 | 2 |" in chunk["text"] for chunk in chunks)
    phases = [event["phase"] for event in progress_events]
    assert phases[0] == "ocr_triage"
    assert "llm_enhancement" in phases
    assert progress_events[-1]["phase"] == "llm_validation"
    assert progress_events[-1]["eta_seconds"] == 0.0


@pytest.mark.anyio
async def test_llm_rewrite_falls_back_to_ocr_when_response_is_empty(test_settings) -> None:
    services = create_services(test_settings)

    class EmptyOllama:
        profile_id = services.document_vision.ollama.profile_id

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            if model == "small-vision-model":
                return {"response": '{"quality":"poor","issues":["Text is scrambled."]}'}
            return {"response": "", "thinking": "internal reasoning only"}

    services.document_vision.ollama = EmptyOllama()  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "Original OCR content",
            "text": "Original OCR content",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.document_vision.enhance_pages(
        pages,
        "thinking-vision-model",
        triage_model="small-vision-model",
    )

    assert pages[0]["text"] == "Original OCR content"
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["llm_enhancement_note"] == (
        "No LLM enhancement was applied because the model returned only an internal "
        "thinking trace. The original OCR output is used directly."
    )
    markdown = services.document_formatter.build_markdown(pages)
    assert markdown == "Original OCR content"


@pytest.mark.anyio
async def test_validation_rejects_unfaithful_rewrite(test_settings) -> None:
    services = create_services(test_settings)

    class RejectingOllama:
        profile_id = services.document_vision.ollama.profile_id

        async def generate(self, model: str, prompt: str, **kwargs: object) -> dict[str, str]:
            if model == "small-vision-model":
                if "CANDIDATE MARKDOWN" in prompt:
                    return {
                        "response": (
                            '{"quality":"poor","issues":'
                            '["Invented accuracy claim not present on the page."]}'
                        )
                    }
                return {"response": '{"quality":"poor","issues":["Text is scrambled."]}'}
            return {"response": "# Invented Results\n\nAccuracy was 100%."}

    services.document_vision.ollama = RejectingOllama()  # type: ignore[assignment]
    pages = [
        {
            "page": 1,
            "raw_text": "Original OCR content",
            "text": "Original OCR content",
            "ocr_used": True,
            "llm_enhanced": False,
            "_image_png": b"rendered-page",
            "figures": [],
        }
    ]

    await services.document_vision.enhance_pages(
        pages,
        "big-vision-model",
        triage_model="small-vision-model",
    )

    assert pages[0]["text"] == "Original OCR content"
    assert pages[0]["llm_enhanced"] is False
    assert pages[0]["llm_validation_status"] == "failed"
    assert pages[0]["llm_validation_issues"] == [
        "Invented accuracy claim not present on the page."
    ]
    assert "validation check rejected it" in pages[0]["llm_enhancement_note"]


@pytest.mark.anyio
async def test_forced_ocr_uses_embedded_text_when_tesseract_fails(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = create_services(test_settings)

    class FakePage:
        def extract_text(self) -> str:
            return "Reliable embedded page text"

    class FakeReader:
        pages = [FakePage()]

    async def raise_processing_error(image_png: bytes, page_number: int) -> str:
        raise DocumentProcessingError("Tesseract exited abnormally")

    monkeypatch.setattr("backend.documents.ocr.PdfReader", lambda _: FakeReader())
    monkeypatch.setattr(services.document_ocr, "available", lambda: True)
    monkeypatch.setattr(services.document_ocr, "render_page_png", lambda *_: b"page")
    monkeypatch.setattr(services.document_ocr, "ocr_image", raise_processing_error)

    pages = await services.document_ocr.extract_pages(
        test_settings.data_dir / "unused.pdf",
        force_ocr=True,
        retain_page_images=True,
    )

    assert pages[0]["raw_text"] == "Reliable embedded page text"
    assert pages[0]["ocr_used"] is False
    assert "embedded text was used instead" in pages[0]["ocr_fallback_note"]


@pytest.mark.anyio
async def test_ingestion_persists_skipped_pages_without_losing_readable_text(
    test_settings, monkeypatch,
) -> None:
    services = create_services(test_settings)
    document = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="partial.pdf", title="Partly readable paper",
    )
    readable = "This later page contains recoverable scientific evidence. " * 4

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Reader:
        pages = [Page(""), Page(readable)]

    monkeypatch.setattr("backend.documents.ocr.PdfReader", lambda _: Reader())
    monkeypatch.setattr(services.document_ocr, "available", lambda: False)

    await services.documents.ingest_document(document.id, enhance_with_llm=False)

    details = services.documents.get_document_details(document.id)
    saved, artifacts, chunks = details
    assert saved.status == "ready"
    assert saved.page_count == 2
    assert saved.metadata_json["ocr_skipped_pages"] == [1]
    assert "requires OCR" in saved.metadata_json["ocr_fallback_notes"]["1"]
    manifest = services.documents.artifact_content(
        next(artifact for artifact in artifacts if artifact.kind == "extracted_manifest")
    )
    assert manifest["pages"][0]["ocr_skipped"] is True
    assert manifest["pages"][0]["text"] == ""
    assert manifest["pages"][1]["citation"] == "p.2"
    assert manifest["content"]["nonempty_page_count"] == 1
    assert chunks and all(chunk.page_start == 2 for chunk in chunks)
