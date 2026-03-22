from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi.testclient import TestClient

from qoder2api import app as app_module
from qoder2api.anthropic_messages import (
    AnthropicMessagesRequest,
    AnthropicStreamTranslator,
    convert_anthropic_request_to_chat_request,
)
from qoder2api.streaming import ParsedStreamEvent


class _FakeLimiter:
    @asynccontextmanager
    async def slot(self):
        yield


class _FakeAnthropicToolInvoker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):  # pragma: no cover - should not be used here
        raise AssertionError("run_text should not be used when tools are provided")

    async def stream_events(self, **kwargs):
        self.calls.append(kwargs)

        async def _iterator():
            yield ParsedStreamEvent(
                tool_call_deltas=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather"},
                    }
                ]
            )
            yield ParsedStreamEvent(
                tool_call_deltas=[
                    {
                        "index": 0,
                        "type": "function",
                        "function": {"arguments": '{"city":"北京"}'},
                    }
                ],
                finish_reason="tool_calls",
            )

        return _iterator(), "MiniMax-M2.5"


class _FakeAnthropicTextInvoker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):  # pragma: no cover - stream route should not use it
        raise AssertionError("run_text should not be used in stream mode")

    async def stream_events(self, **kwargs):
        self.calls.append(kwargs)

        async def _iterator():
            yield ParsedStreamEvent(text_delta="你")
            yield ParsedStreamEvent(text_delta="好", finish_reason="stop")

        return _iterator(), "GLM-5"


def _extract_sse_pairs(lines: list[str]) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    current_event = "message"
    for line in lines:
        if line.startswith("event: "):
            current_event = line[len("event: ") :]
            continue
        if line.startswith("data: {"):
            pairs.append((current_event, json.loads(line[len("data: ") :])))
    return pairs


def test_convert_anthropic_request_to_chat_request_supports_tools_and_images() -> None:
    request = AnthropicMessagesRequest.model_validate(
        {
            "model": "MiniMax-M2.5",
            "max_tokens": 256,
            "system": "你是一个测试助手。",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "先查天气。"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "get_weather",
                            "input": {"city": "北京"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "晴，20°C",
                        },
                        {"type": "text", "text": "顺便描述这张图。"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aGVsbG8=",
                            },
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "获取天气",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
    )

    chat_request = convert_anthropic_request_to_chat_request(request)

    assert [message.role for message in chat_request.messages] == [
        "system",
        "assistant",
        "tool",
        "user",
    ]
    assert chat_request.messages[1].tool_calls == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": '{"city":"北京"}',
            },
        }
    ]
    assert chat_request.messages[2].tool_call_id == "toolu_1"
    assert chat_request.messages[2].content == "晴，20°C"
    assert chat_request.messages[3].content == [
        {"type": "text", "text": "顺便描述这张图。"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
        },
    ]
    assert chat_request.tools == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    assert chat_request.tool_choice == {"type": "function", "function": {"name": "get_weather"}}


def test_anthropic_messages_non_stream_returns_tool_use(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicToolInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    client = TestClient(app_module.app)
    response = client.post(
        "/v1/messages",
        json={
            "model": "MiniMax-M2.5",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "请调用工具"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "获取天气",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": {"type": "any"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["stop_reason"] == "tool_use"
    assert payload["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "北京"},
        }
    ]
    assert fake_invoker.calls[0]["tool_choice"] == "required"


def test_anthropic_messages_stream_emits_named_events(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicTextInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    client = TestClient(app_module.app)
    with client.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "GLM-5",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "你好"}],
        },
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

    pairs = _extract_sse_pairs(lines)
    assert [event for event, _ in pairs] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert pairs[0][1]["type"] == "message_start"
    assert pairs[2][1]["delta"] == {"type": "text_delta", "text": "你"}
    assert pairs[3][1]["delta"] == {"type": "text_delta", "text": "好"}
    assert pairs[-2][1]["delta"]["stop_reason"] == "end_turn"


def test_anthropic_stream_translator_emits_tool_use_events() -> None:
    translator = AnthropicStreamTranslator(message_id="msg_test", prompt_text="请调用工具")
    payloads: list[str] = []
    payloads.extend(translator.start(model="Kimi-K2.5"))
    payloads.extend(
        translator.feed(
            ParsedStreamEvent(
                tool_call_deltas=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather"},
                    }
                ]
            )
        )
    )
    payloads.extend(
        translator.feed(
            ParsedStreamEvent(
                tool_call_deltas=[
                    {
                        "index": 0,
                        "type": "function",
                        "function": {"arguments": '{"city":"北京"}'},
                    }
                ],
                finish_reason="tool_calls",
            )
        )
    )
    payloads.extend(translator.finish())

    lines = [line for line in "".join(payloads).splitlines() if line]
    pairs = _extract_sse_pairs(lines)
    assert [event for event, _ in pairs] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert pairs[1][1]["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "get_weather",
        "input": {},
    }
    assert pairs[2][1]["delta"] == {
        "type": "input_json_delta",
        "partial_json": '{"city":"北京"}',
    }
    assert pairs[-2][1]["delta"]["stop_reason"] == "tool_use"
