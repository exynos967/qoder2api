"""Qoder 独立 HTTP API 客户端。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlencode

import httpx

from . import __version__
from .auth_state import (
    CachedUserInfo,
    ensure_auth_fields,
    ensure_machine_id,
    load_cached_user_info,
    merge_refreshed_tokens,
    save_machine_id,
    save_cached_user_info,
)
from .catalog import QoderModelInfo
from .crypto import build_bearer_token, build_pkce_challenge, generate_pkce_verifier
from .settings import Settings
from .streaming import ParsedStreamEvent, QoderStreamJSONParser

_DEFAULT_CLIENT_ID = "e93fe488-5778-4c35-a6fc-0f54ed7b3139"
_CHAT_PATH = (
    "/algo/api/v2/service/pro/sse/agent_chat_generation"
    "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
)
_LOGIN_POLL_PATH = "/api/v1/deviceToken/poll"
_LOGIN_REFRESH_PATH = "/api/v1/deviceToken/refresh"
_USERINFO_PATH = "/api/v1/userinfo"
_LOGIN_TIMEOUT_SECONDS = 300
_REFRESH_SKEW_MS = 30_000


class QoderAPIError(RuntimeError):
    """Qoder 上游接口错误。"""

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
class BrowserLoginContext:
    auth_url: str
    nonce: str
    verifier: str
    challenge: str
    challenge_method: str
    machine_id: str
    client_id: str


@dataclass(slots=True)
class DeviceTokenResponse:
    token: str
    refresh_token: str
    user_id: str
    user_name: str
    nonce: str
    expires_at: str
    refresh_token_expires_at: str
    expires_in: int = 0
    refresh_token_expires_in: int = 0


@dataclass(slots=True)
class DeviceTokenRefreshResponse:
    device_token: str
    refresh_token: str
    expires_at: str
    refresh_token_expires_at: str


@dataclass(slots=True)
class UserInfoResponse:
    id: str
    name: str
    username: str
    avatar: str
    email: str


class QoderAPIClient:
    """直接与 Qoder 服务端交互，不依赖 qodercli。"""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory or self._default_client

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        )

    @property
    def user_agent(self) -> str:
        return f"qoder2api/{__version__}"

    def create_browser_login(
        self,
        qoder_home: Path,
        *,
        machine_id: str | None = None,
    ) -> BrowserLoginContext:
        resolved_machine_id = (machine_id or "").strip()
        if resolved_machine_id:
            resolved_machine_id = save_machine_id(qoder_home, resolved_machine_id)
        else:
            resolved_machine_id = ensure_machine_id(qoder_home)
        nonce = uuid.uuid4().hex
        verifier = generate_pkce_verifier()
        challenge = build_pkce_challenge(verifier)
        params = {
            "challenge_method": "S256",
            "challenge": challenge,
            "nonce": nonce,
            "machine_id": resolved_machine_id,
            "client_id": _DEFAULT_CLIENT_ID,
        }
        auth_url = (
            f"{self.settings.web_endpoint.rstrip('/')}/device/selectAccounts?{urlencode(params)}"
        )
        return BrowserLoginContext(
            auth_url=auth_url,
            nonce=nonce,
            verifier=verifier,
            challenge=challenge,
            challenge_method="S256",
            machine_id=resolved_machine_id,
            client_id=_DEFAULT_CLIENT_ID,
        )

    async def complete_browser_login(
        self,
        *,
        qoder_home: Path,
        context: BrowserLoginContext,
        progress_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> CachedUserInfo:
        deadline = time.monotonic() + _LOGIN_TIMEOUT_SECONDS
        self._emit(progress_callback, f"登录地址：{context.auth_url}")
        self._emit(progress_callback, "正在轮询 Qoder 登录结果...")

        while time.monotonic() < deadline:
            if cancel_check and cancel_check():
                raise QoderAPIError(
                    "登录已取消",
                    status_code=409,
                    error_type="cancelled",
                )

            token_response = await self.poll_device_token(context)
            if token_response is None:
                await asyncio.sleep(1.0)
                continue

            userinfo = await self.fetch_userinfo(token_response.token)
            info = self.build_cached_user_info(token_response, userinfo)
            info.login_device_id = context.machine_id
            ensure_auth_fields(info)
            save_cached_user_info(qoder_home, info)
            self._emit(progress_callback, f"登录成功，当前账号：{info.name or info.uid}")
            return info

        raise QoderAPIError(
            f"等待网页登录超时（>{_LOGIN_TIMEOUT_SECONDS}s）",
            status_code=408,
            error_type="timeout_error",
        )

    async def poll_device_token(
        self,
        context: BrowserLoginContext,
    ) -> DeviceTokenResponse | None:
        params = {
            "nonce": context.nonce,
            "code_verifier": context.verifier,
            "code_challenge_method": context.challenge_method,
        }
        url = f"{self.settings.openapi_endpoint.rstrip('/')}{_LOGIN_POLL_PATH}"
        headers = self._base_headers(accept="application/json")

        async with self._client_factory() as client:
            response = await client.get(url, params=params, headers=headers)

        text = response.text.strip()
        if response.status_code == 404:
            return None
        if response.status_code == 400:
            raise QoderAPIError(
                self._extract_error_message(text) or "Qoder 登录被拒绝",
                status_code=400,
                error_type="invalid_request_error",
            )
        if response.status_code != 200:
            raise self._map_http_error(response, text, default_message="轮询 Qoder 登录结果失败")

        payload = response.json()
        return DeviceTokenResponse(
            token=str(payload.get("token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            user_id=str(payload.get("user_id") or ""),
            user_name=str(payload.get("user_name") or ""),
            nonce=str(payload.get("nonce") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            refresh_token_expires_at=str(payload.get("refresh_token_expires_at") or ""),
            expires_in=int(payload.get("expires_in") or 0),
            refresh_token_expires_in=int(payload.get("refresh_token_expires_in") or 0),
        )

    async def refresh_device_token(
        self,
        refresh_token: str,
    ) -> DeviceTokenRefreshResponse:
        url = f"{self.settings.openapi_endpoint.rstrip('/')}{_LOGIN_REFRESH_PATH}"
        headers = self._base_headers(accept="application/json")

        async with self._client_factory() as client:
            response = await client.post(
                url,
                headers=headers,
                json={"refresh_token": refresh_token},
            )

        text = response.text.strip()
        if response.status_code != 200:
            raise self._map_http_error(response, text, default_message="刷新 Qoder 登录态失败")

        payload = response.json()
        return DeviceTokenRefreshResponse(
            device_token=str(payload.get("device_token") or payload.get("token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            refresh_token_expires_at=str(payload.get("refresh_token_expires_at") or ""),
        )

    async def fetch_userinfo(self, access_token: str) -> UserInfoResponse:
        if not access_token:
            raise QoderAPIError("缺少 access token，无法获取用户信息", status_code=401)

        url = f"{self.settings.openapi_endpoint.rstrip('/')}{_USERINFO_PATH}"
        headers = self._base_headers(accept="application/json")
        headers["Authorization"] = f"Bearer {access_token}"

        async with self._client_factory() as client:
            response = await client.get(url, headers=headers)

        text = response.text.strip()
        if response.status_code != 200:
            raise self._map_http_error(response, text, default_message="获取 Qoder 用户信息失败")

        payload = response.json()
        return UserInfoResponse(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            username=str(payload.get("username") or ""),
            avatar=str(payload.get("avatar") or ""),
            email=str(payload.get("email") or ""),
        )

    def build_cached_user_info(
        self,
        token_response: DeviceTokenResponse,
        userinfo: UserInfoResponse,
    ) -> CachedUserInfo:
        expire_time = self._parse_token_time(
            token_response.expires_at,
            token_response.expires_in,
        )
        refresh_expire_time = self._parse_token_time(
            token_response.refresh_token_expires_at,
            token_response.refresh_token_expires_in,
        )
        display_name = (
            userinfo.name
            or token_response.user_name
            or userinfo.username
            or token_response.user_id
        )
        return CachedUserInfo(
            aid=token_response.user_id,
            uid=token_response.user_id,
            name=display_name,
            organization_id="",
            organization_name="",
            avatar_url=userinfo.avatar,
            access_token=token_response.token,
            security_oauth_token=token_response.token,
            refresh_token=token_response.refresh_token,
            expire_time=expire_time,
            refresh_token_expire_time=refresh_expire_time,
            user_source_channel="common",
            user_type="personal_standard",
            data_policy_agreed=True,
            login_method="browser",
            email=userinfo.email,
            user_tag="",
            login_device_id="",
            login_timestamp=int(time.time() * 1000),
        )

    async def ensure_cached_user(self, qoder_home: Path) -> CachedUserInfo:
        info = load_cached_user_info(qoder_home)
        if info is None or not info.uid or not info.key:
            raise QoderAPIError(
                "当前账号没有可用登录态，请先在 WebUI 中完成网页登录",
                status_code=401,
                error_type="authentication_error",
            )

        now_ms = int(time.time() * 1000)
        if info.expire_time and info.expire_time > now_ms + _REFRESH_SKEW_MS:
            return info
        if not info.refresh_token:
            return info
        if info.refresh_token_expire_time and info.refresh_token_expire_time <= now_ms:
            raise QoderAPIError(
                "Qoder refresh token 已过期，请重新登录",
                status_code=401,
                error_type="authentication_error",
            )

        refreshed = await self.refresh_device_token(info.refresh_token)
        merge_refreshed_tokens(
            info,
            access_token=refreshed.device_token,
            refresh_token=refreshed.refresh_token or info.refresh_token,
            expire_time=self._parse_token_time(refreshed.expires_at, 0),
            refresh_token_expire_time=self._parse_token_time(
                refreshed.refresh_token_expires_at,
                0,
            ),
        )
        save_cached_user_info(qoder_home, info)
        return info

    async def run_text(
        self,
        *,
        qoder_home: Path,
        prompt: str,
        model_info: QoderModelInfo,
        max_tokens: int | None,
    ) -> str:
        chunks: list[str] = []
        async for event in self.stream_chat(
            qoder_home=qoder_home,
            prompt=prompt,
            model_info=model_info,
            max_tokens=max_tokens,
        ):
            if event.text_delta:
                chunks.append(event.text_delta)
        return "".join(chunks).strip()

    async def stream_chat(
        self,
        *,
        qoder_home: Path,
        prompt: str,
        model_info: QoderModelInfo,
        max_tokens: int | None,
    ) -> AsyncIterator[ParsedStreamEvent]:
        cached = await self.ensure_cached_user(qoder_home)
        request_body = self.build_chat_request(
            prompt=prompt,
            model_info=model_info,
            user_type=cached.user_type,
            max_tokens=max_tokens,
        )
        body_text = json.dumps(request_body, ensure_ascii=False, separators=(",", ":"))
        headers = self.build_chat_headers(
            cached_user=cached,
            body_text=body_text,
            path=_CHAT_PATH,
        )
        url = f"{self.settings.infer_endpoint.rstrip('/')}{_CHAT_PATH}"
        parser = QoderStreamJSONParser()

        async with self._client_factory() as client:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                content=body_text.encode("utf-8"),
            ) as response:
                if response.status_code != 200:
                    text = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._map_http_error(response, text, default_message="Qoder 推理请求失败")

                async for raw_line in response.aiter_lines():
                    line = (raw_line or "").strip()
                    if not line or line.startswith("event:") or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        continue

                    parsed_event = parser.feed_line(line)
                    if parsed_event is None:
                        continue
                    if parsed_event.error_message:
                        raise QoderAPIError(parsed_event.error_message)
                    yield parsed_event

    def build_chat_request(
        self,
        *,
        prompt: str,
        model_info: QoderModelInfo,
        user_type: str,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        model_config = {
            "key": model_info.id,
            "display_name": model_info.display_name,
            "model": model_info.id,
            "format": "openai",
            "is_vl": False,
            "is_reasoning": model_info.is_reasoning,
            "source": "system",
            "max_input_tokens": model_info.max_input_tokens,
        }
        request_body: dict[str, Any] = {
            "stream": True,
            "source": 1,
            "taskId": "common",
            "version": "3",
            "isReply": True,
            "requestId": str(uuid.uuid4()),
            "chatTask": "FREE_INPUT",
            "chatPrompt": prompt,
            "sessionType": "chat",
            "chatContext": {},
            "agent_id": "agent_common",
            "messages": [{"role": "user", "content": prompt}],
            "userType": user_type or "personal_standard",
            "model_config": model_config,
        }
        if max_tokens:
            request_body["parameters"] = {"max_tokens": max_tokens}
        return request_body

    def build_chat_headers(
        self,
        *,
        cached_user: CachedUserInfo,
        body_text: str,
        path: str,
    ) -> dict[str, str]:
        date = str(int(time.time()))
        _, bearer_token = build_bearer_token(
            encrypt_user_info=cached_user.encrypt_user_info,
            key=cached_user.key,
            date=date,
            body=body_text,
            path=path,
            client_version=self.settings.client_version,
        )
        return {
            **self._base_headers(accept="text/event-stream"),
            "Authorization": bearer_token,
            "Cosy-User": cached_user.uid,
            "Cosy-Key": cached_user.key,
            "Cosy-Date": date,
            "Content-Type": "application/json",
        }

    def _base_headers(self, *, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    def _extract_error_message(self, text: str) -> str:
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text[:500]

        for key in ("message", "error", "error_message", "errorMessage", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return text[:500]

    def _map_http_error(
        self,
        response: httpx.Response,
        text: str,
        *,
        default_message: str,
    ) -> QoderAPIError:
        message = self._extract_error_message(text) or default_message
        if response.status_code == 400:
            return QoderAPIError(message, status_code=400, error_type="invalid_request_error")
        if response.status_code in {401, 403}:
            return QoderAPIError(message, status_code=401, error_type="authentication_error")
        if response.status_code == 408:
            return QoderAPIError(message, status_code=504, error_type="timeout_error")
        if response.status_code == 429:
            return QoderAPIError(message, status_code=429, error_type="rate_limit_error")
        return QoderAPIError(message, status_code=502, error_type="api_error")

    def _parse_token_time(self, value: str, fallback_ms: int) -> int:
        normalized = (value or "").strip()
        if normalized:
            try:
                dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return int(dt.timestamp() * 1000)
            except ValueError:
                pass
        if fallback_ms > 0:
            delta = int(fallback_ms)
            if delta < 1_000_000_000:
                delta *= 1000
            return int(time.time() * 1000) + delta
        return 0

    def _emit(self, callback: Callable[[str], None] | None, message: str) -> None:
        if callback is not None:
            callback(message)
