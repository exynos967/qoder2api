"""OpenAI 与 WebUI 兼容请求模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """OpenAI chat message。"""

    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="ignore")


class ChatCompletionRequest(BaseModel):
    """最小可用的 Chat Completions 请求。"""

    model: str = "auto"
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None

    model_config = ConfigDict(extra="ignore")


class LoginSessionInputRequest(BaseModel):
    """兼容保留：向登录会话发送按键。"""

    key: Literal["enter", "up", "down", "escape", "ctrl_c"]

    model_config = ConfigDict(extra="forbid")


class StartBrowserLoginRequest(BaseModel):
    """启动浏览器登录并准备入池账号。"""

    label: str | None = None

    model_config = ConfigDict(extra="ignore")


class AccountToggleRequest(BaseModel):
    """切换账号启用状态。"""

    enabled: bool

    model_config = ConfigDict(extra="forbid")


class AccountLabelUpdateRequest(BaseModel):
    """更新账号标签。"""

    label: str

    model_config = ConfigDict(extra="forbid")


class RuntimeSettingsUpdateRequest(BaseModel):
    """更新 WebUI 运行设置。"""

    proxy_url: str = ""
    llmapi_api_key: str = ""
    account_pool_max_concurrency: int = Field(default=4, ge=1, le=512)

    model_config = ConfigDict(extra="forbid")
