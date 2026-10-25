from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from threading import Lock
from typing import Literal

from backend.core.errors import ApplicationError, ConflictError


class PathPickerError(ApplicationError):
    code = "path_picker_error"


class PathPickerService:
    """Select paths on the backend desktop without opening or authorizing them."""

    def __init__(self) -> None:
        self._lock = Lock()

    def pick(self, kind: Literal["file", "folder"]) -> str | None:
        if not self._lock.acquire(blocking=False):
            raise ConflictError("A file manager picker is already open. Finish or cancel it first.")
        try:
            try:
                result = subprocess.run(
                    self._command(kind), capture_output=True, text=True, encoding="utf-8",
                    timeout=120, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PathPickerError("File manager picker timed out and was closed. Browse again to retry.") from exc
            except OSError as exc:
                raise PathPickerError("File manager picker is unavailable on the backend desktop.") from exc
            if result.returncode:
                raise PathPickerError(
                    "Could not open the file manager picker on the backend desktop. "
                    "Use a local interactive desktop session; headless or remote sessions may not support dialogs."
                )
            try:
                path = json.loads(result.stdout)
                if path is not None and (not isinstance(path, str) or not os.path.isabs(path)):
                    raise ValueError("Invalid picker result")
            except (ValueError, TypeError) as exc:
                raise PathPickerError("The file manager picker returned an invalid selection.") from exc
            return path
        finally:
            self._lock.release()

    @staticmethod
    def _command(kind: Literal["file", "folder"]) -> list[str]:
        if sys.platform == "win32":
            dialog = (
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$dialog.Description = 'Select a folder'; $dialog.ShowNewFolderButton = $false; "
                "$property = 'SelectedPath';"
                if kind == "folder" else
                "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
                "$dialog.Title = 'Select a file'; $dialog.CheckFileExists = $true; "
                "$dialog.Multiselect = $false; $property = 'FileName';"
            )
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; "
                "Add-Type -AssemblyName System.Windows.Forms; "
                "if (-not [Environment]::UserInteractive) { throw 'No interactive desktop' }; "
                + dialog +
                "try { $value = $null; "
                "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ $value = $dialog.$property }; "
                "ConvertTo-Json -InputObject $value -Compress } finally { $dialog.Dispose() }"
            )
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            return ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded]
        selection = (
            "filedialog.askdirectory(parent=root, mustexist=True)"
            if kind == "folder" else "filedialog.askopenfilename(parent=root)"
        )
        script = (
            "import json, tkinter as tk\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "try:\n"
            f"    value = {selection}\n"
            "    print(json.dumps(value or None))\n"
            "finally:\n"
            "    root.destroy()\n"
        )
        return [sys.executable, "-c", script]
