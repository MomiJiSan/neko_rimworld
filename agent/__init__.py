"""Public package-local surface for the independent RimWorld agent runtime."""

from .audit import AgentAuditCapacityError, AgentAuditLedger, AgentAuditRegistry, AgentRunState
from .contracts import AssistantTurn, ToolCall, ToolResult
from .handoff import (
    HANDOFF_TOKEN_BUDGET,
    HandoffValidationError,
    attach_agent_handoff,
    count_handoff_tokens,
    validate_submission,
)
from .loop import AgentRuntime
from .model_client import (
    AgentConfigurationError,
    AgentModelSettings,
    ModelClientError,
    OpenAICompatibleModelClient,
    build_agent_model_settings,
    parse_agent_model_settings,
)
from .session import AgentRunSession
from .tools import ToolExecutionError

__all__ = [
    "AgentAuditLedger",
    "AgentAuditCapacityError",
    "AgentAuditRegistry",
    "AgentConfigurationError",
    "AgentModelSettings",
    "AgentRunState",
    "AgentRunSession",
    "AgentRuntime",
    "AssistantTurn",
    "HANDOFF_TOKEN_BUDGET",
    "HandoffValidationError",
    "ModelClientError",
    "OpenAICompatibleModelClient",
    "ToolCall",
    "ToolExecutionError",
    "ToolResult",
    "attach_agent_handoff",
    "build_agent_model_settings",
    "count_handoff_tokens",
    "parse_agent_model_settings",
    "validate_submission",
]
