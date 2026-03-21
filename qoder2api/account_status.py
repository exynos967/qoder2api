"""Qoder 账号套餐状态的持久化与在线检测。"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from pydantic import BaseModel, Field

from .auth_state import (
    CachedUserInfo,
    ensure_auth_dir,
    load_cached_user_info,
    load_machine_id,
)
from .settings import Settings

_ACCOUNT_STATUS_NAME = "account_status.json"
_DEVICE_METADATA_NAME = "xieyi_device.json"


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
    source: str,
    output_dir: str = "",
    summary_path: str = "",
    status_error: str = "",
) -> AccountStatusSnapshot:
    cached = _cached_payload(cached_user_info)
    raw_user_status = _coerce_mapping(user_status)
    raw_user_plan = _coerce_mapping(user_plan)
    raw_data_policy = _coerce_mapping(data_policy)
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
        source="namespaced_summary",
        output_dir=str(summary.get("outputDir") or ""),
        summary_path=_build_summary_path(summary),
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
    spec.loader.exec_module(module)
    return module


def refresh_account_status(settings: Settings, qoder_home: Path) -> AccountStatusSnapshot:
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

    user_status = protocol.request_official_user_status(
        cached_user_info=cached_payload,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )
    data_policy = protocol.request_official_data_policy(
        cached_user_info=cached_payload,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )
    user_plan = protocol.request_official_user_plan(
        cached_user_info=cached_payload,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=machine_type,
    )

    snapshot = build_account_status_snapshot(
        cached_user_info=cached_payload,
        user_status=user_status,
        user_plan=user_plan,
        data_policy=data_policy,
        source="live_query",
        output_dir=device_metadata.get("output_dir", ""),
        summary_path=device_metadata.get("summary_path", ""),
    )
    return save_account_status(qoder_home, snapshot)
