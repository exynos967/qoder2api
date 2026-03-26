from pathlib import Path

from qoder2api.registration import discover_qoderreg_project_path


def test_discover_qoderreg_project_path_prefers_repo_register() -> None:
    project_path = discover_qoderreg_project_path()
    expected = Path(__file__).resolve().parents[1] / "register"
    assert project_path == expected.resolve()
