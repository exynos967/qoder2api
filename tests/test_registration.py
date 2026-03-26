from pathlib import Path

from qoder2api.auth_state import inspect_local_auth
from qoder2api.registration import discover_qoderreg_project_path, import_external_worker_account
from qoder2api.settings import Settings


def test_discover_qoderreg_project_path_prefers_repo_register() -> None:
    project_path = discover_qoderreg_project_path()
    expected = Path(__file__).resolve().parents[1] / "register"
    assert project_path == expected.resolve()


def test_import_external_worker_account_persists_auth_state(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(qoder_home=str(tmp_path / '.qoder'), workspace=str(tmp_path))

    account = import_external_worker_account(
        settings,
        email='demo@example.com',
        summary={
            'outputDir': str(tmp_path / 'external-output'),
            'device': {
                'hostname': 'demo-host',
                'mac': 'aa:bb:cc:dd:ee:ff',
                'machine_id': 'machine-demo-123',
                'machine_info': {
                    'machineToken': 'token-demo',
                    'machineType': 'type-demo',
                },
            },
            'cached_user_info': {
                'uid': 'uid-demo',
                'aid': 'aid-demo',
                'name': 'Demo User',
                'email': 'demo@example.com',
                'user_type': 'personal_professional_trial',
                'encrypt_user_info': 'encrypted-user-info',
                'key': 'demo-key',
                'access_token': 'access-demo',
                'security_oauth_token': 'access-demo',
                'refresh_token': 'refresh-demo',
                'expire_time': 9999999999999,
                'refresh_token_expire_time': 9999999999999,
                'data_policy_agreed': True,
            },
            'user_status': {
                'result': {
                    'uid': 'uid-demo',
                    'userType': 'personal_professional_trial',
                    'isQuotaExceeded': False,
                }
            },
            'user_plan': {
                'result': {
                    'tierName': 'Pro Trial',
                }
            },
        },
    )

    assert account.enabled is True
    assert account.status == 'ready'
    auth_state = inspect_local_auth(account.qoder_home_path)
    assert auth_state['logged_in_hint'] is True
    assert auth_state['email'] == 'demo@example.com'
    metadata_path = account.qoder_home_path / '.auth' / 'xieyi_device.json'
    assert metadata_path.is_file()
    assert 'pure_protocol_external' in metadata_path.read_text(encoding='utf-8')
