import asyncio
from pathlib import Path

import pytest

from qoder2api.qoder_api import QoderAPIClient, QoderAPIError
from qoder2api.qoder_cli import QoderCLIError, QoderCLIInvoker
from qoder2api.settings import Settings
from qoder2api.streaming import ParsedStreamEvent
from tests.helpers import prepare_qoder_home


def test_run_text_uses_protocol_client_by_default(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_run_text(*, qoder_home, prompt, model_info, max_tokens):
        assert qoder_home == settings.qoder_home_path
        assert prompt == "hello"
        assert model_info.id == "auto"
        assert max_tokens == 32
        return "ok"

    invoker.api_client.run_text = fake_run_text  # type: ignore[method-assign]

    output_text, resolved_model = asyncio.run(
        invoker.run_text(
            prompt="hello",
            attachments=[],
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

    async def fake_stream_chat(*, qoder_home, prompt, model_info, max_tokens):
        assert qoder_home == settings.qoder_home_path
        assert prompt == "hello"
        assert model_info.id == "auto"
        assert max_tokens == 64
        yield ParsedStreamEvent(text_delta="o")
        yield ParsedStreamEvent(text_delta="k")
        yield ParsedStreamEvent(text_delta="", finish_reason="stop")

    invoker.api_client.stream_chat = fake_stream_chat  # type: ignore[method-assign]

    async def collect() -> tuple[list[tuple[str, str | None]], str]:
        iterator, resolved_model = await invoker.stream_events(
            prompt="hello",
            attachments=[],
            model="auto",
            max_tokens=64,
        )
        events: list[tuple[str, str | None]] = []
        async for event in iterator:
            events.append((event.text_delta, event.finish_reason))
        return events, resolved_model

    events, resolved_model = asyncio.run(collect())

    assert events == [("o", None), ("k", None), ("", "stop")]
    assert resolved_model == "auto"


def test_run_text_rewraps_protocol_error(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_run_text(*, qoder_home, prompt, model_info, max_tokens):
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
                model="auto",
                max_tokens=32,
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_type == "authentication_error"


def test_run_text_rejects_attachments(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    with pytest.raises(QoderCLIError) as exc_info:
        asyncio.run(
            invoker.run_text(
                prompt="hello",
                attachments=[tmp_path / "image.png"],
                model="auto",
                max_tokens=32,
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_type == "invalid_request_error"
