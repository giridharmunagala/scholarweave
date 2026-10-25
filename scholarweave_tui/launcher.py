from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from html.parser import HTMLParser
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from backend.core.config import RUNTIME_VERSION, Settings
from scholarweave_tui.client import _local_origin
from scholarweave_tui.server import serve


class LaunchError(RuntimeError):
    """Actionable startup or shutdown failure."""


class OccupiedOrigin(LaunchError):
    """A different application/library occupies a potential listening address."""


_FRONTEND_BUILD_RECORD = ".scholarweave-build.json"


class _FrontendReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []
        self.has_module = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.paths.append(attributes["src"])
            self.has_module |= attributes.get("type") == "module"
        elif tag == "link" and attributes.get("rel") in {"stylesheet", "modulepreload"} and attributes.get("href"):
            self.paths.append(attributes["href"])


def _frontend_digest(paths: list[Path], base: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _frontend_inputs(root: Path) -> str:
    frontend = root / "frontend"
    paths = []
    for directory, directories, filenames in os.walk(frontend):
        directories[:] = [
            name for name in directories
            if name not in {"node_modules", "dist", "coverage", ".node", ".git"}
        ]
        paths.extend(
            Path(directory) / name for name in filenames
            if not name.endswith(".tsbuildinfo")
            and not (Path(directory) == frontend and name in {"vite.config.js", "vite.config.d.ts"})
        )
    paths.extend(path for path in root.glob(".env*") if path.is_file())
    installed_lock = frontend / "node_modules" / ".package-lock.json"
    if installed_lock.is_file():
        paths.append(installed_lock)
    environment = {
        key: value for key, value in os.environ.items()
        if key.startswith("VITE_") or key in {"NODE_ENV", "BROWSERSLIST_ENV"}
    }
    return hashlib.sha256(
        (_frontend_digest(paths, root) + json.dumps(environment, sort_keys=True)).encode(),
    ).hexdigest()


def _frontend_outputs(frontend: Path) -> str | None:
    dist = frontend / "dist"
    index = dist / "index.html"
    if not index.is_file():
        return None
    references = _FrontendReferences()
    try:
        references.feed(index.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError):
        return None
    if not references.has_module:
        return None
    for reference in references.paths:
        url = urlsplit(reference)
        if not url.scheme and not url.netloc:
            path = (dist / unquote(url.path).lstrip("/")).resolve()
            if not path.is_relative_to(dist.resolve()) or not path.is_file():
                return None
    return _frontend_digest([
        path for path in dist.rglob("*")
        if path.is_file() and path.name != _FRONTEND_BUILD_RECORD
    ], dist)


def build_frontend(root: Path, *, force: bool = True) -> None:
    try:
        _build_frontend(root, force=force)
    except OSError as exc:
        raise LaunchError(f"Could not read or update frontend build files: {exc}") from exc


def _build_frontend(root: Path, *, force: bool) -> None:
    frontend = root / "frontend"
    if not (frontend / "package.json").is_file():
        raise LaunchError(f"Frontend source was not found in {frontend}. Use the repository checkout.")
    inputs = _frontend_inputs(root)
    record_path = frontend / "dist" / _FRONTEND_BUILD_RECORD
    if not force:
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            record = None
        outputs = _frontend_outputs(frontend)
        if outputs and record == {"version": 1, "inputs": inputs, "outputs": outputs}:
            print("Reusing the current web frontend.", flush=True)
            return
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise LaunchError("Node.js/npm was not found. Install Node.js 20+ and reopen the launcher.")
    index = frontend / "dist" / "index.html"
    previous_index = index.stat() if index.is_file() else None
    # A failed or interrupted rebuild must never authorize reuse of its remaining files.
    record_path.unlink(missing_ok=True)
    print("Building the web frontend...", flush=True)
    try:
        result = subprocess.run([npm, "run", "build"], cwd=frontend, check=False)
    except OSError as exc:
        raise LaunchError(f"Could not start the frontend build: {exc}") from exc
    if result.returncode:
        raise LaunchError(
            "Frontend build failed; see the output above. If dependencies are missing, "
            "run 'npm ci' in the frontend directory, then launch again."
        )
    if not index.is_file():
        raise LaunchError("The frontend build finished without producing frontend/dist/index.html.")
    if previous_index is not None and index.stat().st_mtime_ns == previous_index.st_mtime_ns:
        raise LaunchError("The frontend build did not refresh frontend/dist/index.html. Launch again to rebuild it.")
    outputs = _frontend_outputs(frontend)
    if not outputs:
        raise LaunchError("The frontend build finished without producing a complete web bundle.")
    if inputs != _frontend_inputs(root):
        raise LaunchError("Frontend sources changed during the build. Launch again to build the current files.")
    record_path.write_text(json.dumps({
        "version": 1, "inputs": inputs, "outputs": outputs,
    }), encoding="utf-8")


class BackendSession:
    """Reserve the listening socket before spawning, and own only our child process."""

    def __init__(
        self,
        base_url: str,
        root: Path,
        *,
        connect_only: bool = False,
        timeout: float = 60,
    ) -> None:
        self.base_url = _local_origin(base_url)
        self.root = root.resolve()
        self.connect_only = connect_only
        self.timeout = timeout
        self.process: BaseProcess | None = None
        self.stop: Event | None = None
        self.log_path: Path | None = None
        self._failed = False
        self._http = httpx.Client(trust_env=False, follow_redirects=False, timeout=1)
        self._settings = Settings(_env_file=self.root / ".env")
        self.runtime_path = self._settings.data_dir / "backend-runtime.json"

    @contextmanager
    def _startup_lock(self) -> Iterator[None]:
        identity = hashlib.sha256(os.path.normcase(str(self._settings.database_path)).encode()).hexdigest()
        path = Path(tempfile.gettempdir()) / f"scholarweave-startup-{identity}.lock"
        with path.open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise LaunchError(f"Could not lock backend startup: {exc}") from exc
                    if time.monotonic() >= deadline:
                        raise LaunchError("Another launcher is still starting this library's backend. Try again shortly.") from exc
                    time.sleep(0.1)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _saved_origin(self) -> str | None:
        try:
            record = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise LaunchError(f"Could not read backend discovery file {self.runtime_path}: {exc}") from exc
        if not isinstance(record, dict) or record.get("database_path") != str(self._settings.database_path):
            raise LaunchError(f"Backend discovery file {self.runtime_path} does not match this library.")
        try:
            if not isinstance(record.get("url"), str):
                raise ValueError("Missing URL")
            return _local_origin(record["url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LaunchError(f"Backend discovery file {self.runtime_path} has an invalid local URL.") from exc

    def _publish_origin(self) -> None:
        temporary = None
        try:
            self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.runtime_path.parent,
                prefix=".backend-runtime-", suffix=".tmp", delete=False,
            ) as file:
                temporary = Path(file.name)
                json.dump({
                    "url": self.base_url, "database_path": str(self._settings.database_path),
                    "data_dir": str(self._settings.data_dir),
                }, file)
            temporary.replace(self.runtime_path)
        except OSError as exc:
            raise LaunchError(f"Could not publish the backend address in {self.runtime_path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _ready(self) -> bool:
        try:
            response = self._http.get(f"{self.base_url}/api/health", params={"check_ocr": "false"})
        except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError):
            return False
        except httpx.RequestError as exc:
            raise LaunchError(f"Could not check the backend at {self.base_url}: {exc}") from exc
        try:
            health = response.json()
        except ValueError as exc:
            raise OccupiedOrigin(f"{self.base_url} is occupied by a service that is not ScholarWeave.") from exc
        if (
            not isinstance(health, dict)
            or not isinstance(health.get("database_path"), str)
            or not isinstance(health.get("data_dir"), str)
        ):
            raise OccupiedOrigin(f"{self.base_url} is not a compatible ScholarWeave backend.")
        if Path(health["database_path"]).resolve() != self._settings.database_path:
            raise OccupiedOrigin(
                f"{self.base_url} belongs to a different ScholarWeave library. "
                "It will not be reused or stopped."
            )
        if (
            Path(health["data_dir"]).resolve() != self._settings.data_dir
            or response.status_code != 200 or health.get("status") != "ok"
            or health.get("runtime_version") != RUNTIME_VERSION
        ):
            raise LaunchError(
                f"{self.base_url} already uses this library's database with incompatible settings or runtime. "
                "Stop that backend before restarting; a second backend will not be started."
            )
        return True

    def __enter__(self) -> BackendSession:
        try:
            if self.connect_only:
                if not self._ready():
                    raise LaunchError(f"No ready backend at {self.base_url}; --connect-only will not start one.")
                print(f"Using the existing backend at {self.base_url}; it will stay running.", flush=True)
                return self
            with self._startup_lock():
                self._start_or_reuse()
                self._publish_origin()
            return self
        except BaseException:
            # Context managers are not exited when __enter__ fails.
            self._failed = True
            self.close()
            raise

    def _start_or_reuse(self) -> None:
        preferred = self.base_url
        saved = self._saved_origin()
        for origin in dict.fromkeys([saved, preferred]):
            if origin is None:
                continue
            self.base_url = origin
            try:
                if self._ready():
                    print(f"Using the existing backend at {self.base_url}; it will stay running.", flush=True)
                    return
            except OccupiedOrigin as exc:
                print(f"{exc} Checking for an available local port.", flush=True)
        self.base_url = preferred
        parsed = urlsplit(self.base_url)
        if parsed.hostname == "::1":
            raise LaunchError("Automatic startup binds to 127.0.0.1. Use an IPv4 loopback --api-url.")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", parsed.port or 80))
            except OSError as exc:
                if exc.errno not in {errno.EADDRINUSE, errno.EACCES}:
                    raise LaunchError(f"Could not reserve the backend port: {exc}") from exc
                try:
                    sock.bind(("127.0.0.1", 0))
                except OSError as fallback_error:
                    raise LaunchError(f"Could not reserve an available backend port: {fallback_error}") from fallback_error
                print(f"Preferred backend port is unavailable; using port {sock.getsockname()[1]}.", flush=True)
            self.base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            sock.listen(128)
            log = tempfile.NamedTemporaryFile(prefix="scholarweave-backend-", suffix=".log", delete=False)
            self.log_path = Path(log.name)
            log.close()
            context = multiprocessing.get_context("spawn")
            self.stop = context.Event()
            self.process = context.Process(
                target=serve, args=(sock, self.stop, self.root, self.log_path),
                name="scholarweave-backend",
            )
            try:
                self.process.start()
            except OSError as exc:
                self.process = None
                raise LaunchError(f"Could not start the backend process: {exc}") from exc
        finally:
            sock.close()
        print(f"Starting the backend at {self.base_url}...", flush=True)
        self._wait_ready()
        print("Backend ready. Closing this launcher will stop this backend.", flush=True)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.exitcode is not None:
                raise LaunchError(self._diagnostic(f"Backend exited with code {self.process.exitcode}."))
            if self._ready():
                return
            time.sleep(0.1)
        raise LaunchError(self._diagnostic(
            f"Backend did not become ready within {self.timeout:g} seconds at {self.base_url}. "
            "The port may be occupied by another program.",
        ))

    def check_web(self) -> None:
        try:
            response = self._http.get(self.base_url)
        except httpx.RequestError as exc:
            raise LaunchError(f"Could not check the web app at {self.base_url}: {exc}") from exc
        if (
            response.status_code != 200
            or "text/html" not in response.headers.get("content-type", "")
        ):
            raise LaunchError(
                "The backend is running but is not serving the web app. "
                "Build the frontend and restart the existing backend before launching again."
            )

    def _diagnostic(self, message: str) -> str:
        if self.log_path is None:
            return message
        with self.log_path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 6000))
            tail = log.read().decode("utf-8", errors="replace").strip()
        return f"{message}\nBackend log: {self.log_path}" + (f"\n{tail}" if tail else "")

    def close(self) -> None:
        self._http.close()
        process, self.process = self.process, None
        if process is not None:
            if self.stop is not None:
                self.stop.set()
            process.join(timeout=20)
            if process.is_alive():
                print("Backend did not stop gracefully; terminating the owned process.", flush=True)
                self._failed = True
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            if process.is_alive():
                raise LaunchError("The owned backend could not be stopped.")
            process.close()
        if self.log_path is not None and not self._failed:
            self.log_path.unlink(missing_ok=True)
        elif self.log_path is not None:
            print(f"Backend diagnostic log retained at {self.log_path}", flush=True)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._failed = self._failed or exc_type is not None
        self.close()
