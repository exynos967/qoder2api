"""qoder2api FastAPI 应用。"""

from __future__ import annotations

import json
import math
import logging
import time
import uuid
from functools import lru_cache
from logging.config import dictConfig
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .accounts import (
    QoderAccount,
    QoderAccountPool,
    create_pending_managed_account,
    finalize_pending_account,
    get_account,
    load_device_metadata,
    remove_account,
    update_account_enabled,
    update_account_label,
)
from .account_status import load_account_status, refresh_account_status
from .anthropic_messages import (
    AnthropicMessagesRequest,
    AnthropicStreamTranslator,
    _build_tool_schema_map,
    _apply_stop_sequences,
    build_anthropic_message_response,
    convert_anthropic_request_to_chat_request,
)
from .auth_state import inspect_local_auth
from .catalog import QoderModelInfo, load_qoder_models
from .concurrency import AdjustableConcurrencyLimiter
from .login import QoderLoginError, QoderLoginManager
from .prompting import PreparedPrompt, render_openai_messages
from .qoder_api import QoderAPIClient, QoderAPIError
from .qoder_cli import QoderCLIError, QoderCLIInvoker
from .registration import QoderRegistrationError, QoderRegistrationManager
from .streaming import merge_tool_call_deltas
from .runtime_settings import RuntimeSettingsManager
from .schemas import (
    AccountLabelUpdateRequest,
    AccountToggleRequest,
    ChatCompletionRequest,
    LoginSessionInputRequest,
    RegistrationStartRequest,
    RuntimeSettingsUpdateRequest,
    StartBrowserLoginRequest,
    RegistrationWorkerProgressRequest,
    RegistrationWorkerCompleteRequest,
    RegistrationWorkerFailRequest,
)
from .settings import Settings
from . import __version__
from .webui import build_logs_payload, get_webui_index_path, get_webui_static_dir


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    ascii_count = max(len(text) - cjk_count, 0)
    return max(1, int(cjk_count / 1.5 + ascii_count / 4.0))


def _openai_error(message: str, *, error_type: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type}},
    )


def _anthropic_error(message: str, *, error_type: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


def _error_response_for_request(
    request: Request | None,
    *,
    message: str,
    error_type: str,
    status_code: int,
) -> JSONResponse:
    if request is not None and request.url.path == "/v1/messages":
        return _anthropic_error(message, error_type=error_type, status_code=status_code)
    return _openai_error(message, error_type=error_type, status_code=status_code)


def _sse_payload(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_log_config(settings: Settings) -> dict[str, Any]:
    log_path = settings.app_log_path
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            },
            "access": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.FileHandler",
                "formatter": "default",
                "filename": str(log_path),
                "encoding": "utf-8",
            },
            "access_file": {
                "class": "logging.FileHandler",
                "formatter": "access",
                "filename": str(log_path),
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {
                "handlers": ["console", "access_file"],
                "level": "INFO",
                "propagate": False,
            },
            "qoder2api": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["console", "file"], "level": "INFO"},
    }


def _model_to_openai_item(model: QoderModelInfo) -> dict[str, Any]:
    return {
        "id": model.public_id,
        "object": "model",
        "created": 0,
        "owned_by": "qoder",
        "permission": [],
        "root": model.public_id,
        "parent": None,
        "display_name": model.display_name,
        "qoder_model_id": model.id,
        "context_window": model.max_input_tokens,
        "max_output_tokens": model.max_output_tokens,
        "is_reasoning": model.is_reasoning,
        "price_factor": model.price_factor,
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_invoker() -> QoderCLIInvoker:
    settings = get_settings()
    return QoderCLIInvoker(
        settings,
        get_account_pool(),
        api_client=QoderAPIClient(settings),
    )


@lru_cache
def get_login_manager() -> QoderLoginManager:
    return QoderLoginManager(get_settings())


@lru_cache
def get_registration_manager() -> QoderRegistrationManager:
    return QoderRegistrationManager(get_settings(), login_manager=get_login_manager())


@lru_cache
def get_account_pool() -> QoderAccountPool:
    return QoderAccountPool(get_settings())


def _get_runtime_services() -> tuple[RuntimeSettingsManager, AdjustableConcurrencyLimiter]:
    settings = get_settings()
    service_key = str(settings.workspace_path)
    services = _runtime_services.get(service_key)
    if services is None:
        manager = RuntimeSettingsManager(settings)
        snapshot = manager.load()
        services = (
            manager,
            AdjustableConcurrencyLimiter(snapshot.account_pool_max_concurrency),
        )
        _runtime_services[service_key] = services
    return services


def get_runtime_settings_manager() -> RuntimeSettingsManager:
    return _get_runtime_services()[0]


def get_invocation_limiter() -> AdjustableConcurrencyLimiter:
    return _get_runtime_services()[1]


def _resolve_openai_request_stream_mode(stream: bool | None) -> bool:
    if stream is not None:
        return stream
    return get_runtime_settings_manager().load().default_stream_openai


def _resolve_anthropic_request_stream_mode(stream: bool | None) -> bool:
    if stream is not None:
        return stream
    return get_runtime_settings_manager().load().default_stream_anthropic


def _log_debug_request(route: str, payload: dict[str, Any]) -> None:
    snapshot = get_runtime_settings_manager().load()
    if not snapshot.debug_logging_enabled:
        return
    logger.info(
        "debug_request route=%s payload=%s",
        route,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


app = FastAPI(
    title="qoder2api",
    description="独立 Qoder OpenAI 兼容代理。",
    version=__version__,
)
logger = logging.getLogger("qoder2api")
_runtime_services: dict[str, tuple[RuntimeSettingsManager, AdjustableConcurrencyLimiter]] = {}

_webui_static_dir = get_webui_static_dir()
if _webui_static_dir.exists():
    app.mount("/webui/static", StaticFiles(directory=_webui_static_dir), name="webui_static")


@app.exception_handler(QoderCLIError)
async def handle_qoder_error(request: Request, exc: QoderCLIError) -> JSONResponse:
    return _error_response_for_request(
        request,
        message=str(exc),
        error_type=exc.error_type,
        status_code=exc.status_code,
    )


@app.exception_handler(QoderLoginError)
async def handle_qoder_login_error(request: Request, exc: QoderLoginError) -> JSONResponse:
    return _error_response_for_request(
        request,
        message=str(exc),
        error_type=exc.error_type,
        status_code=exc.status_code,
    )


@app.exception_handler(QoderAPIError)
async def handle_qoder_api_error(request: Request, exc: QoderAPIError) -> JSONResponse:
    return _error_response_for_request(
        request,
        message=str(exc),
        error_type=exc.error_type,
        status_code=exc.status_code,
    )


@app.exception_handler(QoderRegistrationError)
async def handle_qoder_registration_error(request: Request, exc: QoderRegistrationError) -> JSONResponse:
    return _error_response_for_request(
        request,
        message=str(exc),
        error_type=exc.error_type,
        status_code=exc.status_code,
    )


@app.exception_handler(ValueError)
async def handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
    return _error_response_for_request(
        request,
        message=str(exc),
        error_type="invalid_request_error",
        status_code=400,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    invoker = get_invoker()
    account_pool = get_account_pool()
    runtime_settings = get_runtime_settings_manager().load()
    limiter_snapshot = get_invocation_limiter().snapshot()
    try:
        model_count = len(invoker.list_models())
        model_error = None
    except QoderCLIError as exc:
        model_count = 0
        model_error = str(exc)
    return {
        "status": "ok",
        "version": __version__,
        "qoder_home": str(settings.qoder_home_path),
        "workspace": str(settings.workspace_path),
        "browser_login_backend": settings.browser_login_backend_name,
        "infer_endpoint": settings.infer_endpoint,
        "openapi_endpoint": settings.openapi_endpoint,
        "models": model_count,
        "model_error": model_error,
        "accounts_total": len(account_pool.list_accounts()),
        "accounts_enabled": len(account_pool.get_enabled_accounts()),
        "proxy_configured": bool(runtime_settings.proxy_url),
        "llmapi_api_key_configured": bool(runtime_settings.llmapi_api_key),
        "account_pool_max_concurrency": runtime_settings.account_pool_max_concurrency,
        "active_requests": limiter_snapshot["active"],
    }


def _build_auth_status_payload() -> dict[str, Any]:
    settings = get_settings()
    manager = get_login_manager()
    pool = get_account_pool()
    session = manager.current_session()
    auth_state = inspect_local_auth(settings.qoder_home_path)
    accounts = pool.list_accounts(include_pending=True)
    pooled_logged_in_accounts = sum(
        1
        for account in accounts
        if inspect_local_auth(account.qoder_home_path).get("logged_in_hint")
    )
    auth_state["base_logged_in_hint"] = auth_state.get("logged_in_hint", False)
    auth_state["pool_logged_in_accounts"] = pooled_logged_in_accounts
    auth_state["logged_in_hint"] = auth_state.get("logged_in_hint", False) or pooled_logged_in_accounts > 0
    return {
        "auth": auth_state,
        "session": session.to_public_dict() if session else None,
    }


def _serialize_account(account: QoderAccount) -> dict[str, Any]:
    auth_state = inspect_local_auth(account.qoder_home_path)
    device_metadata = load_device_metadata(account.qoder_home_path)
    status_snapshot = load_account_status(account.qoder_home_path)
    model_count = (
        len(load_qoder_models(account.qoder_home_path, scene=get_settings().model_scene))
        if auth_state.get("logged_in_hint")
        else 0
    )
    return {
        "id": account.id,
        "label": account.label,
        "enabled": account.enabled,
        "managed": account.managed,
        "status": account.status,
        "auth_type": account.auth_type,
        "qoder_home": str(account.qoder_home_path),
        "machine_id": account.machine_id,
        "login_device_id": auth_state.get("login_device_id", "") or account.login_device_id,
        "device_workflow": account.device_workflow or device_metadata.get("device_workflow", ""),
        "machine_token": account.machine_token or device_metadata.get("machine_token", ""),
        "machine_type": account.machine_type or device_metadata.get("machine_type", ""),
        "device_hostname": account.device_hostname or device_metadata.get("device_hostname", ""),
        "device_mac": account.device_mac or device_metadata.get("device_mac", ""),
        "logged_in_hint": auth_state.get("logged_in_hint", False),
        "has_machine_id": auth_state.get("has_machine_id", False),
        "has_user_file": auth_state.get("has_user_file", False),
        "name": auth_state.get("name", ""),
        "email": auth_state.get("email", ""),
        "user_type": (
            status_snapshot.user_type
            if status_snapshot is not None and status_snapshot.user_type
            else auth_state.get("user_type", "")
        ),
        "user_tag": (
            status_snapshot.user_tag
            if status_snapshot is not None and status_snapshot.user_tag
            else auth_state.get("user_tag", "")
        ),
        "plan_tier_name": status_snapshot.plan_tier_name if status_snapshot else "",
        "plan_user_type": status_snapshot.plan_user_type if status_snapshot else "",
        "is_quota_exceeded": (
            status_snapshot.is_quota_exceeded if status_snapshot else None
        ),
        "quota_total": status_snapshot.quota_total if status_snapshot else None,
        "quota_used": status_snapshot.quota_used if status_snapshot else None,
        "quota_remaining": status_snapshot.quota_remaining if status_snapshot else None,
        "quota_percentage": status_snapshot.quota_percentage if status_snapshot else None,
        "quota_unit": status_snapshot.quota_unit if status_snapshot else "",
        "quota_detail_url": status_snapshot.quota_detail_url if status_snapshot else "",
        "total_usage_percentage": (
            status_snapshot.total_usage_percentage if status_snapshot else None
        ),
        "quota_expires_at": status_snapshot.quota_expires_at if status_snapshot else None,
        "add_credits_url": status_snapshot.add_credits_url if status_snapshot else "",
        "upgrade_url": status_snapshot.upgrade_url if status_snapshot else "",
        "credit_usage_error": status_snapshot.credit_usage_error if status_snapshot else "",
        "cloud_type": status_snapshot.cloud_type if status_snapshot else "",
        "status_checked_at": status_snapshot.checked_at if status_snapshot else "",
        "status_source": status_snapshot.source if status_snapshot else "",
        "status_error": status_snapshot.status_error if status_snapshot else "",
        "status_summary_path": (
            status_snapshot.summary_path
            if status_snapshot and status_snapshot.summary_path
            else device_metadata.get("summary_path", "")
        ),
        "status_output_dir": (
            status_snapshot.output_dir
            if status_snapshot and status_snapshot.output_dir
            else device_metadata.get("output_dir", "")
        ),
        "model_count": model_count,
        "consecutive_request_failures": account.consecutive_request_failures,
        "cooldown_until": account.cooldown_until or "",
        "cooldown_active": bool(account.cooldown_until),
        "last_request_failed_at": account.last_request_failed_at or "",
        "last_request_error": account.last_request_error,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _coerce_quota_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, str):
        normalized = value.strip().replace(",", "")
        if not normalized:
            return None
        try:
            numeric = float(normalized)
        except ValueError:
            return None
        return numeric if math.isfinite(numeric) else None
    return None


def _build_account_quota_summary(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0.0
    used = 0.0
    remaining = 0.0
    accounts_with_quota = 0
    quota_unit = ''

    for account in accounts:
        quota_total = _coerce_quota_value(account.get('quota_total'))
        if quota_total is None or quota_total <= 0:
            continue

        quota_used = _coerce_quota_value(account.get('quota_used'))
        quota_remaining = _coerce_quota_value(account.get('quota_remaining'))
        if quota_used is None and quota_remaining is not None:
            quota_used = max(quota_total - quota_remaining, 0.0)
        if quota_remaining is None and quota_used is not None:
            quota_remaining = max(quota_total - quota_used, 0.0)

        total += quota_total
        if quota_used is not None:
            used += max(quota_used, 0.0)
        if quota_remaining is not None:
            remaining += max(quota_remaining, 0.0)
        accounts_with_quota += 1

        unit = str(account.get('quota_unit') or '').strip()
        if unit and not quota_unit:
            quota_unit = unit

    if accounts_with_quota == 0:
        return {
            'total': None,
            'used': None,
            'remaining': None,
            'unit': '',
            'accounts_with_quota': 0,
        }

    return {
        'total': total,
        'used': used,
        'remaining': remaining,
        'unit': quota_unit,
        'accounts_with_quota': accounts_with_quota,
    }


def _build_runtime_settings_payload() -> dict[str, Any]:
    manager = get_runtime_settings_manager()
    snapshot = manager.load()
    return {
        'settings': snapshot.model_dump(),
        'path': str(manager.path),
        'limiter': get_invocation_limiter().snapshot(),
        'registration': get_registration_manager().describe(),
    }


def _build_registration_payload() -> dict[str, Any]:
    return get_registration_manager().describe()


def _build_accounts_payload(*, include_pending: bool = True) -> dict[str, Any]:
    pool = get_account_pool()
    accounts = pool.list_accounts(include_pending=include_pending)
    enabled_accounts = pool.get_enabled_accounts()
    serialized_accounts = [_serialize_account(account) for account in accounts]
    return {
        "accounts": serialized_accounts,
        "total_accounts": len(accounts),
        "enabled_accounts": len(enabled_accounts),
        "quota_summary": _build_account_quota_summary(serialized_accounts),
    }


@app.get("/webui")
@app.get("/webui/")
async def webui() -> FileResponse:
    return FileResponse(get_webui_index_path())


@app.get("/webui/api/overview")
async def webui_overview() -> dict[str, Any]:
    payload = await health()
    payload_auth = _build_auth_status_payload()
    return {
        "health": payload,
        "auth": payload_auth["auth"],
        "session": payload_auth["session"],
        "accounts": _build_accounts_payload(),
        "registration": _build_registration_payload(),
    }


@app.get("/webui/api/logs")
async def webui_logs(lines: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    settings = get_settings()
    return build_logs_payload(settings.app_log_path, lines=lines)


@app.get("/webui/api/settings")
async def webui_get_runtime_settings() -> dict[str, Any]:
    return _build_runtime_settings_payload()


@app.put("/webui/api/settings")
async def webui_update_runtime_settings(
    request: RuntimeSettingsUpdateRequest,
) -> dict[str, Any]:
    manager = get_runtime_settings_manager()
    current_payload = manager.load().model_dump()
    current_payload.update(request.model_dump(exclude_unset=True))
    snapshot = manager.save(current_payload)
    limiter_snapshot = await get_invocation_limiter().update_limit(
        snapshot.account_pool_max_concurrency
    )
    payload = _build_runtime_settings_payload()
    payload['success'] = True
    payload['message'] = '运行设置已保存'
    payload['limiter'] = limiter_snapshot
    return payload


@app.get("/auth/status")
async def auth_status() -> dict[str, Any]:
    return _build_auth_status_payload()


@app.post("/auth/browser/start")
async def start_browser_login(request: StartBrowserLoginRequest | None = None) -> dict[str, Any]:
    login_manager = get_login_manager()
    current = login_manager.current_session()
    if current is not None and not current.done:
        payload = _build_auth_status_payload()
        payload["session"] = current.to_public_dict()
        payload["accounts"] = _build_accounts_payload()
        return payload

    pending_account = create_pending_managed_account(get_settings(), (request.label if request else "") or "")

    def _on_finished(snapshot) -> None:
        if snapshot.status == "success":
            try:
                finalize_pending_account(get_settings(), pending_account.id)
            except KeyError:
                return
            return
        remove_account(get_settings(), pending_account.id)

    try:
        session = login_manager.start_browser_login(
            qoder_home=pending_account.qoder_home_path,
            on_finished=_on_finished,
        )
    except Exception:
        remove_account(get_settings(), pending_account.id)
        raise

    payload = _build_auth_status_payload()
    payload["session"] = session.to_public_dict()
    payload["accounts"] = _build_accounts_payload()
    return payload


@app.get("/auth/browser/sessions/{session_id}")
async def get_browser_login_session(session_id: str) -> dict[str, Any]:
    session = get_login_manager().get_session(session_id)
    return session.snapshot().to_public_dict()


@app.post("/auth/browser/sessions/{session_id}/input")
async def send_browser_login_input(
    session_id: str,
    request: LoginSessionInputRequest,
) -> dict[str, Any]:
    snapshot = get_login_manager().send_key(session_id, request.key)
    return snapshot.to_public_dict()


@app.post("/auth/browser/sessions/{session_id}/cancel")
async def cancel_browser_login_session(session_id: str) -> dict[str, Any]:
    snapshot = get_login_manager().cancel(session_id)
    return snapshot.to_public_dict()


@app.get("/webui/api/accounts")
async def webui_accounts() -> dict[str, Any]:
    return _build_accounts_payload()


@app.patch("/webui/api/accounts/{account_id}/enabled")
async def webui_toggle_account(account_id: str, request: AccountToggleRequest) -> dict[str, Any]:
    try:
        account = update_account_enabled(get_settings(), account_id, request.enabled)
    except KeyError as exc:
        raise QoderLoginError(f"找不到账号: {account_id}", status_code=404) from exc
    return {
        "success": True,
        "message": "账号状态已更新",
        "account": _serialize_account(account),
    }


@app.patch("/webui/api/accounts/{account_id}/label")
async def webui_update_account_label(
    account_id: str,
    request: AccountLabelUpdateRequest,
) -> dict[str, Any]:
    try:
        account = update_account_label(get_settings(), account_id, request.label)
    except KeyError as exc:
        raise QoderLoginError(f"找不到账号: {account_id}", status_code=404) from exc
    except ValueError as exc:
        raise QoderLoginError(str(exc), status_code=400) from exc
    return {
        "success": True,
        "message": "账号标签已更新",
        "account": _serialize_account(account),
    }


@app.delete("/webui/api/accounts/{account_id}")
async def webui_delete_account(account_id: str) -> dict[str, Any]:
    removed = remove_account(get_settings(), account_id)
    if not removed:
        raise QoderLoginError(f"找不到账号: {account_id}", status_code=404)
    return {"success": True, "message": "账号已删除"}


@app.post("/webui/api/accounts/{account_id}/test")
async def webui_test_account(account_id: str) -> dict[str, Any]:
    account = get_account(get_settings(), account_id)
    if account is None:
        raise QoderLoginError(f"找不到账号: {account_id}", status_code=404)
    if not account.enabled:
        raise QoderLoginError("账号已停用，请先启用后再测试", status_code=400)
    if not inspect_local_auth(account.qoder_home_path).get("logged_in_hint"):
        raise QoderLoginError("账号当前没有可用登录态，无法测试", status_code=400)

    async with get_invocation_limiter().slot():
        output_text, resolved_model = await get_invoker().run_text(
            prompt="请只回复 ok",
            attachments=[],
            messages=None,
            model="auto",
            max_tokens=32,
            preferred_account_id=account_id,
        )
    return {
        "success": True,
        "message": f"测试成功（{resolved_model}）",
        "content_preview": output_text[:120],
    }

@app.post("/webui/api/accounts/{account_id}/status")
async def webui_refresh_account_status(account_id: str) -> dict[str, Any]:
    account = get_account(get_settings(), account_id)
    if account is None:
        raise QoderLoginError(f"找不到账号: {account_id}", status_code=404)
    if not inspect_local_auth(account.qoder_home_path).get("logged_in_hint"):
        raise QoderLoginError("账号当前没有可用登录态，无法刷新状态", status_code=400)

    try:
        snapshot = refresh_account_status(get_settings(), account.qoder_home_path)
    except Exception as exc:
        raise QoderLoginError(f"刷新账号状态失败: {exc}", status_code=502) from exc

    refreshed_account = get_account(get_settings(), account_id) or account
    label = snapshot.plan_tier_name or snapshot.user_tag or snapshot.user_type or "unknown"
    return {
        "success": True,
        "message": f"账号状态已刷新：{label}",
        "account": _serialize_account(refreshed_account),
        "status": snapshot.model_dump(exclude_none=True),
    }


@app.get("/webui/api/registration")
async def webui_registration() -> dict[str, Any]:
    return _build_registration_payload()


@app.post("/webui/api/registration/start")
async def webui_start_registration(request: RegistrationStartRequest | None = None) -> dict[str, Any]:
    requested_count = request.requested_count if request is not None else None
    snapshot = get_registration_manager().start_registration(requested_count=requested_count)
    payload = _build_registration_payload()
    payload["session"] = snapshot.to_public_dict()
    payload["message"] = snapshot.message
    return payload


@app.get("/webui/api/registration/sessions/{session_id}")
async def webui_get_registration_session(session_id: str) -> dict[str, Any]:
    return get_registration_manager().get_session(session_id).snapshot().to_public_dict()


@app.post("/webui/api/registration/sessions/{session_id}/cancel")
async def webui_cancel_registration_session(session_id: str) -> dict[str, Any]:
    return get_registration_manager().cancel(session_id).to_public_dict()

@app.post("/webui/api/registration/worker/claim")
async def webui_claim_registration_worker() -> dict[str, Any]:
    claim = get_registration_manager().claim_current()
    return {"session": claim.to_dict() if claim else None}


@app.post("/webui/api/registration/sessions/{session_id}/worker/claim")
async def webui_claim_registration_session(session_id: str) -> dict[str, Any]:
    claim = get_registration_manager().claim_session(session_id)
    return {"session": claim.to_dict()}


@app.post("/webui/api/registration/sessions/{session_id}/worker/progress")
async def webui_registration_worker_progress(
    session_id: str,
    request: RegistrationWorkerProgressRequest,
) -> dict[str, Any]:
    snapshot = get_registration_manager().append_worker_progress(
        session_id,
        line=request.line,
        status=request.status,
        current_email=request.current_email,
        auth_url=request.auth_url,
    )
    return snapshot.to_public_dict()


@app.post("/webui/api/registration/sessions/{session_id}/worker/complete")
async def webui_registration_worker_complete(
    session_id: str,
    request: RegistrationWorkerCompleteRequest,
) -> dict[str, Any]:
    snapshot = get_registration_manager().complete_worker_account(
        session_id,
        email=request.email,
        password=request.password,
        summary=request.summary,
    )
    return snapshot.to_public_dict()


@app.post("/webui/api/registration/sessions/{session_id}/worker/fail")
async def webui_registration_worker_fail(
    session_id: str,
    request: RegistrationWorkerFailRequest,
) -> dict[str, Any]:
    snapshot = get_registration_manager().fail_worker(session_id, message=request.message)
    return snapshot.to_public_dict()



@app.post("/webui/api/accounts/refresh-statuses")
async def webui_refresh_all_account_statuses() -> dict[str, Any]:
    settings = get_settings()
    accounts = get_account_pool().list_accounts(include_pending=False)
    refreshed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    for account in accounts:
        if not inspect_local_auth(account.qoder_home_path).get("logged_in_hint"):
            skipped.append(
                {
                    "id": account.id,
                    "label": account.label,
                    "reason": "账号当前没有可用登录态",
                }
            )
            continue

        try:
            snapshot = refresh_account_status(settings, account.qoder_home_path)
        except Exception as exc:
            failed.append(
                {
                    "id": account.id,
                    "label": account.label,
                    "reason": str(exc),
                }
            )
            continue

        refreshed.append(
            {
                "id": account.id,
                "label": account.label,
                "plan_tier_name": snapshot.plan_tier_name,
                "quota_total": snapshot.quota_total,
                "quota_used": snapshot.quota_used,
                "quota_remaining": snapshot.quota_remaining,
            }
        )

    summary_parts: list[str] = []
    if refreshed:
        summary_parts.append(f"已刷新 {len(refreshed)} 个账号")
    if skipped:
        summary_parts.append(f"跳过 {len(skipped)} 个未登录账号")
    if failed:
        summary_parts.append(f"失败 {len(failed)} 个账号")
    if not summary_parts:
        summary_parts.append("没有可刷新的账号")

    payload = _build_accounts_payload()
    return {
        "success": not failed,
        "message": "，".join(summary_parts),
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
        "accounts": payload["accounts"],
        "total_accounts": payload["total_accounts"],
        "enabled_accounts": payload["enabled_accounts"],
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    invoker = get_invoker()
    return {
        "object": "list",
        "data": [_model_to_openai_item(model) for model in invoker.list_models()],
    }


def _build_completion_response(
    *,
    completion_id: str,
    created: int,
    model: str,
    prompt: PreparedPrompt,
    output_text: str,
    reasoning_content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    normalized_tool_calls = list(tool_calls or [])
    normalized_reasoning = reasoning_content.strip()
    prompt_tokens = _estimate_tokens(prompt.prompt)
    completion_basis = _completion_basis(
        output_text,
        tool_calls=normalized_tool_calls,
        reasoning_content=normalized_reasoning,
    )
    completion_tokens = _estimate_tokens(completion_basis)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": output_text or None,
    }
    if normalized_reasoning:
        message["reasoning_content"] = normalized_reasoning
    if normalized_tool_calls:
        message["tool_calls"] = normalized_tool_calls

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _collect_stream_result(iterator) -> tuple[str, str, str, list[dict[str, Any]], str]:
    output_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_signature = ""
    tool_calls: list[dict[str, Any]] = []
    finish_reason = "stop"

    async for stream_event in iterator:
        if stream_event.reasoning_delta:
            reasoning_parts.append(stream_event.reasoning_delta)
        if stream_event.reasoning_signature_delta:
            reasoning_signature += stream_event.reasoning_signature_delta
        if stream_event.text_delta:
            output_parts.append(stream_event.text_delta)
        if stream_event.tool_call_deltas:
            tool_calls = merge_tool_call_deltas(tool_calls, stream_event.tool_call_deltas)
        if stream_event.finish_reason:
            finish_reason = stream_event.finish_reason

    return "".join(output_parts).strip(), "".join(reasoning_parts).strip(), reasoning_signature, tool_calls, finish_reason


def _completion_basis(
    output_text: str,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str = "",
) -> str:
    normalized_reasoning = reasoning_content.strip()
    normalized_text = output_text.strip()
    if normalized_reasoning and normalized_text:
        return f"{normalized_reasoning}\n{normalized_text}"
    if normalized_reasoning:
        return normalized_reasoning
    if normalized_text:
        return normalized_text
    if tool_calls:
        return json.dumps(tool_calls, ensure_ascii=False)
    return ""


async def _run_non_stream_completion(
    request: ChatCompletionRequest,
    *,
    prompt: PreparedPrompt,
) -> tuple[str, str, str, str, list[dict[str, Any]], str]:
    invoker = get_invoker()
    async with get_invocation_limiter().slot():
        iterator, resolved_model = await invoker.stream_events(
            prompt=prompt.prompt,
            attachments=prompt.attachments,
            messages=prompt.messages,
            model=request.model,
            max_tokens=request.max_tokens,
            tools=request.tools,
            tool_choice=request.tool_choice,
        )
        output_text, reasoning_content, reasoning_signature, tool_calls, finish_reason = await _collect_stream_result(iterator)
        return output_text, reasoning_content, reasoning_signature, resolved_model, tool_calls, finish_reason


async def _start_streaming_completion(
    request: ChatCompletionRequest,
    *,
    prompt: PreparedPrompt,
):
    invoker = get_invoker()
    async with get_invocation_limiter().slot():
        return await invoker.stream_events(
            prompt=prompt.prompt,
            attachments=prompt.attachments,
            messages=prompt.messages,
            model=request.model,
            max_tokens=request.max_tokens,
            tools=request.tools,
            tool_choice=request.tool_choice,
        )


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: ChatCompletionRequest):
    _log_debug_request("/v1/chat/completions", request.model_dump(exclude_none=True))
    prompt = render_openai_messages(request.messages, request.response_format)
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    stream_mode = _resolve_openai_request_stream_mode(request.stream)

    if not stream_mode:
        output_text, reasoning_content, _, resolved_model, tool_calls, finish_reason = await _run_non_stream_completion(
            request,
            prompt=prompt,
        )
        return JSONResponse(
            content=_build_completion_response(
                completion_id=completion_id,
                created=created,
                model=resolved_model,
                prompt=prompt,
                output_text=output_text,
                reasoning_content=reasoning_content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        )

    async def event_stream():
        resolved_model = request.model
        try:
            iterator, resolved_model = await _start_streaming_completion(
                request,
                prompt=prompt,
            )

            finish_reason = "stop"

            yield _sse_payload(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resolved_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": None,
                        }
                    ],
                }
            )

            async for stream_event in iterator:
                if stream_event.finish_reason:
                    finish_reason = stream_event.finish_reason

                if stream_event.tool_call_deltas:
                    yield _sse_payload(
                        {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": resolved_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"tool_calls": stream_event.tool_call_deltas},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )

                if stream_event.reasoning_delta:
                    for index in range(0, len(stream_event.reasoning_delta), get_settings().stream_chunk_chars):
                        piece = stream_event.reasoning_delta[
                            index : index + get_settings().stream_chunk_chars
                        ]
                        yield _sse_payload(
                            {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": resolved_model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"reasoning_content": piece},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                        )

                if not stream_event.text_delta:
                    continue
                for index in range(0, len(stream_event.text_delta), get_settings().stream_chunk_chars):
                    piece = stream_event.text_delta[
                        index : index + get_settings().stream_chunk_chars
                    ]
                    yield _sse_payload(
                        {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": resolved_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": piece},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )

            yield _sse_payload(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": resolved_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": finish_reason,
                        }
                    ],
                }
            )
        except QoderCLIError as exc:
            yield _sse_payload(
                {
                    "error": {
                        "message": str(exc),
                        "type": exc.error_type,
                    }
                }
            )
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/v1/messages", response_model=None)
async def anthropic_messages(request: AnthropicMessagesRequest):
    _log_debug_request("/v1/messages", request.model_dump(exclude_none=True))
    chat_request = convert_anthropic_request_to_chat_request(request)
    tool_schemas = _build_tool_schema_map(request.tools)
    prompt = render_openai_messages(chat_request.messages)
    message_id = f"msg_{uuid.uuid4().hex}"
    input_tokens = _estimate_tokens(prompt.prompt)
    stream_mode = _resolve_anthropic_request_stream_mode(request.stream)

    if not stream_mode:
        output_text, reasoning_content, reasoning_signature, resolved_model, tool_calls, finish_reason = await _run_non_stream_completion(
            chat_request,
            prompt=prompt,
        )
        stop_result = _apply_stop_sequences(output_text, request.stop_sequences)
        output_tokens = _estimate_tokens(
            _completion_basis(
                stop_result.text,
                tool_calls,
                reasoning_content=reasoning_content,
            )
        )
        return JSONResponse(
            content=build_anthropic_message_response(
                message_id=message_id,
                model=resolved_model,
                output_text=stop_result.text,
                reasoning_text=reasoning_content,
                reasoning_signature=reasoning_signature,
                tool_calls=tool_calls,
                finish_reason=("stop_sequence" if stop_result.matched else finish_reason),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_sequence=stop_result.matched,
                tool_schemas=tool_schemas,
            )
        )

    async def event_stream():
        translator = AnthropicStreamTranslator(
            message_id=message_id,
            prompt_text=prompt.prompt,
            stop_sequences=request.stop_sequences,
            tool_schemas=tool_schemas,
        )
        try:
            iterator, resolved_model = await _start_streaming_completion(
                chat_request,
                prompt=prompt,
            )
            for payload in translator.start(model=resolved_model):
                yield payload
            async for stream_event in iterator:
                for payload in translator.feed(stream_event):
                    yield payload
            for payload in translator.finish():
                yield payload
        except (QoderCLIError, QoderAPIError, ValueError) as exc:
            error_type = getattr(exc, "error_type", "api_error")
            for payload in translator.error(message=str(exc), error_type=error_type):
                yield payload

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def main() -> None:
    """CLI 入口。"""
    import uvicorn

    settings = get_settings()
    log_config = _build_log_config(settings)
    dictConfig(log_config)
    logger.info(
        "starting qoder2api server on %s:%s, browser_login_backend=%s, log_path=%s",
        settings.host,
        settings.port,
        settings.browser_login_backend_name,
        settings.app_log_path,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        log_config=log_config,
    )
