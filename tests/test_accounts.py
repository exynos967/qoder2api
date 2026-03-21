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

    assert pool.pick_account().id == "acct-a"
    assert pool.pick_account().id == "acct-b"
    assert pool.pick_account().id == "acct-a"
    assert pool.pick_account(preferred_account_id="acct-b").id == "acct-b"
