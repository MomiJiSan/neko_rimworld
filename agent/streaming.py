"""Bounded SSE framing and native tool-call assembly. No tool executes here."""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..state import redact_text


class StreamProtocolError(ValueError):
    def __init__(self, code: str, message: str, *, diagnostics: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = dict(diagnostics or {})


def _provider_error_diagnostics(value: object, redact_values: Sequence[str]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        encoded = type(value).__name__.encode("utf-8")

    def safe_text(raw: object, limit: int) -> str | None:
        if raw is None or isinstance(raw, (Mapping, list, tuple, set)):
            return None
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(raw)).strip()
        for secret in redact_values:
            if secret:
                text = text.replace(secret, "<redacted>")
        return redact_text(text, limit)

    source = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {
        "shape": "object" if isinstance(value, Mapping) else type(value).__name__,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    for key, limit in (("code", 128), ("type", 128), ("param", 128), ("message", 512)):
        field = safe_text(source.get(key), limit) if source else safe_text(value, limit) if key == "message" else None
        if field:
            result[key] = field
    return result


class SSEDecoder:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self.buffer = ""
        self.data: list[str] = []

    def feed(self, chunk: bytes, *, final: bool = False) -> list[str]:
        try:
            self.buffer += self.decoder.decode(chunk, final=final)
        except UnicodeDecodeError as error:
            raise StreamProtocolError("model_invalid_stream", "The stream contained invalid UTF-8.") from error
        if final and self.buffer:
            self.buffer += "\n"
        records = []
        while True:
            boundary = re.search(r"\r\n|\r|\n", self.buffer)
            if boundary is None or (boundary.group() == "\r" and boundary.end() == len(self.buffer) and not final):
                break
            line = self.buffer[: boundary.start()]
            self.buffer = self.buffer[boundary.end() :]
            if not line:
                if self.data:
                    records.append("\n".join(self.data))
                    self.data.clear()
            elif line.startswith("data:"):
                value = line[5:]
                self.data.append(value[1:] if value.startswith(" ") else value)
        if final and self.data:
            records.append("\n".join(self.data))
            self.data.clear()
        return records


class StreamAssembler:
    def __init__(self, max_tools: int, max_arguments: int, *, redact_values: Sequence[str] = ()):
        self.max_tools = max_tools
        self.max_arguments = max_arguments
        self.redact_values = tuple(value for value in redact_values if value)
        self.done = False
        self.finish_reason: str | None = None
        self.response_id = ""
        self.model = ""
        self.text: list[str] = []
        self.meaningful_bytes = 0
        self.calls: dict[int, dict[str, str]] = {}
        self.usage: dict[str, Any] = {}
        self.reasoning_chars = 0
        self.reasoning_hash = hashlib.sha256()
        self.reasoning_field = ""
        self.reasoning_content: list[str] = []
        self.has_reasoning_content = False

    def feed(self, record: str) -> bool:
        if self.done:
            raise StreamProtocolError("model_invalid_stream", "Data arrived after the terminal marker.")
        if record.strip() == "[DONE]":
            self.done = True
            return True
        try:
            value = json.loads(record)
        except (ValueError, RecursionError) as error:
            raise StreamProtocolError("model_invalid_stream", "The stream contained an invalid JSON event.") from error
        if not isinstance(value, dict):
            raise StreamProtocolError("model_invalid_stream", "Stream events must be objects.")
        if value.get("error"):
            raise StreamProtocolError(
                "model_stream_error",
                "The provider reported a stream error.",
                diagnostics={"provider_error": _provider_error_diagnostics(value["error"], self.redact_values)},
            )
        if value.get("id"):
            self.response_id = str(value["id"])[:256]
        if value.get("model"):
            self.model = str(value["model"])[:256]
        if isinstance(value.get("usage"), dict):
            self.usage.update(value["usage"])
        choices = value.get("choices", [])
        if not isinstance(choices, list):
            raise StreamProtocolError("model_invalid_stream", "Invalid streamed choices.")
        useful = False
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise StreamProtocolError("model_invalid_stream", "Only one response choice is supported.")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise StreamProtocolError("model_invalid_stream", "Invalid streamed delta.")
            for key in ("reasoning_content", "reasoning", "reasoning_details"):
                if delta.get(key) is not None:
                    if key == "reasoning_content":
                        if not isinstance(delta[key], str):
                            raise StreamProtocolError("model_invalid_stream", "Provider continuation content must be text.")
                        self.has_reasoning_content = True
                        self.reasoning_content.append(delta[key])
                    encoded = json.dumps(delta[key], ensure_ascii=False).encode("utf-8")
                    self.reasoning_chars += len(encoded.decode("utf-8"))
                    self.reasoning_hash.update(encoded)
                    self.reasoning_field = key
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise StreamProtocolError("model_invalid_stream", "Streamed text must be a string.")
                if content:
                    if self.finish_reason:
                        raise StreamProtocolError("model_invalid_stream", "Content arrived after finish_reason.")
                    self.text.append(content)
                    self.meaningful_bytes += len(content.encode("utf-8"))
                    useful = True
            calls = delta.get("tool_calls") or []
            if not isinstance(calls, list):
                raise StreamProtocolError("model_invalid_stream", "Invalid streamed tool calls.")
            for call in calls:
                if self.finish_reason or not isinstance(call, dict):
                    raise StreamProtocolError("model_invalid_stream", "Invalid tool delta after completion.")
                index = call.get("index")
                if type(index) is not int or not 0 <= index < self.max_tools:
                    raise StreamProtocolError("model_invalid_stream", "Tool index exceeds the supported range.")
                if call.get("type") not in {None, "function"}:
                    raise StreamProtocolError("model_invalid_stream", "Unsupported streamed tool type.")
                target = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                fn = call.get("function") or {}
                if not isinstance(fn, dict):
                    raise StreamProtocolError("model_invalid_stream", "Invalid streamed function.")
                for key, fragment in (
                    ("id", call.get("id")),
                    ("name", fn.get("name")),
                    ("arguments", fn.get("arguments")),
                ):
                    if fragment is None:
                        continue
                    if not isinstance(fragment, str):
                        raise StreamProtocolError("model_invalid_stream", "Tool fragments must be strings.")
                    target[key] += fragment
                    self.meaningful_bytes += len(fragment.encode("utf-8"))
                    limit = self.max_arguments if key == "arguments" else 256
                    if len(target[key]) > limit:
                        raise StreamProtocolError(
                            "model_response_too_large", "Streamed tool arguments or identity exceeded the limit."
                        )
                    useful = useful or bool(fragment)
            finish = choice.get("finish_reason")
            if finish:
                self.finish_reason = str(finish)
                useful = True
        return useful

    def payload(self) -> dict[str, Any]:
        if not self.done or not self.finish_reason:
            raise StreamProtocolError(
                "model_incomplete_stream",
                "Stream ended before complete finalization; no partial tool call was executed.",
            )
        if self.finish_reason == "length":
            raise StreamProtocolError("model_output_limit", "The model reached the output token limit before completing its answer.")
        if self.finish_reason not in {"stop", "tool_calls"}:
            raise StreamProtocolError(
                "model_output_incomplete", "The model output was truncated or could not finish normally."
            )
        if self.calls and self.finish_reason != "tool_calls":
            raise StreamProtocolError("model_incomplete_stream", "Tool calls lack a matching completion reason.")
        tools = []
        seen = set()
        for _, call in sorted(self.calls.items()):
            if not call["id"] or not call["name"] or call["id"] in seen:
                raise StreamProtocolError(
                    "model_invalid_stream", "A completed tool call has missing or duplicate identity."
                )
            seen.add(call["id"])
            tools.append(
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
            )
        return {
            "id": self.response_id,
            "model": self.model,
            "usage": self.usage,
            "choices": [
                {"finish_reason": self.finish_reason, "message": {
                    "content": "".join(self.text), "tool_calls": tools,
                    **({"reasoning_content": "".join(self.reasoning_content)} if self.has_reasoning_content else {}),
                }}
            ],
        }

    def reasoning_metadata(self) -> dict[str, Any]:
        return (
            {
                "present": True,
                "field": self.reasoning_field,
                "chars": self.reasoning_chars,
                "sha256": self.reasoning_hash.hexdigest(),
            }
            if self.reasoning_field
            else {}
        )
