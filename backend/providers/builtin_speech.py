from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
from typing import Any

import anyio
import httpx

from backend.core.config import Settings
from backend.providers.errors import ProviderRuntimeError
from backend.providers.schemas import BuiltInSpeechStatus

MODEL_NAME = "nvidia/nemotron-speech-streaming-en-0.6b"
MODEL_PACKAGE = (
    "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
)
MODEL_ARCHIVE = f"{MODEL_PACKAGE}.tar.bz2"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_ARCHIVE}"
)
MODEL_SHA256 = "78e2b79fcf7271553a74402a76b771b09ea40117a39566a79f52235b23db6358"
MODEL_SIZE = 463_945_051
RUNTIME_VERSION = "sherpa-onnx-1.13.5"
MODEL_FILES = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "joiner.int8.onnx",
    "tokens.txt",
)


class NemotronSpeechSession:
    def __init__(self, recognizer: Any, decode_lock: anyio.Lock) -> None:
        self._recognizer = recognizer
        self._stream = recognizer.create_stream()
        self._decode_lock = decode_lock
        self._finished = False

    async def accept_pcm(self, content: bytes) -> str:
        if self._finished:
            raise ProviderRuntimeError("The speech stream has already finished.")
        if not content:
            return self.text
        if len(content) % 2:
            raise ProviderRuntimeError("Speech audio must contain 16-bit PCM samples.")
        async with self._decode_lock:
            return await anyio.to_thread.run_sync(self._accept_pcm_sync, content)

    async def finish(self) -> str:
        if self._finished:
            return self.text
        self._finished = True
        async with self._decode_lock:
            return await anyio.to_thread.run_sync(self._finish_sync)

    @property
    def text(self) -> str:
        return str(self._recognizer.get_result(self._stream)).strip()

    def _accept_pcm_sync(self, content: bytes) -> str:
        import numpy as np

        samples = np.frombuffer(content, dtype="<i2").astype(np.float32) / 32768.0
        self._stream.accept_waveform(16_000, samples)
        self._decode_ready()
        return self.text

    def _finish_sync(self) -> str:
        import numpy as np

        self._stream.accept_waveform(16_000, np.zeros(3_200, dtype=np.float32))
        self._stream.input_finished()
        self._decode_ready()
        return self.text

    def _decode_ready(self) -> None:
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)


class BuiltInSpeechRuntime:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.data_dir / "speech"
        self.models_dir = self.root / "models"
        self.model_dir = self.models_dir / MODEL_PACKAGE
        self.manifest_path = self.root / "install.json"
        self._install_task: asyncio.Task[None] | None = None
        self._recognizer: Any | None = None
        self._error: str | None = None
        self._downloaded_bytes = 0
        self._lifecycle_lock = asyncio.Lock()
        self._decode_lock = anyio.Lock()

    def status(self) -> BuiltInSpeechStatus:
        installing = self._install_task is not None and not self._install_task.done()
        installed = self._installed()
        if installing:
            state = "installing"
        elif self._error:
            state = "error"
        elif self._recognizer is not None:
            state = "running"
        elif installed:
            state = "ready"
        else:
            state = "not_installed"
        return BuiltInSpeechStatus(
            state=state,
            available=True,
            installed=installed,
            running=self._recognizer is not None,
            model=MODEL_NAME,
            downloaded_bytes=self._downloaded_bytes,
            total_bytes=MODEL_SIZE,
            error=self._error,
        )

    async def install(self) -> BuiltInSpeechStatus:
        async with self._lifecycle_lock:
            if self._install_task is None or self._install_task.done():
                self._error = None
                self._downloaded_bytes = 0
                self._install_task = asyncio.create_task(self._install_and_load())
            return self.status()

    async def uninstall(self) -> BuiltInSpeechStatus:
        async with self._lifecycle_lock:
            task = self._install_task
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if self._install_task is task:
                self._install_task = None
            self._recognizer = None
            await anyio.to_thread.run_sync(self._remove_root)
            self._downloaded_bytes = 0
            self._error = None
            return self.status()

    async def start(self) -> BuiltInSpeechStatus:
        async with self._lifecycle_lock:
            if not self._installed():
                raise ProviderRuntimeError(
                    "Install the Nemotron 3.5 ASR model before using the microphone."
                )
            if self._recognizer is None:
                try:
                    self._recognizer = await anyio.to_thread.run_sync(
                        self._load_recognizer
                    )
                except ProviderRuntimeError:
                    raise
                except Exception as exc:
                    self._error = str(exc)
                    raise ProviderRuntimeError(
                        f"Nemotron 3.5 ASR could not start: {exc}"
                    ) from exc
            self._error = None
            return self.status()

    async def create_session(self) -> NemotronSpeechSession:
        await self.start()
        assert self._recognizer is not None
        return NemotronSpeechSession(self._recognizer, self._decode_lock)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._install_task and not self._install_task.done():
                self._install_task.cancel()
                try:
                    await self._install_task
                except asyncio.CancelledError:
                    pass
            self._recognizer = None

    async def _install_and_load(self) -> None:
        archive = self.root / MODEL_ARCHIVE
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self._installed():
                await anyio.to_thread.run_sync(self._remove_root)
                self.root.mkdir(parents=True, exist_ok=True)
                await self._download(archive)
                await anyio.to_thread.run_sync(self._extract, archive)
                archive.unlink(missing_ok=True)
                await anyio.to_thread.run_sync(self._write_manifest)
            self._recognizer = await anyio.to_thread.run_sync(self._load_recognizer)
            self._downloaded_bytes = MODEL_SIZE
            self._error = None
        except asyncio.CancelledError:
            archive.unlink(missing_ok=True)
            raise
        except Exception as exc:
            archive.unlink(missing_ok=True)
            self._error = str(exc)

    async def _download(self, destination: Path) -> None:
        partial = destination.with_suffix(destination.suffix + ".part")
        partial.unlink(missing_ok=True)
        digest = hashlib.sha256()
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
                async with client.stream("GET", MODEL_URL) as response:
                    response.raise_for_status()
                    with partial.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            output.write(chunk)
                            digest.update(chunk)
                            self._downloaded_bytes += len(chunk)
            if digest.hexdigest() != MODEL_SHA256:
                raise ProviderRuntimeError(
                    "Checksum verification failed for the Nemotron model package."
                )
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    def _extract(self, archive: Path) -> None:
        staging = self.root / "models.part"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(archive, "r:bz2") as bundle:
                root = staging.resolve()
                for member in bundle.getmembers():
                    target = (root / member.name).resolve()
                    if not target.is_relative_to(root):
                        raise ProviderRuntimeError(
                            "The Nemotron model archive is unsafe."
                        )
                bundle.extractall(staging, filter="data")
            extracted = staging / MODEL_PACKAGE
            if not extracted.is_dir():
                raise ProviderRuntimeError(
                    "The Nemotron model package has an unexpected layout."
                )
            for filename in MODEL_FILES:
                if not (extracted / filename).is_file():
                    raise ProviderRuntimeError(
                        f"The Nemotron model package is missing {filename}."
                    )
            if self.models_dir.exists():
                shutil.rmtree(self.models_dir)
            staging.replace(self.models_dir)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def _load_recognizer(self) -> Any:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise ProviderRuntimeError(
                "The sherpa-onnx speech runtime is not installed."
            ) from exc
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=str(self.model_dir / "encoder.int8.onnx"),
            decoder=str(self.model_dir / "decoder.int8.onnx"),
            joiner=str(self.model_dir / "joiner.int8.onnx"),
            tokens=str(self.model_dir / "tokens.txt"),
            num_threads=2,
            decoding_method="greedy_search",
            provider="cpu",
        )

    def _installed(self) -> bool:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if manifest != self._manifest():
            return False
        return all(
            (self.model_dir / filename).is_file()
            and (self.model_dir / filename).stat().st_size > 0
            for filename in MODEL_FILES
        )

    def _manifest(self) -> dict[str, str]:
        return {
            "model": MODEL_NAME,
            "package": MODEL_ARCHIVE,
            "package_sha256": MODEL_SHA256,
            "runtime": RUNTIME_VERSION,
        }

    def _write_manifest(self) -> None:
        temporary = self.manifest_path.with_suffix(".json.part")
        temporary.write_text(
            json.dumps(self._manifest(), sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.manifest_path)

    def _remove_root(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
