#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_SCRIPT = SCRIPT_DIR / "qoder_batch_device_builder.py"
LOGIN_SCRIPT = SCRIPT_DIR / "qoder_namespaced_login.py"
DEVICE_SCRIPT = SCRIPT_DIR / "qoder_device_login.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Qoder 设备流统一入口。"
            "build=批量造设备；login/full=从零造新设备并直接接登录流；device=保留原 qoder_device_login 能力。"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("build", "调用 qoder_batch_device_builder.py"),
        ("login", "调用 qoder_namespaced_login.py"),
        ("full", "同 login，保留更直观别名"),
        ("device", "调用 qoder_device_login.py"),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("args", nargs=argparse.REMAINDER, help="透传给底层脚本的参数")
    return parser.parse_args()


def resolve_target(command: str) -> Path:
    if command == "build":
        return BUILD_SCRIPT
    if command in {"login", "full"}:
        return LOGIN_SCRIPT
    if command == "device":
        return DEVICE_SCRIPT
    raise ValueError(f"未知命令: {command}")


def main() -> int:
    args = parse_args()
    target = resolve_target(args.command)
    if not target.exists():
        raise SystemExit(f"目标脚本不存在: {target}")

    forwarded = list(args.args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    cmd = [sys.executable, str(target), *forwarded]
    completed = subprocess.run(cmd)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
