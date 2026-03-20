"""qoder2api FastAPI 应用。"""

from __future__ import annotations

import json
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
from .auth_state import inspect_local_auth
from .catalog import QoderModelInfo, load_qoder_models
from .login import QoderLoginError, QoderLoginManager
from .prompting import PreparedPrompt, render_openai_messages
from .qoder_api import QoderAPIError
from .qoder_cli import QoderCLIError, QoderCLIInvoker
from .schemas import (
    AccountLabelUpdateRequest,
    AccountToggleRequest,
    ChatCompletionRequest,
    LoginSessionInputRequest,
    StartBrowserLoginRequest,
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
                "format": '%(asctime)s [%(levelname)s] %(name)s: %(client_addr)s - "%(request_line)s" %(status_code)s',
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
        "id": model.id,
        "object": "model",
        "created": 0,
        "owned_by": "qoder",
        "permission": [],
        "root": model.id,
        "parent": None,
        "display_name": model.display_name,
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
    return QoderCLIInvoker(get_settings(), get_account_pool())


@lru_cache
def get_login_manager() -> QoderLoginManager:
    return QoderLoginManager(get_settings())


@lru_cache
def get_account_pool() -> QoderAccountPool:
    return QoderAccountPool(get_settings())


app = FastAPI(
    title="qoder2api",
    description="独立 Qoder OpenAI 兼容代理。",
    version=__version__,
)
logger = logging.getLogger("qoder2api")

_webui_static_dir = get_webui_static_dir()
if _webui_static_dir.exists():
    app.mount("/webui/static", StaticFiles(directory=_webui_static_dir), name="webui_static")


@app.exception_handler(QoderCLIError)
async def handle_qoder_error(_: Request, exc: QoderCLIError) -> JSONResponse:
    return _openai_error(str(exc), error_type=exc.error_type, status_code=exc.status_code)


@app.exception_handler(QoderLoginError)
async def handle_qoder_login_error(_: Request, exc: QoderLoginError) -> JSONResponse:
    return _openai_error(str(exc), error_type=exc.error_type, status_code=exc.status_code)


@app.exception_handler(QoderAPIError)
async def handle_qoder_api_error(_: Request, exc: QoderAPIError) -> JSONResponse:
    return _openai_error(str(exc), error_type=exc.error_type, status_code=exc.status_code)


@app.exception_handler(ValueError)
async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return _openai_error(str(exc), error_type="invalid_request_error", status_code=400)


@app.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    invoker = get_invoker()
    account_pool = get_account_pool()
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
        "session": session.to_dict() if session else None,
    }


def _serialize_account(account: QoderAccount) -> dict[str, Any]:
    auth_state = inspect_local_auth(account.qoder_home_path)
    device_metadata = load_device_metadata(account.qoder_home_path)
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
        "model_count": model_count,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _build_accounts_payload(*, include_pending: bool = True) -> dict[str, Any]:
    pool = get_account_pool()
    accounts = pool.list_accounts(include_pending=include_pending)
    enabled_accounts = pool.get_enabled_accounts()
    return {
        "accounts": [_serialize_account(account) for account in accounts],
        "total_accounts": len(accounts),
        "enabled_accounts": len(enabled_accounts),
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
    }


@app.get("/webui/api/logs")
async def webui_logs(lines: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    settings = get_settings()
    return build_logs_payload(settings.app_log_path, lines=lines)


@app.get("/auth/status")
async def auth_status() -> dict[str, Any]:
    return _build_auth_status_payload()


@app.post("/auth/browser/start")
async def start_browser_login(request: StartBrowserLoginRequest | None = None) -> dict[str, Any]:
    login_manager = get_login_manager()
    current = login_manager.current_session()
    if current is not None and not current.done:
        payload = _build_auth_status_payload()
        payload["session"] = current.to_dict()
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
    payload["session"] = session.to_dict()
    payload["accounts"] = _build_accounts_payload()
    return payload


@app.get("/auth/browser/sessions/{session_id}")
async def get_browser_login_session(session_id: str) -> dict[str, Any]:
    session = get_login_manager().get_session(session_id)
    return session.snapshot().to_dict()


@app.post("/auth/browser/sessions/{session_id}/input")
async def send_browser_login_input(
    session_id: str,
    request: LoginSessionInputRequest,
) -> dict[str, Any]:
    snapshot = get_login_manager().send_key(session_id, request.key)
    return snapshot.to_dict()


@app.post("/auth/browser/sessions/{session_id}/cancel")
async def cancel_browser_login_session(session_id: str) -> dict[str, Any]:
    snapshot = get_login_manager().cancel(session_id)
    return snapshot.to_dict()


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

    output_text, resolved_model = await get_invoker().run_text(
        prompt="请只回复 ok",
        attachments=[],
        model="auto",
        max_tokens=32,
        preferred_account_id=account_id,
    )
    return {
        "success": True,
        "message": f"测试成功（{resolved_model}）",
        "content_preview": output_text[:120],
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
) -> dict[str, Any]:
    prompt_tokens = _estimate_tokens(prompt.prompt)
    completion_tokens = _estimate_tokens(output_text)

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": output_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: ChatCompletionRequest):
    prompt = render_openai_messages(request.messages, request.response_format)
    invoker = get_invoker()

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if not request.stream:
        output_text, resolved_model = await invoker.run_text(
            prompt=prompt.prompt,
            attachments=prompt.attachments,
            model=request.model,
            max_tokens=request.max_tokens,
        )
        return JSONResponse(
            content=_build_completion_response(
                completion_id=completion_id,
                created=created,
                model=resolved_model,
                prompt=prompt,
                output_text=output_text,
            )
        )

    async def event_stream():
        resolved_model = request.model
        try:
            iterator, resolved_model = await invoker.stream_events(
                prompt=prompt.prompt,
                attachments=prompt.attachments,
                model=request.model,
                max_tokens=request.max_tokens,
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
