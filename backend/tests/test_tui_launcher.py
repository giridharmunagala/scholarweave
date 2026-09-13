from __future__ import annotations

import socket
import subprocess
import importlib.util
import os
import shutil
from argparse import ArgumentTypeError
from pathlib import Path

import httpx
import pytest

from backend.core.config import ROOT_DIR, RUNTIME_VERSION
from scholarweave_tui import launcher
from scholarweave_tui.__main__ import _positive_timeout
from scholarweave_tui.launcher import BackendSession, LaunchError, build_frontend


@pytest.fixture()
def isolated_library(test_settings, monkeypatch):
    for field in (
        "data_dir", "workspace_dir", "frontend_dist_dir", "artifacts_dir",
        "documents_dir", "database_path", "llm_log_path", "prompt_config_dir",
    ):
        monkeypatch.setenv(f"SCHOLARWEAVE_{field.upper()}", str(getattr(test_settings, field)))
    return test_settings


def free_origin() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def install_health(monkeypatch, settings, *, changes=None, status=200):
    data = {
        "status": "ok", "runtime_version": RUNTIME_VERSION,
        "database_path": str(settings.database_path), "data_dir": str(settings.data_dir),
    }
    data.update(changes or {})
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=data),
    ))
    monkeypatch.setattr(launcher.httpx, "Client", lambda **kwargs: client)
    return client


def test_existing_backend_is_reused_without_start_or_stop(isolated_library, monkeypatch) -> None:
    client = install_health(monkeypatch, isolated_library)
    with BackendSession("http://127.0.0.1:8000", ROOT_DIR) as session:
        assert session.process is None
        assert session.stop is None
    assert client.is_closed


@pytest.mark.parametrize("changes,status", [
    ({"status": "not-ready"}, 200),
    ({"runtime_version": "another-app"}, 200),
    ({"database_path": "different-library.sqlite3"}, 200),
    ({"data_dir": "different-library"}, 200),
    ({"database_path": None}, 200),
    ({}, 404),
])
def test_unrelated_or_incompatible_server_is_not_reused(
    isolated_library, monkeypatch, changes, status,
) -> None:
    client = install_health(monkeypatch, isolated_library, changes=changes, status=status)
    session = BackendSession("http://127.0.0.1:8000", ROOT_DIR)
    with pytest.raises(LaunchError):
        with session:
            pytest.fail("Should not launch a TUI against another service/library")
    assert session.process is None
    assert client.is_closed


def test_connect_only_does_not_spawn_when_server_is_absent(isolated_library) -> None:
    session = BackendSession(free_origin(), ROOT_DIR, connect_only=True)
    with pytest.raises(LaunchError, match="connect-only"):
        with session:
            pytest.fail("An unavailable backend cannot be attached")
    assert session.process is None


def test_owned_backend_starts_serves_frontend_and_stops(isolated_library) -> None:
    assets = isolated_library.frontend_dist_dir / "assets"
    assets.mkdir(parents=True)
    (isolated_library.frontend_dist_dir / "index.html").write_text(
        "<!doctype html><title>ScholarWeave startup test</title>", encoding="utf-8",
    )
    origin = free_origin()
    with httpx.Client(trust_env=False, timeout=2) as http:
        with BackendSession(origin, ROOT_DIR, timeout=20) as session:
            assert session.process is not None and session.process.is_alive()
            assert http.get(f"{origin}/api/health").json()["status"] == "ok"
            assert "ScholarWeave startup test" in http.get(origin).text
            assert http.get(f"{origin}/api/agent/conversations").json() == []
            log_path = session.log_path
            with BackendSession(origin, ROOT_DIR) as second:
                assert second.process is None
            assert http.get(f"{origin}/api/health").status_code == 200
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            http.get(f"{origin}/api/health")
    assert session.process is None
    assert log_path is not None and not log_path.exists()


def failed_server(sock, stop, root, log_path) -> None:
    log_path.write_text("Deliberate startup failure for the test.", encoding="utf-8")
    sock.close()


def waiting_server(sock, stop, root, log_path) -> None:
    stop.wait(20)
    sock.close()


def test_startup_exit_surfaces_log_and_releases_port(isolated_library, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "serve", failed_server)
    origin = free_origin()
    session = BackendSession(origin, ROOT_DIR, timeout=10)
    try:
        with pytest.raises(LaunchError, match="Deliberate startup failure"):
            with session:
                pytest.fail("The failed backend must not be treated as ready")
        assert session.process is None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", int(origin.rsplit(":", 1)[1])))
        assert session.log_path is not None and session.log_path.exists()
    finally:
        if session.log_path is not None:
            session.log_path.unlink(missing_ok=True)


def test_startup_timeout_stops_owned_child(isolated_library, monkeypatch) -> None:
    monkeypatch.setattr(launcher, "serve", waiting_server)
    session = BackendSession(free_origin(), ROOT_DIR, timeout=0.2)
    try:
        with pytest.raises(LaunchError, match="did not become ready"):
            with session:
                pytest.fail("Unresponsive backend must not launch")
        assert session.process is None
    finally:
        if session.log_path is not None:
            session.log_path.unlink(missing_ok=True)


def test_occupied_port_does_not_start_a_second_backend(isolated_library) -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        origin = f"http://127.0.0.1:{occupied.getsockname()[1]}"
        session = BackendSession(origin, ROOT_DIR, timeout=0.2)
        with pytest.raises(LaunchError, match="port may be occupied"):
            with session:
                pytest.fail("The occupied port should not create another backend")
        assert session.process is None


def test_frontend_build_uses_checkout_and_explicit_npm_path(tmp_path, monkeypatch) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: r"C:\Program Files\nodejs\npm.cmd")
    calls = []

    def build(command, **kwargs):
        calls.append((command, kwargs))
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("built", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, "run", build)
    build_frontend(tmp_path)
    assert calls == [([r"C:\Program Files\nodejs\npm.cmd", "run", "build"], {
        "cwd": frontend, "check": False,
    })]


@pytest.mark.parametrize("failure", ["missing-source", "missing-npm", "build-error", "no-output"])
def test_frontend_build_failures_are_explicit(tmp_path, monkeypatch, failure) -> None:
    if failure != "missing-source":
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None if failure == "missing-npm" else "npm")
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 1 if failure == "build-error" else 0,
    ))
    with pytest.raises(LaunchError):
        build_frontend(tmp_path)


@pytest.mark.parametrize("value", ["0", "-1", "601", "nan", "inf"])
def test_startup_timeout_has_finite_bounds(value) -> None:
    with pytest.raises(ArgumentTypeError):
        _positive_timeout(value)


@pytest.mark.skipif(os.name != "nt", reason="Windows shortcut launcher")
def test_windows_shortcut_script_builds_and_checks_from_another_directory(
    isolated_library, tmp_path,
) -> None:
    if (
        shutil.which("npm.cmd") is None
        or importlib.util.find_spec("textual") is None
        or not (ROOT_DIR / "frontend" / "node_modules" / ".bin" / "tsc.cmd").is_file()
    ):
        pytest.skip("Desktop launcher smoke test requires the tui extra and frontend dependencies")
    origin = free_origin()
    command = [
        str(Path(os.environ["WINDIR"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT_DIR / "scripts" / "launch-scholarweave.ps1"),
        "-ApiUrl", origin, "-Check", "-NoPause",
    ]
    result = subprocess.run(
        command, cwd=tmp_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Building the web frontend" in result.stdout
    assert "ScholarWeave startup check passed" in result.stdout
    with pytest.raises(LaunchError, match="connect-only"):
        with BackendSession(origin, ROOT_DIR, connect_only=True):
            pytest.fail("The shortcut must stop the backend that it started")
