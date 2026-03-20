#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from qoder_batch_device_builder import (
    BuilderError,
    CommandRunner,
    ForwardingGuard,
    NamespaceSession,
    build_output_dir,
    build_spec,
    detect_public_iface,
    parse_machine_info_output,
)
from qoder_device_login import (
    QoderError,
    QoderInfo,
    connect_client,
    ensure_socket_path,
    find_free_port,
    status_name,
    try_request,
    wait_for_file,
    wait_for_login,
    whitelist_name,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QODER = REPO_ROOT / "temp" / "Qoder"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "temp"


class LoginFlowError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在伪装的新设备环境中启动 Qoder 并直接接入正式登录流。")
    parser.add_argument("--qoder", default=str(DEFAULT_QODER), help=f"Qoder 二进制路径，默认 {DEFAULT_QODER}")
    parser.add_argument("--max-attempts", type=int, default=5, help="最多尝试多少个新设备，默认 5")
    parser.add_argument("--seed", type=int, help="随机种子，默认自动生成")
    parser.add_argument("--region", help="透传给 machine-info 的 region 参数")
    parser.add_argument("--public-iface", help="宿主机出口网卡，默认自动检测")
    parser.add_argument("--socket-port", type=int, help="Qoder websocket 端口，默认自动分配")
    parser.add_argument("--http-port", type=int, help="Qoder 本地 HTTP 端口，默认自动分配")
    parser.add_argument("--timeout", type=float, default=300.0, help="等待登录超时时间（秒），默认 300")
    parser.add_argument("--interval", type=float, default=1.0, help="轮询 auth/status 的间隔（秒），默认 1")
    parser.add_argument(
        "--require-whitelist-pass",
        action="store_true",
        help="等待条件升级为 status=LOGIN 且 whitelist=PASS",
    )
    parser.add_argument("--dump-user-plan", action="store_true", help="登录后查询一次 user/plan")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"输出根目录，默认 {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument(
        "--output-prefix",
        default="qoder_namespaced_login",
        help="输出目录前缀，默认 qoder_namespaced_login",
    )
    parser.add_argument(
        "--hostname-prefix",
        default="login-host",
        help="构造设备时使用的 hostname 前缀",
    )
    parser.add_argument("--verbose", action="store_true", help="打印底层 ip/iptables/unshare 命令")
    parser.add_argument("--json", action="store_true", help="输出最终 JSON 摘要")
    return parser.parse_args()


def ensure_root() -> None:
    if os.geteuid() != 0:
        raise LoginFlowError("该脚本需要 root 权限运行（ip netns / iptables / mount）")


def wait_for_info(info_path: Path) -> QoderInfo:
    if not wait_for_file(info_path, timeout=15):
        raise LoginFlowError("启动 Qoder 后未生成 .info.json")

    deadline = time.monotonic() + 20
    last_info: QoderInfo | None = None
    while time.monotonic() < deadline:
        last_info = QoderInfo.from_file(info_path)
        if last_info is None:
            time.sleep(0.2)
            continue
        socket_path = ensure_socket_path(last_info.ipc_server_path, Path.cwd())
        if socket_path.exists():
            return last_info
        time.sleep(0.2)
    raise LoginFlowError(f"启动后未发现可用 IPC socket: {last_info}")


def start_namespaced_qoder(
    session: NamespaceSession,
    *,
    qoder_path: Path,
    work_dir: Path,
    socket_port: int,
    http_port: int,
) -> subprocess.Popen[str]:
    inner_cmd = [
        str(qoder_path),
        "start",
        "--transportType",
        "websocket",
        "--socketPort",
        str(socket_port),
        "--httpPort",
        str(http_port),
        "--workDir",
        str(work_dir),
    ]
    stdout_handle = (work_dir / "logs" / "qoder.start.stdout.log").open("w", encoding="utf-8")
    stderr_handle = (work_dir / "logs" / "qoder.start.stderr.log").open("w", encoding="utf-8")
    return subprocess.Popen(
        session.build_unshare_command(inner_cmd),
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        start_new_session=True,
    )


def stop_process(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def print_live_hint(login_result: dict[str, Any], work_dir: Path, candidate: dict[str, Any]) -> None:
    print(f"LOGIN_URL={login_result.get('loginUrl', '')}")
    print(f"WORK_DIR={work_dir}")
    print(f"MACHINE_TOKEN={candidate['machine_info']['machineToken']}")
    print(f"MACHINE_TYPE={candidate['machine_info']['machineType']}")
    print("请现在打开 LOGIN_URL 完成登录；脚本会继续等待 auth/status 变化。")
    sys.stdout.flush()


def main() -> int:
    args = parse_args()
    ensure_root()

    qoder_path = Path(args.qoder).expanduser().resolve()
    if not qoder_path.exists():
        raise LoginFlowError(f"Qoder 不存在: {qoder_path}")

    output_root = Path(args.output_root).expanduser().resolve()
    output_dir = build_output_dir(output_root, args.output_prefix)
    fake_virtual_net_dir = output_dir / "fake_virtual_net"
    runner = CommandRunner(verbose=args.verbose)
    public_iface = args.public_iface or detect_public_iface(runner)
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)

    attempts: list[dict[str, Any]] = []
    final_output: dict[str, Any] | None = None

    with ForwardingGuard(runner):
        for attempt in range(1, args.max_attempts + 1):
            spec = build_spec(output_dir, args.hostname_prefix, rng, attempt)
            qoder_process: subprocess.Popen[str] | None = None
            info: QoderInfo | None = None
            client = None
            try:
                with NamespaceSession(
                    spec,
                    public_iface=public_iface,
                    fake_virtual_net_dir=fake_virtual_net_dir,
                    qoder_path=qoder_path,
                    region=args.region,
                    runner=runner,
                ) as session:
                    candidate = parse_machine_info_output(spec, session.run_machine_info())
                    candidate_output = {
                        "attempt": attempt,
                        "hostname": candidate.hostname,
                        "mac": candidate.mac,
                        "machine_info": {
                            "machineToken": candidate.machine_token,
                            "machineType": candidate.machine_type,
                            "classification": candidate.classification,
                            "initCode": candidate.init_code,
                            "initRegion": candidate.init_region,
                            "workDir": candidate.work_dir,
                            "stdoutLog": candidate.stdout_log,
                            "stderrLog": candidate.stderr_log,
                        },
                    }
                    attempts.append(candidate_output)
                    if not candidate.success:
                        continue

                    socket_port = args.socket_port or find_free_port()
                    http_port = args.http_port or find_free_port()
                    qoder_process = start_namespaced_qoder(
                        session,
                        qoder_path=qoder_path,
                        work_dir=spec.work_dir,
                        socket_port=socket_port,
                        http_port=http_port,
                    )
                    info = wait_for_info(spec.work_dir / ".info.json")
                    socket_path = ensure_socket_path(info.ipc_server_path, Path.cwd())
                    client, ping = connect_client(socket_path, timeout=15)
                    if not ping or ping.get("success") is not True:
                        raise LoginFlowError(f"ping 失败: {ping}")

                    initial_status = client.request("auth/status")
                    login_result = client.request("login/generateUrl")
                    print_live_hint(login_result, spec.work_dir, candidate_output)
                    final_status = wait_for_login(
                        client,
                        timeout=args.timeout,
                        interval=args.interval,
                        require_whitelist_pass=args.require_whitelist_pass,
                    )
                    user_plan = None
                    user_plan_error = None
                    if args.dump_user_plan:
                        user_plan, user_plan_error = try_request(client, "user/plan")

                    final_output = {
                        "outputDir": str(output_dir),
                        "qoder": str(qoder_path),
                        "publicIface": public_iface,
                        "seed": seed,
                        "selectedAttempt": attempt,
                        "attempts": attempts,
                        "device": candidate_output,
                        "qoderRuntime": {
                            "pid": info.pid,
                            "websocketPort": info.websocket_port,
                            "ipcSocket": str(socket_path),
                            "workDir": str(spec.work_dir),
                            "socketPortArg": socket_port,
                            "httpPortArg": http_port,
                        },
                        "login": login_result,
                        "initial_status": {
                            **initial_status,
                            "_status_name": status_name(initial_status),
                            "_whitelist_name": whitelist_name(initial_status),
                        },
                        "final_status": {
                            **final_status,
                            "_status_name": status_name(final_status),
                            "_whitelist_name": whitelist_name(final_status),
                        },
                        "user_plan": user_plan,
                        "user_plan_error": user_plan_error,
                    }
                    break
            except (BuilderError, LoginFlowError, QoderError, subprocess.CalledProcessError) as exc:
                attempts.append(
                    {
                        "attempt": attempt,
                        "error": str(exc),
                        "workDir": str(spec.work_dir),
                    }
                )
            finally:
                if client is not None:
                    client.close()
                stop_process(info.pid if info is not None else (qoder_process.pid if qoder_process else None))

            if final_output is not None:
                break

    summary_path = output_dir / "summary.json"
    payload = final_output or {
        "outputDir": str(output_dir),
        "qoder": str(qoder_path),
        "publicIface": public_iface,
        "seed": seed,
        "selectedAttempt": None,
        "attempts": attempts,
        "error": "未找到可接入登录流的可用新设备",
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"输出目录: {output_dir}")
        if final_output is None:
            print("未找到可用设备")
        else:
            print(f"已选择尝试 #{final_output['selectedAttempt']}")
            print(f"Login URL: {final_output['login']['loginUrl']}")
            print(
                "Final status: "
                f"{final_output['final_status']['_status_name']} / "
                f"whitelist={final_output['final_status']['_whitelist_name']}"
            )

    return 0 if final_output is not None else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuilderError, LoginFlowError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
