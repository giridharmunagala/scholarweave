from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from xml.sax.saxutils import escape

from pydantic import ValidationError as SchemaError

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.documents import DocumentService
from backend.persistence.files import SafeStorage
from backend.tracks.service import TrackService
from backend.tracks.writing_schemas import (
    ClaimCollection,
    ClaimEvidence,
    ClaimInput,
    EvidenceInput,
    ManuscriptAudit,
    ManuscriptResponse,
    ResearchClaim,
)
from backend.utils import utcnow
from backend.workspace.service import WorkspaceService

_CITATION = re.compile(r"\[@([A-Za-z0-9_:.+-]+)\]")
_CLAIM = re.compile(r"\[claim:([a-zA-Z0-9-]+)\]")
_MANUSCRIPT = "exploration/manuscript"


class WritingService:
    def __init__(
        self, tracks: TrackService, documents: DocumentService,
        workspace: WorkspaceService, storage: SafeStorage,
    ) -> None:
        self.tracks = tracks
        self.documents = documents
        self.workspace = workspace
        self.storage = storage

    def _path(self, track_id: str) -> str:
        track = self.tracks.get(track_id)
        return f"track_claims/{track.id}.json"

    def _load(self, track_id: str) -> ClaimCollection:
        path = self._path(track_id)
        try:
            raw = self.storage.read_text(self.storage.settings.data_dir, path)
        except FileNotFoundError:
            return ClaimCollection(sha256=None, claims=[])
        try:
            claims = [ResearchClaim.model_validate(item) for item in json.loads(raw)]
        except (ValueError, TypeError, SchemaError) as error:
            raise ValidationError("Saved claims are invalid; restore the claims file before editing.") from error
        return ClaimCollection(sha256=hashlib.sha256(raw.encode()).hexdigest(), claims=claims)

    def _page(self, document_id: str, page: int) -> tuple[str, str, str]:
        paper = self.documents.get_document(document_id)
        if paper is None:
            raise NotFoundError("The evidence paper is no longer in the library.")
        artifacts = self.documents.get_document_artifacts(document_id)
        manifests = [item for item in artifacts if item.kind == "extracted_manifest"]
        if not manifests:
            raise ValidationError("Prepare the paper before attaching page evidence.")
        try:
            manifest = self.documents.artifact_content(manifests[-1])
        except (ValueError, UnicodeError) as error:
            raise ValidationError("The extracted paper manifest is unreadable; prepare the paper again.") from error
        if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
            raise ValidationError("The extracted paper manifest has no readable page list.")
        for item in manifest.get("pages", []):
            if not isinstance(item, dict):
                raise ValidationError("The extracted paper manifest contains an invalid page.")
            if item.get("page") == page:
                text = item.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    raise ValidationError("This page has no extracted text; choose a readable page.")
                return paper.title, text, self.documents.source_revision(document_id)["source_version"]
        raise ValidationError("The selected page is not present in the extracted paper.")

    def page(self, document_id: str, page: int) -> dict[str, str | int]:
        title, text, version = self._page(document_id, page)
        return {"document_id": document_id, "title": title, "page": page, "text": text, "source_version": version}

    def _evidence(self, item: EvidenceInput) -> ClaimEvidence:
        title, text, version = self._page(item.document_id, item.page)
        quote = item.quote.strip()
        if not quote or " ".join(quote.split()) not in " ".join(text.split()):
            raise ValidationError("The quotation must match the selected source page (ignoring whitespace).")
        return ClaimEvidence(
            **item.model_dump(exclude={"quote"}), quote=quote, paper_title=title,
            source_version=version, page_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )

    def claims(self, track_id: str) -> ClaimCollection:
        with self.workspace.mutation_lock:
            collection = self._load(track_id)
            for claim in collection.claims:
                for evidence in claim.evidence:
                    try:
                        _, text, version = self._page(evidence.document_id, evidence.page)
                    except (NotFoundError, FileNotFoundError, ValidationError):
                        evidence.source_status = "missing"
                    else:
                        evidence.source_status = (
                            "current" if version == evidence.source_version
                            and hashlib.sha256(text.encode()).hexdigest() == evidence.page_sha256
                            else "changed"
                        )
            return collection

    def _save(self, track_id: str, collection: ClaimCollection, expected: str | None) -> ClaimCollection:
        if collection.sha256 != expected:
            raise ConflictError("Claims changed; reload and compare before saving.")
        raw = json.dumps([item.model_dump(mode="json") for item in collection.claims], ensure_ascii=False, indent=2)
        self.storage.write_bytes_atomic(self.storage.settings.data_dir, self._path(track_id), raw.encode())
        return self.claims(track_id)

    def save_claim(self, track_id: str, payload: ClaimInput, claim_id: str | None = None) -> ClaimCollection:
        with self.workspace.mutation_lock:
            collection = self._load(track_id)
            if collection.sha256 != payload.expected_sha256:
                raise ConflictError("Claims changed; reload and compare before saving.")
            statement = payload.statement.strip()
            if not statement:
                raise ValidationError("A claim needs a non-empty statement.")
            old = next((item for item in collection.claims if item.id == claim_id), None)
            if claim_id is not None and old is None:
                raise NotFoundError("Claim was not found.")
            now = utcnow()
            claim = ResearchClaim(
                id=old.id if old else str(uuid.uuid4()), statement=statement,
                interpretation=payload.interpretation.strip(),
                evidence=[self._evidence(item) for item in payload.evidence],
                created_at=old.created_at if old else now, updated_at=now,
            )
            if old:
                collection.claims = [claim if item.id == old.id else item for item in collection.claims]
            else:
                collection.claims.append(claim)
            return self._save(track_id, collection, payload.expected_sha256)

    def delete_claim(self, track_id: str, claim_id: str, expected: str) -> ClaimCollection:
        with self.workspace.mutation_lock:
            collection = self._load(track_id)
            if not any(item.id == claim_id for item in collection.claims):
                raise NotFoundError("Claim was not found.")
            collection.claims = [item for item in collection.claims if item.id != claim_id]
            return self._save(track_id, collection, expected)

    def manuscript(self, track_id: str) -> ManuscriptResponse:
        track = self.tracks.get(track_id)
        try:
            section = self.tracks.read_section(track_id, _MANUSCRIPT)
        except NotFoundError:
            return ManuscriptResponse(content="", sha256=None, path=f"{track.folder_path}/{_MANUSCRIPT}.md")
        return ManuscriptResponse(content=section.content, sha256=section.sha256, path=section.path)

    def save_manuscript(self, track_id: str, content: str, expected: str | None) -> ManuscriptResponse:
        self.tracks.write_section(track_id, _MANUSCRIPT, content, expected)
        return self.manuscript(track_id)

    def audit(self, track_id: str, content: str, reference_keys: set[str]) -> ManuscriptAudit:
        keys = list(dict.fromkeys(_CITATION.findall(content)))
        claim_ids = list(dict.fromkeys(_CLAIM.findall(content)))
        claims = {item.id: item for item in self.claims(track_id).claims}
        warnings = [f"Unresolved reference: {key}." for key in keys if key not in reference_keys]
        for identifier in claim_ids:
            claim = claims.get(identifier)
            if claim is None:
                warnings.append(f"Unresolved claim: {identifier}.")
            elif not claim.evidence:
                warnings.append(f"Claim has no source evidence: {claim.statement}")
            else:
                if not any(item.relation == "supports" for item in claim.evidence):
                    warnings.append(f"Claim has no supporting passage: {claim.statement}")
                if any(item.relation == "contradicts" for item in claim.evidence):
                    warnings.append(f"Claim has contradictory evidence to discuss: {claim.statement}")
                if any(item.source_status != "current" for item in claim.evidence):
                    warnings.append(f"Claim has changed or missing sources: {claim.statement}")
        for number, paragraph in enumerate(re.split(r"\n\s*\n", content), 1):
            if (len(paragraph.strip()) >= 100 and not paragraph.lstrip().startswith(("#", "```", "|"))
                    and not _CITATION.search(paragraph) and not _CLAIM.search(paragraph)):
                warnings.append(f"Paragraph {number} has no citation or claim link; review whether evidence is needed.")
        warnings.append("These are structural checks, not a scientific assessment of claim strength or citation support.")
        return ManuscriptAudit(citation_keys=keys, claim_ids=claim_ids, warnings=warnings)

    def export_text(self, track_id: str, bibliography: dict[str, str]) -> str:
        content = self.manuscript(track_id).content
        audit = self.audit(track_id, content, set(bibliography))
        unresolved = [item for item in audit.warnings if item.startswith(("Unresolved reference:", "Unresolved claim:"))]
        if unresolved:
            raise ValidationError("Resolve citations before exporting: " + " ".join(unresolved))
        claims = {item.id: item for item in self.claims(track_id).claims}
        sections = [content]
        if audit.citation_keys:
            sections.append("## References\n\n" + "\n\n".join(
                f"[@{key}] {bibliography[key]}" for key in audit.citation_keys
            ))
        if audit.claim_ids:
            rows = ["## Claim evidence"]
            for identifier in audit.claim_ids:
                claim = claims[identifier]
                rows.append(f"### [claim:{identifier}]\n\n{claim.statement}")
                if claim.interpretation:
                    rows.append(f"Researcher interpretation: {claim.interpretation}")
                if not claim.evidence:
                    rows.append("No source evidence attached.")
                for evidence in claim.evidence:
                    rows.append(
                        f"{evidence.relation.capitalize()}: {evidence.paper_title}, p. {evidence.page}"
                        f" ({evidence.source_status}).\n\n> {evidence.quote.replace(chr(10), chr(10) + '> ')}\n\n"
                        f"Document: {evidence.document_id}; source version: {evidence.source_version}; "
                        f"page SHA-256: {evidence.page_sha256}."
                    )
            sections.append("\n\n".join(rows))
        return "\n\n".join(sections).strip() + "\n"


def export_manuscript(content: str, format: str) -> tuple[bytes, str, str]:
    if format == "markdown":
        return content.encode(), "text/markdown", "manuscript.md"
    if format == "latex":
        replacements = {
            "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
            "#": r"\#", "%": r"\%", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        }
        def tex(text: str) -> str:
            return "".join(replacements.get(char, char) for char in text)
        paragraphs = []
        for paragraph in content.split("\n\n"):
            heading = re.fullmatch(r"(#{1,3}) ([^\n]+)", paragraph)
            if heading:
                command = ["section", "subsection", "subsubsection"][len(heading[1]) - 1]
                paragraphs.append(f"\\{command}*{{{tex(heading[2])}}}")
            else:
                paragraphs.append(tex(paragraph))
        output = "\\documentclass{article}\n\\usepackage[utf8]{inputenc}\n\\begin{document}\n"
        output += "\n\n".join(paragraphs) + "\n\\end{document}\n"
        return output.encode(), "application/x-tex", "manuscript.tex"
    if format != "docx":
        raise ValidationError("Choose markdown, latex or docx.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", content):
        raise ValidationError("DOCX cannot contain invalid XML control characters; remove them before exporting.")
    output = io.BytesIO()
    paragraphs = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>'
        for line in content.splitlines()
    )
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>')
        archive.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
        archive.writestr("word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{paragraphs}<w:sectPr/></w:body></w:document>')
    return output.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "manuscript.docx"
