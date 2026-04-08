"""Qoder 运行时调用器。纯协议直连 Qoder 上游。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .accounts import (
    QoderAccount,
    QoderAccountPool,
    mark_account_request_failure,
    mark_account_request_started,
    mark_account_request_success,
)
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

    def _select_contexts(self, preferred_account_id: str | None = None) -> list[QoderExecutionContext]:
        if self.account_pool is None:
            return [self._default_context()]

        accounts = self.account_pool.list_accounts()
        candidates = self.account_pool.pick_accounts_for_request(
            preferred_account_id=preferred_account_id
        )
        if not candidates and (accounts or self.account_pool.is_configured()):
            raise QoderCLIError(
                "账号池中没有可用的已启用账号，可能账号已失效、已停用或正在冷却中",
                status_code=401,
                error_type="authentication_error",
            )
        if not candidates:
            return [self._default_context()]
        return [self._context_from_account(account) for account in candidates]

    def select_context(self, preferred_account_id: str | None = None) -> QoderExecutionContext:
        return self._select_contexts(preferred_account_id=preferred_account_id)[0]

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

    @staticmethod
    def _is_retryable_account_error(error: QoderCLIError) -> bool:
        if error.status_code == 400 and error.error_type == "invalid_request_error":
            return False
        if error.error_type in {"authentication_error", "timeout_error", "rate_limit_error", "api_error"}:
            return True
        return error.status_code in {401, 408, 409, 429, 500, 502, 503, 504} or error.status_code >= 500

    def _should_try_next_account(
        self,
        error: QoderCLIError,
        *,
        preferred_account_id: str | None,
        has_more_candidates: bool,
    ) -> bool:
        return (
            self.account_pool is not None
            and preferred_account_id is None
            and has_more_candidates
            and self._is_retryable_account_error(error)
        )

    def _mark_account_success(self, context: QoderExecutionContext) -> None:
        if self.account_pool is None or context.account_id in {"default", "legacy-primary"}:
            return
        try:
            mark_account_request_success(self.settings, context.account_id)
        except KeyError:
            return

    def _mark_account_failure(self, context: QoderExecutionContext, error: QoderCLIError) -> None:
        if self.account_pool is None or context.account_id in {"default", "legacy-primary"}:
            return
        if not self._is_retryable_account_error(error):
            return
        try:
            mark_account_request_failure(
                self.settings,
                context.account_id,
                error_message=str(error),
            )
        except KeyError:
            return

    def _mark_account_started(self, context: QoderExecutionContext) -> None:
        if self.account_pool is None or context.account_id in {"default", "legacy-primary"}:
            return
        try:
            mark_account_request_started(self.settings, context.account_id)
        except KeyError:
            return

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
        contexts = self._select_contexts(preferred_account_id=preferred_account_id)
        last_error: QoderCLIError | None = None

        for index, context in enumerate(contexts):
            model_info, resolved_model = self._resolve_model_info(context, model)
            self._validate_attachments(attachments, model_info)
            self._mark_account_started(context)
            try:
                output_text = await self.api_client.run_text(
                    qoder_home=context.qoder_home,
                    prompt=prompt,
                    attachments=attachments,
                    messages=messages,
                    model_info=model_info,
                    max_tokens=max_tokens,
                )
                output_text = output_text.strip()
                if not output_text:
                    raise QoderCLIError("Qoder 推理返回了空响应", status_code=502)
            except QoderAPIError as exc:
                error = QoderCLIError(
                    str(exc),
                    status_code=exc.status_code,
                    error_type=exc.error_type,
                )
                self._mark_account_failure(context, error)
                last_error = error
                if self._should_try_next_account(
                    error,
                    preferred_account_id=preferred_account_id,
                    has_more_candidates=index < len(contexts) - 1,
                ):
                    continue
                raise error from exc
            except QoderCLIError as error:
                self._mark_account_failure(context, error)
                last_error = error
                if self._should_try_next_account(
                    error,
                    preferred_account_id=preferred_account_id,
                    has_more_candidates=index < len(contexts) - 1,
                ):
                    continue
                raise

            self._mark_account_success(context)
            return output_text, resolved_model

        if last_error is None:
            raise QoderCLIError("账号池中没有可用账号", status_code=401, error_type="authentication_error")
        raise last_error

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
        contexts = self._select_contexts(preferred_account_id=preferred_account_id)
        attempts: list[tuple[QoderExecutionContext, QoderModelInfo, str]] = []
        last_error: QoderCLIError | None = None

        for context in contexts:
            try:
                model_info, resolved_model = self._resolve_model_info(context, model)
                self._validate_attachments(attachments, model_info)
            except QoderCLIError as error:
                last_error = error
                if preferred_account_id is not None:
                    raise
                continue
            attempts.append((context, model_info, resolved_model))

        if not attempts:
            if last_error is not None:
                raise last_error
            raise QoderCLIError("账号池中没有可用账号", status_code=401, error_type="authentication_error")

        async def _api_iterator() -> AsyncIterator[ParsedStreamEvent]:
            stream_error: QoderCLIError | None = None

            for index, (context, model_info, _) in enumerate(attempts):
                emitted_any = False
                self._mark_account_started(context)
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
                        if (
                            event.text_delta
                            or event.reasoning_delta
                            or event.reasoning_signature_delta
                            or event.tool_call_deltas
                            or event.finish_reason
                        ):
                            emitted_any = True
                            yield event
                except QoderAPIError as exc:
                    error = QoderCLIError(
                        str(exc),
                        status_code=exc.status_code,
                        error_type=exc.error_type,
                    )
                    self._mark_account_failure(context, error)
                    stream_error = error
                    if not emitted_any and self._should_try_next_account(
                        error,
                        preferred_account_id=preferred_account_id,
                        has_more_candidates=index < len(attempts) - 1,
                    ):
                        continue
                    raise error from exc

                self._mark_account_success(context)
                return

            if stream_error is not None:
                raise stream_error

        return _api_iterator(), attempts[0][2]
