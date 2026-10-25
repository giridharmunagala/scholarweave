from __future__ import annotations

import hashlib
import io
import zipfile

from fastapi.testclient import TestClient

from backend.app import create_app


def test_connected_repository_reference_claim_manuscript_flow(test_settings, tmp_path):
    root = tmp_path / "research-code"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("a" * 40)
    (root / "report.md").write_text("# Run 17\n\nObserved score: 0.85.\n")
    app = create_app(test_settings)
    with TestClient(app) as client:
        track_response = client.post("/api/tracks", json={"title": "Connected research"})
        assert track_response.status_code == 201
        track = track_response.json()
        track_path = f"/api/tracks/{track['id']}"
        experiments = client.get(f"{track_path}/sections/experiments").json()
        assert client.put(f"{track_path}/sections/experiments", json={
            "content": "| ID | Experiment | Status | Success criterion | Evidence |\n"
                       "| --- | --- | --- | --- | --- |\n"
                       "| EXP-001 | Compare retrieval | Completed | Report score | External run report |\n",
            "expected_sha256": experiments["sha256"],
        }).status_code == 200
        link = client.post(f"{track_path}/repositories", json={"root": str(root), "authorize_root": True})
        assert link.status_code == 201, link.text
        repo_path = f"{track_path}/repositories/{link.json()['id']}"
        preview = client.get(f"{repo_path}/preview", params={"path": "report.md"}).json()
        receipt = client.post(f"{repo_path}/artifacts", json={
            "path": "report.md", "experiment_id": "EXP-001", "expected_sha256": preview["sha256"],
            "producing_revision": None, "note": "Researcher-supplied observation; not independently verified.",
        })
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["artifacts"][0]["observed_revision"] == "a" * 40
        assert receipt.json()["artifacts"][0]["producing_revision"] is None
        assert client.get(f"{repo_path}/preview", params={"path": "../outside.txt"}).status_code == 400
        paper = app.state.services.documents.create_document_from_bytes(b"%PDF-1.4\n%%EOF", filename="comparison.pdf", title="Comparison study")
        stored = app.state.services.storage.write_json(test_settings.artifacts_dir, f"{paper.id}/manifest.json", {
            "pages": [{"page": 1, "text": "The reported comparison achieved a score of 0.85."}],
        })
        app.state.services.documents.create_artifact_record(
            owner_type="document", kind="extracted_manifest", relative_path=stored.relative_path,
            media_type="application/json", stored=stored, document_id=paper.id,
        )
        references = client.post("/api/references", json={"expected_revision": 0, "reference": {
            "citation_key": "comparison2026", "title": paper.title, "authors": ["Researcher"],
            "year": "2026", "doi": "10.1234/comparison", "document_id": paper.id,
        }})
        assert references.status_code == 201, references.text
        claim = client.post(f"{track_path}/writing/claims", json={
            "statement": "The paper reports a score of 0.85.", "interpretation": "Only the reported setup is established.",
            "evidence": [{"document_id": paper.id, "page": 1, "quote": "achieved a score of 0.85.", "relation": "supports"}],
            "expected_sha256": None,
        })
        assert claim.status_code == 201, claim.text
        claim_id = claim.json()["claims"][0]["id"]
        manuscript = f"# Related work\n\nThe paper reports 0.85 [@comparison2026] [claim:{claim_id}]."
        assert client.put(f"{track_path}/writing/manuscript", json={
            "content": manuscript, "expected_sha256": None,
        }).status_code == 200
        audit = client.post(f"{track_path}/writing/audit", json={"content": manuscript, "expected_sha256": None})
        assert audit.status_code == 200, audit.text
        assert audit.json()["citation_keys"] == ["comparison2026"]
        markdown = client.get(f"{track_path}/writing/export", params={"format": "markdown"})
        assert markdown.status_code == 200, markdown.text
        assert "https://doi.org/10.1234/comparison" in markdown.text
        assert "achieved a score of 0.85." in markdown.text
        assert "page SHA-256:" in markdown.text
        docx = client.get(f"{track_path}/writing/export", params={"format": "docx"})
        with zipfile.ZipFile(io.BytesIO(docx.content)) as archive:
            assert b"comparison2026" in archive.read("word/document.xml")
        bibtex = client.post("/api/references/export", json={"format": "bibtex", "reference_ids": None})
        assert "@article{comparison2026," in bibtex.json()["content"]
        original_hash = preview["sha256"]
        (root / "report.md").write_text("An externally updated result.")
        refreshed = client.post(f"{repo_path}/refresh").json()
        assert refreshed["artifacts"][0]["status"] == "changed"
        assert refreshed["artifacts"][0]["sha256"] == original_hash
        assert client.put(f"{repo_path}/file", json={
            "path": "report.md", "content": "Overwrite", "expected_sha256": original_hash,
        }).status_code == 409
        assert hashlib.sha256((root / "report.md").read_bytes()).hexdigest() != original_hash
        assert client.delete(repo_path).status_code == 204
        assert (root / "report.md").exists()
