from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from backend.bootstrap import ApplicationServices


def services(request: Request) -> "ApplicationServices":
    return request.app.state.services
