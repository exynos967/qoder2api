from __future__ import annotations

import json
import time
from pathlib import Path

from qoder2api.auth_state import CachedUserInfo, save_cached_user_info, save_machine_id


def build_cached_user(
    *,
    uid: str = "user-1",
    name: str = "测试账号",
    email: str = "demo@example.com",
) -> CachedUserInfo:
    now_ms = int(time.time() * 1000)
    return CachedUserInfo(
        aid=uid,
        uid=uid,
        name=name,
        email=email,
        access_token="access-token",
        security_oauth_token="access-token",
        refresh_token="refresh-token",
        expire_time=now_ms + 3600_000,
        refresh_token_expire_time=now_ms + 7200_000,
        user_type="personal_standard",
        login_method="browser",
        data_policy_agreed=True,
        user_tag="",
    )


def prepare_qoder_home(
    base: Path,
    *,
    machine_id: str = "1234567890abcdef-demo-machine",
    uid: str = "user-1",
    name: str = "测试账号",
) -> Path:
    qoder_home = base / ".qoder"
    auth_dir = qoder_home / ".auth"
    auth_dir.mkdir(parents=True, exist_ok=True)

    save_machine_id(qoder_home, machine_id)
    save_cached_user_info(qoder_home, build_cached_user(uid=uid, name=name))

    (auth_dir / "models").write_text(
        json.dumps(
            {
                "chat": [{"key": "auto", "display_name": "Auto"}],
                "assistant": [{"key": "auto", "display_name": "Auto"}],
            }
        ),
        encoding="utf-8",
    )
    (qoder_home / ".config.json").write_text(
        json.dumps(
            {
                "preferredInferenceNode": {
                    "endpoint": "https://api3.qoder.sh",
                }
            }
        ),
        encoding="utf-8",
    )
    return qoder_home
