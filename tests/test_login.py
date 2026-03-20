import asyncio
import time
from pathlib import Path

import pytest

from qoder2api import app as app_module
from qoder2api.accounts import QoderAccount
from qoder2api.auth_state import CachedUserInfo
from qoder2api.login import BrowserLoginSnapshot, QoderLoginError, QoderLoginManager
from qoder2api.namespaced_login import NamespacedBrowserLoginClient
from qoder2api.qoder_api import BrowserLoginContext, QoderAPIError
from qoder2api.settings import Settings
from tests.helpers import prepare_qoder_home


class _HoldingAPI:
    def create_browser_login(self, qoder_home: Path) -> BrowserLoginContext:
        return BrowserLoginContext(
            auth_url="https://www.qoder.com/device/selectAccounts?challenge=demo",
            nonce="nonce-demo",
            verifier="verifier-demo",
            challenge="challenge-demo",
            challenge_method="S256",
            machine_id="1234567890abcdef-demo",
            client_id="client-demo",
        )

    async def complete_browser_login(
        self,
        *,
        qoder_home: Path,
        context: BrowserLoginContext,
        progress_callback=None,
        cancel_check=None,
    ):
        if progress_callback:
            progress_callback(f"登录地址：{context.auth_url}")
        for _ in range(50):
            if cancel_check and cancel_check():
                raise QoderAPIError(
                    "登录已取消",
                    status_code=409,
                    error_type="cancelled",
                )
            await asyncio.sleep(0.01)
        raise AssertionError("test should cancel before timeout")


class _RetryingAPI:
    def create_browser_login(self, qoder_home: Path) -> BrowserLoginContext:
        return BrowserLoginContext(
            auth_url="https://www.qoder.com/device/selectAccounts?challenge=first",
            nonce="nonce-first",
            verifier="verifier-first",
            challenge="challenge-first",
            challenge_method="S256",
            machine_id="machine-first",
            client_id="client-first",
        )

    async def complete_browser_login(
        self,
        *,
        qoder_home: Path,
        context: BrowserLoginContext,
        progress_callback=None,
        cancel_check=None,
    ):
        if progress_callback:
            progress_callback("LOGIN_URL=https://www.qoder.com/device/selectAccounts?challenge=second")
        return CachedUserInfo(uid="uid-2", name="second")


class _FailingStartAPI:
    def create_browser_login(self, qoder_home: Path) -> BrowserLoginContext:
        raise QoderAPIError(
            "启动登录失败",
            status_code=500,
            error_type="configuration_error",
        )


def test_login_manager_rejects_manual_key_input(tmp_path: Path) -> None:
    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="direct",
    )
    manager = QoderLoginManager(settings, api_factory=lambda _settings: _HoldingAPI())
    manager._create_browser_login_client = lambda: _HoldingAPI()  # type: ignore[method-assign]

    snapshot = manager.start_browser_login(qoder_home=settings.qoder_home_path)

    with pytest.raises(QoderLoginError) as exc_info:
        manager.send_key(snapshot.session_id, "enter")

    assert "无需按键输入" in str(exc_info.value)

    manager.cancel(snapshot.session_id)
    time.sleep(0.1)


def test_login_manager_uses_namespaced_backend_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="namespaced",
    )
    manager = QoderLoginManager(settings, api_factory=lambda _settings: _HoldingAPI())

    client = manager._create_browser_login_client()

    assert isinstance(client, NamespacedBrowserLoginClient)


def test_login_session_updates_auth_url_from_progress(tmp_path: Path) -> None:
    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="direct",
    )
    manager = QoderLoginManager(settings, api_factory=lambda _settings: _RetryingAPI())
    manager._create_browser_login_client = lambda: _RetryingAPI()  # type: ignore[method-assign]

    snapshot = manager.start_browser_login(qoder_home=settings.qoder_home_path)
    assert snapshot.auth_url in {
        "https://www.qoder.com/device/selectAccounts?challenge=first",
        "https://www.qoder.com/device/selectAccounts?challenge=second",
    }

    deadline = time.time() + 2
    updated = snapshot
    while time.time() < deadline:
        updated = manager.get_session(snapshot.session_id).snapshot()
        if updated.auth_url == "https://www.qoder.com/device/selectAccounts?challenge=second":
            break
        time.sleep(0.05)

    assert updated.auth_url == "https://www.qoder.com/device/selectAccounts?challenge=second"


def test_failed_start_does_not_leave_stale_starting_session(tmp_path: Path) -> None:
    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="direct",
    )
    manager = QoderLoginManager(settings, api_factory=lambda _settings: _FailingStartAPI())
    manager._create_browser_login_client = lambda: _FailingStartAPI()  # type: ignore[method-assign]

    with pytest.raises(QoderAPIError) as exc_info:
        manager.start_browser_login(qoder_home=settings.qoder_home_path)

    assert "启动登录失败" in str(exc_info.value)
    assert manager.current_session() is None


def test_qoder_api_error_handler_returns_json() -> None:
    response = asyncio.run(
        app_module.handle_qoder_api_error(
            None,
            QoderAPIError(
                "xieyi 纯协议设备构建失败",
                status_code=502,
                error_type="api_error",
            ),
        )
    )

    assert response.status_code == 502
    assert b"xieyi" in response.body


class _FakeLoginManager:
    def __init__(self, snapshot: BrowserLoginSnapshot | None) -> None:
        self._snapshot = snapshot

    def current_session(self) -> BrowserLoginSnapshot | None:
        return self._snapshot

    def get_session(self, _: str):
        return self

    def snapshot(self) -> BrowserLoginSnapshot:
        assert self._snapshot is not None
        return self._snapshot

    def send_key(self, _: str, __: str) -> BrowserLoginSnapshot:
        raise QoderLoginError("独立网页登录流程无需按键输入", status_code=409)

    def cancel(self, _: str) -> BrowserLoginSnapshot:
        assert self._snapshot is not None
        return self._snapshot


class _FakeInvoker:
    def list_models(self, preferred_account_id: str | None = None):
        return [
            type(
                "Model",
                (),
                {
                    "id": "auto",
                    "display_name": "Auto",
                    "max_input_tokens": 180000,
                    "max_output_tokens": 32768,
                    "is_reasoning": False,
                    "price_factor": 1.0,
                },
            )()
        ]


class _FakeAccountPool:
    def __init__(self, account: QoderAccount) -> None:
        self._account = account

    def list_accounts(self, include_pending: bool = False):
        return [self._account]

    def get_enabled_accounts(self):
        return [self._account]


def test_inspect_local_auth_and_webui_routes(monkeypatch, tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    logs_dir = qoder_home / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "qoder2api.log").write_text("line-1\nline-2\n", encoding="utf-8")
    auth_dir = qoder_home / ".auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "xieyi_device.json").write_text(
        """{
  "workflow": "xieyi",
  "machine_id": "1234567890abcdef-demo-machine",
  "machine_token": "P1gA-demo-token",
  "machine_type": "4e4a13f491a127292e",
  "hostname": "pure-host-demo",
  "mac": "021122334455"
}
""",
        encoding="utf-8",
    )

    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    snapshot = BrowserLoginSnapshot(
        session_id="demo-session",
        status="waiting_browser",
        message="请在浏览器中完成登录",
        auth_url="https://www.qoder.com/device/selectAccounts?challenge=demo",
        started_at=1.0,
        updated_at=2.0,
        done=False,
        already_logged_in=False,
        input_enabled=False,
        terminal_output="已生成网页登录链接",
    )
    account = QoderAccount(
        id="acct-1",
        label="测试账号",
        enabled=True,
        managed=True,
        status="ready",
        auth_type="qoder-browser",
        qoder_home=str(qoder_home),
        machine_id="1234567890abcdef-demo-machine",
    )

    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "get_login_manager", lambda: _FakeLoginManager(snapshot))
    monkeypatch.setattr(app_module, "get_invoker", lambda: _FakeInvoker())
    monkeypatch.setattr(app_module, "get_account_pool", lambda: _FakeAccountPool(account))

    overview_data = asyncio.run(app_module.webui_overview())
    assert overview_data["auth"]["logged_in_hint"] is True
    assert overview_data["session"]["session_id"] == "demo-session"
    assert overview_data["health"]["qoder_home"] == str(qoder_home)
    assert overview_data["health"]["infer_endpoint"] == "https://api3.qoder.sh"
    assert overview_data["accounts"]["total_accounts"] == 1
    assert overview_data["accounts"]["accounts"][0]["label"] == "测试账号"
    assert overview_data["accounts"]["accounts"][0]["machine_token"] == "P1gA-demo-token"
    assert overview_data["accounts"]["accounts"][0]["machine_type"] == "4e4a13f491a127292e"

    logs_data = asyncio.run(app_module.webui_logs(lines=100))
    assert logs_data["exists"] is True
    assert "line-2" in logs_data["content"]

    file_response = asyncio.run(app_module.webui())
    index_html = Path(file_response.path).read_text(encoding="utf-8")
    js_text = (Path(file_response.path).parent / "js" / "app.js").read_text(encoding="utf-8")
    assert file_response.status_code == 200
    assert "qoder2api 管理后台" in index_html
    assert "Infer Endpoint" in index_html
    assert "const OVERVIEW_API = '/webui/api/overview';" in js_text
