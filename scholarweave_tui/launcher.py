from __future__ import annotations

import errno
import multiprocessing
import os
import shutil
import socket
import subprocess
import tempfile
import time
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from backend.core.config import RUNTIME_VERSION, Settings
from scholarweave_tui.client import _local_origin
from scholarweave_tui.server import serve


class LaunchError(RuntimeError):
    """Actionable startup or shutdown failure."""


def build_frontend(root: Path) -> None:
    frontend = root / "frontend"
    if not (frontend / "package.json").is_file():
        raise LaunchError(f"Frontend source was not found in {frontend}. Use the repository checkout.")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        raise LaunchError("Node.js/npm was not found. Install Node.js 20+ and reopen the launcher.")
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
    if not (frontend / "dist" / "index.html").is_file():
        raise LaunchError("The frontend build finished without producing frontend/dist/index.html.")


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

    def _ready(self) -> bool:
        try:
            response = self._http.get(f"{self.base_url}/api/health")
        except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError):
            return False
        except httpx.RequestError as exc:
            raise LaunchError(f"Could not check the backend at {self.base_url}: {exc}") from exc
        try:
            health = response.json()
        except ValueError as exc:
            raise LaunchError(f"{self.base_url} is occupied by a service that is not ScholarWeave.") from exc
        if (
            response.status_code != 200
            or not isinstance(health, dict)
            or health.get("status") != "ok"
            or health.get("runtime_version") != RUNTIME_VERSION
            or not isinstance(health.get("database_path"), str)
            or not isinstance(health.get("data_dir"), str)
        ):
            raise LaunchError(f"{self.base_url} is not a compatible ScholarWeave backend.")
        if (
            Path(health["database_path"]).resolve() != self._settings.database_path
            or Path(health["data_dir"]).resolve() != self._settings.data_dir
        ):
            raise LaunchError(
                f"{self.base_url} belongs to a different ScholarWeave library. "
                "Use its matching checkout/configuration or choose a different --api-url port."
            )
        return True

    def __enter__(self) -> BackendSession:
        try:
            if self._ready():
                print(f"Using the existing backend at {self.base_url}; it will stay running.", flush=True)
                return self
            if self.connect_only:
                raise LaunchError(f"No ready backend at {self.base_url}; --connect-only will not start one.")
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
                    print("The backend port is occupied; waiting for that server to become ready...", flush=True)
                    self._wait_ready()
                    print("Using the existing backend; it will stay running.", flush=True)
                    return self
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
            print("Backend ready. Closing this TUI will stop this backend.", flush=True)
            return self
        except BaseException:
            # Context managers are not exited when __enter__ fails.
            self._failed = True
            self.close()
            raise

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
