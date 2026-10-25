from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.bootstrap import create_services
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.tracks.writing import WritingService, export_manuscript
from backend.tracks.writing_schemas import ClaimInput, EvidenceInput


@pytest.fixture()
def setup(test_settings):
    services = create_services(test_settings)
    track = services.tracks.create("Retrieval comparison")
    paper = services.documents.create_document_from_bytes(
        b"%PDF-1.4\n%%EOF", filename="evidence.pdf", title="Evidence paper",
    )
    stored = services.storage.write_json(test_settings.artifacts_dir, f"{paper.id}/manifest.json", {
        "pages": [{"page": 1, "text": "The baseline scored 80 percent.\nThe new method scored 85 percent."}],
    })
    services.documents.create_artifact_record(
        owner_type="document", kind="extracted_manifest", relative_path=stored.relative_path,
        media_type="application/json", stored=stored, document_id=paper.id,
    )
    return services, track, paper


def payload(paper, expected=None, quote="The new method scored 85 percent."):
    return ClaimInput(
        statement="The new method improves the reported score.",
        interpretation="This does not establish generalization to other datasets.",
        evidence=[EvidenceInput(document_id=paper.id, page=1, quote=quote)],
        expected_sha256=expected,
    )


def test_claims_verify_page_quotes_persist_and_guard_edits(setup):
    services, track, paper = setup
    saved = services.writing.save_claim(track.id, payload(paper))
    assert saved.claims[0].evidence[0].source_status == "current"
    assert saved.claims[0].evidence[0].page_sha256
    restored = WritingService(services.tracks, services.documents, services.workspace, services.storage)
    assert restored.claims(track.id) == saved
    with pytest.raises(ConflictError):
        restored.save_claim(track.id, payload(paper), saved.claims[0].id)
    with pytest.raises(ValidationError, match="quotation"):
        restored.save_claim(track.id, payload(paper, saved.sha256, "Invented result"))
    with pytest.raises(ValidationError, match="page"):
        restored.page(paper.id, 99)
    revised = payload(paper, saved.sha256)
    revised.evidence.append(EvidenceInput(document_id=paper.id, page=1, quote="The baseline scored 80 percent.", relation="context"))
    result = restored.save_claim(track.id, revised, saved.claims[0].id)
    assert len(result.claims) == 1
    assert len(result.claims[0].evidence) == 2
    with pytest.raises(ConflictError):
        restored.delete_claim(track.id, result.claims[0].id, saved.sha256)
    assert restored.delete_claim(track.id, result.claims[0].id, result.sha256).claims == []
    with pytest.raises(NotFoundError):
        restored.claims("../missing")


def test_changed_and_missing_source_is_visible_without_rewriting_evidence(setup):
    services, track, paper = setup
    saved = services.writing.save_claim(track.id, payload(paper))
    manifest = next(item for item in services.documents.get_document_artifacts(paper.id) if item.kind == "extracted_manifest")
    services.storage.write_json(services.settings.artifacts_dir, manifest.relative_path, {
        "pages": [{"page": 1, "text": "The corrected score is 81 percent."}],
    })
    changed = services.writing.claims(track.id)
    assert changed.sha256 == saved.sha256
    assert changed.claims[0].evidence[0].source_status == "changed"
    assert changed.claims[0].evidence[0].quote == saved.claims[0].evidence[0].quote
    services.storage.delete_stored_file(services.settings.artifacts_dir, manifest.relative_path)
    assert services.writing.claims(track.id).claims[0].evidence[0].source_status == "missing"


def test_manuscript_uses_track_history_and_exports_with_evidence(setup):
    services, track, paper = setup
    saved = services.writing.save_claim(track.id, payload(paper))
    claim_id = saved.claims[0].id
    assert services.writing.manuscript(track.id).sha256 is None
    manuscript = services.writing.save_manuscript(track.id, f"# Related work\n\nA claim [claim:{claim_id}] [@paper2026].", None)
    discovered = next(item for item in services.tracks.get(track.id).exploration if item.section == "exploration/manuscript")
    assert discovered.sha256 == manuscript.sha256
    assert services.tracks.read_section(track.id, discovered.section).content == manuscript.content
    with pytest.raises(ConflictError):
        services.writing.save_manuscript(track.id, "Lost update", "0" * 64)
    second = services.writing.save_manuscript(track.id, manuscript.content + "\n\n## Limitations\n", manuscript.sha256)
    history = services.tracks.history(track.id, "exploration/manuscript")
    assert len(history) == 1
    assert services.tracks.read_history(track.id, "exploration/manuscript", history[0].revision).content == manuscript.content
    exported = services.writing.export_text(track.id, {"paper2026": "Researcher. Evidence paper (2026)."})
    assert "## References" in exported
    assert "The new method scored 85 percent." in exported
    assert saved.claims[0].evidence[0].source_version in exported
    assert services.writing.manuscript(track.id) == second
    with pytest.raises(ValidationError, match="Unresolved reference"):
        services.writing.export_text(track.id, {})
    services.writing.save_manuscript(track.id, "[claim:missing]", second.sha256)
    with pytest.raises(ValidationError, match="Unresolved claim"):
        services.writing.export_text(track.id, {})


def test_audit_flags_unsubstantiated_conflicting_and_uncited_text(setup):
    services, track, paper = setup
    draft = payload(paper)
    draft.evidence[0].relation = "contradicts"
    saved = services.writing.save_claim(track.id, draft)
    result = services.writing.audit(track.id, f"[claim:{saved.claims[0].id}] [@unknown]\n\n" + "An uncited assertion. " * 10, set())
    assert any("Unresolved reference" in item for item in result.warnings)
    assert any("no supporting passage" in item for item in result.warnings)
    assert any("contradictory evidence" in item for item in result.warnings)
    assert any("no citation or claim link" in item for item in result.warnings)
    assert "not a scientific assessment" in result.warnings[-1]


def test_export_formats_are_valid_and_escape_user_text():
    text = "# Results\n\n50% & <measurement> _ {safe} \\input{secret}\n"
    markdown, mime, name = export_manuscript(text, "markdown")
    assert markdown.decode() == text and name == "manuscript.md"
    assert mime == "text/markdown"
    latex, _, name = export_manuscript(text, "latex")
    assert name == "manuscript.tex"
    assert r"\section*{Results}" in latex.decode()
    assert r"\textbackslash{}input\{secret\}" in latex.decode()
    docx, _, name = export_manuscript(text, "docx")
    assert name == "manuscript.docx"
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        for path in archive.namelist():
            ElementTree.fromstring(archive.read(path))
        assert b"&lt;measurement&gt;" in archive.read("word/document.xml")
    with pytest.raises(ValidationError):
        export_manuscript(text, "html")
    with pytest.raises(ValidationError, match="control characters"):
        export_manuscript("Invalid \x01 character", "docx")


def test_writing_http_revision_validation(test_settings):
    app = create_app(test_settings)
    with TestClient(app) as client:
        track = client.post("/api/tracks", json={"title": "HTTP writing"}).json()
        path = f"/api/tracks/{track['id']}/writing"
        assert client.get(f"{path}/claims").json() == {"sha256": None, "claims": []}
        saved = client.put(f"{path}/manuscript", json={"content": "# Draft", "expected_sha256": None})
        assert saved.status_code == 200
        assert client.put(f"{path}/manuscript", json={"content": "Overwrite", "expected_sha256": "0" * 64}).status_code == 409
        assert client.post(f"{path}/claims", json={"statement": "Hypothesis", "evidence": [], "expected_sha256": None}).status_code == 201
        assert client.get(f"{path}/page?document_id=missing&page=0").status_code == 422
