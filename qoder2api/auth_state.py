"""Qoder 本地登录态读写。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .crypto import aes_decrypt_base64, aes_encrypt_base64, generate_auth_fields


class CachedUserInfo(BaseModel):
    """与 `.qoder/.auth/user` 对齐的缓存登录信息。"""

    aid: str = ""
    uid: str = ""
    name: str = ""
    yx_uid: str = ""
    organization_id: str = ""
    organization_name: str = ""
    staff_id: str = Field(default="", alias="staffId")
    avatar_url: str = ""
    encrypt_user_info: str = ""
    key: str = ""
    access_token: str = ""
    security_oauth_token: str = ""
    refresh_token: str = ""
    expire_time: int = 0
    refresh_token_expire_time: int = 0
    user_source_channel: str = "common"
    user_type: str = "personal_standard"
    data_policy_agreed: bool = True
    personal_access_token: str = ""
    login_method: str = "browser"
    email: str = ""
    user_tag: str = ""
    is_data_policy_modifiable: bool = False
    login_device_id: str = ""
    login_timestamp: int = 0
    organization_tags: list[str] | None = None

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


def get_auth_dir(qoder_home: Path) -> Path:
    return qoder_home / ".auth"


def ensure_auth_dir(qoder_home: Path) -> Path:
    auth_dir = get_auth_dir(qoder_home)
    auth_dir.mkdir(parents=True, exist_ok=True)
    return auth_dir


def get_machine_id_path(qoder_home: Path) -> Path:
    return get_auth_dir(qoder_home) / "id"


def get_user_file_path(qoder_home: Path) -> Path:
    return get_auth_dir(qoder_home) / "user"


def load_machine_id(qoder_home: Path) -> str:
    path = get_machine_id_path(qoder_home)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def save_machine_id(qoder_home: Path, machine_id: str) -> str:
    normalized = (machine_id or "").strip()
    if not normalized:
        raise ValueError("machine_id 不能为空")
    ensure_auth_dir(qoder_home)
    get_machine_id_path(qoder_home).write_text(normalized, encoding="utf-8")
    return normalized


def ensure_machine_id(qoder_home: Path) -> str:
    machine_id = load_machine_id(qoder_home)
    if machine_id:
        return machine_id
    machine_id = str(uuid.uuid4())
    save_machine_id(qoder_home, machine_id)
    return machine_id


def load_cached_user_info(qoder_home: Path) -> CachedUserInfo | None:
    user_path = get_user_file_path(qoder_home)
    if not user_path.is_file():
        return None

    machine_id = load_machine_id(qoder_home)
    if not machine_id:
        raise ValueError(f"找不到 machine_id 文件: {get_machine_id_path(qoder_home)}")

    encrypted = user_path.read_text(encoding="utf-8", errors="replace").strip()
    if not encrypted:
        return None

    decrypted = aes_decrypt_base64(encrypted, machine_id)
    data = json.loads(decrypted)
    return CachedUserInfo.model_validate(data)


def ensure_auth_fields(info: CachedUserInfo) -> CachedUserInfo:
    if info.encrypt_user_info and info.key:
        return info
    encrypt_user_info, key = generate_auth_fields(info)
    info.encrypt_user_info = encrypt_user_info
    info.key = key
    return info


def save_cached_user_info(qoder_home: Path, info: CachedUserInfo) -> CachedUserInfo:
    ensure_auth_dir(qoder_home)
    machine_id = ensure_machine_id(qoder_home)
    ensure_auth_fields(info)
    raw = info.model_dump_json(by_alias=True, exclude_none=True)
    encrypted = aes_encrypt_base64(raw, machine_id)
    get_user_file_path(qoder_home).write_text(encrypted, encoding="utf-8")
    return info


def inspect_local_auth(qoder_home: Path) -> dict[str, Any]:
    """仅从本地 `.qoder/.auth` 目录给出保守登录态提示。"""
    auth_dir = get_auth_dir(qoder_home)
    machine_id = load_machine_id(qoder_home)
    user_path = get_user_file_path(qoder_home)
    has_user_file = user_path.is_file()
    if has_user_file:
        try:
            has_user_file = user_path.stat().st_size > 0
        except OSError:
            has_user_file = False

    cached: CachedUserInfo | None
    error_message: str | None = None
    try:
        cached = load_cached_user_info(qoder_home)
    except Exception as exc:  # pragma: no cover - 只在坏文件时触发
        cached = None
        error_message = str(exc)

    return {
        "qoder_home": str(qoder_home),
        "auth_dir": str(auth_dir),
        "has_machine_id": bool(machine_id),
        "has_user_file": has_user_file,
        "logged_in_hint": cached is not None and bool(cached.uid) and bool(cached.key),
        "machine_id": machine_id,
        "uid": cached.uid if cached else "",
        "name": cached.name if cached else "",
        "email": cached.email if cached else "",
        "user_type": cached.user_type if cached else "",
        "login_device_id": cached.login_device_id if cached else "",
        "auth_error": error_message,
    }


def merge_refreshed_tokens(
    info: CachedUserInfo,
    *,
    access_token: str,
    refresh_token: str,
    expire_time: int,
    refresh_token_expire_time: int,
) -> CachedUserInfo:
    info.access_token = access_token
    info.security_oauth_token = access_token
    if refresh_token:
        info.refresh_token = refresh_token
    if expire_time:
        info.expire_time = expire_time
    if refresh_token_expire_time:
        info.refresh_token_expire_time = refresh_token_expire_time
    if not info.login_timestamp:
        info.login_timestamp = int(time.time() * 1000)
    return info
