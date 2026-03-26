from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from qoder2api import app as app_module
from qoder2api.runtime_settings import RuntimeSettingsSnapshot
from qoder2api.anthropic_messages import (
    AnthropicMessagesRequest,
    AnthropicStreamTranslator,
    build_anthropic_message_response,
    convert_anthropic_request_to_chat_request,
)
from qoder2api.streaming import ParsedStreamEvent


class _FakeLimiter:
    @asynccontextmanager
    async def slot(self):
        yield


class _FakeRuntimeSettingsManager:
    def __init__(
        self,
        *,
        default_stream_openai: bool = True,
        default_stream_anthropic: bool = False,
        debug_logging_enabled: bool = False,
    ) -> None:
        self._snapshot = RuntimeSettingsSnapshot(
            default_stream_openai=default_stream_openai,
            default_stream_anthropic=default_stream_anthropic,
            debug_logging_enabled=debug_logging_enabled,
        )

    def load(self) -> RuntimeSettingsSnapshot:
        return self._snapshot


async def _collect_streaming_body(response) -> list[str]:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode('utf-8'))
        else:
            chunks.append(chunk)
    return [line for line in ''.join(chunks).splitlines() if line]




class _FakeAnthropicReasoningInvoker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):  # pragma: no cover - reasoning tests use stream_events
        raise AssertionError("run_text should not be used in anthropic reasoning tests")

    async def stream_events(self, **kwargs):
        self.calls.append(kwargs)

        async def _iterator():
            yield ParsedStreamEvent(reasoning_delta="先想一想")
            yield ParsedStreamEvent(reasoning_signature_delta="sig_123")
            yield ParsedStreamEvent(text_delta="答案", finish_reason="stop")

        return _iterator(), "Ultimate"

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

        return _iterator(), "MiniMax-M2.7"


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




class _FakeAnthropicStopInvoker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):  # pragma: no cover - non-stream route now uses stream_events
        raise AssertionError("run_text should not be used in non-stream stop test")

    async def stream_events(self, **kwargs):
        self.calls.append(kwargs)

        async def _iterator():
            yield ParsedStreamEvent(text_delta="你好<END>世界", finish_reason="stop")

        return _iterator(), "GLM-5"


class _FakeAnthropicStopStreamInvoker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):  # pragma: no cover - stream route should not use it
        raise AssertionError("run_text should not be used in stream stop test")

    async def stream_events(self, **kwargs):
        self.calls.append(kwargs)

        async def _iterator():
            yield ParsedStreamEvent(text_delta="你")
            yield ParsedStreamEvent(text_delta="好<END>世界", finish_reason="stop")

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
            "model": "MiniMax-M2.7",
            "stream": False,
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


def test_anthropic_messages_uses_runtime_default_stream_when_omitted(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicTextInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(app_module, "get_runtime_settings_manager", lambda: _FakeRuntimeSettingsManager(default_stream_anthropic=True))

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "GLM-5",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "你好"}],
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    lines = asyncio.run(_collect_streaming_body(response))
    pairs = _extract_sse_pairs(lines)
    assert pairs[0][0] == "message_start"
    assert pairs[-1][0] == "message_stop"


def test_anthropic_messages_non_stream_returns_tool_use(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicToolInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "MiniMax-M2.7",
            "max_tokens": 128,
            "stream": False,
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
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    payload = json.loads(response.body)
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


def test_build_anthropic_message_response_sanitizes_tool_input_for_schema() -> None:
    payload = build_anthropic_message_response(
        message_id="msg_test",
        model="claude-opus-4-6",
        output_text="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '"北京"',
                },
            }
        ],
        finish_reason="tool_calls",
        input_tokens=10,
        output_tokens=10,
        tool_schemas={
            "get_weather": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                },
                "required": ["city"],
            }
        },
    )

    assert payload["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "get_weather",
            "input": {"city": "北京"},
        }
    ]


def test_build_anthropic_message_response_maps_string_to_single_required_field() -> None:
    payload = build_anthropic_message_response(
        message_id="msg_test",
        model="claude-opus-4-6",
        output_text="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "Bash",
                    "arguments": '"pwd"',
                },
            }
        ],
        finish_reason="tool_calls",
        input_tokens=10,
        output_tokens=10,
        tool_schemas={
            "Bash": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            }
        },
    )

    assert payload["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "Bash",
            "input": {"command": "pwd"},
        }
    ]


def test_anthropic_messages_stream_emits_named_events(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicTextInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "GLM-5",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "你好"}],
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    lines = asyncio.run(_collect_streaming_body(response))
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
    }
    assert pairs[2][1]["delta"] == {
        "type": "input_json_delta",
        "partial_json": '{"city":"北京"}',
    }
    assert pairs[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_anthropic_stream_translator_sanitizes_tool_use_input_for_schema() -> None:
    translator = AnthropicStreamTranslator(
        message_id="msg_test",
        prompt_text="请调用工具",
        tool_schemas={
            "get_weather": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                },
                "required": ["city"],
            }
        },
    )
    payloads: list[str] = []
    payloads.extend(translator.start(model="claude-opus-4-6"))
    payloads.extend(
        translator.feed(
            ParsedStreamEvent(
                tool_call_deltas=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather"},
                    },
                    {
                        "index": 0,
                        "type": "function",
                        "function": {"arguments": '"北京"'},
                    },
                ],
                finish_reason="tool_calls",
            )
        )
    )
    payloads.extend(translator.finish())

    lines = [line for line in "".join(payloads).splitlines() if line]
    pairs = _extract_sse_pairs(lines)
    tool_deltas = [
        payload["delta"]["partial_json"]
        for event, payload in pairs
        if event == "content_block_delta" and payload.get("delta", {}).get("type") == "input_json_delta"
    ]

    assert tool_deltas == ['{"city":"北京"}']


def test_anthropic_stream_translator_maps_string_to_single_required_field() -> None:
    translator = AnthropicStreamTranslator(
        message_id="msg_test",
        prompt_text="请调用工具",
        tool_schemas={
            "Bash": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            }
        },
    )
    payloads: list[str] = []
    payloads.extend(translator.start(model="claude-opus-4-6"))
    payloads.extend(
        translator.feed(
            ParsedStreamEvent(
                tool_call_deltas=[
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Bash"},
                    },
                    {
                        "index": 0,
                        "type": "function",
                        "function": {"arguments": '"pwd"'},
                    },
                ],
                finish_reason="tool_calls",
            )
        )
    )
    payloads.extend(translator.finish())

    lines = [line for line in "".join(payloads).splitlines() if line]
    pairs = _extract_sse_pairs(lines)
    tool_deltas = [
        payload["delta"]["partial_json"]
        for event, payload in pairs
        if event == "content_block_delta" and payload.get("delta", {}).get("type") == "input_json_delta"
    ]

    assert tool_deltas == ['{"command":"pwd"}']


def test_anthropic_messages_non_stream_applies_stop_sequences(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicStopInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "GLM-5",
            "max_tokens": 128,
            "stream": False,
            "stop_sequences": ["<END>"],
            "messages": [{"role": "user", "content": "打个招呼"}],
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["stop_reason"] == "stop_sequence"
    assert payload["stop_sequence"] == "<END>"
    assert payload["content"] == [{"type": "text", "text": "你好"}]


def test_anthropic_messages_stream_applies_stop_sequences(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicStopStreamInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "GLM-5",
            "max_tokens": 64,
            "stream": True,
            "stop_sequences": ["<END>"],
            "messages": [{"role": "user", "content": "你好"}],
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    lines = asyncio.run(_collect_streaming_body(response))
    pairs = _extract_sse_pairs(lines)
    text_deltas = [
        payload["delta"]["text"]
        for event, payload in pairs
        if event == "content_block_delta" and payload.get("delta", {}).get("type") == "text_delta"
    ]
    assert "".join(text_deltas) == "你好"
    assert all("<END>" not in item for item in text_deltas)
    assert pairs[-2][1]["delta"]["stop_reason"] == "stop_sequence"
    assert pairs[-2][1]["delta"]["stop_sequence"] == "<END>"


def test_anthropic_messages_non_stream_returns_thinking_block(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicReasoningInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "Ultimate",
            "max_tokens": 128,
            "stream": False,
            "messages": [{"role": "user", "content": "请先思考再回答"}],
            "tools": [
                {
                    "name": "noop",
                    "description": "占位工具",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
            "tool_choice": {"type": "none"},
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["content"][0] == {
        "type": "thinking",
        "thinking": "先想一想",
        "signature": "sig_123",
    }
    assert payload["content"][1] == {"type": "text", "text": "答案"}


def test_anthropic_messages_stream_emits_thinking_events(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicReasoningInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "Ultimate",
            "max_tokens": 128,
            "stream": True,
            "messages": [{"role": "user", "content": "请先思考再回答"}],
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    lines = asyncio.run(_collect_streaming_body(response))
    pairs = _extract_sse_pairs(lines)
    assert [event for event, _ in pairs] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert pairs[1][1]["content_block"] == {"type": "thinking", "thinking": ""}
    assert pairs[2][1]["delta"] == {"type": "thinking_delta", "thinking": "先想一想"}
    assert pairs[3][1]["delta"] == {"type": "signature_delta", "signature": "sig_123"}
    assert pairs[5][1]["content_block"] == {"type": "text", "text": ""}
    assert pairs[6][1]["delta"] == {"type": "text_delta", "text": "答案"}


def test_anthropic_messages_does_not_log_payload_when_debug_logging_disabled(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicTextInvoker()
    log_messages: list[str] = []
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(app_module, "get_runtime_settings_manager", lambda: _FakeRuntimeSettingsManager(debug_logging_enabled=False))
    monkeypatch.setattr(app_module.logger, "info", lambda message, *args: log_messages.append(message % args if args else message))

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "GLM-5",
            "max_tokens": 64,
            "stream": False,
            "messages": [{"role": "user", "content": "你好"}],
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    assert not any("debug_request route=/v1/messages" in item for item in log_messages)


def test_anthropic_messages_logs_raw_anthropic_payload_when_debug_logging_enabled(monkeypatch) -> None:
    fake_invoker = _FakeAnthropicTextInvoker()
    log_messages: list[str] = []
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(app_module, "get_runtime_settings_manager", lambda: _FakeRuntimeSettingsManager(debug_logging_enabled=True))
    monkeypatch.setattr(app_module.logger, "info", lambda message, *args: log_messages.append(message % args if args else message))

    request = app_module.AnthropicMessagesRequest.model_validate(
        {
            "model": "GLM-5",
            "max_tokens": 64,
            "stream": False,
            "messages": [{"role": "user", "content": "你好"}],
        }
    )
    response = asyncio.run(app_module.anthropic_messages(request))

    assert response.status_code == 200
    debug_log = next(item for item in log_messages if item.startswith('debug_request route=/v1/messages payload='))
    assert json.loads(debug_log.split('payload=', 1)[1]) == {
        "model": "GLM-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "你好"}],
        "stream": False,
    }
