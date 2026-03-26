"""外部 register worker 驱动的自动注册并 OAuth 入池。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounts import create_pending_managed_account, finalize_pending_account, remove_account
from .login import BrowserLoginSnapshot, QoderLoginManager
from .runtime_settings import RuntimeSettingsSnapshot
from .settings import Settings

_SESSION_TTL_SECONDS = 15 * 60
_OUTPUT_LIMIT = 24_000


class QoderRegistrationError(RuntimeError):
    """自动注册/自动 OAuth 流程错误。"""

    def __init__(self, message: str, *, status_code: int = 400, error_type: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


@dataclass(slots=True)
class RegistrationSessionSnapshot:
    session_id: str
    status: str
    message: str
    auth_url: str | None
    started_at: float
    updated_at: float
    done: bool
    terminal_output: str
    requested_count: int
    success_count: int
    failure_count: int
    current_email: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "message": self.message,
            "auth_url": self.auth_url,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "done": self.done,
            "terminal_output": self.terminal_output,
            "requested_count": self.requested_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "current_email": self.current_email,
        }

    def to_public_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        if self.done:
            payload["auth_url"] = None
        return payload


@dataclass(slots=True)
class RegistrationWorkerClaim:
    session_id: str
    auth_url: str
    requested_count: int
    success_count: int
    failure_count: int
    current_index: int
    current_email: str | None
    browser_mode: str
    proxy_url: str
    mail_api_base: str
    mail_api_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "auth_url": self.auth_url,
            "requested_count": self.requested_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "current_index": self.current_index,
            "current_email": self.current_email,
            "settings": {
                "registration_browser_mode": self.browser_mode,
                "registration_proxy_url": self.proxy_url,
                "registration_mail_api_base": self.mail_api_base,
                "registration_mail_api_key": self.mail_api_key,
            },
        }


def _trim_tail(text: str, *, limit: int = _OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def discover_qoderreg_project_path(explicit: str | None = None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        return candidate

    current_file = Path(__file__).resolve()
    candidates: list[Path] = []
    for base in (current_file.parent, *current_file.parents):
        candidates.append(base / "register")
        candidates.append(base / "vendor" / "qoderreg")
        candidates.append(base / "qoderreg")
    candidates.extend([
        Path("/opt/qoderreg"),
        Path("/root/workdir-2/qoderreg"),
    ])

    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return candidates[0].resolve()


class RegistrationSession:
    """一次外部 worker 驱动的自动注册任务。"""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime_snapshot: RuntimeSettingsSnapshot,
        login_manager: QoderLoginManager,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> None:
        self.settings = settings
        self.runtime_snapshot = runtime_snapshot
        self.login_manager = login_manager
        self.output_limit = output_limit
        self.session_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._cancelled = False
        self._current_account_id: str | None = None
        self._current_browser_session_id: str | None = None

        started_at = time.time()
        self._snapshot = RegistrationSessionSnapshot(
            session_id=self.session_id,
            status="starting",
            message="正在准备自动注册与 OAuth 入池...",
            auth_url=None,
            started_at=started_at,
            updated_at=started_at,
            done=False,
            terminal_output="正在初始化自动注册流程...",
            requested_count=max(1, int(runtime_snapshot.registration_count or 1)),
            success_count=0,
            failure_count=0,
            current_email=None,
        )

    def start(self) -> RegistrationSessionSnapshot:
        self._prepare_next_account()
        return self.snapshot()

    def snapshot(self) -> RegistrationSessionSnapshot:
        with self._lock:
            return RegistrationSessionSnapshot(**self._snapshot.to_dict())

    def is_expired(self) -> bool:
        snapshot = self.snapshot()
        return snapshot.done and (time.time() - snapshot.updated_at) > _SESSION_TTL_SECONDS

    def claim(self) -> RegistrationWorkerClaim:
        snapshot = self.snapshot()
        if snapshot.done:
            raise QoderRegistrationError("注册会话已结束", status_code=409)
        if not snapshot.auth_url or self._current_browser_session_id is None:
            raise QoderRegistrationError("当前没有可处理的注册任务", status_code=409)

        current_index = snapshot.success_count + snapshot.failure_count + 1
        self._update_snapshot(
            status="worker_running",
            message=f"注册 Worker 已接单，正在处理第 {current_index}/{snapshot.requested_count} 个账号...",
            terminal_output=self._append_output_line("注册 Worker 已接入任务。"),
        )
        return RegistrationWorkerClaim(
            session_id=snapshot.session_id,
            auth_url=snapshot.auth_url,
            requested_count=snapshot.requested_count,
            success_count=snapshot.success_count,
            failure_count=snapshot.failure_count,
            current_index=current_index,
            current_email=snapshot.current_email,
            browser_mode=self.runtime_snapshot.registration_browser_mode,
            proxy_url=(self.runtime_snapshot.registration_proxy_url or self.runtime_snapshot.proxy_url or "").strip(),
            mail_api_base=self.runtime_snapshot.registration_mail_api_base,
            mail_api_key=self.runtime_snapshot.registration_mail_api_key,
        )

    def append_worker_progress(
        self,
        *,
        line: str,
        status: str | None = None,
        current_email: str | None = None,
    ) -> RegistrationSessionSnapshot:
        if line:
            terminal_output = self._append_output_line(line)
        else:
            terminal_output = None
        self._update_snapshot(
            status=status,
            terminal_output=terminal_output,
            current_email=current_email,
        )
        return self.snapshot()

    def complete_current_account(self, *, email: str, password: str = "") -> RegistrationSessionSnapshot:
        del password
        if self._current_account_id is None or self._current_browser_session_id is None:
            raise QoderRegistrationError("当前没有待完成的 OAuth 任务", status_code=409)

        self._update_snapshot(
            status="waiting_qoder_login",
            message=f"正在等待 Qoder OAuth 回写登录态：{email}",
            current_email=email,
            terminal_output=self._append_output_line(f"注册 Worker 已完成 OAuth，等待 Qoder 会话收敛：{email}"),
        )

        browser_snapshot = self._wait_browser_session(self._current_browser_session_id)
        current_account_id = self._current_account_id
        self._current_account_id = None
        self._current_browser_session_id = None

        if browser_snapshot.status != "success":
            remove_account(self.settings, current_account_id)
            with self._lock:
                self._snapshot.failure_count += 1
            self._update_snapshot(
                status="error",
                message=browser_snapshot.message or "OAuth 入池失败",
                auth_url=None,
                done=True,
                terminal_output=self._append_output_line(browser_snapshot.message or "OAuth 入池失败"),
            )
            return self.snapshot()

        try:
            finalize_pending_account(self.settings, current_account_id)
        except KeyError as exc:
            raise QoderRegistrationError("待入池账号不存在，无法完成注册流程", status_code=500, error_type="api_error") from exc

        with self._lock:
            self._snapshot.success_count += 1
            self._snapshot.current_email = email

        if self.snapshot().success_count >= self.snapshot().requested_count:
            self._update_snapshot(
                status="success",
                message=f"自动注册完成：成功 {self.snapshot().success_count} 个账号",
                auth_url=None,
                done=True,
                terminal_output=self._append_output_line(f"账号已成功入池：{email}"),
            )
            return self.snapshot()

        self._update_snapshot(
            status="ready",
            message=f"账号已入池：{email}，准备下一轮...",
            auth_url=None,
            terminal_output=self._append_output_line(f"账号已成功入池：{email}"),
        )
        self._prepare_next_account()
        return self.snapshot()

    def fail_current_account(self, message: str) -> RegistrationSessionSnapshot:
        self._cleanup_current_resources(cancel_browser=True)
        with self._lock:
            self._snapshot.failure_count += 1
        self._update_snapshot(
            status="error",
            message=message,
            auth_url=None,
            done=True,
            terminal_output=self._append_output_line(message),
        )
        return self.snapshot()

    def cancel(self) -> RegistrationSessionSnapshot:
        self._cancelled = True
        self._cleanup_current_resources(cancel_browser=True)
        self._update_snapshot(
            status="cancelled",
            message="自动注册已取消",
            auth_url=None,
            done=True,
            terminal_output=self._append_output_line("自动注册流程已取消。"),
        )
        return self.snapshot()

    def _prepare_next_account(self) -> None:
        if self._cancelled:
            raise QoderRegistrationError("自动注册已取消", status_code=409, error_type="cancelled")

        position = self.snapshot().success_count + self.snapshot().failure_count + 1
        total = self.snapshot().requested_count
        self._update_snapshot(
            status="starting_browser_login",
            message=f"正在为第 {position}/{total} 个账号创建 OAuth 链接...",
            terminal_output=self._append_output_line(f"开始处理第 {position}/{total} 个账号。"),
            current_email=None,
        )
        pending_account = create_pending_managed_account(self.settings, label="")
        browser_snapshot = self.login_manager.start_browser_login(
            qoder_home=pending_account.qoder_home_path,
            on_finished=None,
        )
        auth_url = (browser_snapshot.auth_url or "").strip()
        if not auth_url:
            remove_account(self.settings, pending_account.id)
            raise QoderRegistrationError("未能生成 OAuth 链接", status_code=502, error_type="api_error")

        self._current_account_id = pending_account.id
        self._current_browser_session_id = browser_snapshot.session_id
        self._update_snapshot(
            status="waiting_worker",
            message=f"等待注册 Worker 处理第 {position}/{total} 个账号...",
            auth_url=auth_url,
            terminal_output=self._append_output_line(f"已生成 OAuth 链接：{auth_url}"),
        )

    def _cleanup_current_resources(self, *, cancel_browser: bool) -> None:
        account_id = self._current_account_id
        browser_session_id = self._current_browser_session_id
        self._current_account_id = None
        self._current_browser_session_id = None

        if cancel_browser and browser_session_id:
            try:
                browser_snapshot = self.login_manager.get_session(browser_session_id).snapshot()
            except Exception:
                browser_snapshot = None
            if browser_snapshot is not None and not browser_snapshot.done:
                try:
                    self.login_manager.cancel(browser_session_id)
                except Exception:
                    pass
        if account_id:
            remove_account(self.settings, account_id)

    def _wait_browser_session(self, session_id: str) -> BrowserLoginSnapshot:
        deadline = time.monotonic() + max(60, self.settings.namespaced_login_timeout_seconds + 30)
        last_output = ""
        while time.monotonic() < deadline:
            if self._cancelled:
                raise QoderRegistrationError("自动注册已取消", status_code=409, error_type="cancelled")
            snapshot = self.login_manager.get_session(session_id).snapshot()
            current_output = snapshot.terminal_output or ""
            if current_output and current_output != last_output:
                new_text = current_output[len(last_output):].lstrip("\n") if current_output.startswith(last_output) else current_output
                for line in new_text.splitlines():
                    self._update_snapshot(terminal_output=self._append_output_line(f"[oauth-session] {line}"))
                last_output = current_output
            if snapshot.auth_url:
                self._update_snapshot(auth_url=snapshot.auth_url)
            if snapshot.done:
                return snapshot
            time.sleep(0.5)
        raise QoderRegistrationError("等待 OAuth 会话完成超时", status_code=504, error_type="timeout_error")

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
        current_email: str | None = None,
    ) -> None:
        with self._lock:
            if status is not None:
                self._snapshot.status = status
            if message is not None:
                self._snapshot.message = message
            if auth_url is not None:
                self._snapshot.auth_url = auth_url
            if terminal_output is not None:
                self._snapshot.terminal_output = _trim_tail(terminal_output, limit=self.output_limit)
            if done is not None:
                self._snapshot.done = done
            if current_email is not None:
                self._snapshot.current_email = current_email
            self._snapshot.updated_at = time.time()


class QoderRegistrationManager:
    """外部 worker 驱动的自动注册任务管理器。"""

    def __init__(self, settings: Settings, *, login_manager: QoderLoginManager) -> None:
        self.settings = settings
        self.login_manager = login_manager
        self._lock = threading.RLock()
        self._sessions: dict[str, RegistrationSession] = {}
        self._latest_session_id: str | None = None

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

    def describe(self) -> dict[str, Any]:
        project_path = discover_qoderreg_project_path()
        payload = {
            "available": True,
            "message": "外部注册 Worker 模式：请在 register/ 目录单独运行 worker.ts",
            "worker_mode": "external",
            "project_path": str(project_path),
            "source_available": project_path.is_dir(),
        }
        current = self.current_session()
        payload["session"] = current.to_public_dict() if current else None
        return payload

    def current_session(self) -> RegistrationSessionSnapshot | None:
        with self._lock:
            self._cleanup_expired()
            if self._latest_session_id and self._latest_session_id in self._sessions:
                return self._sessions[self._latest_session_id].snapshot()
            if not self._sessions:
                return None
            latest = max(self._sessions.values(), key=lambda session: session.snapshot().updated_at)
            self._latest_session_id = latest.session_id
            return latest.snapshot()

    def start_registration(self, runtime_snapshot: RuntimeSettingsSnapshot) -> RegistrationSessionSnapshot:
        with self._lock:
            current = self.current_session()
            if current is not None and not current.done:
                return current
            browser_session = self.login_manager.current_session()
            if browser_session is not None and not browser_session.done:
                raise QoderRegistrationError("当前已有进行中的 OAuth 会话，请先完成或取消后再试", status_code=409)
            session = RegistrationSession(
                settings=self.settings,
                runtime_snapshot=runtime_snapshot,
                login_manager=self.login_manager,
            )
            started_snapshot = session.start()
            self._sessions[session.session_id] = session
            self._latest_session_id = session.session_id
            return started_snapshot

    def get_session(self, session_id: str) -> RegistrationSession:
        with self._lock:
            self._cleanup_expired()
            session = self._sessions.get(session_id)
            if session is None:
                raise QoderRegistrationError(f"找不到注册会话: {session_id}", status_code=404)
            return session

    def cancel(self, session_id: str) -> RegistrationSessionSnapshot:
        return self.get_session(session_id).cancel()

    def claim_current(self) -> RegistrationWorkerClaim | None:
        current = self.current_session()
        if current is None or current.done:
            return None
        return self.get_session(current.session_id).claim()

    def claim_session(self, session_id: str) -> RegistrationWorkerClaim:
        return self.get_session(session_id).claim()

    def append_worker_progress(
        self,
        session_id: str,
        *,
        line: str,
        status: str | None = None,
        current_email: str | None = None,
    ) -> RegistrationSessionSnapshot:
        return self.get_session(session_id).append_worker_progress(
            line=line,
            status=status,
            current_email=current_email,
        )

    def complete_worker_account(self, session_id: str, *, email: str, password: str = "") -> RegistrationSessionSnapshot:
        return self.get_session(session_id).complete_current_account(email=email, password=password)

    def fail_worker(self, session_id: str, *, message: str) -> RegistrationSessionSnapshot:
        return self.get_session(session_id).fail_current_account(message)
