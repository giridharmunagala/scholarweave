from __future__ import annotations

from backend.providers.schemas import ProviderModel


class ProviderRuntimeError(RuntimeError):
    pass


class ProviderDiscoveryError(ProviderRuntimeError):
    def __init__(
        self,
        message: str,
        *,
        manual_models: list[ProviderModel],
    ) -> None:
        super().__init__(message)
        self.manual_models = manual_models
