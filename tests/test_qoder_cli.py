import asyncio
import json
from pathlib import Path

import pytest

from qoder2api.qoder_api import QoderAPIClient, QoderAPIError
from qoder2api.qoder_cli import QoderCLIError, QoderCLIInvoker
from qoder2api.prompting import PreparedAttachment
from qoder2api.settings import Settings
from qoder2api.streaming import ParsedStreamEvent
from tests.helpers import prepare_qoder_home


def test_run_text_uses_protocol_client_by_default(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_run_text(*, qoder_home, prompt, attachments, messages, model_info, max_tokens):
        assert qoder_home == settings.qoder_home_path
        assert prompt == "hello"
        assert attachments == []
        assert model_info.id == "auto"
        assert messages == [{"role": "user", "content": "hello"}]
        assert max_tokens == 32
        return "ok"

    invoker.api_client.run_text = fake_run_text  # type: ignore[method-assign]

    output_text, resolved_model = asyncio.run(
        invoker.run_text(
            prompt="hello",
            attachments=[],
            messages=[{"role": "user", "content": "hello"}],
            model="auto",
            max_tokens=32,
        )
    )

    assert isinstance(invoker.api_client, QoderAPIClient)
    assert output_text == "ok"
    assert resolved_model == "auto"


def test_stream_events_uses_protocol_stream(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_stream_chat(*, qoder_home, prompt, attachments, messages, model_info, max_tokens, tools, tool_choice):
        assert qoder_home == settings.qoder_home_path
        assert prompt == "hello"
        assert attachments == []
        assert model_info.id == "auto"
        assert messages == [{"role": "user", "content": "hello"}]
        assert max_tokens == 64
        assert tools is None
        assert tool_choice is None
        yield ParsedStreamEvent(text_delta="o")
        yield ParsedStreamEvent(text_delta="k")
        yield ParsedStreamEvent(text_delta="", finish_reason="stop")

    invoker.api_client.stream_chat = fake_stream_chat  # type: ignore[method-assign]

    async def collect() -> tuple[list[tuple[str, list[dict], str | None]], str]:
        iterator, resolved_model = await invoker.stream_events(
            prompt="hello",
            attachments=[],
            messages=[{"role": "user", "content": "hello"}],
            model="auto",
            max_tokens=64,
        )
        events: list[tuple[str, list[dict], str | None]] = []
        async for event in iterator:
            events.append((event.text_delta, event.tool_call_deltas, event.finish_reason))
        return events, resolved_model

    events, resolved_model = asyncio.run(collect())

    assert events == [("o", [], None), ("k", [], None), ("", [], "stop")]
    assert resolved_model == "auto"


def test_stream_events_keeps_tool_call_deltas(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)
    tools = [{"type": "function", "function": {"name": "get_weather"}}]

    async def fake_stream_chat(*, qoder_home, prompt, attachments, messages, model_info, max_tokens, tools, tool_choice):
        assert tools == [{"type": "function", "function": {"name": "get_weather"}}]
        assert tool_choice == "auto"
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
        yield ParsedStreamEvent(finish_reason="tool_calls")

    invoker.api_client.stream_chat = fake_stream_chat  # type: ignore[method-assign]

    async def collect() -> list[tuple[str, list[dict], str | None]]:
        iterator, _ = await invoker.stream_events(
            prompt="hello",
            attachments=[],
            messages=[{"role": "user", "content": "hello"}],
            model="auto",
            max_tokens=64,
            tools=tools,
            tool_choice="auto",
        )
        items = []
        async for event in iterator:
            items.append((event.text_delta, event.tool_call_deltas, event.finish_reason))
        return items

    items = asyncio.run(collect())

    assert items == [
        (
            "",
            [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather"},
                }
            ],
            None,
        ),
        ("", [], "tool_calls"),
    ]


def test_run_text_rewraps_protocol_error(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_run_text(*, qoder_home, prompt, attachments, messages, model_info, max_tokens):
        raise QoderAPIError(
            "auth failed",
            status_code=401,
            error_type="authentication_error",
        )

    invoker.api_client.run_text = fake_run_text  # type: ignore[method-assign]

    with pytest.raises(QoderCLIError) as exc_info:
        asyncio.run(
            invoker.run_text(
                prompt="hello",
                attachments=[],
                messages=[{"role": "user", "content": "hello"}],
                model="auto",
                max_tokens=32,
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_type == "authentication_error"


def test_run_text_supports_attachments_for_vl_model(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    (qoder_home / '.auth' / 'models').write_text(
        json.dumps(
            {
                'chat': [
                    {'key': 'kmodel', 'display_name': 'Kimi-K2.5', 'is_vl': True},
                ],
                'assistant': [
                    {'key': 'kmodel', 'display_name': 'Kimi-K2.5', 'is_vl': True},
                ],
            }
        ),
        encoding='utf-8',
    )
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)
    attachment = PreparedAttachment(
        url='data:image/png;base64,AAAA',
        display_name='demo.png',
        local_path=tmp_path / 'demo.png',
    )

    async def fake_run_text(*, qoder_home, prompt, attachments, messages, model_info, max_tokens):
        assert qoder_home == settings.qoder_home_path
        assert prompt == 'hello'
        assert attachments == [attachment]
        assert messages == [{"role": "user", "content": "hello"}]
        assert model_info.id == 'kmodel'
        return 'ok'

    invoker.api_client.run_text = fake_run_text  # type: ignore[method-assign]

    output_text, resolved_model = asyncio.run(
        invoker.run_text(
            prompt='hello',
            attachments=[attachment],
            messages=[{"role": "user", "content": "hello"}],
            model='kmodel',
            max_tokens=32,
        )
    )

    assert output_text == 'ok'
    assert resolved_model == 'kmodel'


def test_run_text_rejects_attachments_for_non_vl_model(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)
    attachment = PreparedAttachment(url='data:image/png;base64,AAAA', display_name='demo.png')

    with pytest.raises(QoderCLIError) as exc_info:
        asyncio.run(
            invoker.run_text(
                prompt='hello',
                attachments=[attachment],
                messages=[{"role": "user", "content": "hello"}],
                model='auto',
                max_tokens=32,
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_type == 'invalid_request_error'
    assert '不支持图片输入' in str(exc_info.value)


def test_stream_events_keeps_reasoning_deltas(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_stream_chat(*, qoder_home, prompt, attachments, messages, model_info, max_tokens, tools, tool_choice):
        yield ParsedStreamEvent(reasoning_delta="先分析")
        yield ParsedStreamEvent(reasoning_signature_delta="sig_1")
        yield ParsedStreamEvent(text_delta="答案", finish_reason="stop")

    invoker.api_client.stream_chat = fake_stream_chat  # type: ignore[method-assign]

    async def collect() -> list[tuple[str, str, str, str | None]]:
        iterator, _ = await invoker.stream_events(
            prompt="hello",
            attachments=[],
            messages=[{"role": "user", "content": "hello"}],
            model="auto",
            max_tokens=64,
        )
        items = []
        async for event in iterator:
            items.append((event.text_delta, event.reasoning_delta, event.reasoning_signature_delta, event.finish_reason))
        return items

    items = asyncio.run(collect())

    assert items == [
        ("", "先分析", "", None),
        ("", "", "sig_1", None),
        ("答案", "", "", "stop"),
    ]
