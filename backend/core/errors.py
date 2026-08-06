from __future__ import annotations

from collections.abc import Iterable


class ApplicationError(RuntimeError):
    """Base class for expected, user-visible application failures."""

    code = "application_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(ApplicationError):
    code = "not_found"


class ConflictError(ApplicationError):
    code = "conflict"


class ValidationError(ApplicationError):
    code = "validation_error"

    def __init__(self, message: str, *, issues: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.issues = tuple(issues)
