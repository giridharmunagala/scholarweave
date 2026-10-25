from __future__ import annotations

import shutil

import pytest

from backend.core.config import Settings
from backend.tests.stub_provider import _stub_server, stub_provider  # noqa: F401  (re-exported fixtures)


@pytest.fixture()
def test_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    runtime_dir = tmp_path_factory.mktemp("workspace")
    settings = Settings(
        data_dir=runtime_dir / "data",
        workspace_dir=runtime_dir / "workspace",
        frontend_dist_dir=runtime_dir / "frontend_dist",
    )
    settings.ensure_directories()
    try:
        yield settings
    finally:
        shutil.rmtree(runtime_dir)
