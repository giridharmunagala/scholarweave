from __future__ import annotations

from pathlib import Path

import pytest

from backend.documents.ocr import DocumentOCR


class FakePage:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text


class FakeReader:
    pages: list[FakePage] = []

    def __init__(self, _path: str) -> None:
        pass


@pytest.mark.anyio
async def test_sufficient_native_text_skips_ocr(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeReader.pages = [FakePage("Native paper text " * 10)]
    ocr = DocumentOCR(test_settings)
    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(
        ocr,
        "render_page_png",
        lambda *_args: pytest.fail("A readable native-text page must not be rendered"),
    )

    pages = await ocr.extract_pages(Path("paper.pdf"))

    assert pages[0]["text"] == ("Native paper text " * 10).strip()
    assert pages[0]["ocr_used"] is False


@pytest.mark.anyio
async def test_text_poor_page_uses_tesseract(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeReader.pages = [FakePage("tiny")]
    ocr = DocumentOCR(test_settings)

    async def recognize(_image: bytes, page_number: int) -> str:
        assert page_number == 1
        return "Recovered scanned paper text"

    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "render_page_png", lambda *_args: b"page-image")
    monkeypatch.setattr(ocr, "_tesseract_image", recognize)

    pages = await ocr.extract_pages(Path("scan.pdf"))

    assert pages[0]["text"] == "Recovered scanned paper text"
    assert pages[0]["ocr_used"] is True
    assert pages[0]["ocr_engine"] == "tesseract"


@pytest.mark.anyio
async def test_retaining_page_images_does_not_force_ocr(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeReader.pages = [FakePage("Native paper text " * 10)]
    ocr = DocumentOCR(test_settings)

    async def unexpected_ocr(_image: bytes, _page_number: int) -> str:
        pytest.fail("Retaining an image for vision review must not force OCR")

    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(ocr, "render_page_png", lambda *_args: b"page-image")
    monkeypatch.setattr(ocr, "_tesseract_image", unexpected_ocr)

    pages = await ocr.extract_pages(Path("paper.pdf"), retain_page_images=True)

    assert pages[0]["text"] == ("Native paper text " * 10).strip()
    assert pages[0]["ocr_used"] is False
    assert pages[0]["_image_png"] == b"page-image"
