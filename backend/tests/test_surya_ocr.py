from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import anyio
import pytest

from backend.documents.ocr import DocumentOCR


class FakeOllama:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def unload_all_models(self) -> list[str]:
        self.events.append("unload")
        return ["resident-model"]


def test_surya_availability_requires_torchvision(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = test_settings.model_copy(update={"ocr_engine": "surya"})
    monkeypatch.setattr(
        "backend.documents.ocr.importlib.util.find_spec",
        lambda package: None if package == "torchvision" else object(),
    )

    assert DocumentOCR(settings).available() is False


@pytest.mark.anyio
async def test_surya_batches_pages_through_one_worker(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = test_settings.model_copy(update={"ocr_engine": "surya"})
    ocr = DocumentOCR(settings)

    class FakePage:
        def extract_text(self) -> str:
            return ""

    class FakeReader:
        pages = [FakePage(), FakePage()]

        def __init__(self, _path: str) -> None:
            pass

    captured: list[list[bytes]] = []

    async def fake_surya(image_paths: list[Path]) -> list[str]:
        captured.append([path.read_bytes() for path in image_paths])
        return ["first page", "second page"]

    monkeypatch.setattr("backend.documents.ocr.PdfReader", FakeReader)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(
        ocr,
        "render_page_png",
        lambda _path, index: f"image-{index}".encode(),
    )
    monkeypatch.setattr(ocr, "_surya_image_paths", fake_surya)

    pages = await ocr.extract_pages(Path("scan.pdf"))

    assert captured == [[b"image-0", b"image-1"]]
    assert [page["text"] for page in pages] == ["first page", "second page"]
    assert all(page["ocr_engine"] == "surya" for page in pages)


@pytest.mark.anyio
async def test_surya_unloads_ollama_and_serializes_isolated_workers(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = test_settings.model_copy(
        update={
            "ocr_engine": "surya",
            "surya_timeout_seconds": 30,
        }
    )
    events: list[str] = []
    ocr = DocumentOCR(settings, ollama=FakeOllama(events))
    active_workers = 0
    maximum_active_workers = 0

    async def fake_run_process(command: list[str], **_kwargs: object):
        nonlocal active_workers, maximum_active_workers
        events.append("worker")
        active_workers += 1
        maximum_active_workers = max(maximum_active_workers, active_workers)
        await anyio.sleep(0.01)
        output_path = Path(command[command.index("--output") + 1])
        manifest_path = Path(command[command.index("--manifest") + 1])
        assert json.loads(manifest_path.read_text(encoding="utf-8"))["parent_pid"] == os.getpid()
        output_path.write_text(json.dumps({"pages": ["recognized"]}), encoding="utf-8")
        active_workers -= 1
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(anyio, "run_process", fake_run_process)
    results: list[list[str]] = []

    async def run_job() -> None:
        results.append(await ocr._surya_images([b"image"]))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_job)
        tasks.start_soon(run_job)

    assert results == [["recognized"], ["recognized"]]
    assert maximum_active_workers == 1
    assert events == ["unload", "worker", "unload", "worker"]


@pytest.mark.anyio
async def test_surya_holds_gpu_gate_until_worker_exits(
    test_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = test_settings.model_copy(
        update={
            "ocr_engine": "surya",
            "surya_timeout_seconds": 30,
        }
    )
    gpu_lock = anyio.Lock()
    worker_started = anyio.Event()
    finish_worker = anyio.Event()
    competing_request_started = anyio.Event()
    ocr = DocumentOCR(
        settings,
        ollama=FakeOllama([]),
        ollama_gpu_lock=gpu_lock,
    )

    async def fake_run_process(command: list[str], **_kwargs: object):
        worker_started.set()
        await finish_worker.wait()
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(json.dumps({"pages": ["recognized"]}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    async def competing_request() -> None:
        await worker_started.wait()
        async with gpu_lock:
            competing_request_started.set()

    monkeypatch.setattr(anyio, "run_process", fake_run_process)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(ocr._surya_images, [b"image"])
        tasks.start_soon(competing_request)
        await worker_started.wait()
        await anyio.sleep(0.01)
        assert not competing_request_started.is_set()
        finish_worker.set()

    assert competing_request_started.is_set()
