"""WebUI 可编辑运行设置的持久化与环境同步。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .settings import Settings

_RUNTIME_SETTINGS_DIRNAME = '.qoder2api'
_RUNTIME_SETTINGS_FILENAME = 'runtime_settings.json'
_PROXY_ENV_KEYS = (
    'HTTP_PROXY',
    'HTTPS_PROXY',
    'ALL_PROXY',
    'http_proxy',
    'https_proxy',
    'all_proxy',
)
_LLMAPI_ENV_KEYS = ('LLMAPI_API_KEY', 'QODER2API_LLMAPI_API_KEY')


class RuntimeSettingsSnapshot(BaseModel):
    """运行时可热更新的设置快照。"""

    proxy_url: str = ''
    llmapi_api_key: str = ''
    account_pool_max_concurrency: int = Field(default=4, ge=1, le=512)
    default_stream: bool = True

    model_config = ConfigDict(extra='ignore')


def get_runtime_settings_path(settings: Settings) -> Path:
    return settings.workspace_path / _RUNTIME_SETTINGS_DIRNAME / _RUNTIME_SETTINGS_FILENAME


def load_runtime_settings(settings: Settings) -> RuntimeSettingsSnapshot:
    path = get_runtime_settings_path(settings)
    if not path.is_file():
        snapshot = RuntimeSettingsSnapshot()
        apply_runtime_environment(snapshot)
        return snapshot

    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        snapshot = RuntimeSettingsSnapshot()
        apply_runtime_environment(snapshot)
        return snapshot

    if not isinstance(payload, dict):
        snapshot = RuntimeSettingsSnapshot()
        apply_runtime_environment(snapshot)
        return snapshot

    try:
        snapshot = RuntimeSettingsSnapshot.model_validate(payload)
    except ValidationError:
        snapshot = RuntimeSettingsSnapshot()
    apply_runtime_environment(snapshot)
    return snapshot


def _set_or_clear_env(name: str, value: str) -> None:
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)


def apply_runtime_environment(snapshot: RuntimeSettingsSnapshot) -> RuntimeSettingsSnapshot:
    for key in _PROXY_ENV_KEYS:
        _set_or_clear_env(key, snapshot.proxy_url)
    for key in _LLMAPI_ENV_KEYS:
        _set_or_clear_env(key, snapshot.llmapi_api_key)
    return snapshot


class RuntimeSettingsManager:
    """运行设置读写入口。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def path(self) -> Path:
        return get_runtime_settings_path(self.settings)

    def load(self) -> RuntimeSettingsSnapshot:
        return load_runtime_settings(self.settings)

    def save(self, payload: RuntimeSettingsSnapshot | dict[str, object]) -> RuntimeSettingsSnapshot:
        snapshot = (
            payload
            if isinstance(payload, RuntimeSettingsSnapshot)
            else RuntimeSettingsSnapshot.model_validate(payload)
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            snapshot.model_dump_json(indent=2, exclude_none=True),
            encoding='utf-8',
        )
        return apply_runtime_environment(snapshot)
