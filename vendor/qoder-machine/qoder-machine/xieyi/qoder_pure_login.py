#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from qoder_pure_batch_builder import (
    PureDeviceResult,
    build_context,
    build_machine_id,
    build_machine_type,
    build_output_dir,
    random_hostname,
    random_mac,
)
from qoder_pure_protocol import (
    build_official_poll_headers,
    build_login_url,
    build_pkce_challenge,
    build_pkce_verifier,
    decode_callback_auth_info,
    decode_callback_token_string,
    ensure_official_auth_fields,
    extract_machine_token_from_dt,
    post_rep_pc,
    request_official_data_policy,
    request_official_user_plan,
    request_official_user_status,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "temp"
DEFAULT_WEB_ENDPOINT = "https://www.qoder.com"
DEFAULT_API_ENDPOINT = "https://openapi.qoder.sh"
OFFICIAL_LOGIN_CLIENT_IP = "10.20.1.2"
OFFICIAL_CENTER_HOST = "center.qoder.sh"
OFFICIAL_CENTER_IP = "8.212.124.35"


class PureLoginError(RuntimeError):
    pass


POLL_HOSTS = ("https://center.qoder.sh",)
POLL_PATHS = ("/algo/api/v3/user/oauth2/deviceToken/poll",)
POLL_PARAM_VARIANTS = (("codeVerifier", "codeChallengeMethod"),)


@dataclass(slots=True)
class CallbackCapture:
    path: str
    params: dict[str, str]
    auth_info: dict[str, str]
    token_bundle: dict[str, Any]


@dataclass(slots=True)
class CallbackState:
    expected_nonce: str
    result: CallbackCapture | None = None
    error: str | None = None
    event: threading.Event = threading.Event()

    def __post_init__(self) -> None:
        self.event = threading.Event()


@dataclass(slots=True)
class PollCapture:
    endpoint: str
    params: dict[str, str]
    raw_payload: dict[str, Any]
    auth_info: dict[str, str]
    token_bundle: dict[str, Any]


@dataclass(slots=True)
class PollState:
    result: PollCapture | None = None
    error: str | None = None
    event: threading.Event = threading.Event()
    last_status: str | None = None

    def __post_init__(self) -> None:
        self.event = threading.Event()


def _append_debug_line(path: Path | None, message: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="纯协议生成新设备并完成官方网页登录回调。")
    parser.add_argument("--count", type=int, default=1, help="本次成功设备数量，当前建议保持 1")
    parser.add_argument("--max-attempts", type=int, default=5, help="最多尝试多少个新设备，默认 5")
    parser.add_argument("--seed", type=int, help="随机种子，默认使用当前时间戳")
    parser.add_argument("--region-id", type=int, default=0, help="repPc 区域 ID，默认 0")
    parser.add_argument("--hostname-prefix", default="pure-login-host", help="hostname 前缀")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"输出根目录，默认 {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument("--output-prefix", default="qoder_pure_login", help="输出目录前缀")
    parser.add_argument("--persisted-min-ms", type=int, default=60_000, help="持久时间戳最小回退毫秒数")
    parser.add_argument("--persisted-max-ms", type=int, default=3_600_000, help="持久时间戳最大回退毫秒数")
    parser.add_argument("--timeout", type=float, default=300.0, help="等待网页登录回调超时秒数")
    parser.add_argument("--http-port", type=int, help="本地回调端口，默认自动分配")
    parser.add_argument("--web-endpoint", default=DEFAULT_WEB_ENDPOINT, help="网页登录站点，默认 https://www.qoder.com")
    parser.add_argument("--api-endpoint", default=DEFAULT_API_ENDPOINT, help="OpenAPI 站点，默认 https://openapi.qoder.sh")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="后台纯协议轮询间隔秒数，默认 1")
    parser.add_argument(
        "--post-login-timeout",
        type=float,
        default=180.0,
        help="网页登录成功后继续轮询 user/status 与 user/plan 的稳定时间，默认 180 秒",
    )
    parser.add_argument(
        "--post-login-interval",
        type=float,
        default=3.0,
        help="网页登录成功后轮询 user/status 与 user/plan 的间隔秒数，默认 3",
    )
    parser.add_argument(
        "--client-ip",
        default=OFFICIAL_LOGIN_CLIENT_IP,
        help=f"轮询时使用的 Cosy-ClientIp，默认 {OFFICIAL_LOGIN_CLIENT_IP}",
    )
    parser.add_argument("--json", action="store_true", help="最后输出 JSON 摘要")
    return parser.parse_args()


def _pick_free_port() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        return int(server.server_address[1])
    finally:
        server.server_close()


def _normalize_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _lookup_value(payload: dict[str, Any], *names: str) -> Any:
    normalized = {_normalize_key(key): value for key, value in payload.items()}
    for name in names:
        key = _normalize_key(name)
        if key in normalized:
            return normalized[key]
    return None


def _pick_payload_object(payload: dict[str, Any]) -> dict[str, Any]:
    current = payload
    visited: set[int] = set()
    while isinstance(current, dict) and id(current) not in visited:
        visited.add(id(current))
        for key in ("data", "result", "payload"):
            value = current.get(key)
            if isinstance(value, dict):
                current = value
                break
        else:
            return current
    return payload


def _pick_result_mapping(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    current = payload
    visited: set[int] = set()
    while isinstance(current, dict) and id(current) not in visited:
        visited.add(id(current))
        for key in ("result", "data", "payload"):
            value = current.get(key)
            if isinstance(value, dict):
                current = value
                break
        else:
            return current
    return {}


def _pick_result_value(payload: dict[str, Any] | None, *names: str, default: Any = None) -> Any:
    current = _pick_result_mapping(payload)
    if not current:
        return default
    normalized = {_normalize_key(key): value for key, value in current.items()}
    for name in names:
        key = _normalize_key(name)
        if key in normalized:
            return normalized[key]
    return default


def _parse_poll_response(payload: dict[str, Any], nonce: str) -> PollCapture:
    data = _pick_payload_object(payload)
    token = str(_lookup_value(data, "token", "accessToken", "deviceToken") or "").strip()
    refresh_token = str(_lookup_value(data, "refreshToken", "refresh_token") or "").strip()
    user_id = str(_lookup_value(data, "userId", "uid", "id", "user_id") or "").strip()
    username = str(_lookup_value(data, "username", "userName", "name") or "").strip()
    expires_in_raw = _lookup_value(data, "expiresIn", "expireIn", "expires_in")
    expires_at_raw = _lookup_value(data, "expiresAt", "expireTime", "expireAt", "expires_at")
    if not token:
        raise PureLoginError(f"轮询成功但响应缺少 token: {payload}")
    if not user_id:
        raise PureLoginError(f"轮询成功但响应缺少 userId: {payload}")
    expire_raw_value = 0
    if expires_in_raw not in (None, ""):
        expire_raw_value = int(str(expires_in_raw))
    elif expires_at_raw not in (None, ""):
        expire_raw_value = int(str(expires_at_raw))
    token_bundle = {
        "access_token": token,
        "refresh_token": refresh_token,
        "raw_expire_value": expire_raw_value,
        "expire_time": int(expire_raw_value) if int(expire_raw_value) >= 1_000_000_000_000 else 0,
    }
    if token_bundle["expire_time"] == 0:
        from qoder_pure_protocol import CallbackTokenBundle  # 局部导入，复用既有归一化逻辑

        token_bundle["expire_time"] = CallbackTokenBundle(
            access_token=token,
            refresh_token=refresh_token,
            raw_expire_value=expire_raw_value,
        ).normalized_expire_time_ms()
    return PollCapture(
        endpoint="",
        params={"nonce": nonce},
        raw_payload=payload,
        auth_info={
            "uid": user_id,
            "aid": user_id,
            "name": username or user_id,
            "orgId": str(_lookup_value(data, "orgId", "organizationId", "organization_id") or ""),
        },
        token_bundle=token_bundle,
    )


def _poll_once(
    *,
    host: str,
    path: str,
    nonce: str,
    verifier: str,
    machine_id: str,
    machine_token: str,
    machine_type: str,
    client_ip: str,
    param_names: tuple[str, str],
    timeout: float = 15.0,
) -> tuple[int, str, dict[str, Any] | None]:
    verifier_key, challenge_key = param_names
    query = urllib.parse.urlencode(
        [
            (challenge_key, "S256"),
            (verifier_key, verifier),
            ("nonce", nonce),
        ]
    )
    url = f"{host.rstrip('/')}{path}?{query}"
    headers = build_official_poll_headers(
        machine_id=machine_id,
        machine_token="",
        machine_type="",
        client_ip=client_ip,
    )
    command = [
        "curl",
        "--http2",
        "--silent",
        "--show-error",
        "--max-time",
        str(int(max(timeout, 1.0))),
        "--write-out",
        "\n__STATUS__:%{http_code}",
    ]
    parsed_host = urllib.parse.urlsplit(host).hostname
    if parsed_host == OFFICIAL_CENTER_HOST:
        command.extend(["--resolve", f"{OFFICIAL_CENTER_HOST}:443:{OFFICIAL_CENTER_IP}"])
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    command.append(url)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        return 0, str(exc), None
    if completed.returncode != 0:
        return 0, completed.stderr.strip() or completed.stdout.strip(), None
    stdout = completed.stdout
    marker = "\n__STATUS__:"
    if marker not in stdout:
        return 0, stdout.strip(), None
    body, status_text = stdout.rsplit(marker, 1)
    body = body.strip()
    try:
        status_code = int(status_text.strip())
    except ValueError:
        return 0, stdout.strip(), None
    payload = None
    if body:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
    return status_code, body, payload


def _poll_login_token(
    *,
    nonce: str,
    verifier: str,
    machine_id: str,
    machine_token: str,
    machine_type: str,
    client_ip: str,
    timeout: float,
    interval: float,
    state: PollState,
    log_path: Path | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    candidates = [
        (host, path, params)
        for host in POLL_HOSTS
        for path in POLL_PATHS
        for params in POLL_PARAM_VARIANTS
    ]
    while time.monotonic() < deadline and not state.event.is_set():
        for host, path, params in candidates:
            status_code, body, payload = _poll_once(
                host=host,
                path=path,
                nonce=nonce,
                verifier=verifier,
                machine_id=machine_id,
                machine_token=machine_token,
                machine_type=machine_type,
                client_ip=client_ip,
                param_names=params,
            )
            state.last_status = f"{host}{path} -> {status_code}"
            _append_debug_line(log_path, f"{host}{path} status={status_code} body={body[:240]}")
            if status_code == 0:
                state.last_status = f"{host}{path} -> network_error {body[:120]}"
                continue
            if status_code == HTTPStatus.NOT_FOUND:
                continue
            if status_code != HTTPStatus.OK:
                continue
            if not isinstance(payload, dict):
                continue
            try:
                capture = _parse_poll_response(payload, nonce)
            except PureLoginError:
                if payload == {}:
                    continue
                state.last_status = f"{host}{path} -> 200 {body[:120]}"
                continue
            capture.endpoint = f"{host}{path}"
            capture.params = {
                "nonce": nonce,
                params[0]: verifier,
                params[1]: "S256",
            }
            state.result = capture
            state.event.set()
            _append_debug_line(log_path, f"poll success endpoint={capture.endpoint} user={capture.auth_info.get('uid','')}")
            return
        time.sleep(interval)
    if not state.event.is_set():
        state.error = f"轮询 device token 超时，最后状态：{state.last_status or 'none'}"
        _append_debug_line(log_path, state.error)
        state.event.set()


def _build_cached_user_info(
    *,
    machine_id: str,
    callback: CallbackCapture,
    userinfo: dict[str, Any] | None,
    user_status: dict[str, Any] | None,
    user_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    auth_info = callback.auth_info
    token_bundle = callback.token_bundle
    name = (
        str((userinfo or {}).get("name") or "")
        or auth_info.get("name", "")
        or auth_info.get("uid", "")
    )
    email = str((userinfo or {}).get("email") or (user_status or {}).get("email") or "")
    avatar_url = str((userinfo or {}).get("avatar") or (user_status or {}).get("avatarUrl") or "")
    user_type = str(
        (user_status or {}).get("userType")
        or (user_plan or {}).get("user_type")
        or "personal_standard"
    )
    user_tag = str(
        (user_status or {}).get("userTag")
        or (user_plan or {}).get("plan_tier_name")
        or ""
    )
    now_ms = int(time.time() * 1000)
    return {
        "aid": auth_info.get("aid", ""),
        "uid": auth_info.get("uid", ""),
        "name": name,
        "yx_uid": "",
        "organization_id": auth_info.get("orgId", ""),
        "organization_name": "",
        "staffId": "",
        "avatar_url": avatar_url,
        "encrypt_user_info": "",
        "key": "",
        "access_token": token_bundle["access_token"],
        "security_oauth_token": token_bundle["access_token"],
        "refresh_token": token_bundle["refresh_token"],
        "expire_time": int(token_bundle["expire_time"]),
        "refresh_token_expire_time": 0,
        "user_source_channel": "common",
        "user_type": user_type,
        "data_policy_agreed": True,
        "personal_access_token": "",
        "login_method": "browser",
        "email": email,
        "user_tag": user_tag,
        "is_data_policy_modifiable": bool((user_status or {}).get("isPrivacyPolicyModifiable", False)),
        "login_device_id": machine_id,
        "login_timestamp": now_ms,
        "organization_tags": None,
    }


def _apply_data_policy_result(cached_user_info: dict[str, Any], data_policy: dict[str, Any] | None) -> None:
    status = str(_pick_result_value(data_policy, "status", default="")).upper()
    if status:
        cached_user_info["data_policy_agreed"] = status == "AGREE"
    modifiable = _pick_result_value(
        data_policy,
        "isModifiable",
        "modifiable",
        "canModify",
        "isPrivacyPolicyModifiable",
        default=None,
    )
    if isinstance(modifiable, bool):
        cached_user_info["is_data_policy_modifiable"] = modifiable


def _apply_user_status_result(cached_user_info: dict[str, Any], user_status: dict[str, Any] | None) -> None:
    if not user_status:
        return
    name = user_status.get("name")
    if name:
        cached_user_info["name"] = str(name)
    email = user_status.get("email")
    if email:
        cached_user_info["email"] = str(email)
    avatar_url = user_status.get("avatarUrl")
    if avatar_url:
        cached_user_info["avatar_url"] = str(avatar_url)
    user_type = user_status.get("userType")
    if user_type:
        cached_user_info["user_type"] = str(user_type)
    user_tag = user_status.get("userTag")
    if user_tag:
        cached_user_info["user_tag"] = str(user_tag)
    privacy_modifiable = user_status.get("isPrivacyPolicyModifiable")
    if isinstance(privacy_modifiable, bool):
        cached_user_info["is_data_policy_modifiable"] = privacy_modifiable


def _apply_user_plan_result(cached_user_info: dict[str, Any], user_plan: dict[str, Any] | None) -> None:
    user_type = _pick_result_value(user_plan, "user_type", "userType", default="")
    if user_type:
        cached_user_info["user_type"] = str(user_type)
    plan_tier_name = _pick_result_value(user_plan, "plan_tier_name", "planTierName", default="")
    if plan_tier_name:
        cached_user_info["user_tag"] = str(plan_tier_name)
    organization_id = _pick_result_value(user_plan, "organization_id", "organizationId", default="")
    if organization_id:
        cached_user_info["organization_id"] = str(organization_id)
    organization_name = _pick_result_value(user_plan, "organization_name", "organizationName", default="")
    if organization_name:
        cached_user_info["organization_name"] = str(organization_name)


def _current_state_marker(
    user_status: dict[str, Any] | None,
    user_plan: dict[str, Any] | None,
) -> tuple[str, str, bool | None]:
    user_type = str(_pick_result_value(user_status, "userType", "user_type", default="") or "")
    if not user_type:
        user_type = str(_pick_result_value(user_plan, "user_type", "userType", default="") or "")
    plan_tier_name = str(
        _pick_result_value(user_plan, "plan_tier_name", "planTierName", default="")
        or _pick_result_value(user_status, "userTag", "plan_tier_name", default="")
        or ""
    )
    quota_exceeded = _pick_result_value(
        user_status,
        "isQuotaExceeded",
        "is_quota_exceeded",
        default=None,
    )
    return user_type, plan_tier_name, quota_exceeded if isinstance(quota_exceeded, bool) else None


def _state_has_trial(
    user_status: dict[str, Any] | None,
    user_plan: dict[str, Any] | None,
) -> bool:
    user_type, plan_tier_name, _ = _current_state_marker(user_status, user_plan)
    user_type_lower = user_type.lower()
    plan_lower = plan_tier_name.lower()
    if "trial" in user_type_lower or "trial" in plan_lower:
        return True
    return bool(plan_lower and plan_lower not in {"free"})


def _query_post_login_state(
    *,
    cached_user_info: dict[str, Any],
    machine_id: str,
    machine_token: str,
    machine_type: str,
    auth_info: dict[str, str],
    client_ip: str,
    timeout: float,
    interval: float,
    log_path: Path | None = None,
) -> tuple[
    dict[str, Any] | None,
    str | None,
    dict[str, Any] | None,
    str | None,
    dict[str, Any] | None,
    str | None,
    dict[str, Any],
    list[dict[str, Any]],
]:
    deadline = time.monotonic() + max(timeout, 0.0)
    history: list[dict[str, Any]] = []
    user_status = None
    user_status_error = None
    data_policy = None
    data_policy_error = None
    user_plan = None
    user_plan_error = None
    attempt = 0
    while True:
        attempt += 1
        cached_user_info = ensure_official_auth_fields(cached_user_info, force_refresh=True)
        try:
            user_status = request_official_user_status(
                cached_user_info=cached_user_info,
                machine_id=machine_id,
                machine_token=machine_token,
                machine_type=machine_type,
                auth_info=auth_info,
                client_ip=client_ip,
            )
            user_status_error = None
            _apply_user_status_result(cached_user_info, user_status)
        except Exception as exc:
            user_status_error = str(exc)

        cached_user_info = ensure_official_auth_fields(cached_user_info, force_refresh=True)
        try:
            data_policy = request_official_data_policy(
                cached_user_info=cached_user_info,
                machine_id=machine_id,
                machine_token=machine_token,
                machine_type=machine_type,
                client_ip=client_ip,
            )
            data_policy_error = None
            _apply_data_policy_result(cached_user_info, data_policy)
        except Exception as exc:
            data_policy_error = str(exc)

        cached_user_info = ensure_official_auth_fields(cached_user_info, force_refresh=True)
        try:
            user_plan = request_official_user_plan(
                cached_user_info=cached_user_info,
                machine_id=machine_id,
                machine_token=machine_token,
                machine_type=machine_type,
                client_ip=client_ip,
            )
            user_plan_error = None
            _apply_user_plan_result(cached_user_info, user_plan)
        except Exception as exc:
            user_plan_error = str(exc)

        cached_user_info = ensure_official_auth_fields(cached_user_info, force_refresh=True)
        user_type, plan_tier_name, quota_exceeded = _current_state_marker(user_status, user_plan)
        snapshot = {
            "attempt": attempt,
            "user_type": user_type,
            "plan_tier_name": plan_tier_name,
            "is_quota_exceeded": quota_exceeded,
            "user_status_error": user_status_error,
            "data_policy_error": data_policy_error,
            "user_plan_error": user_plan_error,
            "timestamp_ms": int(time.time() * 1000),
        }
        history.append(snapshot)
        _append_debug_line(
            log_path,
            (
                "post-login"
                f" attempt={attempt}"
                f" user_type={user_type or '-'}"
                f" plan={plan_tier_name or '-'}"
                f" quota_exceeded={quota_exceeded!r}"
                f" status_err={user_status_error or '-'}"
                f" plan_err={user_plan_error or '-'}"
            ),
        )
        if _state_has_trial(user_status, user_plan):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    return (
        user_status,
        user_status_error,
        data_policy,
        data_policy_error,
        user_plan,
        user_plan_error,
        cached_user_info,
        history,
    )


def _build_attempt_output(device: PureDeviceResult) -> dict[str, Any]:
    return {
        "attempt": device.attempt,
        "hostname": device.hostname,
        "mac": device.mac,
        "machine_id": device.machine_id,
        "machine_info": {
            "machineToken": device.machine_token,
            "machineType": device.machine_type,
            "classification": "server_backed_or_real_umid" if device.success else "unknown",
            "initCode": 0 if device.success else -1,
            "initRegion": 0,
            "workDir": "",
            "stdoutLog": "",
            "stderrLog": "",
        },
    }


def _build_device(
    output_dir: Path,
    *,
    attempt: int,
    hostname_prefix: str,
    region_id: int,
    rng: random.Random,
    persisted_min_ms: int,
    persisted_max_ms: int,
) -> PureDeviceResult:
    mac = random_mac(rng).replace(":", "")
    hostname = random_hostname(hostname_prefix, rng, attempt)
    machine_id = build_machine_id(rng)
    now_ms = int(time.time() * 1000)
    persisted_timestamp_ms = now_ms - rng.randint(persisted_min_ms, persisted_max_ms)
    response_path = output_dir / "responses" / f"device_{attempt:04d}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    context = build_context(region_id, hostname, mac, persisted_timestamp_ms)
    payload = post_rep_pc(
        context,
        now_ms=now_ms,
        random_seed=rng.getrandbits(32),
        key_index=rng.randrange(10),
        timeout=30.0,
    )
    response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    ec = int(payload.get("ec", -1))
    if ec != 200 or "dt" not in payload:
        return PureDeviceResult(
            attempt=attempt,
            ec=ec,
            success=False,
            machine_id=machine_id,
            machine_token="",
            machine_type="",
            mac=mac,
            hostname=hostname,
            persisted_timestamp_ms=persisted_timestamp_ms,
            now_ms=now_ms,
            response_path=str(response_path),
            error=f"repPc ec={ec}",
        )
    machine_token = extract_machine_token_from_dt(str(payload["dt"]))
    return PureDeviceResult(
        attempt=attempt,
        ec=ec,
        success=True,
        machine_id=machine_id,
        machine_token=machine_token,
        machine_type=build_machine_type(machine_token, 91),
        mac=mac,
        hostname=hostname,
        persisted_timestamp_ms=persisted_timestamp_ms,
        now_ms=now_ms,
        response_path=str(response_path),
    )


def _callback_handler(state: CallbackState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if parsed.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")
                return
            try:
                params = {
                    key: values[-1]
                    for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()
                    if values
                }
                nonce = params.get("nonce", "").strip()
                if not nonce:
                    raise PureLoginError("回调缺少 nonce")
                if nonce != state.expected_nonce:
                    raise PureLoginError("回调 nonce 与登录请求不匹配")
                auth_raw = params.get("auth", "").strip()
                token_raw = params.get("tokenString", "").strip()
                if not auth_raw or not token_raw:
                    raise PureLoginError("回调缺少 auth 或 tokenString")
                auth_info = decode_callback_auth_info(auth_raw)
                token_bundle = decode_callback_token_string(token_raw)
                state.result = CallbackCapture(
                    path=parsed.path,
                    params=params,
                    auth_info=auth_info,
                    token_bundle={
                        "access_token": token_bundle.access_token,
                        "refresh_token": token_bundle.refresh_token,
                        "raw_expire_value": token_bundle.raw_expire_value,
                        "expire_time": token_bundle.normalized_expire_time_ms(),
                    },
                )
                state.event.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body><h3>Qoder 登录成功</h3><p>可以返回客户端。</p></body></html>".encode("utf-8")
                )
            except Exception as exc:  # pragma: no cover - 真实网页登录异常分支
                state.error = str(exc)
                state.event.set()
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    f"<html><body><h3>Qoder 登录失败</h3><pre>{exc}</pre></body></html>".encode("utf-8")
                )

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    return Handler


def _start_callback_server(port: int, nonce: str) -> tuple[ThreadingHTTPServer, CallbackState, threading.Thread]:
    state = CallbackState(expected_nonce=nonce)
    server = ThreadingHTTPServer(("127.0.0.1", port), _callback_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state, thread


def _print_login_hint(login_url: str, device: PureDeviceResult, work_dir: Path) -> None:
    print(f"LOGIN_URL={login_url}")
    print(f"WORK_DIR={work_dir}")
    print(f"MACHINE_TOKEN={device.machine_token}")
    print(f"MACHINE_TYPE={device.machine_type}")
    print(f"LOGIN_MACHINE_ID={device.machine_id}")
    print("请现在打开 LOGIN_URL 完成登录；脚本会继续等待 /auth/callback 回调。")


def main() -> int:
    args = parse_args()
    if args.count != 1:
        raise PureLoginError("纯协议网页登录当前只支持 --count 1")

    output_root = Path(args.output_root).expanduser().resolve()
    output_dir = build_output_dir(output_root, args.output_prefix)
    work_dir = output_dir / "device_0001"
    work_dir.mkdir(parents=True, exist_ok=True)
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)

    attempts: list[dict[str, Any]] = []
    selected: PureDeviceResult | None = None
    for attempt in range(1, args.max_attempts + 1):
        device = _build_device(
            output_dir,
            attempt=attempt,
            hostname_prefix=args.hostname_prefix,
            region_id=args.region_id,
            rng=rng,
            persisted_min_ms=args.persisted_min_ms,
            persisted_max_ms=args.persisted_max_ms,
        )
        attempts.append(_build_attempt_output(device))
        if device.success:
            selected = device
            break
    if selected is None:
        raise PureLoginError("未能生成可用的新设备身份")

    nonce = uuid.uuid4().hex
    verifier = build_pkce_verifier()
    challenge = build_pkce_challenge(verifier)
    port = args.http_port or _pick_free_port()
    login_url = build_login_url(
        machine_id=selected.machine_id,
        nonce=nonce,
        challenge=challenge,
        port=port,
        web_endpoint=args.web_endpoint,
    )
    live_login_path = output_dir / "live_login.json"
    live_login_path.write_text(
        json.dumps(
            {
                "loginUrl": login_url,
                "nonce": nonce,
                "verifier": verifier,
                "challenge": challenge,
                "challengeMethod": "S256",
                "clientIp": args.client_ip,
                "machineId": selected.machine_id,
                "machineToken": selected.machine_token,
                "machineType": selected.machine_type,
                "port": port,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    poll_log_path = output_dir / "poll.log"

    server, callback_state, thread = _start_callback_server(port, nonce)
    poll_state = PollState()
    poll_thread = threading.Thread(
        target=_poll_login_token,
        kwargs={
            "nonce": nonce,
            "verifier": verifier,
            "machine_id": selected.machine_id,
            "machine_token": selected.machine_token,
            "machine_type": selected.machine_type,
            "client_ip": args.client_ip,
            "timeout": args.timeout,
            "interval": args.poll_interval,
            "state": poll_state,
            "log_path": poll_log_path,
        },
        daemon=True,
    )
    poll_thread.start()
    try:
        _print_login_hint(login_url, selected, work_dir)
        sys.stdout.flush()

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if callback_state.event.is_set() or poll_state.event.is_set():
                break
            time.sleep(0.1)
        else:
            raise PureLoginError(f"等待网页登录回调超时（>{args.timeout} 秒）")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        poll_thread.join(timeout=2)

    if callback_state.error:
        raise PureLoginError(callback_state.error)
    callback_source = "callback"
    callback = callback_state.result
    poll_capture = poll_state.result
    if callback is None and poll_capture is None:
        raise PureLoginError(
            poll_state.error
            or "登录流程结束，但既未捕获有效回调，也未轮询到 device token"
        )
    if callback is None and poll_capture is not None:
        callback_source = "poll"
        auth_info = poll_capture.auth_info
        token_bundle = poll_capture.token_bundle
    else:
        assert callback is not None
        auth_info = callback.auth_info
        token_bundle = callback.token_bundle
    userinfo = None
    userinfo_error = "纯协议 userinfo 尚未实现"
    post_login_log_path = output_dir / "post_login.log"

    cached_user_info = _build_cached_user_info(
        machine_id=selected.machine_id,
        callback=CallbackCapture(
            path=callback.path if callback is not None else "/deviceToken/poll",
            params=callback.params if callback is not None else (poll_capture.params if poll_capture else {}),
            auth_info=auth_info,
            token_bundle=token_bundle,
        ),
        userinfo=None,
        user_status=None,
        user_plan=None,
    )
    (
        user_status,
        user_status_error,
        data_policy,
        data_policy_error,
        user_plan,
        user_plan_error,
        cached_user_info,
        post_login_attempts,
    ) = _query_post_login_state(
        cached_user_info=cached_user_info,
        machine_id=selected.machine_id,
        machine_token=selected.machine_token,
        machine_type=selected.machine_type,
        auth_info=auth_info,
        client_ip=args.client_ip,
        timeout=args.post_login_timeout,
        interval=args.post_login_interval,
        log_path=post_login_log_path,
    )
    summary = {
        "outputDir": str(output_dir),
        "seed": seed,
        "selectedAttempt": selected.attempt,
        "attempts": attempts,
        "device": _build_attempt_output(selected),
        "login": {
            "loginUrl": login_url,
            "nonce": nonce,
            "verifier": verifier,
            "challenge": challenge,
            "challengeMethod": "S256",
            "clientIp": args.client_ip,
            "success": True,
            "httpPortArg": port,
        },
        "callback": {
            "source": callback_source,
            "path": callback.path if callback is not None else "",
            "nonce": callback.params.get("nonce", "") if callback is not None else nonce,
            "authLength": len(callback.params.get("auth", "")) if callback is not None else 0,
            "tokenStringLength": len(callback.params.get("tokenString", "")) if callback is not None else 0,
            "decodedAuth": auth_info,
            "decodedToken": token_bundle,
            "poll": None
            if poll_capture is None
            else {
                "endpoint": poll_capture.endpoint,
                "params": poll_capture.params,
                "rawPayload": poll_capture.raw_payload,
            },
        },
        "userinfo": userinfo,
        "userinfo_error": userinfo_error,
        "user_status": user_status,
        "user_status_error": user_status_error,
        "data_policy": data_policy,
        "data_policy_error": data_policy_error,
        "user_plan": user_plan,
        "user_plan_error": user_plan_error,
        "post_login_attempts": post_login_attempts,
        "cached_user_info": cached_user_info,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
