from __future__ import annotations

import socket
import subprocess
import os
import shutil
from argparse import ArgumentTypeError
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from types import SimpleNamespace

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


def install_health(monkeypatch, settings, *, changes=None, status=200, requests=None):
    data = {
        "status": "ok", "runtime_version": RUNTIME_VERSION,
        "database_path": str(settings.database_path), "data_dir": str(settings.data_dir),
    }
    data.update(changes or {})
    def respond(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(status, json=data)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(launcher.httpx, "Client", lambda **kwargs: client)
    return client


def test_existing_backend_is_reused_without_start_or_stop(isolated_library, monkeypatch) -> None:
    requests = []
    client = install_health(monkeypatch, isolated_library, requests=requests)
    with BackendSession("http://127.0.0.1:8000", ROOT_DIR) as session:
        assert session.process is None
        assert session.stop is None
    assert client.is_closed
    assert requests and all(request.url.params.get("check_ocr") == "false" for request in requests)


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
    session = BackendSession("http://127.0.0.1:8000", ROOT_DIR, connect_only=True)
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
            session.check_web()
            assert http.get(f"{origin}/api/agent/conversations").json() == []
            log_path = session.log_path
            with BackendSession(origin, ROOT_DIR) as second:
                assert second.process is None
            assert http.get(f"{origin}/api/health").status_code == 200
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            http.get(f"{origin}/api/health")
    assert session.process is None
    assert log_path is not None and not log_path.exists()


def test_web_launcher_refuses_backend_without_frontend(isolated_library):
    with BackendSession(free_origin(), ROOT_DIR, timeout=20) as session:
        with pytest.raises(LaunchError, match="not serving the web app"):
            session.check_web()


def test_web_mode_reports_browser_failure(isolated_library, monkeypatch):
    from scholarweave_tui import __main__ as entry

    assets = isolated_library.frontend_dist_dir / "assets"
    assets.mkdir(parents=True)
    (isolated_library.frontend_dist_dir / "index.html").write_text("<!doctype html><title>Web</title>")
    origin = free_origin()
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: False)
    monkeypatch.setattr("sys.argv", ["scholarweave", "--web", "--api-url", origin])
    with pytest.raises(SystemExit) as error:
        entry.main()
    assert error.value.code == 1
    with pytest.raises(LaunchError, match="connect-only"):
        with BackendSession(origin, ROOT_DIR, connect_only=True):
            pytest.fail("A browser failure must stop the owned backend")


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


def test_occupied_port_uses_free_port_and_reuses_discovered_backend(isolated_library, monkeypatch) -> None:
    from scholarweave_tui import __main__ as entry

    assets = isolated_library.frontend_dist_dir / "assets"
    assets.mkdir(parents=True)
    (isolated_library.frontend_dist_dir / "index.html").write_text("<!doctype html><title>Dynamic port</title>")
    opened = []
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: opened.append(url) or True)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        origin = f"http://127.0.0.1:{occupied.getsockname()[1]}"
        with BackendSession(origin, ROOT_DIR, timeout=20) as session:
            assert session.base_url != origin
            assert session.process is not None and session.process.is_alive()
            record = json.loads(session.runtime_path.read_text())
            assert record["url"] == session.base_url
            with httpx.Client(trust_env=False) as http:
                assert http.get(f"{session.base_url}/api/health").json()["status"] == "ok"
                assert "Dynamic port" in http.get(session.base_url).text
            with BackendSession(free_origin(), ROOT_DIR) as second:
                assert second.base_url == session.base_url and second.process is None
            monkeypatch.setattr("sys.argv", ["scholarweave", "--web", "--api-url", origin])
            entry.main()
            assert opened == [session.base_url]
        assert session.process is None


def test_concurrent_launchers_discover_one_backend(isolated_library):
    origin = free_origin()
    sessions = [BackendSession(origin, ROOT_DIR, timeout=20) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda session: session.__enter__(), sessions))
        assert sum(session.process is not None for session in sessions) == 1
        assert sessions[0].base_url == sessions[1].base_url
        with httpx.Client(trust_env=False) as http:
            assert http.get(f"{sessions[0].base_url}/api/health").status_code == 200
    finally:
        for session in sessions:
            session.close()


def test_stale_discovery_is_replaced_when_backend_is_absent(isolated_library):
    record = isolated_library.data_dir / "backend-runtime.json"
    record.write_text(json.dumps({"url": free_origin(), "database_path": str(isolated_library.database_path)}))
    preferred = free_origin()
    with BackendSession(preferred, ROOT_DIR, timeout=20) as session:
        assert session.base_url == preferred and session.process is not None
        assert json.loads(record.read_text())["url"] == preferred


def test_incompatible_backend_for_same_database_does_not_fall_back(isolated_library, monkeypatch):
    install_health(monkeypatch, isolated_library, changes={"runtime_version": "old-runtime"})
    session = BackendSession(free_origin(), ROOT_DIR)
    with pytest.raises(LaunchError, match="a second backend will not be started"):
        with session:
            pytest.fail("Must not start another backend on the same database")
    assert session.process is None


@pytest.fixture()
def frontend_build(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    (frontend / "src").mkdir()
    (frontend / "src" / "main.tsx").write_text("original source", encoding="utf-8")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: r"C:\Program Files\nodejs\npm.cmd")
    calls = []

    def build(command, **kwargs):
        calls.append((command, kwargs))
        assets = frontend / "dist" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (frontend / "dist" / "index.html").write_text(
            '<script type="module" src="/assets/app.js"></script>'
            '<link rel="stylesheet" href="/assets/app.css">',
            encoding="utf-8",
        )
        (assets / "app.js").write_text('import("./lazy.js")', encoding="utf-8")
        (assets / "app.css").write_text("body { color: black; }", encoding="utf-8")
        (assets / "lazy.js").write_text("export default 1", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launcher.subprocess, "run", build)
    return frontend, calls


def test_frontend_build_uses_checkout_and_explicit_npm_path(tmp_path, frontend_build) -> None:
    frontend, calls = frontend_build
    build_frontend(tmp_path)
    assert calls == [([r"C:\Program Files\nodejs\npm.cmd", "run", "build"], {
        "cwd": frontend, "check": False,
    })]


def test_frontend_ensure_reuses_only_verified_unchanged_output(tmp_path, frontend_build, monkeypatch) -> None:
    frontend, calls = frontend_build
    build_frontend(tmp_path, force=False)
    (frontend / "tsconfig.tsbuildinfo").write_text("incremental compiler cache", encoding="utf-8")
    (frontend / "vite.config.js").write_text("generated config", encoding="utf-8")
    (frontend / "vite.config.d.ts").write_text("generated types", encoding="utf-8")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    build_frontend(tmp_path, force=False)
    assert len(calls) == 1


def test_explicit_frontend_build_still_forces_a_rebuild(tmp_path, frontend_build) -> None:
    _, calls = frontend_build
    build_frontend(tmp_path)
    build_frontend(tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("relative", [
    "src/main.tsx", "src/new.tsx", "public/favicon.svg", "vite.config.ts", "tsconfig.json",
    "package.json", "package-lock.json", "node_modules/.package-lock.json", ".env.production",
])
def test_frontend_ensure_rebuilds_changed_sources_configuration_and_dependencies(
    tmp_path, frontend_build, relative,
) -> None:
    frontend, calls = frontend_build
    path = frontend / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old", encoding="utf-8")
    build_frontend(tmp_path, force=False)
    stat = path.stat()
    path.write_text("new", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    build_frontend(tmp_path, force=False)
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["added", "deleted", "root-env", "environment"])
def test_frontend_ensure_tracks_added_deleted_files_and_environment(
    tmp_path, frontend_build, monkeypatch, change,
) -> None:
    frontend, calls = frontend_build
    build_frontend(tmp_path, force=False)
    if change == "added":
        (frontend / "src" / "new.tsx").write_text("new module", encoding="utf-8")
    elif change == "deleted":
        (frontend / "src" / "main.tsx").unlink()
    elif change == "root-env":
        (tmp_path / ".env.production").write_text("VITE_FEATURE=enabled", encoding="utf-8")
    else:
        monkeypatch.setenv("VITE_FEATURE", "enabled")
    build_frontend(tmp_path, force=False)
    assert len(calls) == 2


@pytest.mark.parametrize("damaged", [
    "index.html", "assets/app.js", "assets/app.css", "assets/lazy.js", ".scholarweave-build.json",
])
def test_frontend_ensure_rebuilds_incomplete_output(tmp_path, frontend_build, damaged) -> None:
    frontend, calls = frontend_build
    build_frontend(tmp_path, force=False)
    (frontend / "dist" / damaged).unlink()
    build_frontend(tmp_path, force=False)
    assert len(calls) == 2


def test_frontend_ensure_rebuilds_modified_output_even_with_preserved_timestamp(tmp_path, frontend_build) -> None:
    frontend, calls = frontend_build
    build_frontend(tmp_path, force=False)
    bundle = frontend / "dist" / "assets" / "lazy.js"
    stat = bundle.stat()
    bundle.write_text("export default 2", encoding="utf-8")
    os.utime(bundle, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    build_frontend(tmp_path, force=False)
    assert len(calls) == 2


def test_failed_frontend_build_cannot_reuse_leftover_output(tmp_path, frontend_build, monkeypatch) -> None:
    frontend, calls = frontend_build
    build_frontend(tmp_path, force=False)
    successful_build = launcher.subprocess.run
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )
    with pytest.raises(LaunchError, match="Frontend build failed"):
        build_frontend(tmp_path)
    assert not (frontend / "dist" / ".scholarweave-build.json").exists()
    monkeypatch.setattr(launcher.subprocess, "run", successful_build)
    build_frontend(tmp_path, force=False)
    assert len(calls) == 2


def test_frontend_build_rejects_success_without_refreshing_existing_output(
    tmp_path, frontend_build, monkeypatch,
) -> None:
    frontend, _ = frontend_build
    build_frontend(tmp_path)
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )
    with pytest.raises(LaunchError, match="did not refresh"):
        build_frontend(tmp_path)
    assert not (frontend / "dist" / ".scholarweave-build.json").exists()


@pytest.mark.parametrize("missing", ["assets/app.js", "assets/app.css", "module"])
def test_frontend_build_rejects_incomplete_successful_output(
    tmp_path, frontend_build, monkeypatch, missing,
) -> None:
    frontend, _ = frontend_build
    successful_build = launcher.subprocess.run

    def incomplete_build(*args, **kwargs):
        result = successful_build(*args, **kwargs)
        if missing == "module":
            (frontend / "dist" / "index.html").write_text("<html>empty shell</html>", encoding="utf-8")
        else:
            (frontend / "dist" / missing).unlink()
        return result

    monkeypatch.setattr(launcher.subprocess, "run", incomplete_build)
    with pytest.raises(LaunchError, match="complete web bundle"):
        build_frontend(tmp_path, force=False)
    assert not (frontend / "dist" / ".scholarweave-build.json").exists()


def test_frontend_build_does_not_cache_sources_changed_during_build(tmp_path, frontend_build, monkeypatch) -> None:
    frontend, _ = frontend_build
    successful_build = launcher.subprocess.run

    def changing_build(*args, **kwargs):
        result = successful_build(*args, **kwargs)
        (frontend / "src" / "main.tsx").write_text("changed during build", encoding="utf-8")
        return result

    monkeypatch.setattr(launcher.subprocess, "run", changing_build)
    with pytest.raises(LaunchError, match="changed during the build"):
        build_frontend(tmp_path, force=False)
    assert not (frontend / "dist" / ".scholarweave-build.json").exists()


@pytest.mark.parametrize("flags,forces", [
    ([], []), (["--ensure-frontend"], [False]), (["--build-frontend"], [True]),
    (["--ensure-frontend", "--build-frontend"], [True]),
])
def test_launcher_distinguishes_ensure_from_explicit_frontend_build(monkeypatch, flags, forces) -> None:
    from scholarweave_tui import __main__ as entry

    calls = []
    monkeypatch.setattr(launcher, "build_frontend", lambda root, *, force: calls.append(force))
    monkeypatch.setattr(
        launcher, "BackendSession",
        lambda *args, **kwargs: nullcontext(SimpleNamespace(base_url=free_origin(), check_web=lambda: None)),
    )
    monkeypatch.setattr("sys.argv", ["scholarweave", "--web", "--check", *flags])
    entry.main()
    assert calls == forces


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


@pytest.fixture(params=["launch-scholarweave.sh", "launch-scholarweave-web.sh"])
def shell_launcher(tmp_path, monkeypatch, request):
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("Linux launcher tests require a POSIX shell")
    # Git Bash otherwise expands wildcard arguments before the script receives them.
    monkeypatch.setenv("MSYS", f"{os.environ.get('MSYS', '')} noglob")
    root = tmp_path / "checkout with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    script = request.param
    shutil.copyfile(
        ROOT_DIR / "scripts" / script, scripts / script,
    )
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        '#!/bin/sh\n'
        'if [ "$1" = "-c" ]; then\n'
        '    printf "%s\\n" "$2" > dependency-record.txt\n'
        '    exit "${DEPENDENCY_EXIT_CODE:-0}"\n'
        'fi\n'
        'printf "%s\\n" "$PWD" "$@" > launch-record.txt\n'
        'exit "${LAUNCH_EXIT_CODE:-0}"\n',
        encoding="utf-8", newline="\n",
    )
    python.chmod(0o755)
    return shell, root, script


@pytest.mark.parametrize("exit_code", [0, 7])
def test_linux_shell_launcher_forwards_arguments_directory_and_exit_status(
    shell_launcher, tmp_path, exit_code,
) -> None:
    shell, root, script = shell_launcher
    arguments = ["--api-url", "http://127.0.0.1:8001", "--check", "argument with spaces", "*"]
    result = subprocess.run(
        [shell, str(root / "scripts" / script), *arguments],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
        env={**os.environ, "LAUNCH_EXIT_CODE": str(exit_code), "DEPENDENCY_EXIT_CODE": "0"},
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    expected_directory = subprocess.run(
        [shell, "-c", "pwd -P"], cwd=root, capture_output=True, text=True, check=True, timeout=10,
    ).stdout.strip()
    web_options = ["--web"] if script == "launch-scholarweave-web.sh" else []
    assert (root / "launch-record.txt").read_text(encoding="utf-8").splitlines() == [
        expected_directory, "-m", "scholarweave_tui", *web_options, "--ensure-frontend", *arguments,
    ]
    assert (root / "dependency-record.txt").read_text(encoding="utf-8").strip() == (
        "import uvicorn" if web_options else "import textual, uvicorn"
    )
    assert b"\r" not in (ROOT_DIR / "scripts" / script).read_bytes()


@pytest.mark.parametrize("missing", ["venv", "dependencies"])
def test_linux_shell_launcher_reports_missing_setup(shell_launcher, tmp_path, missing) -> None:
    shell, root, script = shell_launcher
    if missing == "venv":
        (root / ".venv" / "bin" / "python").unlink()
    result = subprocess.run(
        [shell, str(root / "scripts" / script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
        env={**os.environ, "DEPENDENCY_EXIT_CODE": "1"},
    )
    assert result.returncode == 1
    install = 'pip install -e "."' if script == "launch-scholarweave-web.sh" else 'pip install -e ".[tui]"'
    assert ("Create .venv" if missing == "venv" else install) in result.stderr
    assert not (root / "launch-record.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows shortcut launcher")
@pytest.mark.parametrize("script", ["launch-scholarweave.ps1", "launch-scholarweave-web.ps1"])
def test_windows_shortcuts_ensure_frontend_without_running_a_build(tmp_path, script) -> None:
    shell = shutil.which("powershell.exe")
    if shell is None:
        pytest.skip("Windows PowerShell is unavailable")
    root = tmp_path / "checkout with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT_DIR / "scripts" / script, scripts / script)
    (root / "fake-python.ps1").write_text(
        '[System.IO.File]::WriteAllLines(\n'
        '    [System.IO.Path]::Combine($env:LAUNCH_TEST_ROOT, "launch-record.txt"), [string[]]$args)\n'
        '$global:LASTEXITCODE = 0\n',
        encoding="utf-8",
    )
    harness = root / "check-launcher.ps1"
    harness.write_text(
        '$ErrorActionPreference = "Stop"\n'
        'function Join-Path {\n'
        '    param([string]$Path, [string]$ChildPath)\n'
        '    if ($ChildPath -eq ".venv\\Scripts\\python.exe") {\n'
        '        if ($Path -ne $env:LAUNCH_TEST_ROOT) { throw "Wrong checkout root" }\n'
        '        return [System.IO.Path]::Combine($Path, "fake-python.ps1")\n'
        '    }\n'
        '    Microsoft.PowerShell.Management\\Join-Path -Path $Path -ChildPath $ChildPath\n'
        '}\n'
        f'& (Join-Path $PSScriptRoot "scripts\\{script}") '
        '-ApiUrl "http://127.0.0.1:8001" -Check -NoPause\n'
        'exit $LASTEXITCODE\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "LAUNCH_TEST_ROOT": str(root)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    web_options = ["--web"] if script == "launch-scholarweave-web.ps1" else []
    assert (root / "launch-record.txt").read_text(encoding="utf-8-sig").splitlines() == [
        "-m", "scholarweave_tui", *web_options, "--ensure-frontend",
        "--api-url", "http://127.0.0.1:8001", "--check",
    ]
