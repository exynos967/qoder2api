"""独立网页登录会话管理。"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .auth_state import CachedUserInfo
from .namespaced_login import NamespacedBrowserLoginClient
from .auth_state import inspect_local_auth
from .qoder_api import QoderAPIClient, QoderAPIError
from .settings import Settings

_DEFAULT_OUTPUT_LIMIT = 16_000
_SESSION_TTL_SECONDS = 15 * 60
_LOGIN_URL_PREFIX = "LOGIN_URL="
_LOGIN_ADDRESS_PREFIX = "登录地址："


class QoderLoginError(RuntimeError):
    """浏览器登录流程错误。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        error_type: str = "invalid_request_error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


def _trim_tail(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


class BrowserLoginClient(Protocol):
    def create_browser_login(self, qoder_home: Path) -> Any: ...

    async def complete_browser_login(
        self,
        *,
        qoder_home: Path,
        context: Any,
        progress_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> CachedUserInfo: ...


@dataclass(slots=True)
class BrowserLoginSnapshot:
    """供 API 返回的登录会话快照。"""

    session_id: str
    status: str
    message: str
    auth_url: str | None
    started_at: float
    updated_at: float
    done: bool
    already_logged_in: bool
    input_enabled: bool
    terminal_output: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "message": self.message,
            "auth_url": self.auth_url,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "done": self.done,
            "already_logged_in": self.already_logged_in,
            "input_enabled": self.input_enabled,
            "terminal_output": self.terminal_output,
        }


class BrowserLoginSession:
    """单个浏览器登录会话。"""

    def __init__(
        self,
        *,
        settings: Settings,
        qoder_home: Path,
        api_client: BrowserLoginClient,
        on_finished: Callable[[BrowserLoginSnapshot], None] | None = None,
        output_limit: int = _DEFAULT_OUTPUT_LIMIT,
    ) -> None:
        self.settings = settings
        self.qoder_home = qoder_home.resolve()
        self.api_client = api_client
        self.on_finished = on_finished
        self.output_limit = output_limit

        self.session_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished_callback_called = False
        self._context = None

        started_at = time.time()
        auth_state = inspect_local_auth(self.qoder_home)
        self._snapshot = BrowserLoginSnapshot(
            session_id=self.session_id,
            status="starting",
            message="正在准备 Qoder 浏览器登录...",
            auth_url=None,
            started_at=started_at,
            updated_at=started_at,
            done=False,
            already_logged_in=bool(auth_state.get("logged_in_hint")),
            input_enabled=False,
            terminal_output="正在初始化网页登录上下文...",
        )

    def start(self) -> BrowserLoginSnapshot:
        self._context = self.api_client.create_browser_login(self.qoder_home)
        terminal_lines = [
            "已生成网页登录链接。",
            "请在浏览器中完成 Qoder 授权，后台会自动轮询结果。",
            f"登录地址：{self._context.auth_url}",
        ]
        self._update_snapshot(
            status="waiting_browser",
            message="请在浏览器中完成 Qoder 登录，后台会自动检测结果",
            auth_url=self._context.auth_url,
            terminal_output="\n".join(terminal_lines),
            done=False,
        )
        self._thread = threading.Thread(
            target=self._run_worker,
            name=f"qoder-login-{self.session_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return self.snapshot()

    def snapshot(self) -> BrowserLoginSnapshot:
        with self._lock:
            return BrowserLoginSnapshot(**self._snapshot.to_dict())

    def cancel(self) -> BrowserLoginSnapshot:
        self._cancel_event.set()
        self._update_snapshot(
            status="cancelled",
            message="登录已取消",
            done=True,
            terminal_output=self._append_output_line("登录已取消，后台轮询即将停止。"),
        )
        return self.snapshot()

    def send_key(self, _: str) -> BrowserLoginSnapshot:
        raise QoderLoginError(
            "独立网页登录流程无需按键输入",
            status_code=409,
            error_type="invalid_request_error",
        )

    def is_expired(self) -> bool:
        snapshot = self.snapshot()
        return snapshot.done and (time.time() - snapshot.updated_at) > _SESSION_TTL_SECONDS

    def _run_worker(self) -> None:
        assert self._context is not None
        try:
            info = asyncio.run(
                self.api_client.complete_browser_login(
                    qoder_home=self.qoder_home,
                    context=self._context,
                    progress_callback=self._append_progress,
                    cancel_check=self._cancel_event.is_set,
                )
            )
        except QoderAPIError as exc:
            if exc.error_type == "cancelled" or self._cancel_event.is_set():
                self._update_snapshot(
                    status="cancelled",
                    message="登录已取消",
                    done=True,
                    terminal_output=self._append_output_line("登录流程已取消。"),
                )
            else:
                self._update_snapshot(
                    status="error",
                    message=str(exc),
                    done=True,
                    terminal_output=self._append_output_line(f"登录失败：{exc}"),
                )
        except Exception as exc:  # pragma: no cover - 兜底保护
            self._update_snapshot(
                status="error",
                message=str(exc),
                done=True,
                terminal_output=self._append_output_line(f"登录异常：{exc}"),
            )
        else:
            self._update_snapshot(
                status="success",
                message=f"登录成功：{info.name or info.uid}",
                done=True,
                terminal_output=self._append_output_line(
                    f"登录成功：{info.name or info.uid}",
                ),
            )
        finally:
            self._notify_finished_once()

    def _append_progress(self, message: str) -> None:
        auth_url = self._extract_progress_auth_url(message)
        self._update_snapshot(
            auth_url=auth_url if auth_url else None,
            terminal_output=self._append_output_line(message),
        )

    @staticmethod
    def _extract_progress_auth_url(message: str) -> str | None:
        stripped = message.strip()
        if stripped.startswith(_LOGIN_URL_PREFIX):
            candidate = stripped.removeprefix(_LOGIN_URL_PREFIX).strip()
            return candidate or None
        if stripped.startswith(_LOGIN_ADDRESS_PREFIX):
            candidate = stripped.removeprefix(_LOGIN_ADDRESS_PREFIX).strip()
            return candidate or None
        return None

    def _append_output_line(self, line: str) -> str:
        current = self.snapshot().terminal_output
        merged = current if not current else f"{current}\n{line}"
        return _trim_tail(merged, limit=self.output_limit)

    def _update_snapshot(
        self,
        *,
        status: str | None = None,
        message: str | None = None,
        auth_url: str | None = None,
        terminal_output: str | None = None,
        done: bool | None = None,
    ) -> None:
        with self._lock:
            if status is not None:
                self._snapshot.status = status
            if message is not None:
                self._snapshot.message = message
            if auth_url is not None:
                self._snapshot.auth_url = auth_url
            if terminal_output is not None:
                self._snapshot.terminal_output = _trim_tail(
                    terminal_output,
                    limit=self.output_limit,
                )
            if done is not None:
                self._snapshot.done = done
            self._snapshot.input_enabled = False
            self._snapshot.updated_at = time.time()

    def _notify_finished_once(self) -> None:
        if self.on_finished is None:
            return
        with self._lock:
            if self._finished_callback_called:
                return
            self._finished_callback_called = True
            snapshot = BrowserLoginSnapshot(**self._snapshot.to_dict())
        self.on_finished(snapshot)


class QoderLoginManager:
    """网页登录会话管理器。"""

    def __init__(
        self,
        settings: Settings,
        *,
        api_factory: Callable[[Settings], QoderAPIClient] | None = None,
    ) -> None:
        self.settings = settings
        self._api_factory = api_factory or (lambda settings: QoderAPIClient(settings))
        self._lock = threading.RLock()
        self._sessions: dict[str, BrowserLoginSession] = {}
        self._latest_session_id: str | None = None

    def _create_browser_login_client(self) -> BrowserLoginClient:
        if self.settings.browser_login_backend_name == "namespaced":
            return NamespacedBrowserLoginClient(self.settings)
        return self._api_factory(self.settings)

    def _cleanup_expired(self) -> None:
        expired_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session.is_expired()
        ]
        for session_id in expired_ids:
            self._sessions.pop(session_id, None)
            if self._latest_session_id == session_id:
                self._latest_session_id = None

    def current_session(self) -> BrowserLoginSnapshot | None:
        with self._lock:
            self._cleanup_expired()
            if self._latest_session_id and self._latest_session_id in self._sessions:
                return self._sessions[self._latest_session_id].snapshot()

            if not self._sessions:
                return None

            latest = max(
                self._sessions.values(),
                key=lambda session: session.snapshot().updated_at,
            )
            self._latest_session_id = latest.session_id
            return latest.snapshot()

    def start_browser_login(
        self,
        *,
        qoder_home: Path,
        on_finished: Callable[[BrowserLoginSnapshot], None] | None = None,
    ) -> BrowserLoginSnapshot:
        with self._lock:
            current = self.current_session()
            if current is not None and not current.done:
                return current

            session = BrowserLoginSession(
                settings=self.settings,
                qoder_home=qoder_home,
                api_client=self._create_browser_login_client(),
                on_finished=on_finished,
            )
            started_snapshot = session.start()
            self._sessions[session.session_id] = session
            self._latest_session_id = session.session_id
            return started_snapshot

    def get_session(self, session_id: str) -> BrowserLoginSession:
        with self._lock:
            self._cleanup_expired()
            session = self._sessions.get(session_id)
            if session is None:
                raise QoderLoginError(f"找不到登录会话: {session_id}", status_code=404)
            return session

    def send_key(self, session_id: str, key: str) -> BrowserLoginSnapshot:
        session = self.get_session(session_id)
        return session.send_key(key)

    def cancel(self, session_id: str) -> BrowserLoginSnapshot:
        session = self.get_session(session_id)
        return session.cancel()
