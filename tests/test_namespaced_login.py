import asyncio
import json
from pathlib import Path

import pytest

from qoder2api.account_status import load_account_status
from qoder2api.auth_state import CachedUserInfo, load_cached_user_info
from qoder2api.namespaced_login import NamespacedBrowserLoginClient
from qoder2api.qoder_api import QoderAPIError
from qoder2api.settings import Settings


def _write_script(path: Path, lines: list[str]) -> None:
    quoted_lines = ",\n        ".join(repr(line) for line in lines)
    path.write_text(
        f"""
import sys
lines = [
        {quoted_lines}
]
for line in lines:
    print(line, flush=True)
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_namespaced_client_runs_pure_login_script_and_persists_auth(tmp_path: Path) -> None:
    machine_id = "6bbfd188-a196-4d35-9e7b-4f7d7c4a2d7b"
    machine_token = "P1gAwKWCng-ZRKiFGEVRejWJ4sw2PxCq8MxDZuT4tkzVuHzHtgXiQA23TFdXXF59"
    machine_type = "4e4a13f491a127292e"
    output_dir = tmp_path / "pure-output"
    output_dir.mkdir(parents=True)
    work_dir = output_dir / "device_0001"
    work_dir.mkdir(parents=True)
    script_path = tmp_path / "fake_pure_login.py"
    cached_user_info = CachedUserInfo(
        uid="uid-1",
        aid="uid-1",
        name="测试账号",
        access_token="access-demo",
        security_oauth_token="access-demo",
        refresh_token="refresh-demo",
        expire_time=1776599359870,
        refresh_token_expire_time=1777600000000,
        user_type="personal_professional_trial",
        email="tester@example.com",
        login_method="browser",
        user_tag="Pro Trial",
        login_device_id=machine_id,
    ).model_dump(by_alias=True)
    summary = {
        "outputDir": str(output_dir),
        "device": {
            "hostname": "pure-host-001-demo",
            "mac": "021122334455",
            "machine_info": {
                "machineToken": machine_token,
                "machineType": machine_type,
            },
        },
        "cached_user_info": cached_user_info,
        "user_status": {"userType": "personal_professional_trial", "userTag": "Pro Trial", "isQuotaExceeded": False},
        "user_plan": {"plan_tier_name": "Pro Trial", "user_type": "personal_professional_trial"},
        "data_policy": {"dataPolicy": "AGREE"},
    }
    _write_script(
        script_path,
        [
            f"LOGIN_URL=https://www.qoder.com/device/selectAccounts?challenge_method=S256&challenge=demo&nonce=nonce-demo&port=47755&state=2-nonce-demo&machine_id={machine_id}",
            f"WORK_DIR={work_dir}",
            f"MACHINE_TOKEN={machine_token}",
            f"MACHINE_TYPE={machine_type}",
            "请现在打开 LOGIN_URL 完成登录；脚本会继续等待 /auth/callback 回调。",
            json.dumps(summary, ensure_ascii=False, indent=2),
        ],
    )

    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="namespaced",
        namespaced_login_script=str(script_path),
    )
    client = NamespacedBrowserLoginClient(settings)

    context = client.create_browser_login(settings.qoder_home_path)
    assert context.machine_id == machine_id
    assert context.work_dir == work_dir.resolve()
    assert (settings.qoder_home_path / ".auth" / "id").read_text(encoding="utf-8") == machine_id

    progress: list[str] = []
    info = asyncio.run(
        client.complete_browser_login(
            qoder_home=settings.qoder_home_path,
            context=context,
            progress_callback=progress.append,
        )
    )

    stored = load_cached_user_info(settings.qoder_home_path)
    assert info.uid == "uid-1"
    assert stored is not None
    assert stored.uid == "uid-1"
    assert stored.login_device_id == machine_id
    assert any(item.startswith("LOGIN_URL=") for item in progress)

    metadata = json.loads(
        (settings.qoder_home_path / ".auth" / "xieyi_device.json").read_text(encoding="utf-8")
    )
    assert metadata["workflow"] == "pure_protocol_login"
    assert metadata["machine_id"] == machine_id
    assert metadata["machine_token"] == machine_token
    assert metadata["machine_type"] == machine_type

    status_snapshot = load_account_status(settings.qoder_home_path)
    assert status_snapshot is not None
    assert status_snapshot.plan_tier_name == "Pro Trial"
    assert status_snapshot.user_type == "personal_professional_trial"
    assert status_snapshot.is_quota_exceeded is False


def test_namespaced_client_requires_cached_user_info_from_script(tmp_path: Path) -> None:
    machine_id = "a75d6e70-2174-4cfd-a469-c437ab1d9cf7"
    script_path = tmp_path / "broken_pure_login.py"
    summary = {"outputDir": str(tmp_path / "out")}
    _write_script(
        script_path,
        [
            f"LOGIN_URL=https://www.qoder.com/device/selectAccounts?machine_id={machine_id}",
            f"WORK_DIR={tmp_path / 'out'}",
            json.dumps(summary, ensure_ascii=False, indent=2),
        ],
    )

    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="namespaced",
        namespaced_login_script=str(script_path),
    )
    client = NamespacedBrowserLoginClient(settings)
    context = client.create_browser_login(settings.qoder_home_path)

    with pytest.raises(QoderAPIError) as exc_info:
        asyncio.run(
            client.complete_browser_login(
                qoder_home=settings.qoder_home_path,
                context=context,
            )
        )

    assert "cached_user_info" in str(exc_info.value)
