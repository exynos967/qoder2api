import asyncio
import json
from pathlib import Path

from qoder2api.auth_state import CachedUserInfo
from qoder2api.namespaced_login import NamespacedBrowserLoginClient, XieyiDeviceIdentity
from qoder2api.qoder_api import BrowserLoginContext
from qoder2api.settings import Settings


class _FakeDirectLoginClient:
    def __init__(self) -> None:
        self.completed = False

    def create_browser_login(
        self,
        qoder_home: Path,
        *,
        machine_id: str | None = None,
    ) -> BrowserLoginContext:
        resolved_machine_id = machine_id or "machine-id-demo"
        return BrowserLoginContext(
            auth_url="https://www.qoder.com/device/selectAccounts?challenge=demo",
            nonce="nonce-demo",
            verifier="verifier-demo",
            challenge="challenge-demo",
            challenge_method="S256",
            machine_id=resolved_machine_id,
            client_id="client-demo",
        )

    async def complete_browser_login(
        self,
        *,
        qoder_home: Path,
        context: BrowserLoginContext,
        progress_callback=None,
        cancel_check=None,
    ) -> CachedUserInfo:
        self.completed = True
        if progress_callback is not None:
            progress_callback(f"登录地址：{context.auth_url}")
        return CachedUserInfo(uid="uid-1", name="测试账号")


def test_namespaced_client_runs_xieyi_builder_before_browser_login(tmp_path: Path) -> None:
    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="namespaced",
    )
    direct_client = _FakeDirectLoginClient()
    output_dir = tmp_path / "xieyi-output"
    output_dir.mkdir(parents=True)
    selected = {
        "attempt": 1,
        "success": True,
        "machine_id": "6bbfd188-a196-4d35-9e7b-4f7d7c4a2d7b",
        "machine_token": "P1gAwKWCng-ZRKiFGEVRejWJ4sw2PxCq8MxDZuT4tkzVuHzHtgXiQA23TFdXXF59",
        "machine_type": "4e4a13f491a127292e",
        "hostname": "pure-host-001-demo",
        "mac": "021122334455",
        "response_path": str(output_dir / "responses" / "device_0001.json"),
    }

    def _builder_runner(_: Path) -> XieyiDeviceIdentity:
        return XieyiDeviceIdentity(
            output_dir=output_dir,
            summary={
                "output_dir": str(output_dir),
                "seed": 123,
                "count": 1,
                "success_count": 1,
                "results": [selected],
            },
            selected_result=selected,
        )

    client = NamespacedBrowserLoginClient(
        settings,
        api_client=direct_client,  # type: ignore[arg-type]
        builder_runner=_builder_runner,
    )

    context = client.create_browser_login(settings.qoder_home_path)
    assert context.work_dir == output_dir
    assert context.machine_id == selected["machine_id"]
    assert context.machine_type == selected["machine_type"]
    assert context.auth_url == "https://www.qoder.com/device/selectAccounts?challenge=demo"
    assert (settings.qoder_home_path / ".auth" / "id").read_text(encoding="utf-8") == selected["machine_id"]

    metadata = json.loads(
        (settings.qoder_home_path / ".auth" / "xieyi_device.json").read_text(encoding="utf-8")
    )
    assert metadata["workflow"] == "xieyi"
    assert metadata["machine_token"] == selected["machine_token"]
    assert metadata["machine_id"] == selected["machine_id"]

    progress: list[str] = []
    info = asyncio.run(
        client.complete_browser_login(
            qoder_home=settings.qoder_home_path,
            context=context,
            progress_callback=progress.append,
        )
    )
    assert direct_client.completed is True
    assert info.uid == "uid-1"
    assert any("xieyi 纯协议设备构建完成" in item for item in progress)
    assert any(item.startswith("MACHINE_TYPE=") for item in progress)
    assert any(item.startswith("登录地址：") for item in progress)


def test_namespaced_client_falls_back_when_xieyi_lacks_machine_id(tmp_path: Path) -> None:
    settings = Settings(
        qoder_home=str(tmp_path / ".qoder"),
        workspace=str(tmp_path),
        browser_login_backend="namespaced",
    )
    direct_client = _FakeDirectLoginClient()
    output_dir = tmp_path / "xieyi-output-fallback"
    output_dir.mkdir(parents=True)
    selected = {
        "attempt": 1,
        "success": True,
        "machine_token": "P1gA-fallback-token",
        "machine_type": "4e4a13f491a127292e",
        "hostname": "pure-host-002-demo",
        "mac": "02ffeeddccbb",
        "response_path": str(output_dir / "responses" / "device_0001.json"),
    }

    def _builder_runner(_: Path) -> XieyiDeviceIdentity:
        return XieyiDeviceIdentity(
            output_dir=output_dir,
            summary={
                "output_dir": str(output_dir),
                "seed": 456,
                "count": 1,
                "success_count": 1,
                "results": [selected],
            },
            selected_result=selected,
        )

    client = NamespacedBrowserLoginClient(
        settings,
        api_client=direct_client,  # type: ignore[arg-type]
        builder_runner=_builder_runner,
    )

    context = client.create_browser_login(settings.qoder_home_path)

    assert context.machine_id
    assert len(context.machine_id) == 36
    assert (settings.qoder_home_path / ".auth" / "id").read_text(encoding="utf-8") == context.machine_id

    metadata = json.loads(
        (settings.qoder_home_path / ".auth" / "xieyi_device.json").read_text(encoding="utf-8")
    )
    assert metadata["machine_id"] == context.machine_id
