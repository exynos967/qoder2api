from pathlib import Path

from qoder2api.auth_state import (
    CachedUserInfo,
    inspect_local_auth,
    load_cached_user_info,
    load_machine_id,
    save_cached_user_info,
    save_machine_id,
)
from tests.helpers import build_cached_user


def test_machine_id_roundtrip(tmp_path: Path) -> None:
    qoder_home = tmp_path / ".qoder"
    save_machine_id(qoder_home, "1234567890abcdef-local")

    assert load_machine_id(qoder_home) == "1234567890abcdef-local"


def test_cached_user_roundtrip(tmp_path: Path) -> None:
    qoder_home = tmp_path / ".qoder"
    save_machine_id(qoder_home, "1234567890abcdef-roundtrip")
    info = build_cached_user(uid="roundtrip-user", name="Roundtrip")

    save_cached_user_info(qoder_home, info)
    loaded = load_cached_user_info(qoder_home)

    assert isinstance(loaded, CachedUserInfo)
    assert loaded is not None
    assert loaded.uid == "roundtrip-user"
    assert loaded.name == "Roundtrip"
    assert loaded.key
    assert loaded.encrypt_user_info


def test_inspect_local_auth_detects_logged_in_state(tmp_path: Path) -> None:
    qoder_home = tmp_path / ".qoder"
    save_machine_id(qoder_home, "1234567890abcdef-inspect")
    save_cached_user_info(qoder_home, build_cached_user(uid="inspect-user", name="Inspect"))

    auth = inspect_local_auth(qoder_home)

    assert auth["has_machine_id"] is True
    assert auth["has_user_file"] is True
    assert auth["logged_in_hint"] is True
    assert auth["uid"] == "inspect-user"
