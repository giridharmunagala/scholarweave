from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident

import anyio
import pytest

from backend.documents.ocr import DocumentOCR
from backend.core.errors import DocumentProcessingError
from pypdf.errors import PdfReadError


class FakePage:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text


class FakeReader:
    pages: list[FakePage] = []

    def __init__(self, _path: str) -> None:
        pass


@pytest.mark.parametrize(("text", "expected", "warns"), [
    ("  Valid \u4e2d\u6587 \U0001f600 \U0001d400  ", "Valid \u4e2d\u6587 \U0001f600 \U0001d400", False),
    ("Pair \ud835\udc00", "Pair \U0001d400", True),
    ("Broken \ud835\ud835 and \udc00", "Broken \ufffd\ufffd and \ufffd", True),
    ("Pair \ud835\udc00, broken \ud835, emoji \U0001f600", "Pair \U0001d400, broken \ufffd, emoji \U0001f600", True),
])
def test_native_text_repairs_surrogates_before_utf8_encoding(text, expected, warns, caplog):
    reader = FakeReader("paper.pdf")
    reader.pages = [FakePage(text)]
    actual = DocumentOCR._extract_native_text(reader, 0)
    assert actual == expected
    assert actual.encode("utf-8").decode("utf-8") == expected
    assert ("PDF page 1" in caplog.text) is warns


@pytest.mark.anyio
@pytest.mark.parametrize("force_ocr", [False, True])
async def test_pdf_parsing_and_ocr_probe_run_off_event_loop(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
    force_ocr: bool,
) -> None:
    event_loop_thread = get_ident()
    operations: list[tuple[str, int]] = []
    completed: list[int] = []
    native_text = "Native paper text " * 10

    def record(operation: str) -> None:
        operations.append((operation, get_ident()))

    class Page(FakePage):
        def extract_text(self) -> str:
            record("extract")
            return super().extract_text()

    class PageTree(list):
        def __len__(self) -> int:
            record("page_count")
            return super().__len__()

    class Reader:
        def __init__(self, _path: str) -> None:
            record("open")

        @property
        def pages(self) -> PageTree:
            record("page_tree")
            return PageTree([Page(native_text), Page(native_text)])

    def available() -> bool:
        record("available")
        return True

    async def recognize(_image: bytes, page_number: int) -> str:
        return f"Recovered page {page_number}"

    async def progress(payload: dict) -> None:
        assert get_ident() == event_loop_thread
        completed.append(payload["completed_pages"])

    ocr = DocumentOCR(test_settings)
    monkeypatch.setattr("backend.documents.ocr.PdfReader", Reader)
    monkeypatch.setattr(ocr, "available", available)
    monkeypatch.setattr(ocr, "render_page_png", lambda *_args: b"page-image")
    monkeypatch.setattr(ocr, "_tesseract_image", recognize)

    pages = await ocr.extract_pages(
        Path("paper.pdf"), force_ocr=force_ocr, progress=progress,
    )

    assert [page["text"] for page in pages] == (
        ["Recovered page 1", "Recovered page 2"]
        if force_ocr else [native_text.strip()] * 2
    )
    assert [page["ocr_used"] for page in pages] == [force_ocr] * 2
    assert completed == [0, 1, 2]
    names = [operation for operation, _thread in operations]
    assert names.count("open") == 1
    assert names.count("extract") == 2
    assert names.count("available") == int(force_ocr)
    assert all(thread != event_loop_thread for _operation, thread in operations), operations


@pytest.mark.anyio
async def test_cached_ocr_pages_remain_available_for_optional_vision_enhancement(
    test_settings, monkeypatch,
) -> None:
    ocr = DocumentOCR(test_settings)
    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(FakeReader, "pages", [FakePage(""), FakePage("")])
    monkeypatch.setattr(ocr, "render_page_png", lambda *_args: b"page-image")

    async def unexpected_ocr(*_args):
        pytest.fail("Cached pages must not make OCR requests")

    monkeypatch.setattr(ocr, "ocr_image", unexpected_ocr)
    cached = {
        1: {"page": 1, "text": "Saved OCR draft.", "ocr_used": True},
        2: {
            "page": 2, "text": "Previously enhanced evidence.", "ocr_used": True,
            "llm_enhanced": True, "ocr_quality": "good",
        },
    }
    progress = []

    async def report(payload):
        progress.append(payload)

    pages = await ocr.extract_pages(
        Path("paper.pdf"), cached_pages=cached, retain_page_images=True, progress=report,
    )
    assert pages[0]["_image_png"] == b"page-image"
    assert pages[1]["text"] == cached[2]["text"]
    assert "_image_png" not in pages[1]
    assert progress[0]["completed_pages"] == progress[0]["reused_pages"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize("is_available", [False, True])
async def test_single_page_ocr_probes_off_event_loop(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
    is_available: bool,
) -> None:
    event_loop_thread = get_ident()
    operations: list[str] = []
    ocr = DocumentOCR(test_settings)

    def available() -> bool:
        assert get_ident() != event_loop_thread
        operations.append("probe")
        return is_available

    def render(pdf_path: Path, index: int) -> bytes:
        assert get_ident() != event_loop_thread
        assert pdf_path == Path("paper.pdf")
        assert index == 2
        operations.append("render")
        return b"page-image"

    async def recognize(image: bytes, page_number: int) -> str:
        assert get_ident() == event_loop_thread
        assert image == b"page-image"
        assert page_number == 3
        operations.append("recognize")
        return "Recovered page"

    monkeypatch.setattr(ocr, "available", available)
    monkeypatch.setattr(ocr, "render_page_png", render)
    monkeypatch.setattr(ocr, "ocr_image", recognize)

    result = await ocr.ocr_page(Path("paper.pdf"), 2)

    assert result == ("Recovered page" if is_available else "")
    assert operations == (["probe", "render", "recognize"] if is_available else ["probe"])


@pytest.mark.anyio
async def test_cancelled_single_page_ocr_waits_for_probe_without_rendering(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    completed = Event()
    exited = anyio.Event()
    scopes: list[anyio.CancelScope] = []
    ocr = DocumentOCR(test_settings)

    def available() -> bool:
        started.set()
        assert release.wait(timeout=5)
        completed.set()
        return True

    async def run() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await ocr.ocr_page(Path("paper.pdf"), 0)
        exited.set()

    monkeypatch.setattr(ocr, "available", available)
    monkeypatch.setattr(
        ocr, "render_page_png",
        lambda *_args: pytest.fail("Cancellation must stop before rendering"),
    )

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run)
        try:
            with anyio.fail_after(5):
                while not started.is_set():
                    await anyio.sleep(0.001)
                scopes[0].cancel()
                await anyio.sleep(0)
                assert not exited.is_set()
                release.set()
                await exited.wait()
                assert completed.is_set()
        finally:
            release.set()


def test_tesseract_command_can_be_configured_with_an_environment_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.touch()
    monkeypatch.setattr("backend.documents.ocr.shutil.which", lambda _name: None)
    monkeypatch.setenv("TESSERACT_CMD", str(executable))

    assert DocumentOCR._tesseract_path() == str(executable)


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


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["native", "ocr", "render", "empty", "unavailable"])
async def test_unrecoverable_page_does_not_stop_later_pages(
    test_settings, monkeypatch, failure, caplog,
) -> None:
    class BrokenPage(FakePage):
        def extract_text(self) -> str:
            if failure == "native":
                raise PdfReadError("Malformed page text")
            return ""

    native_text = ("Readable later page. " * 10).strip()
    FakeReader.pages = [BrokenPage(""), FakePage(native_text)]
    ocr = DocumentOCR(test_settings)
    completed = []

    async def recognize(_image, _page):
        if failure == "empty":
            return ""
        raise DocumentProcessingError("Cannot recognize page")

    def render(_path, index):
        if failure == "render" and index == 0:
            raise ValueError("Cannot render page")
        return b"page-image"

    async def progress(payload):
        completed.append(payload["completed_pages"])

    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(ocr, "available", lambda: failure != "unavailable")
    monkeypatch.setattr(ocr, "render_page_png", render)
    monkeypatch.setattr(ocr, "ocr_image", recognize)

    pages = await ocr.extract_pages(
        Path("paper.pdf"), retain_page_images=True, progress=progress,
    )

    assert [page["page"] for page in pages] == [1, 2]
    assert pages[0]["text"] == ""
    assert pages[0]["ocr_skipped"] is True
    assert "Skipped unreadable page 1" in pages[0]["ocr_fallback_note"]
    assert "_image_png" not in pages[0]
    assert pages[1]["text"] == native_text
    assert pages[1]["_image_png"] == b"page-image"
    assert completed == [0, 1, 2]
    assert "Skipped unreadable page 1" in caplog.text


@pytest.mark.anyio
async def test_native_extraction_error_recovers_with_ocr(test_settings, monkeypatch) -> None:
    class BrokenPage(FakePage):
        def extract_text(self):
            raise PdfReadError("Malformed text")

    FakeReader.pages = [BrokenPage("")]
    ocr = DocumentOCR(test_settings)

    async def recognize(_image, _page):
        return "Recovered text"

    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "render_page_png", lambda *_args: b"page-image")
    monkeypatch.setattr(ocr, "ocr_image", recognize)

    pages = await ocr.extract_pages(Path("paper.pdf"))

    assert pages[0]["text"] == "Recovered text"
    assert pages[0]["ocr_used"] is True
    assert "Malformed text" in pages[0]["native_extraction_note"]
    assert not pages[0].get("ocr_skipped")


@pytest.mark.anyio
async def test_render_failure_preserves_native_text_without_enhancement(test_settings, monkeypatch):
    native_text = ("Native paper text. " * 10).strip()
    FakeReader.pages = [FakePage(native_text)]
    ocr = DocumentOCR(test_settings)

    def render(*_args):
        raise ValueError("Broken page image")

    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(ocr, "render_page_png", render)

    pages = await ocr.extract_pages(Path("paper.pdf"), retain_page_images=True)

    assert pages[0]["text"] == native_text
    assert "Broken page image" in pages[0]["llm_enhancement_note"]
    assert "_image_png" not in pages[0]


def test_text_layer_inspection_continues_after_bad_page(test_settings, monkeypatch):
    class BrokenPage(FakePage):
        def extract_text(self):
            raise PdfReadError("Malformed text")

    FakeReader.pages = [BrokenPage(""), FakePage("Readable page. " * 10)]
    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)

    result = DocumentOCR(test_settings).inspect_text_layer(Path("paper.pdf"))

    assert result["total_pages"] == 2
    assert result["embedded_text_pages"] == 1
