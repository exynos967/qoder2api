"""qoder2api 配置与路径发现。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_INFER_ENDPOINT = "https://api3.qoder.sh"
_DEFAULT_OPENAPI_ENDPOINT = "https://openapi.qoder.sh"
_DEFAULT_CENTER_ENDPOINT = "https://center.qoder.sh"
_DEFAULT_WEB_ENDPOINT = "https://www.qoder.com"
_DEFAULT_BROWSER_LOGIN_BACKEND = "namespaced"


def _iter_parent_candidates(start_dir: Path) -> Iterable[Path]:
    current = start_dir.resolve()
    yield current
    yield from current.parents


def discover_qoder_home(
    explicit: str | None = None,
    start_dir: Path | None = None,
) -> Path:
    """优先使用显式配置，否则从当前目录向上寻找 `.qoder`。"""
    if explicit:
        return Path(explicit).expanduser().resolve()

    search_start = (start_dir or Path.cwd()).resolve()
    for base in _iter_parent_candidates(search_start):
        candidate = base / ".qoder"
        if candidate.is_dir():
            return candidate

    return (Path.home() / ".qoder").resolve()


def discover_qoder_cli_path(qoder_home: Path, explicit: str | None = None) -> Path:
    """兼容保留：发现 Qoder CLI 可执行文件。"""
    if explicit:
        return Path(explicit).expanduser().resolve()

    direct_candidate = qoder_home / "bin" / "qodercli"
    if direct_candidate.is_file():
        return direct_candidate.resolve()

    version_dir = qoder_home / "bin" / "qodercli"
    if version_dir.is_dir():
        candidates = [
            path
            for path in version_dir.iterdir()
            if path.is_file() and path.name.startswith("qodercli-")
        ]
        if candidates:
            candidates.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
            return candidates[0].resolve()

    recursive_candidates = [
        path
        for path in qoder_home.rglob("qodercli*")
        if path.is_file() and "bin" in path.parts
    ]
    if recursive_candidates:
        recursive_candidates.sort(
            key=lambda path: (path.stat().st_mtime, path.name),
            reverse=True,
        )
        return recursive_candidates[0].resolve()

    return direct_candidate.resolve()


def discover_workspace(explicit: str | None, qoder_home: Path) -> Path:
    """默认把 `.qoder` 所在目录当成工作区根目录。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    return qoder_home.parent.resolve()


def _load_qoder_config(qoder_home: Path) -> dict[str, Any]:
    config_path = qoder_home / ".config.json"
    if not config_path.is_file():
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def discover_infer_endpoint(qoder_home: Path, explicit: str | None = None) -> str:
    """发现 Qoder 推理入口。"""
    if explicit:
        return explicit.strip()

    config = _load_qoder_config(qoder_home)
    preferred_node = config.get("preferredInferenceNode")
    if isinstance(preferred_node, dict):
        endpoint = preferred_node.get("endpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()

    return _DEFAULT_INFER_ENDPOINT


def discover_openapi_endpoint(explicit: str | None = None) -> str:
    """发现 Qoder OpenAPI 入口。"""
    return explicit.strip() if explicit else _DEFAULT_OPENAPI_ENDPOINT


def discover_center_endpoint(explicit: str | None = None) -> str:
    """发现 Qoder Center 入口。"""
    return explicit.strip() if explicit else _DEFAULT_CENTER_ENDPOINT


def discover_web_endpoint(explicit: str | None = None) -> str:
    """发现 Qoder Web 入口。"""
    return explicit.strip() if explicit else _DEFAULT_WEB_ENDPOINT


def _append_unique_candidate(candidates: list[Path], candidate: Path) -> None:
    if candidate not in candidates:
        candidates.append(candidate)


def discover_namespaced_login_script(explicit: str | None = None) -> Path:
    """发现 namespaced 纯协议登录工作流脚本。"""
    if explicit:
        return Path(explicit).expanduser().resolve()

    candidates = [Path("/opt/qoder-machine/qoder-machine/xieyi/qoder_pure_login.py")]
    current_file = Path(__file__).resolve()
    relative_candidates = (
        Path("vendor") / "qoder-machine" / "qoder-machine" / "xieyi" / "qoder_pure_login.py",
        Path("machine") / "qoder-machine" / "xieyi" / "qoder_pure_login.py",
    )
    for base in (current_file.parent, *current_file.parents):
        for relative in relative_candidates:
            _append_unique_candidate(candidates, base / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


class Settings(BaseSettings):
    """运行配置。"""

    host: str = "0.0.0.0"
    port: int = Field(default=28001, ge=1, le=65535)

    qoder_home: str | None = None
    qoder_cli_path: str | None = None
    workspace: str | None = None

    qoder_infer_endpoint: str | None = None
    qoder_openapi_endpoint: str | None = None
    qoder_center_endpoint: str | None = None
    qoder_web_endpoint: str | None = None
    client_version: str = "0.1.32"
    browser_login_backend: str = _DEFAULT_BROWSER_LOGIN_BACKEND
    namespaced_login_script: str | None = None
    namespaced_login_max_attempts: int = Field(default=3, ge=1, le=20)
    namespaced_login_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    namespaced_login_output_root: str | None = None

    default_model: str = "auto"
    model_scene: str = "chat"
    request_timeout_seconds: int = Field(default=600, ge=10, le=3600)
    stream_chunk_chars: int = Field(default=512, ge=32, le=8192)

    model_config = SettingsConfigDict(
        env_prefix="QODER2API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def qoder_home_path(self) -> Path:
        return discover_qoder_home(self.qoder_home)

    @property
    def cli_executable(self) -> Path:
        return discover_qoder_cli_path(self.qoder_home_path, self.qoder_cli_path)

    @property
    def workspace_path(self) -> Path:
        return discover_workspace(self.workspace, self.qoder_home_path)

    @property
    def infer_endpoint(self) -> str:
        return discover_infer_endpoint(self.qoder_home_path, self.qoder_infer_endpoint)

    @property
    def openapi_endpoint(self) -> str:
        return discover_openapi_endpoint(self.qoder_openapi_endpoint)

    @property
    def center_endpoint(self) -> str:
        return discover_center_endpoint(self.qoder_center_endpoint)

    @property
    def web_endpoint(self) -> str:
        return discover_web_endpoint(self.qoder_web_endpoint)

    @property
    def browser_login_backend_name(self) -> str:
        candidate = (self.browser_login_backend or "").strip().lower()
        if candidate in {"direct", "namespaced"}:
            return candidate
        return _DEFAULT_BROWSER_LOGIN_BACKEND

    @property
    def logs_dir_path(self) -> Path:
        path = self.qoder_home_path / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def app_log_path(self) -> Path:
        return self.logs_dir_path / "qoder2api.log"

    @property
    def namespaced_login_script_path(self) -> Path:
        return discover_namespaced_login_script(self.namespaced_login_script)

    @property
    def namespaced_login_output_root_path(self) -> Path | None:
        if not self.namespaced_login_output_root:
            return None
        return Path(self.namespaced_login_output_root).expanduser().resolve()
