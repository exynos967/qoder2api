import asyncio
import base64
import json
from pathlib import Path

import httpx

from qoder2api.auth_state import load_cached_user_info, load_machine_id
from qoder2api.qoder_api import QoderAPIClient
from qoder2api.settings import Settings
from tests.helpers import prepare_qoder_home


def _make_client(
    settings: Settings,
    handler,
) -> QoderAPIClient:
    transport = httpx.MockTransport(handler)
    return QoderAPIClient(
        settings,
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )


def test_poll_device_token_returns_none_on_404(tmp_path: Path) -> None:
    settings = Settings(qoder_home=str(tmp_path / ".qoder"), workspace=str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/deviceToken/poll"
        assert request.url.params["verifier"]
        assert request.url.params["challenge_method"] == "S256"
        assert "code_verifier" not in request.url.params
        return httpx.Response(404, text="not ready")

    client = _make_client(settings, handler)
    context = client.create_browser_login(Path(settings.qoder_home_path))

    result = asyncio.run(client.poll_device_token(context))

    assert result is None


def test_refresh_device_token_and_fetch_userinfo(tmp_path: Path) -> None:
    settings = Settings(qoder_home=str(tmp_path / ".qoder"), workspace=str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/deviceToken/refresh":
            return httpx.Response(
                200,
                json={
                    "device_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_at": "2026-03-20T12:00:00Z",
                    "refresh_token_expires_at": "2026-03-21T12:00:00Z",
                },
            )
        if request.url.path == "/api/v1/userinfo":
            return httpx.Response(
                200,
                json={
                    "id": "user-1",
                    "name": "Tester",
                    "username": "tester",
                    "avatar": "https://example.com/avatar.png",
                    "email": "tester@example.com",
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = _make_client(settings, handler)

    refreshed = asyncio.run(client.refresh_device_token("refresh-token"))
    userinfo = asyncio.run(client.fetch_userinfo("access-token"))

    assert refreshed.device_token == "new-access"
    assert refreshed.refresh_token == "new-refresh"
    assert userinfo.id == "user-1"
    assert userinfo.email == "tester@example.com"


def test_complete_browser_login_writes_local_auth(tmp_path: Path) -> None:
    qoder_home = tmp_path / ".qoder"
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/deviceToken/poll":
            return httpx.Response(
                200,
                json={
                    "token": "access-token",
                    "refresh_token": "refresh-token",
                    "user_id": "user-42",
                    "user_name": "Tester 42",
                    "nonce": request.url.params["nonce"],
                    "expires_at": "2026-03-20T12:00:00Z",
                    "refresh_token_expires_at": "2026-03-21T12:00:00Z",
                },
            )
        if request.url.path == "/api/v1/userinfo":
            return httpx.Response(
                200,
                json={
                    "id": "user-42",
                    "name": "Tester 42",
                    "username": "tester42",
                    "avatar": "https://example.com/avatar.png",
                    "email": "tester42@example.com",
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = _make_client(settings, handler)
    context = client.create_browser_login(qoder_home)
    progress: list[str] = []

    info = asyncio.run(
        client.complete_browser_login(
            qoder_home=qoder_home,
            context=context,
            progress_callback=progress.append,
        )
    )

    stored = load_cached_user_info(qoder_home)

    assert info.uid == "user-42"
    assert stored is not None
    assert stored.uid == "user-42"
    assert stored.login_device_id == context.machine_id
    assert stored.key
    assert any("登录成功" in item for item in progress)


def test_build_chat_headers_matches_official_qoder_shape(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    auth_dir = qoder_home / ".auth"
    (auth_dir / "xieyi_device.json").write_text(
        json.dumps(
            {
                "machine_token": "machine-token-demo",
                "machine_type": "machine-type-demo",
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    client = QoderAPIClient(settings)
    cached = load_cached_user_info(qoder_home)
    assert cached is not None

    headers = client.build_chat_headers(
        qoder_home=qoder_home,
        cached_user=cached,
        body_text='{"demo":true}',
        path='/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1',
    )

    assert headers['User-Agent'] == 'Go-http-client/2.0'
    assert headers['Accept'] == 'text/event-stream'
    assert headers['Accept-Encoding'] == 'identity'
    assert headers['Cosy-Version'] == '0.14.2'
    assert headers['Cosy-Data-Policy'] == 'DISAGREE'
    assert headers['Cosy-SigPath'] == '/algo/api/v2/service/pro/sse/agent_chat_generation'
    assert headers['Cosy-MachineId'] == load_machine_id(qoder_home)
    assert headers['Cosy-MachineToken'] == 'machine-token-demo'
    assert headers['Cosy-MachineType'] == 'machine-type-demo'
    assert headers['Cosy-BodyLength'] == str(len('{"demo":true}'.encode('utf-8')))

    _, payload_b64, signature = headers['Authorization'].split('.', 2)
    assert signature
    payload = json.loads(base64.b64decode(payload_b64).decode('utf-8'))
    assert payload['cosyVersion'] == '0.14.2'
    assert payload['version'] == 'v1'
    assert payload['info'] == cached.encrypt_user_info
    assert 'cli_version' not in payload
