"""Private tool registry used only by the plugin-owned agent loop."""

from __future__ import annotations

import json
import time
from typing import Any

from ..observation import Observation
from .contracts import PreparedToolCall, ToolCall, ToolResult
from .model_context import CONTEXT_TOOLS, MAX_SOURCE_CHARS, model_size
from .tool_contracts import (
    MAX_TOOL_RESULT_CHARS as MAX_TOOL_RESULT_CHARS,
)
from .tool_contracts import (
    WORLD_OBSERVATION_VERSIONS as WORLD_OBSERVATION_VERSIONS,
)
from .tool_contracts import (
    ToolExecutionError as ToolExecutionError,
)
from .tool_contracts import (
    ToolHandler as ToolHandler,
)
from .tool_contracts import (
    ToolSetup as ToolSetup,
)
from .tool_contracts import (
    ToolSpec as ToolSpec,
)


class ToolRegistry:
    def __init__(self, *, context_pruner=None, context_enabled=None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._context_pruner = context_pruner
        self._context_enabled = True if context_enabled is None else context_enabled

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
        schemas = [self._tools[name].schema() for name in sorted(self._tools)]
        if self._context_enabled:
            from .context_tools import model_tools

            return model_tools(schemas)
        return schemas

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
        if (call.name in ("look", "count")) and (isinstance(parsed.get("object"), dict)):
            # Preserve explicit selector intent without spending another model
            # round on an unambiguous nesting repair. Conflicts still fail.
            for key in ("include_held", "include_fogged", "map"):
                if key not in parsed:
                    continue
                if key in parsed["object"] and parsed["object"][key] != parsed[key]:
                    return self.error_result(
                        call,
                        "invalid_tool_arguments",
                        "Conflicting selector values at object." + key + " and " + key,
                        recoverable=True,
                    )
                parsed["object"] = {**parsed["object"], key: parsed.pop(key)}
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
        compact = self._context_enabled and spec.name in CONTEXT_TOOLS and not spec.terminal
        over_source = (
            len(encoded) > max(spec.max_result_chars, MAX_SOURCE_CHARS)
            if compact
            else len(encoded) > spec.max_result_chars
        )
        view = None
        over_view = False
        if compact and not over_source:
            if self._context_pruner is not None:
                try:
                    view = self._context_pruner.compile(
                        value, max_view_chars=spec.max_result_chars, arguments=prepared.arguments
                    )
                except ValueError as error:
                    if str(error) != "evidence_view_limit":
                        raise
                    over_view = True
            else:
                over_view = (
                    model_size(value, arguments=prepared.arguments, context_enabled=True)
                    > spec.max_result_chars
                )
        if over_source or over_view:
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
            model_content=view.content if view is not None else None,
            model_view_metrics=view.metrics() if view is not None else {},
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
