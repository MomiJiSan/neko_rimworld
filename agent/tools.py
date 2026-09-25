"""Private tool registry used only by the plugin-owned agent loop."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..observation import Observation
from .contracts import PreparedToolCall, ToolCall, ToolResult

MAX_TOOL_RESULT_CHARS = 131_072


class ToolExecutionError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, recoverable: bool = True, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.details = details or {}


ToolHandler = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any] | Observation]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    terminal: bool = False
    exclusive: bool = False
    max_result_chars: int = MAX_TOOL_RESULT_CHARS

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True, slots=True)
class ToolSetup:
    tools: tuple[ToolSpec, ...]
    context: str = ""
    references_supported: bool = False
    directory: dict[str, Any] | None = None
    context_blocks: tuple[tuple[str, str], ...] = ()


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if type(spec.max_result_chars) is not int or not 1 <= spec.max_result_chars <= 32 * MAX_TOOL_RESULT_CHARS:
            raise ValueError("invalid private tool result limit")
        if not spec.name or spec.name in self._tools:
            raise ValueError(f"invalid or duplicate private tool name: {spec.name!r}")
        self._tools[spec.name] = spec

    def is_terminal(self, name: str) -> bool:
        spec = self._tools.get(name)
        return bool(spec and spec.terminal)

    def is_exclusive(self, name: str) -> bool:
        spec = self._tools.get(name)
        return bool(spec and spec.exclusive)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in sorted(self._tools)]

    def prepare(self, call: ToolCall) -> PreparedToolCall | ToolResult:
        spec = self._tools.get(call.name)
        if spec is None:
            return self.error_result(
                call,
                "unknown_tool",
                "The requested private tool is not available in this agent runtime.",
                recoverable=True,
            )
        try:
            parsed = json.loads(call.raw_arguments or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.error_result(
                call,
                "invalid_tool_arguments",
                "Tool arguments were not valid JSON.",
                recoverable=True,
            )
        if not isinstance(parsed, dict):
            return self.error_result(
                call,
                "invalid_tool_arguments",
                "Tool arguments must be a JSON object.",
                recoverable=True,
            )
        properties = spec.parameters.get("properties")
        additional = spec.parameters.get("additionalProperties", True)
        if additional is False and isinstance(properties, dict):
            unknown = sorted(set(parsed).difference(properties))
            if unknown:
                return self.error_result(
                    call,
                    "invalid_tool_arguments",
                    "Tool arguments contained unsupported fields.",
                    recoverable=True,
                )
        return PreparedToolCall(call=call, arguments=parsed)

    async def execute(self, prepared: PreparedToolCall) -> ToolResult:
        spec = self._tools[prepared.call.name]
        started = time.monotonic()
        try:
            raw_value = await spec.handler(dict(prepared.arguments))
            observation = raw_value.to_dict() if isinstance(raw_value, Observation) else None
            value = {
                "ok": True,
                "tool": prepared.call.name,
                "data": observation if observation is not None else dict(raw_value),
            }
            if observation is not None:
                payload = observation["response"]["payload"]
                if observation.get("request", {}).get("operation") in {"query", "spatial_query"}:
                    # Preserve full provenance in the audit observation; the model needs only the
                    # operation's data and issues. Keeping the full envelope here made specialized
                    # semantic tools accidentally expose transport/session noise to the model.
                    value["data"] = {
                        "observation_id": observation["observation_id"],
                        "status": payload["status"],
                        "data": payload.get("data"),
                        "issues": payload.get("issues", []),
                    }
                if payload["status"] not in {"ok", "partial"}:
                    value.update(
                        ok=False,
                        recoverable=True,
                        error={"code": payload["error_code"], "message": payload["message"]},
                    )
        except ToolExecutionError as exception:
            return ToolResult(
                call_id=prepared.call.call_id,
                name=prepared.call.name,
                ok=False,
                value={
                    "ok": False,
                    "tool": prepared.call.name,
                    "error": {"code": exception.code, "message": str(exception), "details": exception.details},
                    "recoverable": exception.recoverable,
                },
                arguments=dict(prepared.arguments),
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
            )
        except Exception as exception:
            return ToolResult(
                call_id=prepared.call.call_id,
                name=prepared.call.name,
                ok=False,
                value={
                    "ok": False,
                    "tool": prepared.call.name,
                    "error": {
                        "code": "tool_internal",
                        "message": f"The private tool failed with {type(exception).__name__}.",
                    },
                    "recoverable": True,
                },
                arguments=dict(prepared.arguments),
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
            )

        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded) > spec.max_result_chars:
            return ToolResult(
                call_id=prepared.call.call_id,
                name=prepared.call.name,
                ok=False,
                value={
                    "ok": False,
                    "tool": prepared.call.name,
                    "error": {
                        "code": "tool_result_too_large",
                        "message": "The private tool result exceeded the agent context limit.",
                    },
                    "recoverable": True,
                },
                arguments=dict(prepared.arguments),
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
            )
        return ToolResult(
            call_id=prepared.call.call_id,
            name=prepared.call.name,
            ok=bool(value["ok"]),
            observation=observation,
            terminal=spec.terminal,
            value=value,
            arguments=dict(prepared.arguments),
            duration_ms=round((time.monotonic() - started) * 1000.0, 2),
        )

    @staticmethod
    def error_result(
        call: ToolCall,
        code: str,
        message: str,
        *,
        recoverable: bool,
    ) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            ok=False,
            value={
                "ok": False,
                "tool": call.name,
                "error": {"code": code, "message": message},
                "recoverable": recoverable,
            },
        )


__all__ = ["ToolExecutionError", "ToolRegistry", "ToolSpec", "ToolSetup"]
