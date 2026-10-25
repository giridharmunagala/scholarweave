from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.core.http import services
from backend.core.path_picker_schemas import PathPickerRequest, PathPickerResponse

router = APIRouter(prefix="/path-picker", tags=["path-picker"])
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def require_local_browser(request: Request) -> None:
    # Vite proxies the Host header, so local development origins can use a different port.
    # The custom header also forces a CORS preflight; this route does not permit CORS.
    try:
        origin = urlsplit(request.headers.get("origin", ""))
        valid = (
            request.url.hostname in _LOOPBACK
            and origin.hostname in _LOOPBACK
            and origin.scheme in {"http", "https"}
            and not origin.username and not origin.password
            and origin.path in {"", "/"} and not origin.query and not origin.fragment
        )
    except ValueError:
        valid = False
    if not valid or request.headers.get("x-scholarweave-picker") != "1":
        raise HTTPException(status_code=403, detail="File manager picking requires the local ScholarWeave browser.")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="File manager picking requires JSON.")


@router.post("", response_model=PathPickerResponse, dependencies=[Depends(require_local_browser)])
def pick_path(payload: PathPickerRequest, container=Depends(services)) -> PathPickerResponse:
    # Synchronous route runs in FastAPI's thread pool, never on the event loop.
    return PathPickerResponse(path=container.path_picker.pick(payload.kind))
