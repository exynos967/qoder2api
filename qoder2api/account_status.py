"""Qoder 账号套餐状态的持久化与在线检测。"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import httpx
from pydantic import BaseModel, Field

from .auth_state import (
    CachedUserInfo,
    ensure_auth_dir,
    load_cached_user_info,
    load_machine_id,
)
from .runtime_settings import load_runtime_settings
from .settings import Settings

_ACCOUNT_STATUS_NAME = "account_status.json"
_DEVICE_METADATA_NAME = "xieyi_device.json"
_OFFICIAL_STATUS_CLIENT_LIMITS = httpx.Limits(
    max_connections=16,
    max_keepalive_connections=8,
    keepalive_expiry=30.0,
)
_OFFICIAL_STATUS_CLIENTS = threading.local()


class AccountStatusSnapshot(BaseModel):
    uid: str = ""
    aid: str = ""
    name: str = ""
    email: str = ""
    user_type: str = ""
    user_tag: str = ""
    plan_tier_name: str = ""
    plan_user_type: str = ""
    is_quota_exceeded: bool | None = None
    total_usage_percentage: float | None = None
    quota_total: float | None = None
    quota_used: float | None = None
    quota_remaining: float | None = None
    quota_percentage: float | None = None
    quota_unit: str = ""
    quota_detail_url: str = ""
    quota_expires_at: int | None = None
    add_credits_url: str = ""
    upgrade_url: str = ""
    credit_usage_error: str = ""
    whitelist: str = ""
    cloud_type: str = ""
    data_policy: str = ""
    checked_at: str = ""
    source: str = ""
    output_dir: str = ""
    summary_path: str = ""
    status_error: str = ""
    user_status: dict[str, Any] = Field(default_factory=dict)
    user_plan: dict[str, Any] = Field(default_factory=dict)
    data_policy_payload: dict[str, Any] = Field(default_factory=dict)
    credit_usage_payload: dict[str, Any] = Field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_account_status_path(qoder_home: Path) -> Path:
    return qoder_home / ".auth" / _ACCOUNT_STATUS_NAME


def load_account_status(qoder_home: Path) -> AccountStatusSnapshot | None:
    path = get_account_status_path(qoder_home)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return AccountStatusSnapshot.model_validate(payload)
    except Exception:
        return None


def save_account_status(
    qoder_home: Path,
    snapshot: AccountStatusSnapshot,
) -> AccountStatusSnapshot:
    ensure_auth_dir(qoder_home)
    path = get_account_status_path(qoder_home)
    path.write_text(
        snapshot.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    return snapshot


def _normalize_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _coerce_mapping(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    return dict(payload)


def _pick_result_mapping(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    current = _coerce_mapping(payload)
    visited: set[int] = set()
    while current and id(current) not in visited:
        visited.add(id(current))
        for key in ("result", "data", "payload"):
            candidate = current.get(key)
            if isinstance(candidate, Mapping):
                current = dict(candidate)
                break
        else:
            return current
    return current


def _pick_value(
    payload: Mapping[str, Any] | Any,
    *names: str,
    default: Any = "",
) -> Any:
    mapping = _pick_result_mapping(payload)
    if not mapping:
        return default
    normalized = {_normalize_key(key): value for key, value in mapping.items()}
    for name in names:
        key = _normalize_key(name)
        if key in normalized:
            return normalized[key]
    return default


def _coerce_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return None


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _cached_payload(cached_user_info: Mapping[str, Any] | CachedUserInfo) -> dict[str, Any]:
    if isinstance(cached_user_info, CachedUserInfo):
        return cached_user_info.model_dump(by_alias=True, exclude_none=True)
    if isinstance(cached_user_info, Mapping):
        return dict(cached_user_info)
    raise TypeError("cached_user_info 必须是 Mapping 或 CachedUserInfo")


def _build_summary_path(summary: Mapping[str, Any]) -> str:
    output_dir = str(summary.get("outputDir") or summary.get("output_dir") or "").strip()
    if not output_dir:
        return ""
    return str(Path(output_dir) / "summary.json")


def _load_device_metadata(qoder_home: Path) -> dict[str, str]:
    path = qoder_home / ".auth" / _DEVICE_METADATA_NAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: str(value or "").strip() for key, value in payload.items()}


def build_account_status_snapshot(
    *,
    cached_user_info: Mapping[str, Any] | CachedUserInfo,
    user_status: Mapping[str, Any] | Any = None,
    user_plan: Mapping[str, Any] | Any = None,
    data_policy: Mapping[str, Any] | Any = None,
    credit_usage: Mapping[str, Any] | Any = None,
    source: str,
    output_dir: str = "",
    summary_path: str = "",
    status_error: str = "",
    credit_usage_error: str = "",
) -> AccountStatusSnapshot:
    cached = _cached_payload(cached_user_info)
    raw_user_status = _coerce_mapping(user_status)
    raw_user_plan = _coerce_mapping(user_plan)
    raw_data_policy = _coerce_mapping(data_policy)
    raw_credit_usage = _coerce_mapping(credit_usage)
    raw_user_quota = _coerce_mapping(
        _pick_value(raw_credit_usage, "userQuota", "user_quota", default={})
    )
    cached_policy = "AGREE" if cached.get("data_policy_agreed") else "DISAGREE"
    return AccountStatusSnapshot(
        uid=str(cached.get("uid") or _pick_value(raw_user_status, "uid", "userId") or ""),
        aid=str(cached.get("aid") or cached.get("uid") or ""),
        name=str(cached.get("name") or _pick_value(raw_user_status, "name", "userName") or ""),
        email=str(cached.get("email") or ""),
        user_type=str(
            _pick_value(raw_user_status, "userType", "user_type", default=cached.get("user_type") or "")
            or cached.get("user_type")
            or ""
        ),
        user_tag=str(
            _pick_value(raw_user_status, "userTag", "user_tag", default=cached.get("user_tag") or "")
            or cached.get("user_tag")
            or ""
        ),
        plan_tier_name=str(_pick_value(raw_user_plan, "plan_tier_name", "planTierName") or ""),
        plan_user_type=str(_pick_value(raw_user_plan, "user_type", "userType") or ""),
        is_quota_exceeded=_coerce_bool(
            _pick_value(raw_user_status, "isQuotaExceeded", "quotaExceeded", default=None)
        ),
        total_usage_percentage=_coerce_float(
            _pick_value(raw_credit_usage, "totalUsagePercentage", "total_usage_percentage", default=None)
        ),
        quota_total=_coerce_float(_pick_value(raw_user_quota, "total", default=None)),
        quota_used=_coerce_float(_pick_value(raw_user_quota, "used", default=None)),
        quota_remaining=_coerce_float(_pick_value(raw_user_quota, "remaining", default=None)),
        quota_percentage=_coerce_float(_pick_value(raw_user_quota, "percentage", default=None)),
        quota_unit=str(_pick_value(raw_user_quota, "unit", default="") or ""),
        quota_detail_url=str(
            _pick_value(raw_user_quota, "detailUrl", "detail_url", default="") or ""
        ),
        quota_expires_at=_coerce_int(
            _pick_value(raw_credit_usage, "expiresAt", "expires_at", default=None)
        ),
        add_credits_url=str(
            _pick_value(raw_credit_usage, "addCreditsUrl", "add_credits_url", default="") or ""
        ),
        upgrade_url=str(
            _pick_value(raw_credit_usage, "upgradeUrl", "upgrade_url", default="") or ""
        ),
        credit_usage_error=credit_usage_error,
        whitelist=str(_pick_value(raw_user_status, "whitelist", default="") or ""),
        cloud_type=str(_pick_value(raw_user_status, "cloudType", "cloud_type", default="") or ""),
        data_policy=str(
            _pick_value(raw_data_policy, "dataPolicy", "policy", default=cached_policy)
            or cached_policy
        ),
        checked_at=_now_iso(),
        source=source,
        output_dir=output_dir,
        summary_path=summary_path,
        status_error=status_error,
        user_status=raw_user_status,
        user_plan=raw_user_plan,
        data_policy_payload=raw_data_policy,
        credit_usage_payload=raw_credit_usage,
    )


def persist_summary_account_status(
    qoder_home: Path,
    summary: Mapping[str, Any] | Any,
) -> AccountStatusSnapshot | None:
    if not isinstance(summary, Mapping):
        return None
    cached_user_info = summary.get("cached_user_info")
    if not isinstance(cached_user_info, Mapping):
        return None
    snapshot = build_account_status_snapshot(
        cached_user_info=cached_user_info,
        user_status=summary.get("user_status"),
        user_plan=summary.get("user_plan"),
        data_policy=summary.get("data_policy") or summary.get("dataPolicy"),
        credit_usage=summary.get("user_credit_usage") or summary.get("credit_usage"),
        source="namespaced_summary",
        output_dir=str(summary.get("outputDir") or ""),
        summary_path=_build_summary_path(summary),
        credit_usage_error=str(
            summary.get("user_credit_usage_error")
            or summary.get("credit_usage_error")
            or ""
        ),
    )
    return save_account_status(qoder_home, snapshot)


def _load_protocol_path(settings: Settings) -> Path:
    script_path = settings.namespaced_login_script_path
    candidate = script_path.with_name("qoder_pure_protocol.py")
    if candidate.is_file():
        return candidate.resolve()
    raise FileNotFoundError(f"找不到 qoder_pure_protocol.py: {candidate}")


@lru_cache(maxsize=4)
def _load_protocol_module(path_text: str) -> ModuleType:
    path = Path(path_text)
    module_name = f"qoder2api_vendor_pure_protocol_{abs(hash(path_text))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载纯协议模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _is_http2_supported() -> bool:
    return importlib.util.find_spec("h2") is not None


def _get_official_status_http_client(proxy_url: str) -> httpx.Client:
    cache = getattr(_OFFICIAL_STATUS_CLIENTS, "clients", None)
    if cache is None:
        cache = {}
        _OFFICIAL_STATUS_CLIENTS.clients = cache

    normalized_proxy = str(proxy_url or "").strip()
    cache_key = (normalized_proxy, _is_http2_supported())
    client = cache.get(cache_key)
    if client is not None:
        return client

    client_kwargs: dict[str, Any] = {
        "http2": cache_key[1],
        "follow_redirects": True,
        "limits": _OFFICIAL_STATUS_CLIENT_LIMITS,
        "trust_env": not bool(normalized_proxy),
    }
    if normalized_proxy:
        client_kwargs["proxy"] = normalized_proxy

    client = httpx.Client(**client_kwargs)
    cache[cache_key] = client
    return client


def _request_json_via_http_client(
    protocol: ModuleType,
    settings: Settings,
    *,
    url: str,
    headers: Mapping[str, str],
    method: str = "GET",
    body: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    runtime_settings = load_runtime_settings(settings)
    client = _get_official_status_http_client(runtime_settings.proxy_url)
    request_body = body if method.upper() != "GET" else None
    try:
        response = client.request(
            method=method.upper(),
            url=url,
            headers=dict(headers),
            content=request_body,
            timeout=max(timeout, 1.0),
        )
    except httpx.RequestError as exc:
        curl_json_request = getattr(protocol, "_curl_json_request", None)
        if callable(curl_json_request):
            return curl_json_request(
                url=url,
                headers=headers,
                method=method,
                body=body,
                timeout=timeout,
            )
        raise RuntimeError(str(exc)) from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text.strip()[:400]}")
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"HTTP 响应不是有效 JSON: {response.text[:400]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"HTTP 响应 JSON 不是对象: {type(payload)!r}")
    return payload


def _request_official_user_plan(
    protocol: ModuleType,
    settings: Settings,
    *,
    cached_user_info: Mapping[str, Any],
    machine_id: str,
    machine_token: str = "",
    machine_type: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    request = getattr(protocol, "request_official_user_plan", None)
    build_headers = getattr(protocol, "build_official_authorization_headers", None)
    center_base = str(getattr(protocol, "OFFICIAL_CENTER_BASE", "https://center.qoder.sh")).rstrip("/")
    if not callable(build_headers):
        if callable(request):
            return request(
                cached_user_info=cached_user_info,
                machine_id=machine_id,
                machine_token=machine_token,
                machine_type=machine_type,
                timeout=timeout,
            )
        raise AttributeError("纯协议模块缺少 user plan 所需的授权构建函数")

    headers, _ = build_headers(
        cached_user_info=cached_user_info,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
        sig_path="/api/v2/user/plan",
        body="",
    )
    return _request_json_via_http_client(
        protocol,
        settings,
        url=f"{center_base}/algo/api/v2/user/plan?Encode=1",
        headers=headers,
        method="POST",
        body="",
        timeout=timeout,
    )


def _request_official_data_policy(
    protocol: ModuleType,
    settings: Settings,
    *,
    cached_user_info: Mapping[str, Any],
    machine_id: str,
    machine_token: str = "",
    machine_type: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    request = getattr(protocol, "request_official_data_policy", None)
    build_headers = getattr(protocol, "build_official_authorization_headers", None)
    center_base = str(getattr(protocol, "OFFICIAL_CENTER_BASE", "https://center.qoder.sh")).rstrip("/")
    if not callable(build_headers):
        if callable(request):
            return request(
                cached_user_info=cached_user_info,
                machine_id=machine_id,
                machine_token=machine_token,
                machine_type=machine_type,
                timeout=timeout,
            )
        raise AttributeError("纯协议模块缺少 data policy 所需的授权构建函数")

    request_id = ""
    headers, request_id = build_headers(
        cached_user_info=cached_user_info,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
        sig_path="/api/v2/config/getDataPolicy",
        body="",
        request_id=request_id,
    )
    return _request_json_via_http_client(
        protocol,
        settings,
        url=(
            f"{center_base}/algo/api/v2/config/getDataPolicy"
            f"?requestId={request_id}&version=2"
        ),
        headers=headers,
        method="GET",
        body="",
        timeout=timeout,
    )


def _request_official_user_status(
    protocol: ModuleType,
    settings: Settings,
    *,
    cached_user_info: Mapping[str, Any],
    machine_id: str,
    machine_token: str = "",
    machine_type: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    request = getattr(protocol, "request_official_user_status", None)
    ensure_auth_fields = getattr(protocol, "ensure_official_auth_fields", None)
    build_body = getattr(protocol, "build_official_user_status_body", None)
    build_headers = getattr(protocol, "build_official_poll_headers", None)
    center_base = str(getattr(protocol, "OFFICIAL_CENTER_BASE", "https://center.qoder.sh")).rstrip("/")
    if not callable(ensure_auth_fields) or not callable(build_body) or not callable(build_headers):
        if callable(request):
            return request(
                cached_user_info=cached_user_info,
                machine_id=machine_id,
                machine_token=machine_token,
                machine_type=machine_type,
                timeout=timeout,
            )
        raise AttributeError("纯协议模块缺少 user status 所需的授权构建函数")

    payload = ensure_auth_fields(cached_user_info)
    user_id = str(payload.get("uid") or "")
    if not user_id:
        raise ValueError("cached_user_info 缺少 uid")
    body = build_body(
        user_id=user_id,
        personal_token=str(payload.get("personal_access_token") or payload.get("access_token") or ""),
        security_oauth_token=str(payload.get("security_oauth_token") or payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        auth_info={
            "uid": str(payload.get("uid") or ""),
            "aid": str(payload.get("aid") or payload.get("uid") or ""),
            "name": str(payload.get("name") or ""),
            "orgId": str(payload.get("organization_id") or ""),
        },
    )
    headers = build_headers(
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )
    return _request_json_via_http_client(
        protocol,
        settings,
        url=f"{center_base}/algo/api/v3/user/status?Encode=1",
        headers=headers,
        method="POST",
        body=body,
        timeout=timeout,
    )


def _request_official_credit_usage(
    protocol: ModuleType,
    settings: Settings,
    *,
    cached_user_info: Mapping[str, Any],
    machine_id: str,
    machine_token: str = "",
    machine_type: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    request = getattr(protocol, "request_official_credit_usage", None)
    if callable(request):
        return request(
            cached_user_info=cached_user_info,
            machine_id=machine_id,
            machine_token=machine_token,
            machine_type=machine_type,
            timeout=timeout,
        )

    build_headers = getattr(protocol, "build_official_authorization_headers", None)
    center_base = str(getattr(protocol, "OFFICIAL_CENTER_BASE", "https://center.qoder.sh")).rstrip("/")
    if not callable(build_headers):
        raise AttributeError("纯协议模块缺少 credit usage 所需的授权构建函数")

    headers, _ = build_headers(
        cached_user_info=cached_user_info,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
        sig_path="/api/v2/quota/usage",
        body="",
    )
    return _request_json_via_http_client(
        protocol,
        settings,
        url=f"{center_base}/algo/api/v2/quota/usage?Encode=1",
        headers=headers,
        method="POST",
        body="",
        timeout=timeout,
    )


def _build_credit_usage_fallback_payload(snapshot: AccountStatusSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    if snapshot.credit_usage_payload:
        return dict(snapshot.credit_usage_payload)

    user_quota: dict[str, Any] = {}
    if snapshot.quota_total is not None:
        user_quota["total"] = snapshot.quota_total
    if snapshot.quota_used is not None:
        user_quota["used"] = snapshot.quota_used
    if snapshot.quota_remaining is not None:
        user_quota["remaining"] = snapshot.quota_remaining
    if snapshot.quota_percentage is not None:
        user_quota["percentage"] = snapshot.quota_percentage
    if snapshot.quota_unit:
        user_quota["unit"] = snapshot.quota_unit
    if snapshot.quota_detail_url:
        user_quota["detailUrl"] = snapshot.quota_detail_url

    payload: dict[str, Any] = {}
    if snapshot.total_usage_percentage is not None:
        payload["totalUsagePercentage"] = snapshot.total_usage_percentage
    if snapshot.is_quota_exceeded is not None:
        payload["isQuotaExceeded"] = snapshot.is_quota_exceeded
    if user_quota:
        payload["userQuota"] = user_quota
    if snapshot.quota_expires_at is not None:
        payload["expiresAt"] = snapshot.quota_expires_at
    if snapshot.add_credits_url:
        payload["addCreditsUrl"] = snapshot.add_credits_url
    if snapshot.upgrade_url:
        payload["upgradeUrl"] = snapshot.upgrade_url
    return payload


def refresh_account_status(
    settings: Settings,
    qoder_home: Path,
    *,
    credit_usage_timeout: float = 20.0,
    reuse_cached_credit_usage_on_error: bool = False,
) -> AccountStatusSnapshot:
    cached_user_info = load_cached_user_info(qoder_home)
    if cached_user_info is None:
        raise ValueError("账号当前没有可用登录态")

    machine_id = load_machine_id(qoder_home)
    if not machine_id:
        raise ValueError("账号缺少 machine_id，无法检测状态")

    device_metadata = _load_device_metadata(qoder_home)
    protocol = _load_protocol_module(str(_load_protocol_path(settings)))
    cached_payload = cached_user_info.model_dump(by_alias=True, exclude_none=True)
    machine_token = device_metadata.get("machine_token", "")
    machine_type = device_metadata.get("machine_type", "")
    cached_snapshot = load_account_status(qoder_home)

    user_status = _request_official_user_status(
        protocol,
        settings,
        cached_user_info=cached_payload,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )
    data_policy = _request_official_data_policy(
        protocol,
        settings,
        cached_user_info=cached_payload,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )
    user_plan = _request_official_user_plan(
        protocol,
        settings,
        cached_user_info=cached_payload,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )

    credit_usage: dict[str, Any] = {}
    credit_usage_error = ""
    try:
        credit_usage = _request_official_credit_usage(
            protocol,
            settings,
            cached_user_info=cached_payload,
            machine_id=machine_id,
            machine_token=machine_token,
            machine_type=machine_type,
            timeout=credit_usage_timeout,
        )
    except Exception as exc:
        credit_usage_error = str(exc)
        if reuse_cached_credit_usage_on_error:
            credit_usage = _build_credit_usage_fallback_payload(cached_snapshot)

    snapshot = build_account_status_snapshot(
        cached_user_info=cached_payload,
        user_status=user_status,
        user_plan=user_plan,
        data_policy=data_policy,
        credit_usage=credit_usage,
        source="live_query",
        output_dir=device_metadata.get("output_dir", ""),
        summary_path=device_metadata.get("summary_path", ""),
        credit_usage_error=credit_usage_error,
    )
    return save_account_status(qoder_home, snapshot)
