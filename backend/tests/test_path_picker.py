from __future__ import annotations

import json
import os
import base64
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core.errors import ConflictError
from backend.core.http import install_error_handlers
from backend.core.path_picker import PathPickerError, PathPickerService
from backend.core.path_picker_router import router


@pytest.mark.parametrize("kind", ["file", "folder"])
@pytest.mark.parametrize("selected", [None, os.path.abspath("研究-repository")])
def test_picker_selection_and_cancel(monkeypatch, kind, selected):
    process = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(selected), ""))
    monkeypatch.setattr("backend.core.path_picker.subprocess.run", process)
    assert PathPickerService().pick(kind) == selected
    assert process.call_args.kwargs["timeout"] == 120
    assert process.call_args.kwargs["capture_output"] is True
    assert "shell" not in process.call_args.kwargs


@pytest.mark.parametrize("failure", [
    FileNotFoundError("no desktop toolkit"),
    subprocess.TimeoutExpired("picker", 120),
    subprocess.CompletedProcess([], 1, "", "headless"),
    subprocess.CompletedProcess([], 0, "not json", ""),
    subprocess.CompletedProcess([], 0, '"relative"', ""),
])
def test_picker_failure_releases_lock(monkeypatch, failure):
    process = Mock()
    if isinstance(failure, Exception):
        process.side_effect = failure
    else:
        process.return_value = failure
    monkeypatch.setattr("backend.core.path_picker.subprocess.run", process)
    service = PathPickerService()
    with pytest.raises(PathPickerError):
        service.pick("folder")
    process.side_effect = None
    process.return_value = subprocess.CompletedProcess([], 0, "null", "")
    assert service.pick("file") is None


def test_only_one_dialog(monkeypatch):
    process = Mock()
    monkeypatch.setattr("backend.core.path_picker.subprocess.run", process)
    service = PathPickerService()
    with service._lock:
        with pytest.raises(ConflictError):
            service.pick("folder")
    process.assert_not_called()


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_platform_dialog_commands(monkeypatch, kind):
    monkeypatch.setattr("backend.core.path_picker.sys.platform", "win32")
    command = PathPickerService._command(kind)
    assert command[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA"]
    script = base64.b64decode(command[-1]).decode("utf-16le")
    assert ("FolderBrowserDialog" if kind == "folder" else "OpenFileDialog") in script
    assert "$dialog.Dispose()" in script
    monkeypatch.setattr("backend.core.path_picker.sys.platform", "linux")
    command = PathPickerService._command(kind)
    compile(command[-1], "<picker>", "exec")
    assert ("askdirectory" if kind == "folder" else "askopenfilename") in command[-1]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    install_error_handlers(app)
    app.state.services = SimpleNamespace(path_picker=SimpleNamespace(pick=Mock(return_value=None)))
    with TestClient(app, base_url="http://127.0.0.1:1102") as client:
        yield client


def test_endpoint_selection_cancel_and_failure(client):
    picker = client.app.state.services.path_picker.pick
    headers = {"Origin": "http://127.0.0.1:5173", "X-ScholarWeave-Picker": "1"}
    for path in [None, "C:\\code"]:
        picker.return_value = path
        response = client.post("/api/path-picker", json={"kind": "folder"}, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"path": path}
    picker.side_effect = PathPickerError("Desktop unavailable")
    assert client.post("/api/path-picker", json={"kind": "file"}, headers=headers).json() == {
        "code": "path_picker_error", "message": "Desktop unavailable",
    }


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "https://evil.example", "X-ScholarWeave-Picker": "1"},
    {"Origin": "null", "X-ScholarWeave-Picker": "1"},
    {"Origin": "http://127.0.0.1:1102"},
    {"Origin": "http://localhost.evil.example", "X-ScholarWeave-Picker": "1"},
    {"Origin": "http://localhost", "Host": "evil.example", "X-ScholarWeave-Picker": "1"},
])
def test_cross_origin_requests_cannot_open_dialog(client, headers):
    assert client.post("/api/path-picker", json={"kind": "folder"}, headers=headers).status_code == 403
    client.app.state.services.path_picker.pick.assert_not_called()


def test_preflight_and_invalid_payload_do_not_open_dialog(client):
    assert client.options("/api/path-picker", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-scholarweave-picker",
    }).status_code == 405
    headers = {"Origin": "http://localhost", "X-ScholarWeave-Picker": "1"}
    assert client.post("/api/path-picker", json={"kind": "save"}, headers=headers).status_code == 422
    assert client.post("/api/path-picker", content='{"kind":"folder"}', headers={
        **headers, "Content-Type": "text/plain",
    }).status_code == 415
    client.app.state.services.path_picker.pick.assert_not_called()
