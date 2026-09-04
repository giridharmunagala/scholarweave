from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

SUPPORTED_SDK_VERSION = "0.19.4"
SDK_DISTRIBUTION = "openai-agents"


def installed_sdk_version() -> str:
    try:
        return version(SDK_DISTRIBUTION)
    except PackageNotFoundError as exc:
        raise RuntimeError("The OpenAI Agents SDK is not installed.") from exc


def assert_supported_sdk() -> None:
    installed = installed_sdk_version()
    if installed != SUPPORTED_SDK_VERSION:
        raise RuntimeError(
            f"ScholarWeave requires {SDK_DISTRIBUTION}=={SUPPORTED_SDK_VERSION}; "
            f"found {installed}."
        )
