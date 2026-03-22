from pathlib import Path
import importlib.util
import subprocess
import sys

import pytest

from qoder2api.account_status import load_account_status, persist_summary_account_status
from tests.helpers import build_cached_user


def test_persist_summary_account_status(tmp_path: Path) -> None:
    qoder_home = tmp_path / ".qoder"
    cached = build_cached_user(uid="trial-user", name="试用账号", email="trial@example.com")
    cached.user_type = "personal_professional_trial"
    cached.user_tag = "Pro Trial"
    summary = {
        "outputDir": str(tmp_path / "pure-output"),
        "cached_user_info": cached.model_dump(by_alias=True, exclude_none=True),
        "user_status": {
            "result": {
                "userType": "personal_professional_trial",
                "userTag": "Pro Trial",
                "isQuotaExceeded": False,
                "whitelist": "PASS",
            }
        },
        "user_plan": {
            "result": {
                "plan_tier_name": "Pro Trial",
                "user_type": "personal_professional_trial",
            }
        },
        "data_policy": {"result": {"dataPolicy": "AGREE"}},
        "user_credit_usage": {
            "userId": "trial-user",
            "totalUsagePercentage": 0.25,
            "isQuotaExceeded": False,
            "userQuota": {
                "total": 300.0,
                "used": 75.0,
                "remaining": 225.0,
                "percentage": 0.25,
                "unit": "credits",
                "detailUrl": "https://qoder.com/account/usage",
            },
            "expiresAt": 1775025863354,
            "upgradeUrl": "https://qoder.com/pricing?client=qoder",
        },
    }

    snapshot = persist_summary_account_status(qoder_home, summary)
    stored = load_account_status(qoder_home)

    assert snapshot is not None
    assert snapshot.plan_tier_name == "Pro Trial"
    assert snapshot.user_type == "personal_professional_trial"
    assert snapshot.is_quota_exceeded is False
    assert snapshot.summary_path.endswith("summary.json")
    assert snapshot.quota_total == 300.0
    assert snapshot.quota_used == 75.0
    assert snapshot.quota_remaining == 225.0
    assert snapshot.quota_unit == "credits"
    assert stored is not None
    assert stored.plan_tier_name == "Pro Trial"
    assert stored.data_policy == "AGREE"
    assert stored.quota_total == 300.0
    assert stored.quota_detail_url == "https://qoder.com/account/usage"



def _load_vendor_protocol_module() -> object:
    module_path = Path(__file__).resolve().parents[1] / 'vendor/qoder-machine/qoder-machine/xieyi/qoder_pure_protocol.py'
    module_name = 'test_qoder_pure_protocol_vendor'
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise AssertionError('无法加载 vendor 纯协议模块')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_vendor_curl_json_request_retries_proxy_tls_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = _load_vendor_protocol_module()
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')

    calls: list[list[str]] = []

    def _fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                35,
                stdout='',
                stderr='curl: (35) TLS connect error: error:0A000126:SSL routines::unexpected eof while reading',
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"ok":true}\n__STATUS__:200',
            stderr='',
        )

    monkeypatch.setattr(protocol.subprocess, 'run', _fake_run)

    payload = protocol._curl_json_request(
        url='https://center.qoder.sh/algo/api/v2/user/plan?Encode=1',
        headers={'Accept': 'application/json'},
        method='POST',
        body='',
        timeout=20.0,
    )

    assert payload == {'ok': True}
    assert len(calls) == 2
    assert '--http2' in calls[0]
    assert '--resolve' in calls[0]
    assert '--http1.1' in calls[1]
    assert '--resolve' in calls[1]


def test_vendor_curl_json_request_retries_without_resolve_after_proxy_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = _load_vendor_protocol_module()
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')

    calls: list[list[str]] = []

    def _fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) < 3:
            return subprocess.CompletedProcess(
                command,
                56,
                stdout='',
                stderr='curl: (56) CONNECT tunnel failed, response 502',
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"ok":true}\n__STATUS__:200',
            stderr='',
        )

    monkeypatch.setattr(protocol.subprocess, 'run', _fake_run)

    payload = protocol._curl_json_request(
        url='https://center.qoder.sh/algo/api/v2/quota/usage?Encode=1',
        headers={'Accept': 'application/json'},
        method='POST',
        body='',
        timeout=20.0,
    )

    assert payload == {'ok': True}
    assert len(calls) == 3
    assert '--http2' in calls[0]
    assert '--http1.1' in calls[1]
    assert '--http1.1' in calls[2]
    assert '--resolve' in calls[1]
    assert '--resolve' not in calls[2]
