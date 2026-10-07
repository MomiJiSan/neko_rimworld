"""Typed contracts for the plugin-owned, request-scoped agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    raw_arguments: str

    def as_message_value(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments,
            },
        }


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    provider_request_id: str
    model: str
    finish_reason: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Provider continuation data stays in this session, never in audit/UI output.
    reasoning_content: str | None = field(default=None, repr=False)

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content or None,
        }
        if self.tool_calls:
            message["tool_calls"] = [call.as_message_value() for call in self.tool_calls]
        if self.reasoning_content is not None:
            message["reasoning_content"] = self.reasoning_content
        return message

    def as_audit_value(self) -> dict[str, Any]:
        return {
            "provider_request_id": self.provider_request_id,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "content": self.content,
            "tool_calls": [call.as_message_value() for call in self.tool_calls],
            "usage": dict(self.usage),
            "reasoning_metadata": dict(self.reasoning_metadata),
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    call: ToolCall
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    ok: bool
    value: dict[str, Any]
    arguments: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    observation: dict[str, Any] | None = None
    terminal: bool = False
    model_content: str | None = field(default=None, repr=False)
    model_view_metrics: dict[str, Any] = field(default_factory=dict)

    def as_message(self, *, content: str | None = None) -> dict[str, Any]:
        import json

        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "name": self.name,
            "content": content
            if content is not None
            else self.model_content
            if self.model_content is not None
            else json.dumps(self.value, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        }


__all__ = ["AssistantTurn", "PreparedToolCall", "ToolCall", "ToolResult"]
