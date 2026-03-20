"""通过 namespaced 官方工作流完成浏览器登录。"""

from __future__ import annotations

import json
import selectors
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .auth_state import (
    CachedUserInfo,
    ensure_auth_dir,
    load_cached_user_info,
    save_cached_user_info,
    save_machine_id,
)
from .qoder_api import QoderAPIError
from .settings import Settings

_DEVICE_METADATA_NAME = "xieyi_device.json"
_LOGIN_URL_PREFIX = "LOGIN_URL="
_WORK_DIR_PREFIX = "WORK_DIR="
_MACHINE_TOKEN_PREFIX = "MACHINE_TOKEN="
_MACHINE_TYPE_PREFIX = "MACHINE_TYPE="
_WAIT_LOGIN_URL_TIMEOUT_SECONDS = 120.0
_POLL_INTERVAL_SECONDS = 0.5


@dataclass(slots=True)
class NamespacedBrowserLoginContext:
    auth_url: str
    work_dir: Path
    machine_id: str
    machine_token: str
    machine_type: str
    process: subprocess.Popen[str]
    buffered_output: list[str] = field(default_factory=list)


class NamespacedBrowserLoginClient:
    """启动 qoder_namespaced_login.py，并在成功后接管其登录态。"""

    def __init__(
        self,
        settings: Settings,
        *,
        process_factory: Callable[[Path], subprocess.Popen[str]] | None = None,
    ) -> None:
        self.settings = settings
        self._process_factory = process_factory or self._start_login_process

    def create_browser_login(self, qoder_home: Path) -> NamespacedBrowserLoginContext:
        script_path = self.settings.namespaced_login_script_path
        if not script_path.is_file():
            raise QoderAPIError(
                f"找不到 namespaced 登录脚本: {script_path}",
                status_code=500,
                error_type="configuration_error",
            )

        process = self._process_factory(script_path)
        buffered_output: list[str] = []
        details = self._wait_until_login_url(process, buffered_output)
        auth_url = details.get("auth_url") or ""
        work_dir_value = details.get("work_dir") or ""
        if not auth_url:
            self._terminate_process(process)
            raise QoderAPIError(
                "namespaced 登录脚本未输出 LOGIN_URL",
                status_code=502,
                error_type="api_error",
            )
        if not work_dir_value:
            self._terminate_process(process)
            raise QoderAPIError(
                "namespaced 登录脚本未输出 WORK_DIR",
                status_code=502,
                error_type="api_error",
            )

        machine_id = self._extract_machine_id(auth_url)
        save_machine_id(qoder_home, machine_id)
        return NamespacedBrowserLoginContext(
            auth_url=auth_url,
            work_dir=Path(work_dir_value).expanduser().resolve(),
            machine_id=machine_id,
            machine_token=details.get("machine_token") or "",
            machine_type=details.get("machine_type") or "",
            process=process,
            buffered_output=buffered_output,
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

        output_lines = list(context.buffered_output)
        self._drain_process_output(
            context.process,
            output_lines,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        return_code = context.process.wait(timeout=5)
        if return_code != 0:
            raise QoderAPIError(
                self._build_process_error(output_lines) or "namespaced 登录流程失败",
                status_code=502,
                error_type="api_error",
            )

        summary = self._extract_summary_output(output_lines)
        self._copy_auth_cache(qoder_home, context.work_dir)
        self._persist_device_identity(qoder_home=qoder_home, context=context, summary=summary)

        info = load_cached_user_info(qoder_home)
        if info is None:
            raise QoderAPIError(
                "namespaced 登录结束后未找到本地登录态",
                status_code=502,
                error_type="api_error",
            )
        if not info.login_device_id:
            info.login_device_id = context.machine_id
            save_cached_user_info(qoder_home, info)
        return info

    def _start_login_process(self, script_path: Path) -> subprocess.Popen[str]:
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

        try:
            return subprocess.Popen(
                cmd,
                cwd=str(script_path.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise QoderAPIError(
                f"启动 namespaced 登录脚本失败: {exc}",
                status_code=500,
                error_type="configuration_error",
            ) from exc

    def _wait_until_login_url(
        self,
        process: subprocess.Popen[str],
        output_lines: list[str],
    ) -> dict[str, str]:
        details: dict[str, str] = {}
        deadline = time.monotonic() + min(
            self.settings.namespaced_login_timeout_seconds,
            _WAIT_LOGIN_URL_TIMEOUT_SECONDS,
        )
        with self._selector_for_process(process) as selector:
            while time.monotonic() < deadline:
                line = self._read_one_line(process, selector, timeout=_POLL_INTERVAL_SECONDS)
                if line is None:
                    if process.poll() is not None:
                        break
                    continue
                normalized = line.rstrip("\r\n")
                output_lines.append(normalized)
                self._capture_line(details, normalized)
                if details.get("auth_url") and details.get("work_dir"):
                    return details

        self._read_remaining_output(process, output_lines)
        raise QoderAPIError(
            self._build_process_error(output_lines) or "等待 namespaced 登录链接超时",
            status_code=504,
            error_type="timeout_error",
        )

    def _drain_process_output(
        self,
        process: subprocess.Popen[str],
        output_lines: list[str],
        *,
        progress_callback: Callable[[str], None] | None,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        with self._selector_for_process(process) as selector:
            while True:
                if cancel_check and cancel_check():
                    self._terminate_process(process)
                    raise QoderAPIError(
                        "登录已取消",
                        status_code=409,
                        error_type="cancelled",
                    )
                line = self._read_one_line(process, selector, timeout=_POLL_INTERVAL_SECONDS)
                if line is None:
                    if process.poll() is not None:
                        break
                    continue
                normalized = line.rstrip("\r\n")
                output_lines.append(normalized)
                self._emit(progress_callback, normalized)
        self._read_remaining_output(process, output_lines)

    @staticmethod
    def _selector_for_process(process: subprocess.Popen[str]) -> selectors.BaseSelector:
        if process.stdout is None:
            raise QoderAPIError(
                "namespaced 登录脚本未提供 stdout",
                status_code=500,
                error_type="configuration_error",
            )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        return selector

    @staticmethod
    def _read_one_line(
        process: subprocess.Popen[str],
        selector: selectors.BaseSelector,
        *,
        timeout: float,
    ) -> str | None:
        events = selector.select(timeout)
        if not events:
            return None
        assert process.stdout is not None
        line = process.stdout.readline()
        if line == "":
            return None
        return line

    @staticmethod
    def _read_remaining_output(process: subprocess.Popen[str], output_lines: list[str]) -> None:
        if process.stdout is None:
            return
        remainder = process.stdout.read()
        if not remainder:
            return
        for line in remainder.splitlines():
            output_lines.append(line)

    @staticmethod
    def _capture_line(details: dict[str, str], line: str) -> None:
        stripped = line.strip()
        if stripped.startswith(_LOGIN_URL_PREFIX):
            details["auth_url"] = stripped.removeprefix(_LOGIN_URL_PREFIX).strip()
        elif stripped.startswith(_WORK_DIR_PREFIX):
            details["work_dir"] = stripped.removeprefix(_WORK_DIR_PREFIX).strip()
        elif stripped.startswith(_MACHINE_TOKEN_PREFIX):
            details["machine_token"] = stripped.removeprefix(_MACHINE_TOKEN_PREFIX).strip()
        elif stripped.startswith(_MACHINE_TYPE_PREFIX):
            details["machine_type"] = stripped.removeprefix(_MACHINE_TYPE_PREFIX).strip()

    @staticmethod
    def _extract_machine_id(auth_url: str) -> str:
        params = parse_qs(urlsplit(auth_url).query)
        machine_id = ""
        values = params.get("machine_id") or []
        if values:
            machine_id = values[0].strip()
        if not machine_id:
            raise QoderAPIError(
                "namespaced 登录链接缺少 machine_id",
                status_code=502,
                error_type="api_error",
            )
        return machine_id

    @staticmethod
    def _copy_auth_cache(qoder_home: Path, work_dir: Path) -> None:
        cache_dir = work_dir / "cache"
        source_id = cache_dir / "id"
        source_user = cache_dir / "user"
        if not source_id.is_file() or not source_user.is_file():
            raise QoderAPIError(
                f"namespaced 工作目录缺少登录缓存: {work_dir}",
                status_code=502,
                error_type="api_error",
            )
        auth_dir = ensure_auth_dir(qoder_home)
        shutil.copy2(source_id, auth_dir / "id")
        shutil.copy2(source_user, auth_dir / "user")

    def _persist_device_identity(
        self,
        *,
        qoder_home: Path,
        context: NamespacedBrowserLoginContext,
        summary: dict[str, object],
    ) -> None:
        device = summary.get("device") if isinstance(summary.get("device"), dict) else {}
        device_info = device.get("machine_info") if isinstance(device, dict) else {}
        payload = {
            "workflow": "namespaced_qoder",
            "output_dir": str(summary.get("outputDir") or ""),
            "summary_path": self._build_summary_path(summary),
            "machine_id": context.machine_id,
            "machine_token": context.machine_token or str(device_info.get("machineToken") or ""),
            "machine_type": context.machine_type or str(device_info.get("machineType") or ""),
            "hostname": str(device.get("hostname") or ""),
            "mac": str(device.get("mac") or ""),
        }
        auth_dir = ensure_auth_dir(qoder_home)
        (auth_dir / _DEVICE_METADATA_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _build_summary_path(summary: dict[str, object]) -> str:
        output_dir = str(summary.get("outputDir") or "").strip()
        if not output_dir:
            return ""
        return str(Path(output_dir) / "summary.json")

    @staticmethod
    def _extract_summary_output(output_lines: list[str]) -> dict[str, object]:
        text = "\n".join(output_lines).strip()
        if not text:
            raise QoderAPIError(
                "namespaced 登录脚本没有输出结果",
                status_code=502,
                error_type="api_error",
            )
        decoder = json.JSONDecoder()
        for index in range(len(text) - 1, -1, -1):
            if text[index] != "{":
                continue
            try:
                payload, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if text[index + end :].strip():
                continue
            if isinstance(payload, dict):
                return payload
        raise QoderAPIError(
            "解析 namespaced 登录脚本输出失败",
            status_code=502,
            error_type="api_error",
        )

    @staticmethod
    def _build_process_error(output_lines: list[str]) -> str:
        meaningful = [line.strip() for line in output_lines if line.strip()]
        if not meaningful:
            return ""
        return "\n".join(meaningful[-20:])[-4000:]

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return

    @staticmethod
    def _emit(callback: Callable[[str], None] | None, message: str) -> None:
        if callback is not None:
            callback(message)
