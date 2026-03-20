from pathlib import Path

from qoder2api.settings import (
    discover_infer_endpoint,
    discover_qoder_cli_path,
    discover_qoder_home,
)


def test_discover_qoder_home_from_parent(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "qoder2api"
    qoder_home = repo_root / ".qoder"

    qoder_home.mkdir(parents=True)
    project_dir.mkdir(parents=True)

    assert discover_qoder_home(start_dir=project_dir) == qoder_home.resolve()


def test_discover_qoder_cli_path_from_versioned_dir(tmp_path: Path) -> None:
    qoder_home = tmp_path / ".qoder"
    version_dir = qoder_home / "bin" / "qodercli"
    version_dir.mkdir(parents=True)

    old_cli = version_dir / "qodercli-0.1.31"
    new_cli = version_dir / "qodercli-0.1.32"
    old_cli.write_text("old", encoding="utf-8")
    new_cli.write_text("new", encoding="utf-8")

    assert discover_qoder_cli_path(qoder_home) == new_cli.resolve()


def test_discover_infer_endpoint_from_qoder_config(tmp_path: Path) -> None:
    qoder_home = tmp_path / ".qoder"
    qoder_home.mkdir(parents=True)
    (qoder_home / ".config.json").write_text(
        '{"preferredInferenceNode":{"endpoint":"https://api3.qoder.sh"}}',
        encoding="utf-8",
    )

    assert discover_infer_endpoint(qoder_home) == "https://api3.qoder.sh"
