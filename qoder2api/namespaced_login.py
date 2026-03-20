"""通过 xieyi 纯协议设备工作流完成 namespaced 浏览器登录。"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .auth_state import CachedUserInfo, ensure_auth_dir, save_machine_id
from .qoder_api import BrowserLoginContext, QoderAPIClient, QoderAPIError
from .settings import Settings

_DEVICE_METADATA_NAME = "xieyi_device.json"
_DEFAULT_PROTOCOL_TIMEOUT_SECONDS = 30


@dataclass(slots=True)
class XieyiDeviceIdentity:
    output_dir: Path
    summary: dict[str, Any]
    selected_result: dict[str, Any]


@dataclass(slots=True)
class NamespacedBrowserLoginContext:
    auth_url: str
    work_dir: Path
    machine_id: str
    machine_token: str
    machine_type: str
    direct_context: BrowserLoginContext
    buffered_output: list[str]


class NamespacedBrowserLoginClient:
    """先跑 xieyi 纯协议设备构建，再复用独立网页登录轮询。"""

    def __init__(
        self,
        settings: Settings,
        *,
        api_client: QoderAPIClient | None = None,
        builder_runner: Callable[[Path], XieyiDeviceIdentity] | None = None,
    ) -> None:
        self.settings = settings
        self.api_client = api_client or QoderAPIClient(settings)
        self._builder_runner = builder_runner or self._run_xieyi_builder

    def create_browser_login(self, qoder_home: Path) -> NamespacedBrowserLoginContext:
        script_path = self.settings.namespaced_login_script_path
        if not script_path.is_file():
            raise QoderAPIError(
                f"找不到 xieyi 纯协议脚本: {script_path}",
                status_code=500,
                error_type="configuration_error",
            )

        identity = self._builder_runner(script_path)
        selected = identity.selected_result
        machine_id = self._resolve_machine_id(selected)
        save_machine_id(qoder_home, machine_id)
        direct_context = self.api_client.create_browser_login(
            qoder_home,
            machine_id=machine_id,
        )
        self._persist_device_identity(
            qoder_home=qoder_home,
            identity=identity,
            direct_context=direct_context,
        )
        prelude = self._build_prelude_lines(identity, direct_context)
        return NamespacedBrowserLoginContext(
            auth_url=direct_context.auth_url,
            work_dir=identity.output_dir,
            machine_id=machine_id,
            machine_token=str(selected.get("machine_token") or ""),
            machine_type=str(selected.get("machine_type") or ""),
            direct_context=direct_context,
            buffered_output=prelude,
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
        return await self.api_client.complete_browser_login(
            qoder_home=qoder_home,
            context=context.direct_context,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    def _run_xieyi_builder(self, script_path: Path) -> XieyiDeviceIdentity:
        cmd = [
            sys.executable,
            str(script_path),
            "--count",
            str(self.settings.namespaced_login_max_attempts),
            "--json",
        ]
        output_root = self.settings.namespaced_login_output_root_path
        if output_root is not None:
            cmd.extend(["--output-root", str(output_root)])

        protocol_timeout = min(
            self.settings.namespaced_login_timeout_seconds,
            _DEFAULT_PROTOCOL_TIMEOUT_SECONDS,
        )
        cmd.extend(["--timeout", str(protocol_timeout)])
        process_timeout = max(
            protocol_timeout * self.settings.namespaced_login_max_attempts + 10,
            protocol_timeout + 10,
        )

        try:
            completed = subprocess.run(
                cmd,
                cwd=str(script_path.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=process_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise QoderAPIError(
                f"xieyi 纯协议设备构建超时（>{process_timeout}s）",
                status_code=504,
                error_type="timeout_error",
            ) from exc
        except OSError as exc:
            raise QoderAPIError(
                f"启动 xieyi 纯协议脚本失败: {exc}",
                status_code=500,
                error_type="configuration_error",
            ) from exc

        if completed.returncode != 0:
            detail = self._join_output(completed.stdout, completed.stderr)
            raise QoderAPIError(
                detail or "xieyi 纯协议设备构建失败",
                status_code=502,
                error_type="api_error",
            )

        summary = self._parse_summary_output(completed.stdout)
        output_dir = Path(str(summary.get("output_dir") or "")).expanduser().resolve()
        if not summary.get("output_dir"):
            raise QoderAPIError("xieyi 输出缺少 output_dir", status_code=500)

        results = summary.get("results")
        if not isinstance(results, list):
            raise QoderAPIError("xieyi 输出缺少 results", status_code=500)

        selected = next(
            (
                item
                for item in results
                if isinstance(item, dict) and bool(item.get("success"))
            ),
            None,
        )
        if selected is None:
            raise QoderAPIError(
                self._build_failure_message(summary),
                status_code=502,
                error_type="api_error",
            )

        return XieyiDeviceIdentity(
            output_dir=output_dir,
            summary=summary,
            selected_result=selected,
        )

    def _persist_device_identity(
        self,
        *,
        qoder_home: Path,
        identity: XieyiDeviceIdentity,
        direct_context: BrowserLoginContext,
    ) -> None:
        auth_dir = ensure_auth_dir(qoder_home)
        selected = identity.selected_result
        payload = {
            "workflow": "xieyi",
            "output_dir": str(identity.output_dir),
            "summary_path": str(identity.output_dir / "summary.json"),
            "seed": identity.summary.get("seed"),
            "count": identity.summary.get("count"),
            "success_count": identity.summary.get("success_count"),
            "machine_id": direct_context.machine_id,
            "machine_token": str(selected.get("machine_token") or ""),
            "machine_type": str(selected.get("machine_type") or ""),
            "hostname": str(selected.get("hostname") or ""),
            "mac": str(selected.get("mac") or ""),
            "response_path": str(selected.get("response_path") or ""),
        }
        (auth_dir / _DEVICE_METADATA_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_prelude_lines(
        self,
        identity: XieyiDeviceIdentity,
        direct_context: BrowserLoginContext,
    ) -> list[str]:
        selected = identity.selected_result
        return [
            "xieyi 纯协议设备构建完成，开始创建网页登录链接。",
            f"WORK_DIR={identity.output_dir}",
            f"MACHINE_TOKEN_PREVIEW={self._mask_secret(str(selected.get('machine_token') or ''))}",
            f"MACHINE_TYPE={str(selected.get('machine_type') or '')}",
            f"DEVICE_HOSTNAME={str(selected.get('hostname') or '')}",
            f"DEVICE_MAC={str(selected.get('mac') or '')}",
            f"MACHINE_ID={direct_context.machine_id}",
        ]

    @staticmethod
    def _resolve_machine_id(selected: dict[str, Any]) -> str:
        machine_id = str(selected.get("machine_id") or "").strip()
        return machine_id or str(uuid.uuid4())

    @staticmethod
    def _mask_secret(value: str) -> str:
        normalized = value.strip()
        if len(normalized) <= 16:
            return normalized
        return f"{normalized[:8]}...{normalized[-6:]}"

    @staticmethod
    def _emit(callback: Callable[[str], None] | None, message: str) -> None:
        if callback is not None:
            callback(message)

    @staticmethod
    def _join_output(stdout: str, stderr: str) -> str:
        chunks = [stdout.strip(), stderr.strip()]
        meaningful = [item for item in chunks if item]
        if not meaningful:
            return ""
        return "\n".join(meaningful)[-4000:]

    @staticmethod
    def _parse_summary_output(stdout: str) -> dict[str, Any]:
        text = stdout.strip()
        if not text:
            raise QoderAPIError("xieyi 没有输出任何结果", status_code=500)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise QoderAPIError(
                f"解析 xieyi 输出失败: {exc}",
                status_code=500,
                error_type="api_error",
            ) from exc
        if not isinstance(payload, dict):
            raise QoderAPIError("xieyi 输出格式异常", status_code=500)
        return payload

    @staticmethod
    def _build_failure_message(summary: dict[str, Any]) -> str:
        details: list[str] = []
        for item in summary.get("results") or []:
            if not isinstance(item, dict):
                continue
            attempt = item.get("attempt")
            error = str(item.get("error") or "").strip()
            ec = item.get("ec")
            if error:
                details.append(f"attempt={attempt}, ec={ec}, error={error}")
            else:
                details.append(f"attempt={attempt}, ec={ec}, success=False")
        if not details:
            return "xieyi 没有生成可用的设备身份"
        return "xieyi 没有生成可用的设备身份：\n" + "\n".join(details[-5:])
