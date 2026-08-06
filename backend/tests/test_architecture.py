from __future__ import annotations

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


def test_relocated_config_keeps_repository_relative_defaults() -> None:
    settings = Settings()

    assert settings.data_dir == (ROOT_DIR / "local_data").resolve()
    assert settings.workspace_dir == (ROOT_DIR / "workspace").resolve()
    assert settings.frontend_dist_dir == (ROOT_DIR / "frontend" / "dist").resolve()
    assert {
        "default_generation_model",
        "default_embedding_model",
    }.isdisjoint(Settings.model_fields)
