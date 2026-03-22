from pathlib import Path

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
