from datetime import datetime, timedelta
from pathlib import Path

from qoder2api.accounts import (
    QoderAccount,
    QoderAccountPool,
    build_legacy_account,
    create_pending_managed_account,
    finalize_pending_account,
    get_account,
    get_enabled_accounts,
    list_accounts,
    mark_account_request_failure,
    mark_account_request_success,
    remove_account,
    update_account_enabled,
    update_account_label,
)
from qoder2api.settings import Settings
from tests.helpers import build_cached_user, prepare_qoder_home

from qoder2api.auth_state import save_cached_user_info, save_machine_id


def test_legacy_account_visible_before_pool_created(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))

    legacy = build_legacy_account(settings)
    assert legacy is not None
    assert legacy.id == "legacy-primary"

    accounts = list_accounts(settings)
    assert len(accounts) == 1
    assert accounts[0].id == "legacy-primary"
    assert get_enabled_accounts(settings)[0].id == "legacy-primary"


def test_create_finalize_and_remove_managed_account(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))

    pending = create_pending_managed_account(settings, "主力号")
    pending_auth_dir = pending.qoder_home_path / ".auth"
    assert pending.status == "pending_login"
    assert (pending.qoder_home_path / ".config.json").is_file()
    assert (pending_auth_dir / "models").is_file()
    assert not (pending.qoder_home_path / "bin").exists()

    save_machine_id(pending.qoder_home_path, "1234567890abcdef-managed")
    save_cached_user_info(
        pending.qoder_home_path,
        build_cached_user(uid="managed-user", name="新账号"),
    )

    finalized = finalize_pending_account(settings, pending.id)
    assert finalized.status == "ready"
    assert finalized.enabled is True
    assert finalized.machine_id == "1234567890abcdef-managed"

    accounts = list_accounts(settings)
    assert len(accounts) == 1
    assert accounts[0].label == "主力号"
    assert accounts[0].id == pending.id

    updated = update_account_label(settings, pending.id, "备用号")
    assert updated.label == "备用号"

    toggled = update_account_enabled(settings, pending.id, False)
    assert toggled.enabled is False
    assert get_enabled_accounts(settings) == []

    stored = get_account(settings, pending.id)
    assert stored is not None
    assert stored.label == "备用号"

    assert remove_account(settings, pending.id) is True
    assert list_accounts(settings) == []


def test_account_pool_pick_account_round_robin(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(qoder_home=str(tmp_path / ".qoder"), workspace=str(tmp_path))
    pool = QoderAccountPool(settings)
    account_a = QoderAccount(id="acct-a", label="账号 A", qoder_home=str(tmp_path / "acct-a"))
    account_b = QoderAccount(id="acct-b", label="账号 B", qoder_home=str(tmp_path / "acct-b"))

    monkeypatch.setattr(pool, "get_enabled_accounts", lambda: [account_a, account_b])

    assert [account.id for account in pool.pick_accounts_for_request()] == ["acct-a", "acct-b"]
    assert [account.id for account in pool.pick_accounts_for_request()] == ["acct-b", "acct-a"]
    assert pool.pick_account().id == "acct-a"
    assert pool.pick_account(preferred_account_id="acct-b").id == "acct-b"


def test_mark_account_request_failure_sets_cooldown_and_auto_disable(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    pending = create_pending_managed_account(settings, "主力号")
    save_machine_id(pending.qoder_home_path, "1234567890abcdef-managed")
    save_cached_user_info(
        pending.qoder_home_path,
        build_cached_user(uid="managed-user", name="新账号"),
    )
    account = finalize_pending_account(settings, pending.id)

    updated = mark_account_request_failure(settings, account.id, error_message="boom-1")
    assert updated.consecutive_request_failures == 1
    assert updated.cooldown_until is None
    assert updated.enabled is True

    updated = mark_account_request_failure(settings, account.id, error_message="boom-2")
    assert updated.consecutive_request_failures == 2
    assert updated.cooldown_until is not None
    assert updated.enabled is True
    assert datetime.fromisoformat(updated.cooldown_until) > datetime.now()

    updated = mark_account_request_failure(settings, account.id, error_message="boom-5")
    updated = mark_account_request_failure(settings, account.id, error_message="boom-6")
    updated = mark_account_request_failure(settings, account.id, error_message="boom-7")
    assert updated.consecutive_request_failures == 5
    assert updated.enabled is False
    assert updated.cooldown_until is None
    assert updated.last_request_error == "boom-7"


def test_mark_account_request_success_resets_failure_state(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    pending = create_pending_managed_account(settings, "主力号")
    save_machine_id(pending.qoder_home_path, "1234567890abcdef-managed")
    save_cached_user_info(
        pending.qoder_home_path,
        build_cached_user(uid="managed-user", name="新账号"),
    )
    account = finalize_pending_account(settings, pending.id)
    mark_account_request_failure(settings, account.id, error_message="boom-1")
    updated = mark_account_request_failure(settings, account.id, error_message="boom-2")
    assert updated.cooldown_until is not None

    recovered = mark_account_request_success(settings, account.id)
    assert recovered.consecutive_request_failures == 0
    assert recovered.cooldown_until is None
    assert recovered.last_request_error == ""


def test_get_enabled_accounts_skips_accounts_in_cooldown(tmp_path: Path) -> None:
    qoder_home = prepare_qoder_home(tmp_path)
    settings = Settings(qoder_home=str(qoder_home), workspace=str(tmp_path))
    pending = create_pending_managed_account(settings, "主力号")
    save_machine_id(pending.qoder_home_path, "1234567890abcdef-managed")
    save_cached_user_info(
        pending.qoder_home_path,
        build_cached_user(uid="managed-user", name="新账号"),
    )
    account = finalize_pending_account(settings, pending.id)
    cooled = mark_account_request_failure(settings, account.id, error_message="boom-1")
    cooled = mark_account_request_failure(settings, cooled.id, error_message="boom-2")

    assert get_enabled_accounts(settings) == []

    stored = get_account(settings, account.id)
    assert stored is not None
    stored.cooldown_until = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
    stored.updated_at = datetime.now().isoformat(timespec="seconds")
    update_account_label(settings, stored.id, stored.label)
