from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import HTTPConnection

if TYPE_CHECKING:
    from backend.bootstrap import ApplicationServices


def services(connection: HTTPConnection) -> "ApplicationServices":
    return connection.app.state.services
