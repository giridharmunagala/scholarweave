from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.config import Settings
from backend.tests.stub_provider import stub_provider  # noqa: F401  (re-exported fixture)


RUNTIME_ROOT = Path(__file__).resolve().parent / "_runtime"


@pytest.fixture()
def test_settings(request: pytest.FixtureRequest) -> Settings:
    runtime_dir = RUNTIME_ROOT / request.node.name
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=runtime_dir / "data",
        workspace_dir=runtime_dir / "workspace",
        frontend_dist_dir=runtime_dir / "frontend_dist",
    )
    settings.ensure_directories()
    yield settings
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
