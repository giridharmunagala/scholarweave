"""Runtime wiring for the OpenAI Agents SDK.

The SDK's defaults assume the OpenAI cloud and its Responses API. Locally we point the
same SDK at Ollama's OpenAI-compatible ``/v1`` endpoint, which only speaks Chat
Completions and has no tracing backend, so the provider has to be configured
explicitly rather than left to the SDK's global defaults.

Configuration is per-run, not global: ``build_run_config`` returns a ``RunConfig`` the
executor passes to ``Runner.run_streamed``. That keeps two runs with different
providers from fighting over process-wide state, which is what
``set_default_openai_client`` would cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents import ModelProvider, ModelSettings, OpenAIChatCompletionsModel, RunConfig, set_tracing_disabled
from agents.models.interface import Model
from openai import AsyncOpenAI

from backend.config import Settings
from backend.provider_runtime import ModelRuntime, ResolvedModel

# Ollama ignores the key but the OpenAI client refuses to start without one.
OLLAMA_PLACEHOLDER_KEY = "ollama"


class AgentProviderError(RuntimeError):
    """Raised when the configured provider cannot be used as-is."""


@dataclass(slots=True)
class ProviderProfile:
    """Everything the executor needs to know about the active provider."""

    name: str
    base_url: str
    default_model: str | None
    supports_hosted_tools: bool
    supports_strict_schemas: bool
    supports_parallel_tool_calls: bool

    @property
    def is_local(self) -> bool:
        return self.name == "ollama"


class ChatCompletionsProvider(ModelProvider):
    """Resolves model names against one OpenAI-compatible endpoint.

    Ollama's compatibility layer only implements Chat Completions, so every model is
    wrapped in ``OpenAIChatCompletionsModel`` rather than left to the SDK's default
    Responses-API resolution.
    """

    def __init__(self, client: AsyncOpenAI, default_model: str | None) -> None:
        self._client = client
        self._default_model = default_model

    def get_model(self, model_name: str | None) -> Model:
        resolved = model_name or self._default_model
        if not resolved:
            raise AgentProviderError(
                "No model configured. Set a default generation model in Settings or name one on the agent node."
            )
        return OpenAIChatCompletionsModel(model=resolved, openai_client=self._client)


def provider_profile(settings: Settings) -> ProviderProfile:
    if settings.agent_provider == "openai":
        return ProviderProfile(
            name="openai",
            base_url=settings.openai_base_url or "https://api.openai.com/v1",
            default_model=settings.default_generation_model or "gpt-4.1-mini",
            supports_hosted_tools=True,
            supports_strict_schemas=True,
            supports_parallel_tool_calls=True,
        )
    return ProviderProfile(
        name="ollama",
        base_url=f"{settings.ollama_base_url.rstrip('/')}/v1",
        default_model=settings.default_generation_model,
        # Hosted tools are Responses-API only, and Ollama honours neither strict JSON
        # schemas nor parallel tool calls reliably, so the executor must not assume them.
        supports_hosted_tools=False,
        supports_strict_schemas=False,
        supports_parallel_tool_calls=False,
    )


def build_client(settings: Settings) -> AsyncOpenAI:
    profile = provider_profile(settings)
    if profile.name == "openai":
        if not settings.openai_api_key:
            raise AgentProviderError("The OpenAI provider is selected but no API key is configured.")
        return AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=profile.base_url,
            timeout=settings.request_timeout_seconds,
        )
    return AsyncOpenAI(
        api_key=OLLAMA_PLACEHOLDER_KEY,
        base_url=profile.base_url,
        timeout=settings.request_timeout_seconds,
    )


def build_run_config(settings: Settings, *, workflow_name: str = "workflow") -> RunConfig:
    profile = provider_profile(settings)
    tracing_disabled = not (settings.agent_tracing_enabled and profile.name == "openai")
    if tracing_disabled:
        # Without this the SDK tries to upload spans to OpenAI and warns on every run.
        set_tracing_disabled(True)
    return RunConfig(
        model_provider=ChatCompletionsProvider(build_client(settings), profile.default_model),
        model_settings=default_model_settings(settings),
        workflow_name=workflow_name,
        tracing_disabled=tracing_disabled,
    )


def build_run_config_for_resolved(
    settings: Settings, resolved: ResolvedModel, *, workflow_name: str = "workflow"
) -> RunConfig:
    """A per-agent provider avoids global SDK state and permits mixed-provider DAGs."""
    runtime_client = _client_for_resolved(settings, resolved)
    tracing_disabled = not (settings.agent_tracing_enabled and resolved.kind == "openai")
    if tracing_disabled:
        set_tracing_disabled(True)
    return RunConfig(
        model_provider=ChatCompletionsProvider(runtime_client, resolved.model),
        model_settings=default_model_settings_for_kind(resolved.kind),
        workflow_name=workflow_name,
        tracing_disabled=tracing_disabled,
    )


def _client_for_resolved(settings: Settings, resolved: ResolvedModel) -> Any:
    # ModelRuntime centralizes the credential policy; this tiny adapter avoids adding
    # a second copy of the profile/client construction rules to agent nodes.
    return ModelRuntime(None, settings, None).client(resolved)  # type: ignore[arg-type]


def default_model_settings(settings: Settings) -> ModelSettings:
    profile = provider_profile(settings)
    return ModelSettings(parallel_tool_calls=True if profile.supports_parallel_tool_calls else False)


def default_model_settings_for_kind(kind: str) -> ModelSettings:
    return ModelSettings(parallel_tool_calls=True if kind == "openai" else False)


def chat_model_for_resolved(settings: Settings, resolved: ResolvedModel) -> OpenAIChatCompletionsModel:
    return OpenAIChatCompletionsModel(model=resolved.model, openai_client=_client_for_resolved(settings, resolved))


async def verify_resolved_provider(
    settings: Settings, runtime: ModelRuntime, resolved: ResolvedModel
) -> dict[str, Any]:
    """Tests a selected profile/model without exposing its credential."""
    from agents import Agent, Runner, function_tool

    report: dict[str, Any] = {
        "provider": resolved.kind,
        "base_url": resolved.base_url,
        "model": resolved.model,
        "reachable": False,
        "tool_calling": False,
        "detail": "",
    }
    called = False

    @function_tool
    def record_colour(colour: str) -> str:
        nonlocal called
        called = True
        return f"recorded {colour}"

    agent = Agent(
        name="Provider check",
        instructions="Call record_colour exactly once with the user's colour, then reply done.",
        model=chat_model_for_resolved(settings, resolved),
        tools=[record_colour],
        model_settings=default_model_settings_for_kind(resolved.kind),
    )
    try:
        result = await Runner.run(
            agent, "The colour is teal.", run_config=build_run_config_for_resolved(settings, resolved, workflow_name="provider-check"), max_turns=3
        )
    except Exception as exc:  # noqa: BLE001
        report["detail"] = f"{type(exc).__name__}: {exc}"
        return report
    report["reachable"] = True
    report["tool_calling"] = called
    report["detail"] = "Provider answered and called the test tool." if called else f"Provider answered but ignored the tool. Final message: {str(result.final_output)[:200]}"
    return report


async def verify_provider(settings: Settings) -> dict[str, Any]:
    """Live round trip that proves the provider answers and can call a tool.

    Ollama's tool-calling adherence varies by model, so a plain "the endpoint is up"
    check is not enough to know an agent workflow will work.
    """
    from agents import Agent, Runner, function_tool

    profile = provider_profile(settings)
    report: dict[str, Any] = {
        "provider": profile.name,
        "base_url": profile.base_url,
        "model": profile.default_model,
        "reachable": False,
        "tool_calling": False,
        "detail": "",
    }
    if not profile.default_model:
        report["detail"] = "No default generation model is configured."
        return report

    called = False

    @function_tool
    def record_colour(colour: str) -> str:
        """Records the colour the user asked about."""
        nonlocal called
        called = True
        return f"recorded {colour}"

    agent = Agent(
        name="Provider check",
        instructions="Call the record_colour tool exactly once with the colour the user names, then reply 'done'.",
        tools=[record_colour],
    )
    try:
        result = await Runner.run(
            agent,
            "The colour is teal.",
            run_config=build_run_config(settings, workflow_name="provider-check"),
            max_turns=3,
        )
    except Exception as exc:  # noqa: BLE001 - the report is the error channel here
        report["detail"] = f"{type(exc).__name__}: {exc}"
        return report

    report["reachable"] = True
    report["tool_calling"] = called
    report["detail"] = (
        "Provider answered and called the test tool."
        if called
        else f"Provider answered but ignored the tool. Final message: {str(result.final_output)[:200]}"
    )
    return report
