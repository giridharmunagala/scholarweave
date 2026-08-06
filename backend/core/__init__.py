"""Shared application primitives with no feature-specific dependencies."""

from backend.core.errors import (
    ApplicationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

__all__ = [
    "ApplicationError",
    "ConflictError",
    "NotFoundError",
    "ValidationError",
]
