from __future__ import annotations

import inspect

import agents

from backend.runtime.sdk_compat import SUPPORTED_SDK_VERSION, assert_supported_sdk


def test_sdk_version_is_exactly_supported() -> None:
    assert agents.__version__ == SUPPORTED_SDK_VERSION
    assert_supported_sdk()


def test_required_sdk_primitives_are_public() -> None:
    required = {
        "Agent",
        "Runner",
        "RunConfig",
        "RunHooks",
        "RunResult",
        "RunResultStreaming",
        "RunState",
        "FunctionTool",
        "function_tool",
        "Handoff",
        "handoff",
        "InputGuardrail",
        "OutputGuardrail",
        "ToolInputGuardrail",
        "ToolOutputGuardrail",
        "SQLiteSession",
        "Session",
        "SessionSettings",
        "ModelProvider",
        "OpenAIResponsesModel",
        "OpenAIChatCompletionsModel",
        "ModelSettings",
        "RunContextWrapper",
    }
    assert all(getattr(agents, name, None) is not None for name in required)


def test_runner_and_state_support_required_lifecycle_arguments() -> None:
    run_parameters = inspect.signature(agents.Runner.run).parameters
    assert {"context", "hooks", "run_config", "session"} <= set(run_parameters)
    streamed_parameters = inspect.signature(agents.Runner.run_streamed).parameters
    assert {"context", "hooks", "run_config", "session"} <= set(streamed_parameters)
    assert "context_override" in inspect.signature(agents.RunState.from_json).parameters
    assert "context_serializer" in inspect.signature(agents.RunState.to_json).parameters
    assert "mode" in inspect.signature(agents.RunResultStreaming.cancel).parameters


def test_run_item_union_contains_semantic_items() -> None:
    required = {
        "MessageOutputItem",
        "ToolCallItem",
        "ToolCallOutputItem",
        "HandoffCallItem",
        "HandoffOutputItem",
        "ReasoningItem",
        "ToolApprovalItem",
    }
    assert required <= set(dir(agents))
