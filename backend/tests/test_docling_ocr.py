from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.documents.ocr import DocumentOCR


def test_docling_availability_requires_package(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = test_settings.model_copy(update={"ocr_engine": "docling"})
    monkeypatch.setattr(
        "backend.documents.ocr.importlib.util.find_spec",
        lambda package: None if package == "docling" else object(),
    )

    assert DocumentOCR(settings).available() is False


@pytest.mark.anyio
async def test_docling_parses_every_page_and_marks_ocr_pages(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = test_settings.model_copy(update={"ocr_engine": "docling"})
    ocr = DocumentOCR(settings)

    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        pages = [FakePage("Embedded text " * 10), FakePage("")]

        def __init__(self, _path: str) -> None:
            pass

    captured: list[tuple[list[int], bool]] = []

    async def fake_docling(
        _path: Path,
        page_numbers: list[int],
        **_kwargs: object,
    ) -> dict[int, str]:
        captured.append((page_numbers, bool(_kwargs["force_ocr"])))
        return {1: "Structured embedded page", 2: "Docling page"}

    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "_docling_pdf_pages", fake_docling)

    pages = await ocr.extract_pages(Path("scan.pdf"))

    assert captured == [([1, 2], False)]
    assert pages[0]["text"] == "Structured embedded page"
    assert pages[0]["ocr_used"] is False
    assert pages[1]["text"] == "Docling page"
    assert pages[1]["ocr_engine"] == "docling"


@pytest.mark.anyio
async def test_docling_converts_page_batches_and_reports_progress(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = test_settings.model_copy(
        update={"ocr_engine": "docling", "docling_batch_size": 2}
    )
    ocr = DocumentOCR(settings)
    page_ranges: list[tuple[int, int]] = []

    class FakeDocument:
        def export_to_markdown(self, *, page_no: int) -> str:
            return f"page {page_no}"

    class FakeConverter:
        def convert(self, _path: Path, *, page_range: tuple[int, int]):
            page_ranges.append(page_range)
            return SimpleNamespace(document=FakeDocument())

    monkeypatch.setattr(
        ocr,
        "_get_docling_converter",
        lambda *, force_ocr: FakeConverter(),
    )
    progress: list[dict[str, object]] = []

    async def capture(payload: dict[str, object]) -> None:
        progress.append(payload)

    result = await ocr._docling_pdf_pages(
        Path("paper.pdf"),
        [1, 2, 3],
        progress=capture,
    )

    assert page_ranges == [(1, 2), (3, 3)]
    assert result == {1: "page 1", 2: "page 2", 3: "page 3"}
    assert [event["completed_pages"] for event in progress] == [2, 3]


def test_docling_disables_layout_compilation_without_reducing_quality(test_settings) -> None:
    from docling.datamodel.base_models import InputFormat

    settings = test_settings.model_copy(update={"ocr_engine": "docling"})
    ocr = DocumentOCR(settings)

    converter = ocr._get_docling_converter(force_ocr=False)
    options = converter.format_to_options[InputFormat.PDF]

    assert options.pipeline_options.layout_options.engine_options.compile_model is False
    assert options.pipeline_options.do_formula_enrichment is True
    assert options.pipeline_options.do_code_enrichment is True
    assert options.pipeline_options.table_structure_options.mode.value == "accurate"
