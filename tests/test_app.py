from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi.testclient import TestClient

from qoder2api import app as app_module
from qoder2api.streaming import ParsedStreamEvent


class _FakeLimiter:
    @asynccontextmanager
    async def slot(self):
        yield


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


def test_chat_completions_non_stream_returns_tool_calls(monkeypatch) -> None:
    fake_invoker = _FakeInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    client = TestClient(app_module.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "Kimi-K2.5",
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
        },
    )

    assert response.status_code == 200
    payload = response.json()
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

    client = TestClient(app_module.app)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
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
        },
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

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
