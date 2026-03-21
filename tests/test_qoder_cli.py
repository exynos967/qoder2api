import asyncio
from pathlib import Path

from qoder2api.qoder_cli import QoderCLIInvoker
from qoder2api.settings import Settings
from tests.helpers import prepare_qoder_home


def test_build_cli_command_uses_context_workspace_and_model(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    cli_path = qoder_home / 'bin' / 'qodercli' / 'qodercli-0.1.32'
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text('#!/bin/sh\n', encoding='utf-8')
    settings = Settings(
        qoder_home=str(qoder_home),
        qoder_cli_path=str(cli_path),
        workspace=str(tmp_path / 'workspace'),
    )
    invoker = QoderCLIInvoker(settings)
    context = invoker.select_context()
    command = invoker._build_cli_command(
        context=context,
        prompt='hello',
        model_info=context.models[0],
        max_tokens=100,
        output_format='json',
    )

    assert command[:7] == [
        str(cli_path),
        '-w',
        str(settings.workspace_path),
        '-p',
        'hello',
        '-f',
        'json',
    ]
    assert '--model' in command
    assert '--max-output-tokens' in command
    assert command[command.index('--max-output-tokens') + 1] == '16k'


def test_run_text_reads_qodercli_json_payload(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_run_cli_json_payload(*, context, prompt, model_info, max_tokens):
        assert context.qoder_home == qoder_home
        assert prompt == 'hello'
        assert model_info.id == 'auto'
        assert max_tokens == 32
        return {
            'type': 'result',
            'subtype': 'success',
            'message': {
                'content': [{'type': 'text', 'text': 'ok'}],
            },
        }

    invoker._run_cli_json_payload = fake_run_cli_json_payload  # type: ignore[method-assign]

    output_text, resolved_model = asyncio.run(
        invoker.run_text(
            prompt='hello',
            attachments=[],
            model='auto',
            max_tokens=32,
        )
    )

    assert output_text == 'ok'
    assert resolved_model == 'auto'


def test_stream_events_parses_qodercli_stream_json(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    invoker = QoderCLIInvoker(settings)

    async def fake_iter_cli_stream_payloads(*, context, prompt, model_info, max_tokens):
        assert context.qoder_home == qoder_home
        assert prompt == 'hello'
        assert model_info.id == 'auto'
        assert max_tokens == 64
        yield {
            'type': 'system',
            'subtype': 'init',
            'message': {'content': []},
        }
        yield {
            'type': 'assistant',
            'subtype': 'message',
            'message': {
                'content': [{'type': 'text', 'text': 'o'}],
            },
        }
        yield {
            'type': 'assistant',
            'subtype': 'message',
            'message': {
                'content': [{'type': 'text', 'text': 'ok'}],
            },
        }
        yield {
            'type': 'result',
            'subtype': 'success',
            'message': {
                'content': [{'type': 'text', 'text': 'ok'}],
            },
        }

    invoker._iter_cli_stream_payloads = fake_iter_cli_stream_payloads  # type: ignore[method-assign]

    async def collect() -> tuple[list[tuple[str, str | None]], str]:
        iterator, resolved_model = await invoker.stream_events(
            prompt='hello',
            attachments=[],
            model='auto',
            max_tokens=64,
        )
        events: list[tuple[str, str | None]] = []
        async for event in iterator:
            events.append((event.text_delta, event.finish_reason))
        return events, resolved_model

    events, resolved_model = asyncio.run(collect())

    assert events == [('o', None), ('k', None), ('', 'stop')]
    assert resolved_model == 'auto'
