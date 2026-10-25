from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pydantic import ValidationError as SchemaValidationError

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.core.http import services
from backend.documents.models import Document, PaperFolder
from backend.documents.repository import DocumentRepository
from backend.persistence.files import SafeStorage
from backend.references.formats import export_references, parse_references
from backend.references.router import router
from backend.references.schemas import ReferenceFields
from backend.references.service import ReferenceService


@pytest.fixture()
def references(test_settings):
    documents = SimpleNamespace(get_document=lambda key: object() if key == "paper-1" else None)
    return ReferenceService(SafeStorage(test_settings), documents)


def fields(**overrides):
    return ReferenceFields(citation_key="Lovelace1843", title="Notes on engines", **overrides)


@pytest.mark.parametrize("format", ["bibtex", "ris"])
def test_bibliography_round_trip(format):
    record = fields(authors=["Lovelace, Ada", "Smith, John"], year="1843", doi="https://doi.org/10.1234/ABC",
                    arxiv_id="2401.12345v2", journal="A & B", volume="2", number="3", pages="1--9",
                    publisher="Press", url="https://example.org/a_b?q=1&x=2", abstract=r"100% {literal} #_ $ ~ ^ \ é")
    assert parse_references(export_references([record], format), format) == [record]


def test_bibtex_nested_braces_and_numeric_year():
    parsed = parse_references('@article{Test, title={A {Nested} title}, year=2024}', "bibtex")
    assert parsed[0].title == "A {Nested} title"
    assert parsed[0].year == "2024"


def test_standard_ris_aliases_and_proceedings():
    content = "TY  - CONF\nT1  - A study\nA1  - Smith, Ada\nPY  - 2024\nT2  - Proceedings\nSP  - 10\nEP  - 19\nER  - "
    record = parse_references(content, "ris")[0]
    assert record.pages == "10--19"
    assert record.citation_key == "ref_A_study_2024"
    assert "booktitle = {Proceedings}" in export_references([record], "bibtex")
    for format in ("bibtex", "ris"):
        assert parse_references(export_references([record], format), format) == [record]


@pytest.mark.parametrize("format,content", [
    ("bibtex", "@article{bad, title={unfinished}"),
    ("bibtex", "@article{bad, title=macro}"),
    ("bibtex", "@article{bad, title={Hi}, unsupported={value}}"),
    ("bibtex", r"@article{bad, title={\command{value}}}"),
    ("bibtex", "@article{bad, title={Hi} title={Again}}"),
    ("ris", "TY  - JOUR\nTI  - Hi"),
    ("ris", "TY  - JOUR\nTI  - Hi\nXX  - unsupported\nER  - "),
])
def test_invalid_interchange_is_explicit(format, content):
    with pytest.raises(ValidationError):
        parse_references(content, format)


def test_identity_revision_and_persistence(references):
    library = references.create(fields(doi="10.1234/paper"), expected_revision=0)
    record = library.references[0]
    assert library.revision == 1
    with pytest.raises(ConflictError, match="changed"):
        references.create(fields(), expected_revision=0)
    duplicate = fields().model_copy(update={"citation_key": "other", "doi": "10.1234/paper"})
    with pytest.raises(ConflictError, match="No merge"):
        references.create(duplicate, expected_revision=1)
    update = fields(doi="10.1234/paper").model_copy(update={"title": "Corrected title"})
    library = references.update(record.id, update, expected_revision=1)
    assert library.references[0].id == record.id
    assert library.references[0].citation_key == "Lovelace1843"
    assert ReferenceService(references.storage, references.documents).get(record.id).title == "Corrected title"
    assert "Corrected title" in references.bibliography()["Lovelace1843"]
    with pytest.raises(ValidationError, match="immutable"):
        references.update(record.id, update.model_copy(update={"citation_key": "new"}), expected_revision=2)
    with pytest.raises(NotFoundError):
        references.export("bibtex", ["missing"])


def test_atomic_duplicate_batch_and_case_insensitive_key(references):
    content = "@article{First, title={One}}\n@article{first, title={Two}}"
    with pytest.raises(ConflictError, match="Citation key"):
        references.import_references(content, "bibtex", expected_revision=0)
    assert references.list().revision == 0
    assert references.list().references == []


def test_link_validation_and_arxiv_versions(references):
    with pytest.raises(NotFoundError):
        references.create(fields(document_id="missing"), expected_revision=0)
    first = references.create(fields(arxiv_id="2401.12345v1", document_id="paper-1"), expected_revision=0).references[0]
    with pytest.raises(ConflictError):
        references.create(fields().model_copy(update={"citation_key": "Other", "arxiv_id": "2401.12345v2"}), expected_revision=1)
    second = fields(doi="10.1234/published", related_reference_ids=[first.id]).model_copy(update={"citation_key": "Published"})
    assert references.create(second, expected_revision=1).references[1].related_reference_ids == [first.id]


def test_concurrent_revision_guard(references):
    def create(index):
        try:
            references.create(fields().model_copy(update={"citation_key": f"Key{index}"}), expected_revision=0)
            return True
        except ConflictError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, [1, 2])) == [False, True]
    assert len(references.list().references) == 1


def test_reference_router(references):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[services] = lambda: SimpleNamespace(references=references)
    with TestClient(app) as client:
        response = client.post("/api/references", json={"expected_revision": 0, "reference": fields().model_dump()})
        assert response.status_code == 201
        assert client.get("/api/references").json()["revision"] == 1
        record_id = response.json()["references"][0]["id"]
        assert client.get(f"/api/references/{record_id}").json()["citation_key"] == "Lovelace1843"
        result = client.post("/api/references/export", json={"format": "bibtex", "reference_ids": [record_id]})
        assert "@article{Lovelace1843" in result.json()["content"]


@pytest.mark.parametrize("content", [b"{broken", b'{"revision":-1}', b'{"references":[{"id":"r1"}]}'],
                         ids=["malformed-json", "negative-revision", "invalid-reference"])
def test_corrupt_library_is_semantic_and_never_overwritten(references, content):
    references.storage.write_bytes_atomic(references.storage.settings.data_dir, references._path, content)
    with pytest.raises(ValidationError, match="corrupt"):
        references.list()
    with pytest.raises(ValidationError, match="corrupt"):
        references.create(fields(), expected_revision=0)
    assert references.storage.read_stored_bytes(references.storage.settings.data_dir, references._path) == content


@pytest.mark.parametrize("field,value", [
    ("title", "Control\x00text"), ("journal", "Line\ninjection"), ("authors", ["Bad\x1bname"]),
    ("url", "https://example.org/\x7f"), ("abstract", "bad\x85control"), ("publisher", "x" * 2001),
    ("authors", ["x" * 501]), ("related_reference_ids", ["x" * 101]),
])
def test_metadata_bounds_and_control_characters(field, value):
    with pytest.raises(SchemaValidationError):
        ReferenceFields.model_validate({**fields().model_dump(), field: value})


def test_abstract_supports_readable_multiline_text():
    assert fields(abstract="First line\nSecond\tline").abstract == "First line\nSecond\tline"


def test_nested_collections_preserve_membership_and_paths(test_settings):
    engine = create_engine("sqlite://")
    PaperFolder.__table__.create(engine)
    Document.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repository = DocumentRepository(factory, test_settings, SafeStorage(test_settings))
    child = repository.create_folder("Methods / Deep / Study")
    folders = repository.list_folders()
    assert [item.name for item in folders] == ["Methods", "Methods/Deep", "Methods/Deep/Study"]
    with factory() as session:
        session.add(Document(id="paper", title="Paper", source_filename="canonical.pdf", metadata_json={"folder_id": child.id, "pdf_path": "library/paper/source.pdf"}))
        session.add(Document(id="parent-paper", title="Parent paper", source_filename="parent.pdf", metadata_json={"folder_id": folders[0].id}))
        session.commit()
    assert [paper.id for paper in repository.list() if paper.metadata_json.get("folder_id") == child.id] == ["paper"]
    assert [paper.id for paper in repository.list() if paper.metadata_json.get("folder_id") == folders[0].id] == ["parent-paper"]
    with pytest.raises(ConflictError, match="child"):
        repository.delete_folder(folders[0].id)
    assert repository.get("paper").metadata_json["folder_id"] == child.id
    assert repository.get("paper").metadata_json["pdf_path"] == "library/paper/source.pdf"
    assert repository.delete_folder(child.id)
    assert "folder_id" not in repository.get("paper").metadata_json
    assert repository.get("parent-paper").metadata_json["folder_id"] == folders[0].id
    assert repository.delete_folder(folders[1].id)
    assert repository.delete_folder(folders[0].id)
    assert "folder_id" not in repository.get("parent-paper").metadata_json
    for name in ("A//B", "A/../B", "A\\B", "/", "/A", "A/"):
        with pytest.raises(ValidationError):
            repository.create_folder(name)
    engine.dispose()
