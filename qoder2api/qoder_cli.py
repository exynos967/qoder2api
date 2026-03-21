"""Qoder 运行时调用器。优先复用官方 qodercli，确保上游请求与官方一致。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .accounts import QoderAccount, QoderAccountPool
from .catalog import QoderModelInfo, load_qoder_models, resolve_model_name
from .qoder_api import QoderAPIClient
from .runtime_settings import load_runtime_settings
from .settings import Settings, discover_qoder_cli_path
from .streaming import ParsedStreamEvent, QoderStreamJSONParser, _join_text_blocks


class QoderCLIError(RuntimeError):
    """统一给 API 层抛出的上游错误。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        error_type: str = "api_error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


@dataclass(slots=True)
class QoderExecutionContext:
    """单次执行上下文。"""

    account_id: str
    account_label: str
    qoder_home: Path
    workspace: Path
    models: list[QoderModelInfo]


class QoderCLIInvoker:
    """兼容原接口的官方 qodercli 调用器。"""

    def __init__(
        self,
        settings: Settings,
        account_pool: QoderAccountPool | None = None,
        api_client: QoderAPIClient | None = None,
    ) -> None:
        self.settings = settings
        self.account_pool = account_pool
        self.api_client = api_client

    @property
    def qoder_home(self) -> Path:
        return self.settings.qoder_home_path

    @property
    def workspace(self) -> Path:
        return self.settings.workspace_path

    def _load_models(self, qoder_home: Path) -> list[QoderModelInfo]:
        return load_qoder_models(qoder_home, scene=self.settings.model_scene)

    def _default_context(self) -> QoderExecutionContext:
        return QoderExecutionContext(
            account_id="default",
            account_label="默认账号",
            qoder_home=self.qoder_home,
            workspace=self.workspace,
            models=self._load_models(self.qoder_home),
        )

    def _context_from_account(self, account: QoderAccount) -> QoderExecutionContext:
        return QoderExecutionContext(
            account_id=account.id,
            account_label=account.label,
            qoder_home=account.qoder_home_path,
            workspace=self.workspace,
            models=self._load_models(account.qoder_home_path),
        )

    def select_context(self, preferred_account_id: str | None = None) -> QoderExecutionContext:
        if self.account_pool is None:
            return self._default_context()

        accounts = self.account_pool.list_accounts()
        account = self.account_pool.pick_account(preferred_account_id=preferred_account_id)
        if account is None and (accounts or self.account_pool.is_configured()):
            raise QoderCLIError(
                "账号池中没有可用的已启用账号，请先在 WebUI 中登录并启用账号",
                status_code=401,
                error_type="authentication_error",
            )
        if account is None:
            return self._default_context()
        return self._context_from_account(account)

    def list_models(self, preferred_account_id: str | None = None) -> list[QoderModelInfo]:
        return list(self.select_context(preferred_account_id).models)

    def resolve_model(self, requested_model: str | None, models: list[QoderModelInfo]) -> str:
        return resolve_model_name(requested_model, models, self.settings.default_model)

    def _resolve_model_info(
        self,
        context: QoderExecutionContext,
        requested_model: str | None,
    ) -> tuple[QoderModelInfo, str]:
        resolved_model = self.resolve_model(requested_model, context.models)
        for model in context.models:
            if model.id == resolved_model:
                return model, resolved_model
        raise QoderCLIError(
            f"找不到模型配置: {resolved_model}",
            status_code=400,
            error_type="invalid_request_error",
        )

    @staticmethod
    def _ensure_no_attachments(attachments: list[Path]) -> None:
        if attachments:
            raise QoderCLIError(
                "官方 qodercli 非交互模式暂不支持本地图片附件上传",
                status_code=400,
                error_type="invalid_request_error",
            )

    def _resolve_cli_executable(self, context: QoderExecutionContext) -> Path:
        preferred = discover_qoder_cli_path(context.qoder_home, self.settings.qoder_cli_path)
        if preferred.is_file():
            return preferred.resolve()

        fallback = self.settings.cli_executable
        if fallback.is_file():
            return fallback.resolve()

        raise QoderCLIError(
            f"找不到 qodercli 可执行文件: {preferred}",
            status_code=500,
            error_type="configuration_error",
        )

    def _build_cli_env(self) -> dict[str, str]:
        runtime_settings = load_runtime_settings(self.settings)
        env = os.environ.copy()
        if runtime_settings.proxy_url:
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            ):
                env[key] = runtime_settings.proxy_url
        if runtime_settings.llmapi_api_key:
            env["LLMAPI_API_KEY"] = runtime_settings.llmapi_api_key
            env["QODER2API_LLMAPI_API_KEY"] = runtime_settings.llmapi_api_key
        return env

    @staticmethod
    def _map_max_output_tokens(max_tokens: int | None) -> str | None:
        if not max_tokens or max_tokens <= 0:
            return None
        return "16k" if max_tokens <= 16_384 else "32k"

    def _build_cli_command(
        self,
        *,
        context: QoderExecutionContext,
        prompt: str,
        model_info: QoderModelInfo,
        max_tokens: int | None,
        output_format: str,
    ) -> list[str]:
        command = [
            str(self._resolve_cli_executable(context)),
            "-w",
            str(context.workspace),
            "-p",
            prompt,
            "-f",
            output_format,
            "-q",
            "--model",
            model_info.id,
        ]
        mapped_tokens = self._map_max_output_tokens(max_tokens)
        if mapped_tokens is not None:
            command.extend(["--max-output-tokens", mapped_tokens])
        return command

    @staticmethod
    def _extract_cli_message_text(payload: dict[str, Any]) -> str:
        for candidate in (
            payload.get("message"),
            payload.get("result"),
            payload,
        ):
            text = _join_text_blocks(candidate)
            if text:
                return text
        return ""

    @classmethod
    def _extract_cli_error_message(cls, payload: dict[str, Any], stderr_text: str = "") -> str:
        candidates: list[Any] = [
            payload.get("message"),
            payload.get("error"),
            payload.get("cause"),
            payload.get("detail"),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, dict):
                nested = cls._extract_cli_message_text(candidate)
                if nested:
                    return nested.strip()
                for key in ("message", "error", "cause", "detail"):
                    value = candidate.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        text = stderr_text.strip()
        if text:
            return text.splitlines()[-1].strip()
        return "qodercli 调用失败"

    @classmethod
    def _build_cli_error(cls, message: str) -> QoderCLIError:
        normalized = (message or "").strip() or "qodercli 调用失败"
        lower = normalized.lower()
        if any(keyword in lower for keyword in ("not logged in", "login failed", "auth", "token invalid")):
            return QoderCLIError(normalized, status_code=401, error_type="authentication_error")
        if "rate limit" in lower or "too many requests" in lower:
            return QoderCLIError(normalized, status_code=429, error_type="rate_limit_error")
        if "timeout" in lower:
            return QoderCLIError(normalized, status_code=504, error_type="timeout_error")
        return QoderCLIError(normalized, status_code=502, error_type="api_error")

    @staticmethod
    def _normalize_cli_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("type") == "result" and "result" not in payload and isinstance(payload.get("message"), dict):
            normalized = dict(payload)
            normalized["result"] = payload["message"]
            return normalized
        return payload

    async def _run_cli_json_payload(
        self,
        *,
        context: QoderExecutionContext,
        prompt: str,
        model_info: QoderModelInfo,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        command = self._build_cli_command(
            context=context,
            prompt=prompt,
            model_info=model_info,
            max_tokens=max_tokens,
            output_format="json",
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(context.qoder_home.parent),
            env=self._build_cli_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if process.returncode != 0:
            raise self._build_cli_error(stderr_text or stdout_text)

        payloads: list[dict[str, Any]] = []
        for raw_line in stdout_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if payloads:
            payload = self._normalize_cli_payload(payloads[-1])
            if payload.get("subtype") not in {None, "success", "message"} and not self._extract_cli_message_text(payload):
                raise self._build_cli_error(self._extract_cli_error_message(payload, stderr_text))
            return payload

        plain_text = stdout_text.strip()
        if plain_text:
            return {
                "type": "result",
                "subtype": "success",
                "message": {"content": [{"type": "text", "text": plain_text}]},
            }
        raise self._build_cli_error(stderr_text)

    async def _iter_cli_stream_payloads(
        self,
        *,
        context: QoderExecutionContext,
        prompt: str,
        model_info: QoderModelInfo,
        max_tokens: int | None,
    ) -> AsyncIterator[dict[str, Any]]:
        command = self._build_cli_command(
            context=context,
            prompt=prompt,
            model_info=model_info,
            max_tokens=max_tokens,
            output_format="stream-json",
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(context.qoder_home.parent),
            env=self._build_cli_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        stderr_chunks: list[str] = []

        async def _read_stderr() -> None:
            while True:
                chunk = await process.stderr.readline()
                if not chunk:
                    break
                stderr_chunks.append(chunk.decode("utf-8", errors="replace"))

        stderr_task = asyncio.create_task(_read_stderr())
        try:
            while True:
                chunk = await process.stdout.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": line}]},
                    }
                yield self._normalize_cli_payload(payload)
            return_code = await process.wait()
            await stderr_task
            if return_code != 0:
                raise self._build_cli_error("".join(stderr_chunks))
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

    async def run_text(
        self,
        *,
        prompt: str,
        attachments: list[Path],
        model: str | None,
        max_tokens: int | None,
        preferred_account_id: str | None = None,
    ) -> tuple[str, str]:
        self._ensure_no_attachments(attachments)
        context = self.select_context(preferred_account_id=preferred_account_id)
        model_info, resolved_model = self._resolve_model_info(context, model)
        payload = await self._run_cli_json_payload(
            context=context,
            prompt=prompt,
            model_info=model_info,
            max_tokens=max_tokens,
        )
        output_text = self._extract_cli_message_text(payload).strip()
        if not output_text:
            raise self._build_cli_error(self._extract_cli_error_message(payload))
        return output_text, resolved_model

    async def stream_events(
        self,
        *,
        prompt: str,
        attachments: list[Path],
        model: str | None,
        max_tokens: int | None,
        preferred_account_id: str | None = None,
    ) -> tuple[AsyncIterator[ParsedStreamEvent], str]:
        self._ensure_no_attachments(attachments)
        context = self.select_context(preferred_account_id=preferred_account_id)
        model_info, resolved_model = self._resolve_model_info(context, model)

        async def _iterator() -> AsyncIterator[ParsedStreamEvent]:
            parser = QoderStreamJSONParser()
            async for payload in self._iter_cli_stream_payloads(
                context=context,
                prompt=prompt,
                model_info=model_info,
                max_tokens=max_tokens,
            ):
                event = parser.feed_event(payload)
                if event is None:
                    continue
                if event.error_message:
                    raise self._build_cli_error(event.error_message)
                if event.text_delta or event.finish_reason:
                    yield event

        return _iterator(), resolved_model
