"""Qoder 运行时调用器。纯协议直连 Qoder 上游。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .accounts import QoderAccount, QoderAccountPool
from .catalog import QoderModelInfo, load_qoder_models, resolve_model_name
from .prompting import PreparedAttachment
from .qoder_api import QoderAPIClient, QoderAPIError
from .settings import Settings
from .streaming import ParsedStreamEvent


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
    """兼容原接口的纯协议调用器。"""

    def __init__(
        self,
        settings: Settings,
        account_pool: QoderAccountPool | None = None,
        api_client: QoderAPIClient | None = None,
    ) -> None:
        self.settings = settings
        self.account_pool = account_pool
        self.api_client = api_client or QoderAPIClient(settings)

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
    def _validate_attachments(
        attachments: list[PreparedAttachment],
        model_info: QoderModelInfo,
    ) -> None:
        if attachments and not model_info.is_vl:
            raise QoderCLIError(
                f"模型 {model_info.public_id} 当前不支持图片输入",
                status_code=400,
                error_type="invalid_request_error",
            )

    async def run_text(
        self,
        *,
        prompt: str,
        attachments: list[PreparedAttachment],
        messages: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        preferred_account_id: str | None = None,
    ) -> tuple[str, str]:
        context = self.select_context(preferred_account_id=preferred_account_id)
        model_info, resolved_model = self._resolve_model_info(context, model)
        self._validate_attachments(attachments, model_info)
        try:
            output_text = await self.api_client.run_text(
                qoder_home=context.qoder_home,
                prompt=prompt,
                attachments=attachments,
                messages=messages,
                model_info=model_info,
                max_tokens=max_tokens,
            )
        except QoderAPIError as exc:
            raise QoderCLIError(
                str(exc),
                status_code=exc.status_code,
                error_type=exc.error_type,
            ) from exc
        output_text = output_text.strip()
        if not output_text:
            raise QoderCLIError("Qoder 推理返回了空响应", status_code=502)
        return output_text, resolved_model

    async def stream_events(
        self,
        *,
        prompt: str,
        attachments: list[PreparedAttachment],
        messages: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        preferred_account_id: str | None = None,
    ) -> tuple[AsyncIterator[ParsedStreamEvent], str]:
        context = self.select_context(preferred_account_id=preferred_account_id)
        model_info, resolved_model = self._resolve_model_info(context, model)
        self._validate_attachments(attachments, model_info)

        async def _api_iterator() -> AsyncIterator[ParsedStreamEvent]:
            try:
                async for event in self.api_client.stream_chat(
                    qoder_home=context.qoder_home,
                    prompt=prompt,
                    attachments=attachments,
                    messages=messages,
                    model_info=model_info,
                    max_tokens=max_tokens,
                    tools=tools,
                    tool_choice=tool_choice,
                ):
                    if event.text_delta or event.tool_call_deltas or event.finish_reason:
                        yield event
            except QoderAPIError as exc:
                raise QoderCLIError(
                    str(exc),
                    status_code=exc.status_code,
                    error_type=exc.error_type,
                ) from exc

        return _api_iterator(), resolved_model
