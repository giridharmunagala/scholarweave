from __future__ import annotations

import importlib.util
import tomllib

from backend.core.config import ROOT_DIR, Settings


def test_sdk_cutover_has_no_parallel_runtime_modules() -> None:
    backend = ROOT_DIR / "backend"
    retired = [
        "agent_graph.py",
        "agent_nodes.py",
        "agent_runtime.py",
        "agent_tools.py",
        "builder_agent.py",
        "conditions.py",
        "custom_nodes.py",
        "engine.py",
        "nodes.py",
        "ports.py",
        "registry.py",
        "schemas.py",
        "subagents.py",
        "workflows.py",
    ]

    assert [name for name in retired if (backend / name).exists()] == []
    assert list((backend / "agent_framework").glob("*.py")) == []


def test_native_runtime_has_no_agent_sdk_dependency() -> None:
    backend = ROOT_DIR / "backend"
    retired_modules = [
        backend / "agents" / "sdk.py",
        backend / "agents" / "guardrails.py",
        backend / "providers" / "sdk_models.py",
    ]
    pyproject = tomllib.loads((ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert [path.name for path in retired_modules if path.exists()] == []
    assert not any(name.startswith("openai-agents") for name in dependencies)
    assert any(name.startswith("openai") for name in dependencies)
    assert importlib.util.find_spec("agents") is None
    assert [
        path
        for path in backend.rglob("*.py")
        if "_runtime" not in path.parts
        and any(
            line.startswith(("from agents", "import agents", "from agents."))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ] == []


def test_relocated_config_keeps_repository_relative_defaults() -> None:
    settings = Settings()

    assert settings.data_dir == (ROOT_DIR / "local_data").resolve()
    assert settings.workspace_dir == (ROOT_DIR / "workspace").resolve()
    assert settings.frontend_dist_dir == (ROOT_DIR / "frontend" / "dist").resolve()
    assert {
        "default_generation_model",
        "default_embedding_model",
    }.isdisjoint(Settings.model_fields)
