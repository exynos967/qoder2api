from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from qoder2api import app as app_module
from qoder2api.registration import QoderRegistrationError, RegistrationSessionSnapshot
from qoder2api.schemas import RegistrationStartRequest
from qoder2api.runtime_settings import RuntimeSettingsSnapshot
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




class _FakeReasoningInvoker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_text(self, **kwargs):  # pragma: no cover - tools path should not use it
        raise AssertionError("run_text should not be used in reasoning tests")

    async def stream_events(self, **kwargs):
        self.calls.append(kwargs)

        async def _iterator():
            yield ParsedStreamEvent(reasoning_delta="先分析一下")
            yield ParsedStreamEvent(text_delta="最终答案", finish_reason="stop")

        return _iterator(), "Ultimate"

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
    monkeypatch.setattr(app_module, "get_runtime_settings_manager", lambda: _FakeRuntimeSettingsManager(default_stream_openai=True))

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


def test_chat_completions_non_stream_returns_reasoning_content(monkeypatch) -> None:
    fake_invoker = _FakeReasoningInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.ChatCompletionRequest.model_validate(
        {
            "model": "Ultimate",
            "stream": False,
            "messages": [{"role": "user", "content": "请先思考再回答"}],
        }
    )
    response = asyncio.run(app_module.chat_completions(request))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["choices"][0]["message"]["reasoning_content"] == "先分析一下"
    assert payload["choices"][0]["message"]["content"] == "最终答案"


def test_chat_completions_stream_emits_reasoning_content(monkeypatch) -> None:
    fake_invoker = _FakeReasoningInvoker()
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())

    request = app_module.ChatCompletionRequest.model_validate(
        {
            "model": "Ultimate",
            "stream": True,
            "messages": [{"role": "user", "content": "请先思考再回答"}],
        }
    )
    response = asyncio.run(app_module.chat_completions(request))

    assert response.status_code == 200
    lines = asyncio.run(_collect_streaming_body(response))
    data_payloads = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
    reasoning_deltas = [
        payload["choices"][0]["delta"].get("reasoning_content", "")
        for payload in data_payloads
        if payload["choices"][0]["delta"].get("reasoning_content")
    ]
    content_deltas = [
        payload["choices"][0]["delta"].get("content", "")
        for payload in data_payloads
        if payload["choices"][0]["delta"].get("content")
    ]
    assert "".join(reasoning_deltas) == "先分析一下"
    assert "".join(content_deltas) == "最终答案"


def test_chat_completions_does_not_log_payload_when_debug_logging_disabled(monkeypatch) -> None:
    fake_invoker = _FakeStreamingTextInvoker()
    log_messages: list[str] = []
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(app_module, "get_runtime_settings_manager", lambda: _FakeRuntimeSettingsManager(debug_logging_enabled=False))
    monkeypatch.setattr(app_module.logger, "info", lambda message, *args: log_messages.append(message % args if args else message))

    request = app_module.ChatCompletionRequest.model_validate(
        {
            "model": "GLM-5",
            "stream": False,
            "messages": [{"role": "user", "content": "请只回复 OK"}],
        }
    )
    response = asyncio.run(app_module.chat_completions(request))

    assert response.status_code == 200
    assert not any("debug_request route=/v1/chat/completions" in item for item in log_messages)


def test_chat_completions_logs_raw_openai_payload_when_debug_logging_enabled(monkeypatch) -> None:
    fake_invoker = _FakeStreamingTextInvoker()
    log_messages: list[str] = []
    monkeypatch.setattr(app_module, "get_invoker", lambda: fake_invoker)
    monkeypatch.setattr(app_module, "get_invocation_limiter", lambda: _FakeLimiter())
    monkeypatch.setattr(app_module, "get_runtime_settings_manager", lambda: _FakeRuntimeSettingsManager(debug_logging_enabled=True))
    monkeypatch.setattr(app_module.logger, "info", lambda message, *args: log_messages.append(message % args if args else message))

    request = app_module.ChatCompletionRequest.model_validate(
        {
            "model": "GLM-5",
            "stream": False,
            "messages": [{"role": "user", "content": "请只回复 OK"}],
        }
    )
    response = asyncio.run(app_module.chat_completions(request))

    assert response.status_code == 200
    debug_log = next(item for item in log_messages if item.startswith('debug_request route=/v1/chat/completions payload='))
    assert json.loads(debug_log.split('payload=', 1)[1]) == {
        "model": "GLM-5",
        "messages": [{"role": "user", "content": "请只回复 OK"}],
        "stream": False,
    }


class _FakeRegistrationSession:
    def __init__(self, snapshot: RegistrationSessionSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> RegistrationSessionSnapshot:
        return self._snapshot


class _FakeRegistrationManager:
    def __init__(self, session: RegistrationSessionSnapshot | None = None) -> None:
        self._session = session
        self.cancelled = False
        self.started_with: int | None = None

    def describe(self) -> dict[str, Any]:
        return {
            'available': True,
            'message': '外部注册 Worker 模式',
            'worker_mode': 'external',
            'project_path': '/root/workdir/qoder/proxy/qoder2api/register',
            'session': self._session.to_public_dict() if self._session else None,
        }

    def current_session(self) -> RegistrationSessionSnapshot | None:
        return self._session

    def start_registration(self, requested_count: int | None = None) -> RegistrationSessionSnapshot:
        self.started_with = requested_count
        if self._session is None:
            current_requested_count = max(1, int(requested_count or 1))
            self._session = RegistrationSessionSnapshot(
                session_id='reg-1',
                status='waiting_worker',
                message=f'等待注册 Worker 处理第 1/{current_requested_count} 个账号...',
                auth_url=None,
                started_at=1.0,
                updated_at=2.0,
                done=False,
                terminal_output='registering...',
                requested_count=current_requested_count,
                success_count=0,
                failure_count=0,
                current_email='demo@example.com',
            )
        return self._session

    def get_session(self, session_id: str) -> _FakeRegistrationSession:
        if self._session is None or self._session.session_id != session_id:
            raise QoderRegistrationError(f'找不到注册会话: {session_id}', status_code=404)
        return _FakeRegistrationSession(self._session)

    def cancel(self, session_id: str) -> RegistrationSessionSnapshot:
        session = self.get_session(session_id).snapshot()
        self.cancelled = True
        self._session = RegistrationSessionSnapshot(
            session_id=session.session_id,
            status='cancelled',
            message='自动注册已取消',
            auth_url=None,
            started_at=session.started_at,
            updated_at=session.updated_at + 1,
            done=True,
            terminal_output='自动注册流程已取消。',
            requested_count=session.requested_count,
            success_count=session.success_count,
            failure_count=session.failure_count,
            current_email=session.current_email,
        )
        return self._session

    def claim_current(self) -> Any:
        if self._session is None or self._session.done:
            return None
        return type('Claim', (), {
            'to_dict': lambda self_: {
                'session_id': 'reg-1',
                'auth_url': None,
                'requested_count': 1,
                'success_count': 0,
                'failure_count': 0,
                'current_index': 1,
                'current_email': self._session.current_email,
            },
        })()

    def claim_session(self, session_id: str) -> Any:
        if self._session is None or self._session.session_id != session_id:
            raise QoderRegistrationError(f'找不到注册会话: {session_id}', status_code=404)
        return self.claim_current()

    def append_worker_progress(self, session_id: str, *, line: str, status: str | None = None, current_email: str | None = None, auth_url: str | None = None) -> RegistrationSessionSnapshot:
        session = self.get_session(session_id).snapshot()
        self._session = RegistrationSessionSnapshot(
            session_id=session.session_id,
            status=status or session.status,
            message=session.message,
            auth_url=auth_url if auth_url is not None else session.auth_url,
            started_at=session.started_at,
            updated_at=session.updated_at + 1,
            done=session.done,
            terminal_output=(session.terminal_output + '\n' + line).strip(),
            requested_count=session.requested_count,
            success_count=session.success_count,
            failure_count=session.failure_count,
            current_email=current_email or session.current_email,
        )
        return self._session

    def complete_worker_account(self, session_id: str, *, email: str, password: str = '', summary: dict[str, Any] | None = None) -> RegistrationSessionSnapshot:
        session = self.get_session(session_id).snapshot()
        self._session = RegistrationSessionSnapshot(
            session_id=session.session_id,
            status='success',
            message='自动注册完成：成功 1 个账号',
            auth_url=None,
            started_at=session.started_at,
            updated_at=session.updated_at + 1,
            done=True,
            terminal_output=session.terminal_output + '\n账号已成功入池：' + email,
            requested_count=session.requested_count,
            success_count=1,
            failure_count=session.failure_count,
            current_email=email,
        )
        return self._session

    def fail_worker(self, session_id: str, *, message: str) -> RegistrationSessionSnapshot:
        session = self.get_session(session_id).snapshot()
        self._session = RegistrationSessionSnapshot(
            session_id=session.session_id,
            status='error',
            message=message,
            auth_url=None,
            started_at=session.started_at,
            updated_at=session.updated_at + 1,
            done=True,
            terminal_output=session.terminal_output + '\n' + message,
            requested_count=session.requested_count,
            success_count=session.success_count,
            failure_count=session.failure_count + 1,
            current_email=session.current_email,
        )
        return self._session


def test_registration_error_handler_returns_json() -> None:
    response = asyncio.run(
        app_module.handle_qoder_registration_error(
            None,
            QoderRegistrationError('注册流程失败', status_code=502, error_type='api_error'),
        )
    )

    assert response.status_code == 502
    assert '注册流程失败' in response.body.decode('utf-8')


def test_webui_registration_routes(monkeypatch) -> None:
    manager = _FakeRegistrationManager()
    monkeypatch.setattr(app_module, 'get_registration_manager', lambda: manager)
    monkeypatch.setattr(app_module, 'get_runtime_settings_manager', lambda: _FakeRuntimeSettingsManager())

    overview = asyncio.run(app_module.webui_registration())
    assert overview['available'] is True
    assert overview['session'] is None

    started = asyncio.run(
        app_module.webui_start_registration(
            RegistrationStartRequest(requested_count=3),
        )
    )
    assert started['session']['session_id'] == 'reg-1'
    assert manager.started_with == 3
    assert started['session']['requested_count'] == 3

    fetched = asyncio.run(app_module.webui_get_registration_session('reg-1'))
    assert fetched['session_id'] == 'reg-1'
    assert fetched['auth_url'] is None

    claimed = asyncio.run(app_module.webui_claim_registration_worker())
    assert claimed['session']['session_id'] == 'reg-1'

    progressed = asyncio.run(
        app_module.webui_registration_worker_progress(
            'reg-1',
            app_module.RegistrationWorkerProgressRequest(line='worker alive', status='worker_running', current_email='demo@example.com', auth_url='https://qoder.com/device/selectAccounts?challenge=demo'),
        )
    )
    assert 'worker alive' in progressed['terminal_output']

    completed = asyncio.run(
        app_module.webui_registration_worker_complete(
            'reg-1',
            app_module.RegistrationWorkerCompleteRequest(email='demo@example.com', password='secret', summary={'cached_user_info': {'uid': '1'}, 'device': {'machine_id': 'mid-1'}}),
        )
    )
    assert completed['status'] == 'success'
    assert completed['current_email'] == 'demo@example.com'

    manager._session = RegistrationSessionSnapshot(
        session_id='reg-2',
        status='waiting_worker',
        message='等待注册 Worker 处理第 1/1 个账号...',
        auth_url='https://qoder.com/device/selectAccounts?challenge=demo2',
        started_at=1.0,
        updated_at=2.0,
        done=False,
        terminal_output='waiting...',
        requested_count=1,
        success_count=0,
        failure_count=0,
        current_email=None,
    )
    failed = asyncio.run(
        app_module.webui_registration_worker_fail(
            'reg-2',
            app_module.RegistrationWorkerFailRequest(message='worker failed'),
        )
    )
    assert failed['status'] == 'error'

    manager._session = RegistrationSessionSnapshot(
        session_id='reg-1',
        status='registering',
        message='正在注册第 1/1 个账号...',
        auth_url=None,
        started_at=1.0,
        updated_at=2.0,
        done=False,
        terminal_output='registering...',
        requested_count=1,
        success_count=0,
        failure_count=0,
        current_email='demo@example.com',
    )
    cancelled = asyncio.run(app_module.webui_cancel_registration_session('reg-1'))
    assert cancelled['status'] == 'cancelled'
    assert cancelled['auth_url'] is None
    assert manager.cancelled is True
