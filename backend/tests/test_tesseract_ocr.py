from __future__ import annotations

from pathlib import Path
from threading import Event, get_ident

import anyio
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
