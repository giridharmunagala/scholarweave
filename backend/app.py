from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.agents.router import router as agents_router
from backend.api.errors import install_error_handlers
from backend.bootstrap import ApplicationServices, create_services
from backend.core.config import Settings
from backend.conversations.router import router as conversations_router
from backend.core.router import router as core_router
from backend.providers.router import router as providers_router
from backend.research.router import router as research_router
from backend.runs.router import router as runs_router
from backend.tools.router import router as tools_router
from backend.workspace.router import router as workspace_router


def create_app(settings: Settings | None = None) -> FastAPI:
    container = create_services(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await container.close()

    app = FastAPI(
        title="ScholarWeave API",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.services = container
    install_error_handlers(app)

    prefix = container.settings.api_prefix
    for router in (
        core_router,
        providers_router,
        agents_router,
        tools_router,
        conversations_router,
        runs_router,
        research_router,
        workspace_router,
    ):
        app.include_router(router, prefix=prefix)

    _mount_frontend(app, container)
    return app


def _mount_frontend(app: FastAPI, container: ApplicationServices) -> None:
    frontend = container.settings.frontend_dist_dir
    assets = frontend / "assets"
    if not frontend.exists() or not assets.is_dir():
        return
    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> Any:
        base = frontend.resolve()
        candidate = (base / full_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=404, detail="Frontend path was not found.")
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        index = base / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="Frontend build was not found.")


app = create_app()

__all__ = ["app", "create_app"]
