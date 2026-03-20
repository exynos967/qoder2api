"""通过外部 namespaced Qoder 工作流完成网页登录。"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .auth_state import (
    CachedUserInfo,
    ensure_auth_fields,
    save_cached_user_info,
    save_machine_id,
)
from .qoder_api import QoderAPIError
from .settings import Settings

_LOGIN_URL_PREFIX = "LOGIN_URL="
_WORK_DIR_PREFIX = "WORK_DIR="
_MACHINE_TOKEN_PREFIX = "MACHINE_TOKEN="
_MACHINE_TYPE_PREFIX = "MACHINE_TYPE="


@dataclass(slots=True)
class NamespacedBrowserLoginContext:
    auth_url: str
    work_dir: Path
    machine_token: str
    machine_type: str
    process: subprocess.Popen[str]
    buffered_output: list[str]


class NamespacedBrowserLoginClient:
    """适配外部 namespaced 登录脚本，接口保持与 QoderAPIClient 一致。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_browser_login(self, qoder_home: Path) -> NamespacedBrowserLoginContext:
        script_path = self.settings.namespaced_login_script_path
        if not script_path.is_file():
            raise QoderAPIError(
                f"找不到 namespaced 登录脚本: {script_path}",
                status_code=500,
                error_type="configuration_error",
            )

        cmd = [
            sys.executable,
            str(script_path),
            "--max-attempts",
            str(self.settings.namespaced_login_max_attempts),
            "--timeout",
            str(self.settings.namespaced_login_timeout_seconds),
            "--dump-user-plan",
            "--json",
        ]
        output_root = self.settings.namespaced_login_output_root_path
        if output_root is not None:
            cmd.extend(["--output-root", str(output_root)])

        process = subprocess.Popen(
            cmd,
            cwd=str(script_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        if process.stdout is None:
            raise QoderAPIError("无法读取 namespaced 登录脚本输出", status_code=500)

        buffered_output: list[str] = []
        auth_url = ""
        work_dir = Path()
        machine_token = ""
        machine_type = ""
        deadline = time.monotonic() + 30

        while time.monotonic() < deadline:
            if process.poll() is not None:
                buffered_output.extend(self._drain_available_lines(process))
                raise QoderAPIError(
                    self._join_output(buffered_output) or "namespaced 登录脚本提前退出",
                    status_code=502,
                    error_type="api_error",
                )

            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            if not ready:
                continue
            line = process.stdout.readline()
            if not line:
                continue
            stripped = line.rstrip("\n")
            buffered_output.append(stripped)
            if stripped.startswith(_LOGIN_URL_PREFIX):
                auth_url = stripped.removeprefix(_LOGIN_URL_PREFIX).strip()
            elif stripped.startswith(_WORK_DIR_PREFIX):
                work_dir = Path(stripped.removeprefix(_WORK_DIR_PREFIX).strip()).resolve()
            elif stripped.startswith(_MACHINE_TOKEN_PREFIX):
                machine_token = stripped.removeprefix(_MACHINE_TOKEN_PREFIX).strip()
            elif stripped.startswith(_MACHINE_TYPE_PREFIX):
                machine_type = stripped.removeprefix(_MACHINE_TYPE_PREFIX).strip()

            if auth_url and work_dir:
                return NamespacedBrowserLoginContext(
                    auth_url=auth_url,
                    work_dir=work_dir,
                    machine_token=machine_token,
                    machine_type=machine_type,
                    process=process,
                    buffered_output=buffered_output,
                )

        self._terminate_process(process)
        raise QoderAPIError(
            "等待 namespaced 登录地址超时",
            status_code=504,
            error_type="timeout_error",
        )

    async def complete_browser_login(
        self,
        *,
        qoder_home: Path,
        context: NamespacedBrowserLoginContext,
        progress_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> CachedUserInfo:
        for line in context.buffered_output:
            self._emit(progress_callback, line)

        process = context.process
        stdout = process.stdout
        if stdout is None:
            raise QoderAPIError("namespaced 登录脚本 stdout 不可用", status_code=500)

        while True:
            if cancel_check and cancel_check():
                self._terminate_process(process)
                raise QoderAPIError(
                    "登录已取消",
                    status_code=409,
                    error_type="cancelled",
                )

            ready, _, _ = select.select([stdout], [], [], 0.5)
            if ready:
                line = stdout.readline()
                if line:
                    stripped = line.rstrip("\n")
                    context.buffered_output.append(stripped)
                    self._emit(progress_callback, stripped)
                    continue

            if process.poll() is not None:
                context.buffered_output.extend(self._drain_available_lines(process))
                break

        if process.returncode != 0:
            raise QoderAPIError(
                self._join_output(context.buffered_output) or "namespaced 登录脚本执行失败",
                status_code=502,
                error_type="api_error",
            )

        summary = self._load_summary(context.work_dir)
        final_status = summary.get("final_status") or {}
        login_payload = summary.get("login") or {}
        machine_id = self._extract_machine_id(str(login_payload.get("loginUrl") or context.auth_url))
        if not machine_id:
            raise QoderAPIError("namespaced 登录结果缺少 machine_id", status_code=500)

        info = self._build_cached_user_info(final_status)
        ensure_auth_fields(info)
        save_machine_id(qoder_home, machine_id)
        save_cached_user_info(qoder_home, info)
        self._emit(progress_callback, f"登录成功，当前账号：{info.name or info.uid}")
        return info

    @staticmethod
    def _emit(callback: Callable[[str], None] | None, message: str) -> None:
        if callback is not None:
            callback(message)

    @staticmethod
    def _join_output(lines: list[str]) -> str:
        meaningful = [line for line in lines if line.strip()]
        return "\n".join(meaningful[-80:])

    @staticmethod
    def _drain_available_lines(process: subprocess.Popen[str]) -> list[str]:
        stdout = process.stdout
        if stdout is None:
            return []
        collected: list[str] = []
        while True:
            ready, _, _ = select.select([stdout], [], [], 0)
            if not ready:
                break
            line = stdout.readline()
            if not line:
                break
            collected.append(line.rstrip("\n"))
        return collected

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            process.terminate()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.2)
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            process.kill()

    @staticmethod
    def _load_summary(work_dir: Path) -> dict[str, Any]:
        summary_path = work_dir.parent.parent / "summary.json"
        if not summary_path.is_file():
            raise QoderAPIError(
                f"找不到 namespaced 登录汇总文件: {summary_path}",
                status_code=500,
                error_type="api_error",
            )
        try:
            return json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QoderAPIError(
                f"读取 namespaced 登录汇总失败: {exc}",
                status_code=500,
                error_type="api_error",
            ) from exc

    @staticmethod
    def _extract_machine_id(login_url: str) -> str:
        query = parse_qs(urlparse(login_url).query)
        return (query.get("machine_id") or [""])[0].strip()

    @staticmethod
    def _build_cached_user_info(final_status: dict[str, Any]) -> CachedUserInfo:
        now_ms = int(time.time() * 1000)
        return CachedUserInfo(
            aid=str(final_status.get("accountId") or final_status.get("id") or ""),
            uid=str(final_status.get("id") or ""),
            name=str(final_status.get("name") or ""),
            organization_id=str(final_status.get("orgId") or ""),
            organization_name=str(final_status.get("orgName") or ""),
            avatar_url=str(final_status.get("avatarUrl") or ""),
            access_token=str(final_status.get("token") or ""),
            security_oauth_token=str(final_status.get("token") or ""),
            refresh_token=str(final_status.get("refreshToken") or ""),
            expire_time=int(final_status.get("expireTime") or 0),
            refresh_token_expire_time=0,
            user_source_channel="common",
            user_type=str(final_status.get("userType") or "personal_standard"),
            data_policy_agreed=bool(final_status.get("privacyPolicyAgreed")),
            login_method="browser",
            email=str(final_status.get("email") or ""),
            user_tag="",
            is_data_policy_modifiable=bool(final_status.get("isPrivacyPolicyModifiable")),
            login_device_id="",
            login_timestamp=now_ms,
        )
