from __future__ import annotations

import asyncio

import pytest

from backend.tools.sandbox import SandboxError, SandboxLimits, run_python

ALLOWED = ["json", "math", "re", "statistics", "datetime"]


def _run(code: str, inputs: dict | None = None, **kwargs):
    return asyncio.run(run_python(code, inputs or {}, allowed_imports=ALLOWED, **kwargs))


def test_transform_receives_inputs_and_returns_json() -> None:
    result = _run("def transform(inputs):\n    return {'total': inputs['a'] + inputs['b']}", {"a": 2, "b": 3})
    assert result.value == {"total": 5}


def test_transform_can_receive_an_optional_config_second_argument() -> None:
    result = _run(
        "def transform(inputs, config):\n    return inputs['value'] + config['increment']",
        {"value": 2},
        config={"increment": 3},
    )
    assert result.value == 5


def test_allowed_imports_work_and_stdout_is_captured() -> None:
    result = _run("import math\ndef transform(inputs):\n    print('working')\n    return math.sqrt(inputs['x'])", {"x": 9})
    assert result.value == 3.0
    assert "working" in result.stdout


def test_imports_outside_the_allowlist_are_blocked() -> None:
    with pytest.raises(SandboxError) as excinfo:
        _run("import subprocess\ndef transform(inputs):\n    return 1")
    assert excinfo.value.kind == "import"
    assert "not allowed" in str(excinfo.value)


def test_dotted_allowlist_entries_reach_their_submodule() -> None:
    result = asyncio.run(
        run_python(
            "import urllib.parse\ndef transform(inputs):\n    return urllib.parse.quote(inputs['s'])",
            {"s": "a b"},
            allowed_imports=["urllib.parse"],
        )
    )
    assert result.value == "a%20b"


def test_network_access_is_refused() -> None:
    with pytest.raises(SandboxError) as excinfo:
        asyncio.run(
            run_python(
                "import socket\ndef transform(inputs):\n    return socket.create_connection(('127.0.0.1', 80))",
                {},
                allowed_imports=["socket"],
            )
        )
    assert excinfo.value.kind == "permission"


def test_filesystem_access_outside_the_scratch_directory_is_refused() -> None:
    with pytest.raises(SandboxError) as excinfo:
        _run("def transform(inputs):\n    return open('/etc/passwd').read()")
    assert excinfo.value.kind == "permission"


def test_scratch_directory_is_writable() -> None:
    result = _run(
        "def transform(inputs):\n"
        "    with open('note.txt', 'w') as handle:\n"
        "        handle.write('ok')\n"
        "    return open('note.txt').read()"
    )
    assert result.value == "ok"


def test_runaway_code_is_stopped_by_the_timeout() -> None:
    with pytest.raises(SandboxError) as excinfo:
        _run("def transform(inputs):\n    while True:\n        pass", limits=SandboxLimits(timeout_seconds=2))
    assert excinfo.value.kind == "timeout"


def test_memory_hogs_are_stopped() -> None:
    with pytest.raises(SandboxError) as excinfo:
        _run(
            "def transform(inputs):\n    return len(bytearray(400 * 1024 * 1024))",
            limits=SandboxLimits(memory_mb=128, timeout_seconds=30),
        )
    assert excinfo.value.kind in {"memory", "killed"}


def test_missing_entrypoint_is_reported_clearly() -> None:
    with pytest.raises(SandboxError, match="must define a function named 'transform"):
        _run("value = 1")


def test_exceptions_are_reported_with_their_type() -> None:
    with pytest.raises(SandboxError, match="ValueError: boom"):
        _run("def transform(inputs):\n    raise ValueError('boom')")


def test_circular_results_are_rejected() -> None:
    with pytest.raises(SandboxError, match="not JSON-serialisable"):
        _run("def transform(inputs):\n    out = []\n    out.append(out)\n    return out")


def test_a_custom_entrypoint_name_can_be_used() -> None:
    result = asyncio.run(
        run_python("def handler(inputs):\n    return 'ok'", {}, allowed_imports=ALLOWED, entrypoint="handler")
    )
    assert result.value == "ok"
