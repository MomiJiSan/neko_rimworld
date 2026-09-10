"""Plugin-owned OpenAI-compatible model client for the local agent loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from .contracts import AssistantTurn, ToolCall
from .streaming import SSEDecoder, StreamAssembler, StreamProtocolError

DEFAULT_MODEL_TIMEOUT_SECONDS = 60.0
DEFAULT_RUN_BUDGET_SECONDS = 90.0
DEFAULT_PREFERRED_CYCLES = 4
DEFAULT_MAX_OUTPUT_TOKENS = 32768
DEFAULT_TEMPERATURE = 0.2
MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOOL_CALLS_PER_TURN = 32
MAX_TOOL_ARGUMENT_CHARS = 32_768


class AgentConfigurationError(ValueError):
    """Raised when plugin-owned model settings are unsafe or incomplete."""


class ModelClientError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, diagnostics: dict[str, Any] | None = None, usage: dict[str, int] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}
        self.usage = usage or {}


@dataclass(frozen=True, slots=True)
class AgentModelSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    request_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    run_budget_seconds: float = DEFAULT_RUN_BUDGET_SECONDS
    preferred_cycles: int = DEFAULT_PREFERRED_CYCLES
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    connect_timeout_seconds: float = 10.0
    first_response_timeout_seconds: float = 45.0
    stream_idle_timeout_seconds: float = 20.0
    finalization_reserve_seconds: float = 15.0
    stream_include_usage: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        if not self.base_url:
            return ""
        return self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"

    def public_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_configured": self.api_key_configured,
            "request_timeout_seconds": self.request_timeout_seconds,
            "run_budget_seconds": self.run_budget_seconds,
            "preferred_cycles": self.preferred_cycles,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "first_response_timeout_seconds": self.first_response_timeout_seconds,
            "stream_idle_timeout_seconds": self.stream_idle_timeout_seconds,
            "finalization_reserve_seconds": self.finalization_reserve_seconds,
            "stream_include_usage": self.stream_include_usage,
        }


def _normalize_base_url(value: object, *, api_key: str) -> str:
    if not isinstance(value, str):
        raise AgentConfigurationError("Agent base URL must be text.")
    candidate = value.strip().rstrip("/")
    if not candidate:
        raise AgentConfigurationError("Agent base URL is required.")
    if len(candidate) > 2048:
        raise AgentConfigurationError("Agent base URL is too long.")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentConfigurationError("Agent base URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise AgentConfigurationError("Agent base URL cannot contain credentials, query parameters, or a fragment.")
    loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and api_key and not loopback:
        raise AgentConfigurationError("An API key cannot be sent to a non-loopback HTTP endpoint; use HTTPS.")
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", "")).rstrip("/")


def build_agent_model_settings(
    *,
    base_url: object,
    model: object,
    api_key: object = "",
    request_timeout_seconds: object = DEFAULT_MODEL_TIMEOUT_SECONDS,
    run_budget_seconds: object = DEFAULT_RUN_BUDGET_SECONDS,
    preferred_cycles: object = DEFAULT_PREFERRED_CYCLES,
    max_output_tokens: object = DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: object = DEFAULT_TEMPERATURE,
    connect_timeout_seconds: object = 10.0,
    first_response_timeout_seconds: object = 45.0,
    stream_idle_timeout_seconds: object = 20.0,
    finalization_reserve_seconds: object = 15.0,
    stream_include_usage: object = True,
) -> AgentModelSettings:
    if not isinstance(api_key, str):
        raise AgentConfigurationError("Agent API key must be text.")
    normalized_key = api_key.strip()
    if len(normalized_key) > 4096:
        raise AgentConfigurationError("Agent API key is too long.")
    normalized_base = _normalize_base_url(base_url, api_key=normalized_key)
    if not isinstance(model, str) or not model.strip():
        raise AgentConfigurationError("Agent model is required.")
    normalized_model = model.strip()
    if len(normalized_model) > 256:
        raise AgentConfigurationError("Agent model name is too long.")

    if isinstance(request_timeout_seconds, bool) or not isinstance(request_timeout_seconds, (int, float)):
        raise AgentConfigurationError("Agent request timeout must be a number.")
    request_timeout = float(request_timeout_seconds)
    if not 3.0 <= request_timeout <= 60.0:
        raise AgentConfigurationError("Agent request timeout must be between 3 and 60 seconds.")

    if isinstance(run_budget_seconds, bool) or not isinstance(run_budget_seconds, (int, float)):
        raise AgentConfigurationError("Agent run budget must be a number.")
    run_budget = float(run_budget_seconds)
    if not 10.0 <= run_budget <= 90.0:
        raise AgentConfigurationError("Agent run budget must be between 10 and 90 seconds.")

    if isinstance(preferred_cycles, bool) or not isinstance(preferred_cycles, int):
        raise AgentConfigurationError("Preferred cycles must be an integer.")
    if not 2 <= preferred_cycles <= 12:
        raise AgentConfigurationError("Preferred cycles must be between 2 and 12.")

    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
        raise AgentConfigurationError("Agent output token limit must be an integer.")
    if not 64 <= max_output_tokens <= 131072:
        raise AgentConfigurationError("Agent output token limit must be between 64 and 131072.")

    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise AgentConfigurationError("Agent temperature must be a number.")
    normalized_temperature = float(temperature)
    if not 0.0 <= normalized_temperature <= 2.0:
        raise AgentConfigurationError("Agent temperature must be between 0 and 2.")

    extra = {}
    for name, value, low, high in (
        ("connect_timeout_seconds", connect_timeout_seconds, 1.0, 30.0),
        ("first_response_timeout_seconds", first_response_timeout_seconds, 1.0, 60.0),
        ("stream_idle_timeout_seconds", stream_idle_timeout_seconds, 1.0, 60.0),
        ("finalization_reserve_seconds", finalization_reserve_seconds, 0.0, 30.0),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise AgentConfigurationError(f"{name} must be between {low} and {high} seconds.")
        extra[name] = float(value)
    if not isinstance(stream_include_usage, bool):
        raise AgentConfigurationError("stream_include_usage must be a boolean.")

    return AgentModelSettings(
        base_url=normalized_base,
        model=normalized_model,
        api_key=normalized_key,
        request_timeout_seconds=request_timeout,
        run_budget_seconds=run_budget,
        preferred_cycles=preferred_cycles,
        max_output_tokens=max_output_tokens,
        temperature=normalized_temperature,
        stream_include_usage=stream_include_usage,
        **extra,
    )


def parse_agent_model_settings(config: object) -> tuple[AgentModelSettings, list[str]]:
    root = config if isinstance(config, Mapping) else {}
    plugin_section = root.get("neko_rimworld")
    plugin_config = plugin_section if isinstance(plugin_section, Mapping) else {}
    agent_section = plugin_config.get("agent")
    agent = agent_section if isinstance(agent_section, Mapping) else {}
    if not agent:
        return AgentModelSettings(), []
    base_value = agent.get("base_url", "")
    model_value = agent.get("model", "")
    key_value = agent.get("api_key", "")
    if all(isinstance(value, str) and not value.strip() for value in (base_value, model_value, key_value)):
        return AgentModelSettings(), []
    try:
        return (
            build_agent_model_settings(
                base_url=base_value,
                model=model_value,
                api_key=key_value,
                request_timeout_seconds=agent.get("request_timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS),
                run_budget_seconds=agent.get("run_budget_seconds", DEFAULT_RUN_BUDGET_SECONDS),
                preferred_cycles=agent.get("preferred_cycles", DEFAULT_PREFERRED_CYCLES),
                max_output_tokens=agent.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                temperature=agent.get("temperature", DEFAULT_TEMPERATURE),
                connect_timeout_seconds=agent.get("connect_timeout_seconds", 10.0),
                first_response_timeout_seconds=agent.get("first_response_timeout_seconds", 45.0),
                stream_idle_timeout_seconds=agent.get("stream_idle_timeout_seconds", 20.0),
                finalization_reserve_seconds=agent.get("finalization_reserve_seconds", 15.0),
                stream_include_usage=agent.get("stream_include_usage", True),
            ),
            [],
        )
    except AgentConfigurationError as exception:
        return AgentModelSettings(), [str(exception)]


def parse_usage(value: object) -> dict[str, int]:
    raw = value if isinstance(value, Mapping) else {}
    usage = {
        key: raw[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(raw.get(key)) is int and raw[key] >= 0
    }
    for parent, key in (("prompt_tokens_details", "cached_tokens"), ("completion_tokens_details", "reasoning_tokens")):
        details = raw.get(parent)
        item = details.get(key) if isinstance(details, Mapping) else None
        if type(item) is int and item >= 0:
            usage[key] = item
    return usage


class ModelClient(Protocol):
    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        timeout: float,
    ) -> AssistantTurn: ...


class OpenAICompatibleModelClient:
    """Streaming Chat Completions client with bounded deadlines and native tool assembly."""

    def __init__(
        self,
        settings: AgentModelSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.configured:
            raise AgentConfigurationError("Agent model is not configured.")
        self._settings = settings
        self._transport = transport

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        timeout: float,
    ) -> AssistantTurn:
        self.last_diagnostics = {}
        self.last_usage = {}
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        first_deadline = min(deadline, loop.time() + self._settings.first_response_timeout_seconds)
        diagnostics: dict[str, Any] = {
            "request_id": hashlib.sha256(f"{started}:{id(self)}".encode()).hexdigest()[:24],
            "phase": "starting",
            "stream": True,
            "endpoint_host": urlsplit(self._settings.endpoint).hostname,
            "request_timeout_seconds": timeout,
            "max_output_tokens": self._settings.max_output_tokens,
            "first_response_timeout_seconds": min(timeout, self._settings.first_response_timeout_seconds),
        }
        assembler = StreamAssembler(
            MAX_TOOL_CALLS_PER_TURN,
            MAX_TOOL_ARGUMENT_CHARS,
            redact_values=(self._settings.api_key,),
        )
        decoder = SSEDecoder()

        def elapsed() -> float:
            return round((time.monotonic() - started) * 1000.0, 2)

        async def trace(event: str, info: dict[str, Any]) -> None:
            points = {
                "connection.connect_tcp.started": ("connecting", "connect_started_ms"),
                "connection.connect_tcp.complete": ("connected", "connect_completed_ms"),
                "connection.start_tls.started": ("tls", "tls_started_ms"),
                "connection.start_tls.complete": ("tls_complete", "tls_completed_ms"),
                "http11.send_request_body.complete": ("response_headers", "request_sent_ms"),
                "http2.send_request_body.complete": ("response_headers", "request_sent_ms"),
            }
            if event in points:
                phase, key = points[event]
                diagnostics["phase"] = phase
                diagnostics[key] = elapsed()
            if event.endswith(".failed"):
                error = info.get("exception")
                diagnostics["transport_exception"] = type(error).__name__ if error is not None else "unknown"

        request: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [dict(message) for message in messages],
            "temperature": self._settings.temperature,
            "max_tokens": self._settings.max_output_tokens,
            "stream": True,
        }
        if self._settings.stream_include_usage:
            request["stream_options"] = {"include_usage": True}
        if tools:
            request["tools"] = [dict(tool) for tool in tools]
            request["tool_choice"] = "auto"
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "N.E.K.O-RimWorld-Agent/0.6",
        }
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        useful = False
        bytes_read = 0
        timeout_kind = "model_request_deadline"
        request_scope = None
        try:
            async with asyncio.timeout_at(deadline) as request_scope:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=min(timeout, self._settings.connect_timeout_seconds),
                        read=None,
                        write=min(timeout, 10.0),
                        pool=min(timeout, 10.0),
                    ),
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    diagnostics["phase"] = "response_headers"
                    timeout_kind = (
                        "model_request_deadline" if first_deadline == deadline else "model_first_response_timeout"
                    )
                    async with asyncio.timeout_at(first_deadline):
                        response = await client.send(
                            client.build_request(
                                "POST",
                                self._settings.endpoint,
                                json=request,
                                headers=headers,
                                extensions={"trace": trace},
                            ),
                            stream=True,
                        )
                    try:
                        diagnostics.update(headers_ms=elapsed(), http_status=response.status_code)
                        if not 200 <= response.status_code < 300:
                            raise ModelClientError(
                                "model_http_error", f"The agent model endpoint returned HTTP {response.status_code}."
                            )
                        if "text/event-stream" not in response.headers.get("content-type", "").lower():
                            raise ModelClientError(
                                "model_stream_expected",
                                "The endpoint did not return SSE for stream=true; check provider support.",
                            )
                        iterator = response.aiter_bytes().__aiter__()
                        while not assembler.done:
                            diagnostics["phase"] = "stream_read" if useful else "first_response"
                            wait_until = (
                                min(deadline, loop.time() + self._settings.stream_idle_timeout_seconds)
                                if useful
                                else first_deadline
                            )
                            timeout_kind = (
                                "model_request_deadline"
                                if wait_until == deadline
                                else "model_stream_idle_timeout"
                                if useful
                                else "model_first_response_timeout"
                            )
                            try:
                                async with asyncio.timeout_at(wait_until):
                                    chunk = await anext(iterator)
                            except StopAsyncIteration:
                                records = decoder.feed(b"", final=True)
                                for record in records:
                                    assembler.feed(record)
                                break
                            bytes_read += len(chunk)
                            diagnostics.setdefault("first_chunk_ms", elapsed())
                            if bytes_read > MAX_MODEL_RESPONSE_BYTES:
                                raise ModelClientError(
                                    "model_response_too_large", "The model stream exceeded its byte limit."
                                )
                            for record in decoder.feed(chunk):
                                if assembler.feed(record):
                                    useful = True
                                    diagnostics.setdefault("first_useful_delta_ms", elapsed())
                                    diagnostics["last_useful_delta_ms"] = elapsed()
                        payload = assembler.payload()
                    finally:
                        await response.aclose()
            turn = self._parse_response(payload)
            diagnostics.update(phase="completed", total_ms=elapsed(), response_bytes=bytes_read)
            return replace(turn, diagnostics=diagnostics, reasoning_metadata=assembler.reasoning_metadata())
        except asyncio.CancelledError:
            diagnostics.update(exception_type="CancelledError", total_ms=elapsed(), response_bytes=bytes_read)
            self.last_diagnostics = diagnostics
            self.last_usage = parse_usage(assembler.usage)
            raise
        except Exception as error:
            diagnostics.update(finish_reason=assembler.finish_reason, usage=parse_usage(assembler.usage))
            diagnostics.update(exception_type=type(error).__name__, total_ms=elapsed(), response_bytes=bytes_read)
            if isinstance(error, ModelClientError):
                code, message = error.code, str(error)
            elif isinstance(error, StreamProtocolError):
                diagnostics.update(error.diagnostics)
                code, message = error.code, str(error)
            elif isinstance(error, TimeoutError):
                code = (
                    "model_request_deadline" if request_scope is not None and request_scope.expired() else timeout_kind
                )
                message = f"The model request exceeded its {code.removeprefix('model_')} limit."
            elif isinstance(error, httpx.TimeoutException):
                code, message = (
                    "model_transport_timeout",
                    "The model transport timed out during connection, pool or upload.",
                )
            elif isinstance(error, httpx.HTTPError):
                code, message = (
                    "model_transport",
                    "The model transport disconnected or failed; inspect the request phase.",
                )
            else:
                raise
            raise ModelClientError(code, message, diagnostics=diagnostics, usage=parse_usage(assembler.usage)) from None

    def _parse_response(self, payload: object) -> AssistantTurn:
        if not isinstance(payload, Mapping):
            raise ModelClientError("model_invalid_response", "The agent model response must be an object.")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ModelClientError("model_invalid_response", "The agent model response has no usable choice.")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ModelClientError("model_invalid_response", "The agent model response has no assistant message.")
        content = self._normalize_content(message.get("content"))
        calls_value = message.get("tool_calls")
        calls_raw = calls_value if isinstance(calls_value, list) else []
        if len(calls_raw) > MAX_TOOL_CALLS_PER_TURN:
            raise ModelClientError("model_invalid_response", "The agent model returned too many tool calls.")

        seen_ids: set[str] = set()
        tool_calls: list[ToolCall] = []
        response_id = str(payload.get("id") or "")[:256]
        for index, raw_call in enumerate(calls_raw):
            if not isinstance(raw_call, Mapping):
                continue
            function = raw_call.get("function")
            function_obj = function if isinstance(function, Mapping) else {}
            name = str(function_obj.get("name") or "invalid_tool")[:128]
            arguments_value = function_obj.get("arguments", "{}")
            raw_arguments = (
                arguments_value
                if isinstance(arguments_value, str)
                else json.dumps(arguments_value, ensure_ascii=False, separators=(",", ":"))
            )
            if len(raw_arguments) > MAX_TOOL_ARGUMENT_CHARS:
                raw_arguments = json.dumps({"_error": "arguments_too_large"})
            provider_call_id = str(raw_call.get("id") or "")[:256]
            call_id = provider_call_id
            if not call_id or call_id in seen_ids:
                seed = f"{response_id}:{index}:{name}:{raw_arguments}".encode("utf-8", "replace")
                call_id = f"call_{hashlib.sha256(seed).hexdigest()[:24]}"
            seen_ids.add(call_id)
            tool_calls.append(ToolCall(call_id=call_id, name=name, raw_arguments=raw_arguments))

        usage = parse_usage(payload.get("usage"))

        reasoning_metadata: dict[str, Any] = {}
        for key in ("reasoning_content", "reasoning", "reasoning_details"):
            if key not in message or message[key] is None:
                continue
            encoded = json.dumps(message[key], ensure_ascii=False, default=str)
            reasoning_metadata = {
                "present": True,
                "field": key,
                "chars": len(encoded),
                "sha256": hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest(),
            }
            break

        return AssistantTurn(
            provider_request_id=response_id,
            model=str(payload.get("model") or self._settings.model)[:256],
            finish_reason=str(choice.get("finish_reason") or "")[:128],
            content=content,
            tool_calls=tuple(tool_calls),
            usage=usage,
            reasoning_metadata=reasoning_metadata,
        )

    @staticmethod
    def _normalize_content(value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        if not isinstance(value, list):
            return ""
        parts: list[str] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()


__all__ = [
    "AgentConfigurationError",
    "AgentModelSettings",
    "ModelClient",
    "ModelClientError",
    "OpenAICompatibleModelClient",
    "build_agent_model_settings",
    "parse_agent_model_settings",
]
