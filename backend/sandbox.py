"""Runs user-supplied Python for the code node in a constrained subprocess.

This is a hardening measure, not a jail. It stops accidents and casual misuse — runaway
loops, memory blowups, stray network calls, writes outside a scratch directory — but a
determined attacker with local access can still get out. The README says so plainly and
so does this docstring, because pretending otherwise would be worse than the limits
themselves.

The child is a fresh ``python -I`` process, so it inherits no environment, no user site
directory, and none of the parent's imported state. Communication is JSON over stdin and
stdout, which keeps the boundary narrow and means nothing the user writes can touch the
FastAPI process directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ENTRYPOINT = "transform"

# Runs inside the child. Kept as source text rather than a module so the child can stay
# isolated (`-I`) without needing the backend package on its path.
_CHILD_RUNNER = r'''
import builtins, io, json, os, resource, sys

def _fail(message, kind="error"):
    sys.stdout.write(json.dumps({"ok": False, "error": message, "kind": kind}))
    sys.stdout.flush()
    os._exit(0)

try:
    payload = json.loads(sys.stdin.read())
except Exception as exc:
    _fail("Sandbox received malformed input: %s" % exc)

limits = payload["limits"]
allowed = set(payload["allowed_imports"])
scratch = payload["scratch_dir"]
entrypoint = payload["entrypoint"]

# --- resource caps ---------------------------------------------------------
memory_bytes = int(limits["memory_mb"]) * 1024 * 1024
cpu_seconds = int(limits["cpu_seconds"])
try:
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(limits["file_bytes"]), int(limits["file_bytes"])))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
except (ValueError, OSError):
    pass

# --- network ---------------------------------------------------------------
def _no_network(*args, **kwargs):
    raise PermissionError("Network access is disabled in the Python node sandbox")

try:
    import socket
    socket.socket = _no_network
    socket.create_connection = _no_network
    socket.socketpair = _no_network
    socket.getaddrinfo = _no_network
except Exception:
    pass

# --- imports ---------------------------------------------------------------
_real_import = builtins.__import__

def _permitted(name):
    if name in allowed:
        return True
    for entry in allowed:
        # A submodule of something allowed outright, e.g. json.decoder.
        if name.startswith(entry + "."):
            return True
        # A parent package needed to reach an allowed dotted entry, e.g. urllib
        # on the way to urllib.parse.
        if entry.startswith(name + "."):
            return True
    return False

def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    # Only user code is policed. Once an allowed module is loading, its own internal
    # imports (_io, encodings, ...) are the standard library's business, not ours.
    importer = (globals or {}).get("__name__")
    if importer == "__sandbox__" and not _permitted(name):
        raise ImportError(
            "Importing '%s' is not allowed in the Python node sandbox. "
            "Allowed imports are configured in Settings." % name
        )
    return _real_import(name, globals, locals, fromlist, level)

# --- filesystem ------------------------------------------------------------
_real_open = builtins.open
_scratch_real = os.path.realpath(scratch)

def _guarded_open(file, mode="r", *args, **kwargs):
    try:
        target = os.path.realpath(file)
    except TypeError:
        raise PermissionError("The Python node sandbox only opens paths inside its scratch directory")
    if not (target == _scratch_real or target.startswith(_scratch_real + os.sep)):
        raise PermissionError(
            "The Python node sandbox may only read and write inside its scratch directory (%s)" % scratch
        )
    return _real_open(file, mode, *args, **kwargs)

os.chdir(scratch)

# Warm the allowlist before the guard goes up so the standard library's own
# transitive imports never reach the guard at all.
for _entry in sorted(allowed):
    try:
        _real_import(_entry)
    except Exception:
        pass

# --- run -------------------------------------------------------------------
stdout_capture = io.StringIO()
namespace = {"__name__": "__sandbox__", "__builtins__": builtins}
real_stdout = sys.stdout
sys.stdout = stdout_capture

builtins.__import__ = _guarded_import
builtins.open = _guarded_open

result = None
error = None
kind = "error"
try:
    exec(compile(payload["code"], "<python node>", "exec"), namespace)
    function = namespace.get(entrypoint)
    if not callable(function):
        raise NameError(
            "The code must define a function named '%s(inputs)'." % entrypoint
        )
    if payload.get("with_config"):
        result = function(payload["inputs"], payload.get("config", {}))
    else:
        result = function(payload["inputs"])
except MemoryError:
    error = "The code ran out of memory (limit %s MB)." % limits["memory_mb"]
    kind = "memory"
except ImportError as exc:
    error = str(exc)
    kind = "import"
except PermissionError as exc:
    error = str(exc)
    kind = "permission"
except BaseException as exc:
    error = "%s: %s" % (type(exc).__name__, exc)
finally:
    builtins.__import__ = _real_import
    builtins.open = _real_open
    sys.stdout = real_stdout

printed = stdout_capture.getvalue()[-20000:]

if error is not None:
    sys.stdout.write(json.dumps({"ok": False, "error": error, "kind": kind, "stdout": printed}))
    sys.stdout.flush()
    os._exit(0)

try:
    # default=str keeps a stray datetime or custom object from failing the whole node;
    # circular references still raise and are reported.
    encoded = json.dumps(result, default=str)
except (TypeError, ValueError) as exc:
    sys.stdout.write(
        json.dumps({"ok": False, "error": "The return value is not JSON-serialisable: %s" % exc, "kind": "error", "stdout": printed})
    )
    sys.stdout.flush()
    os._exit(0)

sys.stdout.write(json.dumps({"ok": True, "result": json.loads(encoded), "stdout": printed}))
sys.stdout.flush()
os._exit(0)
'''


class SandboxError(RuntimeError):
    """Raised when user code fails, is blocked, or exceeds its limits."""

    def __init__(self, message: str, *, kind: str = "error", stdout: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.stdout = stdout


@dataclass(slots=True)
class SandboxLimits:
    timeout_seconds: float = 10.0
    memory_mb: int = 512
    file_bytes: int = 4 * 1024 * 1024

    @property
    def cpu_seconds(self) -> int:
        # CPU time is the backstop for a process that ignores the wall clock, so it is
        # allowed to exceed the timeout slightly rather than firing first.
        return max(1, int(self.timeout_seconds) + 1)


@dataclass(slots=True)
class SandboxResult:
    value: Any
    stdout: str


async def run_python(
    code: str,
    inputs: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    limits: SandboxLimits | None = None,
    allowed_imports: list[str] | None = None,
    entrypoint: str = DEFAULT_ENTRYPOINT,
) -> SandboxResult:
    """Executes an ``entrypoint(inputs)`` or ``entrypoint(inputs, config)`` in a subprocess."""
    resolved_limits = limits or SandboxLimits()
    scratch = Path(tempfile.mkdtemp(prefix="python-node-"))
    payload = json.dumps(
        {
            "code": code,
            "inputs": inputs,
            "config": config or {},
            "with_config": config is not None,
            "entrypoint": entrypoint,
            "scratch_dir": str(scratch),
            "allowed_imports": allowed_imports or [],
            "limits": {
                "memory_mb": resolved_limits.memory_mb,
                "cpu_seconds": resolved_limits.cpu_seconds,
                "file_bytes": resolved_limits.file_bytes,
            },
        }
    )

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _CHILD_RUNNER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(scratch),
        # Its own session, so a timeout can kill everything the code spawned.
        start_new_session=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
    )

    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload.encode("utf-8")),
                timeout=resolved_limits.timeout_seconds,
            )
        except asyncio.TimeoutError:
            _kill_process_group(process)
            await process.wait()
            raise SandboxError(
                f"The code did not finish within {resolved_limits.timeout_seconds:g} seconds.",
                kind="timeout",
            ) from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode and process.returncode < 0:
            raise SandboxError(
                f"The code was killed by signal {-process.returncode}, usually a memory or CPU limit.",
                kind="killed",
            )
        raise SandboxError(detail or "The sandbox produced no output.", kind="crashed")

    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        raise SandboxError(f"The sandbox returned unreadable output: {text[:500]}", kind="crashed") from None

    if not report.get("ok"):
        raise SandboxError(
            report.get("error") or "The code failed.",
            kind=report.get("kind", "error"),
            stdout=report.get("stdout", ""),
        )
    return SandboxResult(value=report.get("result"), stdout=report.get("stdout", ""))


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
