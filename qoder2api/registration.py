"""外部 register worker 驱动的自动注册并 OAuth 入池。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .account_status import persist_summary_account_status
from .accounts import create_pending_managed_account, finalize_pending_account, remove_account
from .auth_state import CachedUserInfo, ensure_auth_dir, save_cached_user_info, save_machine_id
from .login import QoderLoginManager
from .settings import Settings

_SESSION_TTL_SECONDS = 15 * 60
_OUTPUT_LIMIT = 24_000
_AUTH_URL_UNSET = object()
_CURRENT_EMAIL_UNSET = object()


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
    auth_url: str | None
    requested_count: int
    success_count: int
    failure_count: int
    current_index: int
    current_email: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "auth_url": self.auth_url,
            "requested_count": self.requested_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "current_index": self.current_index,
            "current_email": self.current_email,
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
        requested_count: int,
        login_manager: QoderLoginManager,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> None:
        self.settings = settings
        self.login_manager = login_manager
        self.output_limit = output_limit
        self.session_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._cancelled = False

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
            requested_count=max(1, int(requested_count or 1)),
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
        )

    def append_worker_progress(
        self,
        *,
        line: str,
        status: str | None = None,
        current_email: str | None = None,
        auth_url: str | None = None,
    ) -> RegistrationSessionSnapshot:
        if line:
            terminal_output = self._append_output_line(line)
        else:
            terminal_output = None
        self._update_snapshot(
            status=status,
            terminal_output=terminal_output,
            current_email=current_email,
            auth_url=auth_url,
        )
        return self.snapshot()

    def complete_current_account(
        self,
        *,
        email: str,
        password: str = "",
        summary: Mapping[str, Any] | None = None,
    ) -> RegistrationSessionSnapshot:
        del password
        imported_account = import_external_worker_account(
            self.settings,
            email=email,
            summary=summary,
        )

        with self._lock:
            self._snapshot.success_count += 1
            self._snapshot.current_email = email

        if self.snapshot().success_count >= self.snapshot().requested_count:
            self._update_snapshot(
                status="success",
                message=f"自动注册完成：成功 {self.snapshot().success_count} 个账号",
                auth_url=None,
                done=True,
                terminal_output=self._append_output_line(
                    f"账号已成功入池：{email} ({imported_account.id})"
                ),
            )
            return self.snapshot()

        self._update_snapshot(
            status="ready",
            message=f"账号已入池：{email}，准备下一轮...",
            auth_url=None,
            terminal_output=self._append_output_line(f"账号已成功入池：{email} ({imported_account.id})"),
        )
        self._prepare_next_account()
        return self.snapshot()

    def fail_current_account(self, message: str) -> RegistrationSessionSnapshot:
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
            status="waiting_worker",
            message=f"等待外部注册 Worker 处理第 {position}/{total} 个账号...",
            auth_url=None,
            terminal_output=self._append_output_line(
                f"开始处理第 {position}/{total} 个账号，等待外部 Worker 生成 OAuth 链接并回传结果。"
            ),
            current_email=None,
        )

    def _append_output_line(self, line: str) -> str:
        current = self.snapshot().terminal_output
        merged = current if not current else f"{current}\n{line}"
        return _trim_tail(merged, limit=self.output_limit)

    def _update_snapshot(
        self,
        *,
        status: str | None = None,
        message: str | None = None,
        auth_url: str | None | object = _AUTH_URL_UNSET,
        terminal_output: str | None = None,
        done: bool | None = None,
        current_email: str | None | object = _CURRENT_EMAIL_UNSET,
    ) -> None:
        with self._lock:
            if status is not None:
                self._snapshot.status = status
            if message is not None:
                self._snapshot.message = message
            if auth_url is not _AUTH_URL_UNSET:
                self._snapshot.auth_url = auth_url if isinstance(auth_url, str) or auth_url is None else self._snapshot.auth_url
            if terminal_output is not None:
                self._snapshot.terminal_output = _trim_tail(terminal_output, limit=self.output_limit)
            if done is not None:
                self._snapshot.done = done
            if current_email is not _CURRENT_EMAIL_UNSET:
                self._snapshot.current_email = current_email if isinstance(current_email, str) or current_email is None else self._snapshot.current_email
            self._snapshot.updated_at = time.time()


def _build_external_device_metadata(summary: Mapping[str, Any], *, machine_id: str) -> dict[str, str]:
    device = summary.get("device") if isinstance(summary.get("device"), Mapping) else {}
    device_info = device.get("machine_info") if isinstance(device, Mapping) else {}
    output_dir = str(summary.get("outputDir") or "").strip()
    return {
        "workflow": "pure_protocol_external",
        "output_dir": output_dir,
        "summary_path": str(Path(output_dir) / "summary.json") if output_dir else "",
        "machine_id": machine_id,
        "machine_token": str(device_info.get("machineToken") or "").strip(),
        "machine_type": str(device_info.get("machineType") or "").strip(),
        "hostname": str(device.get("hostname") or "").strip(),
        "mac": str(device.get("mac") or "").strip(),
    }


def import_external_worker_account(
    settings: Settings,
    *,
    email: str,
    summary: Mapping[str, Any] | None,
):
    if not isinstance(summary, Mapping):
        raise QoderRegistrationError("外部 Worker 未回传 summary，无法导入账号", status_code=502, error_type="api_error")
    cached_payload = summary.get("cached_user_info")
    if not isinstance(cached_payload, Mapping):
        raise QoderRegistrationError("外部 Worker 回传 summary 缺少 cached_user_info", status_code=502, error_type="api_error")

    try:
        info = CachedUserInfo.model_validate(cached_payload)
    except Exception as exc:
        raise QoderRegistrationError(f"解析 cached_user_info 失败: {exc}", status_code=502, error_type="api_error") from exc

    device = summary.get("device") if isinstance(summary.get("device"), Mapping) else {}
    machine_id = str(device.get("machine_id") or info.login_device_id or "").strip()
    if not machine_id:
        raise QoderRegistrationError("外部 Worker 回传 summary 缺少 machine_id", status_code=502, error_type="api_error")
    if len(machine_id) < 16:
        raise QoderRegistrationError(
            "外部 Worker 回传的 machine_id 长度不足 16，无法导入账号",
            status_code=502,
            error_type="api_error",
        )

    if not info.email:
        info.email = email
    if not info.login_device_id:
        info.login_device_id = machine_id

    account = create_pending_managed_account(settings, label=email)
    qoder_home = account.qoder_home_path
    try:
        save_machine_id(qoder_home, machine_id)
        save_cached_user_info(qoder_home, info)
        metadata = _build_external_device_metadata(summary, machine_id=machine_id)
        auth_dir = ensure_auth_dir(qoder_home)
        (auth_dir / "xieyi_device.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        persist_summary_account_status(qoder_home, summary)
        return finalize_pending_account(settings, account.id)
    except Exception:
        remove_account(settings, account.id)
        raise


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
            "message": "外部注册 Worker 模式：请在 register/ 目录单独运行 worker.ts，浏览器/代理/邮箱参数由 Worker 自己控制。",
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

    def start_registration(self, requested_count: int | None = None) -> RegistrationSessionSnapshot:
        with self._lock:
            current = self.current_session()
            if current is not None and not current.done:
                return current
            session = RegistrationSession(
                settings=self.settings,
                requested_count=max(1, int(requested_count or 1)),
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
        auth_url: str | None = None,
    ) -> RegistrationSessionSnapshot:
        return self.get_session(session_id).append_worker_progress(
            line=line,
            status=status,
            current_email=current_email,
            auth_url=auth_url,
        )

    def complete_worker_account(
        self,
        session_id: str,
        *,
        email: str,
        password: str = "",
        summary: Mapping[str, Any] | None = None,
    ) -> RegistrationSessionSnapshot:
        return self.get_session(session_id).complete_current_account(email=email, password=password, summary=summary)

    def fail_worker(self, session_id: str, *, message: str) -> RegistrationSessionSnapshot:
        return self.get_session(session_id).fail_current_account(message)
