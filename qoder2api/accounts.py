"""Qoder 账号池持久化与调度。"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .auth_state import inspect_local_auth
from .settings import Settings

_ACCOUNT_FILE_LOCK = threading.RLock()
_DEVICE_METADATA_NAME = "xieyi_device.json"
_ACCOUNT_REQUEST_COOLDOWN_FAILURE_THRESHOLD = 2
_ACCOUNT_REQUEST_DISABLE_FAILURE_THRESHOLD = 5
_ACCOUNT_REQUEST_COOLDOWN_SECONDS = 300


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _reset_request_failure_state(account: 'QoderAccount') -> None:
    account.consecutive_request_failures = 0
    account.cooldown_until = None
    account.last_request_failed_at = None
    account.last_request_error = ""


def _is_account_in_cooldown(account: 'QoderAccount') -> bool:
    cooldown_until = _parse_iso(account.cooldown_until)
    return cooldown_until is not None and cooldown_until > datetime.now()


class QoderAccount(BaseModel):
    """账号池中的单个 Qoder 账号。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    label: str = ""
    enabled: bool = True
    managed: bool = True
    status: str = "ready"
    auth_type: str = "qoder-browser"
    qoder_home: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    machine_id: str = ""
    login_device_id: str = ""
    device_workflow: str = ""
    machine_token: str = ""
    machine_type: str = ""
    device_hostname: str = ""
    device_mac: str = ""
    consecutive_request_failures: int = 0
    cooldown_until: str | None = None
    last_request_failed_at: str | None = None
    last_request_error: str = ""

    @property
    def qoder_home_path(self) -> Path:
        return Path(self.qoder_home).expanduser().resolve()


class AccountPoolConfig(BaseModel):
    """账号池配置。"""

    accounts: list[QoderAccount] = Field(default_factory=list)


def load_device_metadata(qoder_home: Path) -> dict[str, str]:
    auth_dir = qoder_home / ".auth"
    metadata_path = auth_dir / _DEVICE_METADATA_NAME
    if not metadata_path.is_file():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "device_workflow": str(payload.get("workflow") or "").strip(),
        "machine_token": str(payload.get("machine_token") or "").strip(),
        "machine_type": str(payload.get("machine_type") or "").strip(),
        "device_hostname": str(payload.get("hostname") or "").strip(),
        "device_mac": str(payload.get("mac") or "").strip(),
        "output_dir": str(payload.get("output_dir") or "").strip(),
        "summary_path": str(payload.get("summary_path") or "").strip(),
    }


def get_pool_root(settings: Settings) -> Path:
    return settings.workspace_path / ".qoder2api"


def get_accounts_root(settings: Settings) -> Path:
    return get_pool_root(settings) / "accounts"


def get_account_config_path(settings: Settings) -> Path:
    return get_pool_root(settings) / "accounts.json"


def is_pool_configured(settings: Settings) -> bool:
    return get_account_config_path(settings).exists()


def _normalize_account(account: QoderAccount) -> QoderAccount:
    if not account.id:
        account.id = uuid4().hex

    account.label = (account.label or "").strip()
    account.qoder_home = str(Path(account.qoder_home).expanduser().resolve()) if account.qoder_home else ""
    account.status = (account.status or "ready").strip() or "ready"
    account.auth_type = (account.auth_type or "qoder-browser").strip() or "qoder-browser"
    account.machine_id = (account.machine_id or "").strip()
    account.login_device_id = (account.login_device_id or "").strip()
    account.device_workflow = (account.device_workflow or "").strip()
    account.machine_token = (account.machine_token or "").strip()
    account.machine_type = (account.machine_type or "").strip()
    account.device_hostname = (account.device_hostname or "").strip()
    account.device_mac = (account.device_mac or "").strip()
    account.consecutive_request_failures = max(int(account.consecutive_request_failures or 0), 0)
    account.cooldown_until = str(account.cooldown_until or "").strip() or None
    account.last_request_failed_at = str(account.last_request_failed_at or "").strip() or None
    account.last_request_error = (account.last_request_error or "").strip()

    cooldown_until = _parse_iso(account.cooldown_until)
    if cooldown_until is not None and cooldown_until <= datetime.now():
        account.cooldown_until = None

    if not account.created_at:
        account.created_at = _now_iso()
    if not account.updated_at:
        account.updated_at = account.created_at
    if not account.label:
        prefix = "Qoder 账号" if account.managed else "外部账号"
        account.label = f"{prefix}-{account.id[:6]}"
    return account


def _load_pool_config_unlocked(settings: Settings) -> AccountPoolConfig:
    config_path = get_account_config_path(settings)
    if not config_path.is_file():
        return AccountPoolConfig()

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AccountPoolConfig()

    config = AccountPoolConfig.model_validate(data)
    config.accounts = [_normalize_account(account) for account in config.accounts]
    return config


def _save_pool_config_unlocked(settings: Settings, config: AccountPoolConfig) -> None:
    config_path = get_account_config_path(settings)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = AccountPoolConfig(accounts=[_normalize_account(account) for account in config.accounts])
    config_path.write_text(
        normalized.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )


def load_pool_config(settings: Settings) -> AccountPoolConfig:
    with _ACCOUNT_FILE_LOCK:
        return _load_pool_config_unlocked(settings)


def _bootstrap_managed_qoder_home(settings: Settings, qoder_home: Path) -> None:
    qoder_home.mkdir(parents=True, exist_ok=True)
    (qoder_home / ".auth").mkdir(parents=True, exist_ok=True)

    base_home = settings.qoder_home_path

    source_config = base_home / ".config.json"
    target_config = qoder_home / ".config.json"
    if source_config.is_file() and not target_config.exists():
        shutil.copy2(source_config, target_config)

    source_models = base_home / ".auth" / "models"
    target_models = qoder_home / ".auth" / "models"
    if source_models.is_file() and not target_models.exists():
        shutil.copy2(source_models, target_models)


def _account_from_auth_state(
    *,
    account: QoderAccount,
    auth_state: dict[str, Any],
) -> QoderAccount:
    refreshed = account.model_copy(deep=True)
    refreshed.machine_id = ""
    refreshed.login_device_id = str(auth_state.get("login_device_id") or "").strip()
    refreshed.device_workflow = ""
    refreshed.machine_token = ""
    refreshed.machine_type = ""
    refreshed.device_hostname = ""
    refreshed.device_mac = ""

    auth_dir = refreshed.qoder_home_path / ".auth"
    id_path = auth_dir / "id"
    if id_path.is_file():
        try:
            refreshed.machine_id = id_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            refreshed.machine_id = ""

    metadata = load_device_metadata(refreshed.qoder_home_path)
    if metadata:
        refreshed.device_workflow = metadata.get("device_workflow", "")
        refreshed.machine_token = metadata.get("machine_token", "")
        refreshed.machine_type = metadata.get("machine_type", "")
        refreshed.device_hostname = metadata.get("device_hostname", "")
        refreshed.device_mac = metadata.get("device_mac", "")

    refreshed.updated_at = _now_iso()
    if refreshed.status == "pending_login" and auth_state.get("logged_in_hint"):
        refreshed.status = "ready"
        refreshed.enabled = True

    return _normalize_account(refreshed)


def build_legacy_account(settings: Settings) -> QoderAccount | None:
    auth_state = inspect_local_auth(settings.qoder_home_path)
    if not settings.qoder_home_path.exists():
        return None

    account = QoderAccount(
        id="legacy-primary",
        label="当前 .qoder",
        enabled=True,
        managed=False,
        status="ready" if auth_state.get("logged_in_hint") else "detected",
        auth_type="qoder-browser",
        qoder_home=str(settings.qoder_home_path),
    )
    return _account_from_auth_state(account=account, auth_state=auth_state)


def list_accounts(settings: Settings, *, include_pending: bool = False) -> list[QoderAccount]:
    with _ACCOUNT_FILE_LOCK:
        config_path = get_account_config_path(settings)
        config = _load_pool_config_unlocked(settings)
        accounts: list[QoderAccount] = []
        seen_homes: set[str] = set()

        for raw_account in config.accounts:
            normalized = _normalize_account(raw_account)
            auth_state = inspect_local_auth(normalized.qoder_home_path)
            refreshed = _account_from_auth_state(account=normalized, auth_state=auth_state)
            if refreshed.status == "pending_login" and not include_pending:
                continue
            accounts.append(refreshed)
            seen_homes.add(refreshed.qoder_home)

        legacy = build_legacy_account(settings)
        if (
            not config_path.exists()
            and not accounts
            and legacy is not None
            and legacy.qoder_home not in seen_homes
        ):
            accounts.insert(0, legacy)

        return accounts


def get_account(settings: Settings, account_id: str, *, include_pending: bool = True) -> QoderAccount | None:
    for account in list_accounts(settings, include_pending=include_pending):
        if account.id == account_id:
            return account
    return None


def get_enabled_accounts(settings: Settings) -> list[QoderAccount]:
    accounts = list_accounts(settings)
    return [
        account
        for account in accounts
        if (
            account.enabled
            and not _is_account_in_cooldown(account)
            and inspect_local_auth(account.qoder_home_path).get("logged_in_hint")
        )
    ]


def create_pending_managed_account(settings: Settings, label: str = "") -> QoderAccount:
    with _ACCOUNT_FILE_LOCK:
        config = _load_pool_config_unlocked(settings)
        account = _normalize_account(
            QoderAccount(
                label=label.strip() or "",
                enabled=False,
                managed=True,
                status="pending_login",
                auth_type="qoder-browser",
                qoder_home=str((get_accounts_root(settings) / uuid4().hex / ".qoder").resolve()),
            )
        )
        _bootstrap_managed_qoder_home(settings, account.qoder_home_path)
        config.accounts.append(account)
        _save_pool_config_unlocked(settings, config)
        return account


def finalize_pending_account(settings: Settings, account_id: str) -> QoderAccount:
    with _ACCOUNT_FILE_LOCK:
        config = _load_pool_config_unlocked(settings)
        for index, existing in enumerate(config.accounts):
            if existing.id != account_id:
                continue
            auth_state = inspect_local_auth(existing.qoder_home_path)
            refreshed = _account_from_auth_state(account=existing, auth_state=auth_state)
            refreshed.status = "ready"
            refreshed.enabled = True
            _reset_request_failure_state(refreshed)
            refreshed.updated_at = _now_iso()
            config.accounts[index] = refreshed
            _save_pool_config_unlocked(settings, config)
            return refreshed
    raise KeyError(account_id)


def remove_account(settings: Settings, account_id: str) -> bool:
    with _ACCOUNT_FILE_LOCK:
        config = _load_pool_config_unlocked(settings)
        remaining: list[QoderAccount] = []
        removed: QoderAccount | None = None
        for account in config.accounts:
            if account.id == account_id:
                removed = account
                continue
            remaining.append(account)

        if removed is None:
            return False

        config.accounts = remaining
        _save_pool_config_unlocked(settings, config)

    if removed.managed:
        managed_root = removed.qoder_home_path.parent
        shutil.rmtree(managed_root, ignore_errors=True)
    return True


def update_account_enabled(settings: Settings, account_id: str, enabled: bool) -> QoderAccount:
    with _ACCOUNT_FILE_LOCK:
        config = _load_pool_config_unlocked(settings)
        for index, existing in enumerate(config.accounts):
            if existing.id != account_id:
                continue
            updated = existing.model_copy(deep=True)
            updated.enabled = enabled
            if enabled:
                _reset_request_failure_state(updated)
            updated.updated_at = _now_iso()
            config.accounts[index] = _normalize_account(updated)
            _save_pool_config_unlocked(settings, config)
            return config.accounts[index]
    raise KeyError(account_id)


def update_account_label(settings: Settings, account_id: str, label: str) -> QoderAccount:
    normalized_label = label.strip()
    if not normalized_label:
        raise ValueError("label 不能为空")

    with _ACCOUNT_FILE_LOCK:
        config = _load_pool_config_unlocked(settings)
        for index, existing in enumerate(config.accounts):
            if existing.id != account_id:
                continue
            updated = existing.model_copy(deep=True)
            updated.label = normalized_label
            updated.updated_at = _now_iso()
            config.accounts[index] = _normalize_account(updated)
            _save_pool_config_unlocked(settings, config)
            return config.accounts[index]
    raise KeyError(account_id)


def mark_account_request_success(settings: Settings, account_id: str) -> QoderAccount:
    with _ACCOUNT_FILE_LOCK:
        config = _load_pool_config_unlocked(settings)
        for index, existing in enumerate(config.accounts):
            if existing.id != account_id:
                continue
            updated = existing.model_copy(deep=True)
            _reset_request_failure_state(updated)
            updated.updated_at = _now_iso()
            config.accounts[index] = _normalize_account(updated)
            _save_pool_config_unlocked(settings, config)
            return config.accounts[index]
    raise KeyError(account_id)


def mark_account_request_failure(
    settings: Settings,
    account_id: str,
    *,
    error_message: str,
    cooldown_seconds: int = _ACCOUNT_REQUEST_COOLDOWN_SECONDS,
    cooldown_threshold: int = _ACCOUNT_REQUEST_COOLDOWN_FAILURE_THRESHOLD,
    disable_threshold: int = _ACCOUNT_REQUEST_DISABLE_FAILURE_THRESHOLD,
) -> QoderAccount:
    with _ACCOUNT_FILE_LOCK:
        config = _load_pool_config_unlocked(settings)
        for index, existing in enumerate(config.accounts):
            if existing.id != account_id:
                continue
            updated = existing.model_copy(deep=True)
            updated.consecutive_request_failures = max(updated.consecutive_request_failures, 0) + 1
            updated.last_request_failed_at = _now_iso()
            updated.last_request_error = error_message.strip()
            if updated.consecutive_request_failures >= disable_threshold:
                updated.enabled = False
                updated.cooldown_until = None
            elif updated.consecutive_request_failures >= cooldown_threshold:
                updated.cooldown_until = (
                    datetime.now() + timedelta(seconds=max(cooldown_seconds, 1))
                ).isoformat(timespec="seconds")
            updated.updated_at = _now_iso()
            config.accounts[index] = _normalize_account(updated)
            _save_pool_config_unlocked(settings, config)
            return config.accounts[index]
    raise KeyError(account_id)


class QoderAccountPool:
    """账号池运行时调度器。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._round_robin_index = 0

    def list_accounts(self, *, include_pending: bool = False) -> list[QoderAccount]:
        return list_accounts(self.settings, include_pending=include_pending)

    def get_account(self, account_id: str, *, include_pending: bool = True) -> QoderAccount:
        account = get_account(self.settings, account_id, include_pending=include_pending)
        if account is None:
            raise KeyError(account_id)
        return account

    def get_enabled_accounts(self) -> list[QoderAccount]:
        return get_enabled_accounts(self.settings)

    def is_configured(self) -> bool:
        return is_pool_configured(self.settings)

    def pick_accounts_for_request(
        self,
        preferred_account_id: str | None = None,
    ) -> list[QoderAccount]:
        enabled_accounts = self.get_enabled_accounts()
        if not enabled_accounts:
            return []

        if preferred_account_id:
            for account in enabled_accounts:
                if account.id == preferred_account_id:
                    return [account]

        with self._lock:
            index = self._round_robin_index % len(enabled_accounts)
            self._round_robin_index = (self._round_robin_index + 1) % len(enabled_accounts)
            return enabled_accounts[index:] + enabled_accounts[:index]

    def pick_account(self, preferred_account_id: str | None = None) -> QoderAccount | None:
        accounts = self.pick_accounts_for_request(preferred_account_id=preferred_account_id)
        return accounts[0] if accounts else None
