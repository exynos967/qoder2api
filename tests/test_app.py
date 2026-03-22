from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from qoder2api import app as app_module
from qoder2api.runtime_settings import RuntimeSettingsSnapshot
from qoder2api.streaming import ParsedStreamEvent


class _FakeLimiter:
    @asynccontextmanager
    async def slot(self):
        yield


class _FakeRuntimeSettingsManager:
    def __init__(self, *, default_stream: bool = True) -> None:
        self._snapshot = RuntimeSettingsSnapshot(default_stream=default_stream)

    def load(self) -> RuntimeSettingsSnapshot:
        return self._snapshot


class _FakeStreamingTextInvoker:
    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):
        self.run_calls.append(kwargs)
        return 'OK', 'GLM-5'

    async def stream_events(self, **kwargs):
        self.stream_calls.append(kwargs)

        async def _iterator():
            yield ParsedStreamEvent(text_delta='O')
            yield ParsedStreamEvent(text_delta='K', finish_reason='stop')

        return _iterator(), 'GLM-5'


async def _collect_streaming_body(response) -> list[str]:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode('utf-8'))
        else:
            chunks.append(chunk)
    return [line for line in ''.join(chunks).splitlines() if line]


class _FakeInvoker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):  # pragma: no cover - should not be used in tool tests
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

        return _iterator(), "Kimi-K2.5"


def test_chat_completions_uses_runtime_default_stream_when_omitted(monkeypatch) -> None:
    fake_invoker = _FakeStreamingTextInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(app_module, "get_runtime_settings_manager", lambda: _FakeRuntimeSettingsManager(default_stream=True))

    request = app_module.ChatCompletionRequest.model_validate(
        {
            "model": "GLM-5",
            "messages": [{"role": "user", "content": "请只回复 OK"}],
        }
    )
    response = asyncio.run(app_module.chat_completions(request))

    assert response.status_code == 200
    lines = asyncio.run(_collect_streaming_body(response))
    assert lines[-1] == "data: [DONE]"
    assert fake_invoker.stream_calls
    assert not fake_invoker.run_calls


def test_chat_completions_non_stream_returns_tool_calls(monkeypatch) -> None:
    fake_invoker = _FakeInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.ChatCompletionRequest.model_validate(
        {
            "model": "Kimi-K2.5",
            "stream": False,
            "messages": [{"role": "user", "content": "请调用工具"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": "auto",
        }
    )
    response = asyncio.run(app_module.chat_completions(request))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert payload["choices"][0]["message"]["content"] is None
    assert payload["choices"][0]["message"]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
        }
    ]
    assert fake_invoker.calls[0]["tool_choice"] == "auto"
    assert fake_invoker.calls[0]["messages"] == [{"role": "user", "content": "请调用工具"}]


def test_chat_completions_stream_emits_tool_calls(monkeypatch) -> None:
    fake_invoker = _FakeInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.ChatCompletionRequest.model_validate(
        {
            "model": "Kimi-K2.5",
            "stream": True,
            "messages": [{"role": "user", "content": "请调用工具"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": "auto",
        }
    )
    response = asyncio.run(app_module.chat_completions(request))

    assert response.status_code == 200
    lines = asyncio.run(_collect_streaming_body(response))
    data_payloads = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
    assert data_payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert data_payloads[1]["choices"][0]["delta"]["tool_calls"] == [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather"},
        }
    ]
    assert data_payloads[2]["choices"][0]["delta"]["tool_calls"] == [
        {
            "index": 0,
            "type": "function",
            "function": {"arguments": '{"city":"北京"}'},
        }
    ]
    assert data_payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert lines[-1] == "data: [DONE]"
    assert fake_invoker.calls[0]["messages"] == [{"role": "user", "content": "请调用工具"}]
