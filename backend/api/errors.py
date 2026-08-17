from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.core.errors import ApplicationError, ConflictError, NotFoundError, ValidationError
from backend.documents import DocumentProcessingError
from backend.persistence.files import StorageError
from backend.providers.errors import ProviderRuntimeError


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        detail = [
            {
                key: value
                for key, value in error.items()
                if key not in {"input", "ctx"}
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(ApplicationError)
    async def application_error(
        _request: Request,
        exc: ApplicationError,
    ) -> JSONResponse:
        status = 404 if isinstance(exc, NotFoundError) else 409 if isinstance(exc, ConflictError) else 400
        content = {
            "code": exc.code,
            "message": exc.message,
        }
        if isinstance(exc, ValidationError):
            content["issues"] = list(exc.issues)
        return JSONResponse(status_code=status, content=content)

    @app.exception_handler(ProviderRuntimeError)
    @app.exception_handler(DocumentProcessingError)
    @app.exception_handler(StorageError)
    async def feature_error(
        _request: Request,
        exc: Exception,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "feature_error", "message": str(exc)},
        )
