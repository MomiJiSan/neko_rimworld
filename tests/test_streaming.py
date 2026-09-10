from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from plugin.plugins.neko_rimworld.agent.model_client import (
    AgentModelSettings,
    ModelClientError,
    OpenAICompatibleModelClient,
)
from plugin.plugins.neko_rimworld.agent.streaming import SSEDecoder, StreamAssembler


def event(delta=None, finish=None, usage=None):
    value = {"id": "resp", "model": "test", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value = {"choices": [], "usage": usage}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def client_for(stream, **settings):
    config = replace(AgentModelSettings(base_url="http://localhost/v1", model="test"), **settings)

    def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body.get("stream_options") == {"include_usage": True}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    return OpenAICompatibleModelClient(config, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_fragmented_utf8_and_interleaved_tools_wait_for_usage_and_done():
    one = {"index": 0, "id": "call0", "function": {"name": "get_game_info", "arguments": '{"requests":'}}
    two = {"index": 1, "id": "call1", "function": {"name": "query_game", "arguments": '{"document":"'}}
    body = b": heartbeat\r\n\r\n" + event(
        {"content": "先查看。", "tool_calls": [one, two], "reasoning_content": "must never be stored"}
    )
    body += event(
        {
            "tool_calls": [
                {"index": 1, "function": {"arguments": '{game{paused}}"}'}},
                {"index": 0, "function": {"arguments": "[]}"}},
            ]
        },
        "tool_calls",
    )
    body += event(
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
    )
    body += b"data: [DONE]\r\n\r\n"
    stream = Chunks([body[i : i + 3] for i in range(0, len(body), 3)])
    result = await client_for(stream).complete([], [], timeout=2)
    assert result.content == "先查看。"
    assert json.loads(result.tool_calls[0].raw_arguments) == {"requests": []}
    assert json.loads(result.tool_calls[1].raw_arguments) == {"document": "{game{paused}}"}
    assert result.usage["total_tokens"] == 30 and result.usage["reasoning_tokens"] == 3
    assert "must never" not in json.dumps(result.as_audit_value())
    assert result.reasoning_metadata["present"]
    assert result.diagnostics["phase"] == "completed" and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        event({"tool_calls": [{"index": 0, "id": "x", "function": {"name": "query_game", "arguments": '{"doc'}}]}),
        event({"content": "partial"}, "length") + b"data: [DONE]\n\n",
    ],
)
async def test_truncated_stream_cannot_return_executable_tools(body):
    with pytest.raises(ModelClientError) as error:
        await client_for(Chunks([body])).complete([], [], timeout=2)
    assert error.value.code in {"model_incomplete_stream", "model_output_limit"}


@pytest.mark.asyncio
async def test_length_failure_retains_requested_limit_finish_reason_and_usage():
    body = event({"content": "partial"}, "length") + event(usage={
        "completion_tokens": 32768, "completion_tokens_details": {"reasoning_tokens": 32000}
    }) + b"data: [DONE]\n\n"
    with pytest.raises(ModelClientError) as caught:
        await client_for(Chunks([body])).complete([], [], timeout=2)
    assert caught.value.code == "model_output_limit"
    assert caught.value.diagnostics["finish_reason"] == "length"
    assert caught.value.diagnostics["max_output_tokens"] == 32768
    assert caught.value.usage["reasoning_tokens"] == 32000


@pytest.mark.asyncio
async def test_usage_received_before_incomplete_eof_survives_as_known_usage():
    body = event({"content": "answer"}, "stop") + event(
        usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    )
    with pytest.raises(ModelClientError) as error:
        await client_for(Chunks([body])).complete([], [], timeout=2)
    assert error.value.code == "model_incomplete_stream"
    assert error.value.usage["total_tokens"] == 8


@pytest.mark.asyncio
async def test_provider_stream_error_retains_bounded_redacted_diagnostics():
    api_key = "private-provider-key"
    provider_error = {
        "message": f"upstream quota exhausted; api_key={api_key}",
        "type": "insufficient_quota",
        "param": "model",
        "code": "quota_exceeded",
    }
    body = ("data: " + json.dumps({"error": provider_error}) + "\n\n").encode()

    with pytest.raises(ModelClientError) as caught:
        await client_for(Chunks([body]), api_key=api_key).complete([], [], timeout=2)

    assert caught.value.code == "model_stream_error"
    diagnostics = caught.value.diagnostics["provider_error"]
    assert diagnostics["code"] == "quota_exceeded"
    assert diagnostics["type"] == "insufficient_quota"
    assert diagnostics["param"] == "model"
    assert diagnostics["message"] == "upstream quota exhausted; api_key=<redacted>"
    assert diagnostics["shape"] == "object"
    assert diagnostics["bytes"] > 0
    assert len(diagnostics["sha256"]) == 64
    assert api_key not in json.dumps(caught.value.diagnostics)


@pytest.mark.asyncio
async def test_keepalives_do_not_extend_first_useful_deadline():
    stream = Chunks([b": ping\n\n"] * 30, delay=0.01)
    with pytest.raises(ModelClientError) as error:
        await client_for(stream, first_response_timeout_seconds=0.035).complete([], [], timeout=1)
    assert error.value.code == "model_first_response_timeout" and stream.closed


@pytest.mark.asyncio
async def test_request_absolute_deadline_bounds_a_healthy_stream():
    stream = Chunks([event({"content": "x"})] * 30, delay=0.01)
    with pytest.raises(ModelClientError) as error:
        await client_for(stream, first_response_timeout_seconds=1, stream_idle_timeout_seconds=1).complete(
            [], [], timeout=0.06
        )
    assert error.value.code == "model_request_deadline" and stream.closed


@pytest.mark.asyncio
async def test_idle_timeout_and_external_cancellation_close_response():
    stream = Chunks([event({"content": "x"}), event({"content": "y"})], delay=0.03)
    with pytest.raises(ModelClientError) as error:
        await client_for(stream, stream_idle_timeout_seconds=0.01).complete([], [], timeout=1)
    assert error.value.code == "model_stream_idle_timeout" and stream.closed
    blocked = Chunks([event({"content": "x"})], delay=1)
    client = client_for(blocked)
    task = asyncio.create_task(client.complete([], [], timeout=2))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert blocked.closed and client.last_diagnostics["exception_type"] == "CancelledError"


def test_multiple_data_lines_and_absent_usage():
    decoder = SSEDecoder()
    records = decoder.feed(b'data: {"choices":[],\ndata: "usage":{}}\n\n')
    assembler = StreamAssembler(32, 100)
    assembler.feed(records[0])
    assembler.feed('{"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}')
    assembler.feed("[DONE]")
    assert assembler.payload()["usage"] == {}


def test_sse_bom_and_cr_only_line_boundaries():
    decoder = SSEDecoder()
    assert decoder.feed(b"\xef\xbb\xbfdata: one\r") == []
    assert decoder.feed(b"\rdata: two\r\n\r\n") == ["one", "two"]
