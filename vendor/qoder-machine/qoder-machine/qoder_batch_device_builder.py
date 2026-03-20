#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import secrets
import string
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from qoder_device_login import analyze_machine_type


INIT_REGION_RE = re.compile(r"Initialized region (\d+) code (\d+)")
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QODER = REPO_ROOT / "temp" / "Qoder"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "temp"
TOKEN_LEN_REAL = 88


class BuilderError(RuntimeError):
    pass


@dataclass(slots=True)
class DeviceSpec:
    attempt: int
    namespace: str
    host_if: str
    ns_if: str
    subnet: str
    host_ip: str
    ns_ip: str
    mac: str
    hostname: str
    work_dir: Path
    log_dir: Path


@dataclass(slots=True)
class DeviceResult:
    attempt: int
    success: bool
    classification: str
    init_region: int | None
    init_code: int | None
    machine_token: str
    machine_type: str
    machine_id: str
    mac: str
    hostname: str
    work_dir: str
    stdout_log: str
    stderr_log: str
    elapsed_seconds: float
    error: str | None = None


class CommandRunner:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose

    def run(self, cmd: list[str], *, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
        if self.verbose:
            print("+", " ".join(cmd), file=sys.stderr)
        return subprocess.run(
            cmd,
            check=check,
            text=True,
            capture_output=capture_output,
        )


class ForwardingGuard:
    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner
        self.original_value: str | None = None
        self.changed = False

    def __enter__(self) -> "ForwardingGuard":
        result = self.runner.run(["sysctl", "-n", "net.ipv4.ip_forward"])
        self.original_value = result.stdout.strip()
        if self.original_value != "1":
            self.runner.run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
            self.changed = True
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.changed and self.original_value is not None:
            self.runner.run(
                ["sysctl", "-w", f"net.ipv4.ip_forward={self.original_value}"],
                check=False,
            )


class NamespaceSession:
    def __init__(
        self,
        spec: DeviceSpec,
        *,
        public_iface: str,
        fake_virtual_net_dir: Path,
        qoder_path: Path,
        region: str | None,
        runner: CommandRunner,
    ) -> None:
        self.spec = spec
        self.public_iface = public_iface
        self.fake_virtual_net_dir = fake_virtual_net_dir
        self.qoder_path = qoder_path
        self.region = region
        self.runner = runner

    def __enter__(self) -> "NamespaceSession":
        self.spec.work_dir.mkdir(parents=True, exist_ok=True)
        self.spec.log_dir.mkdir(parents=True, exist_ok=True)
        self.fake_virtual_net_dir.mkdir(parents=True, exist_ok=True)

        self.runner.run(["ip", "netns", "add", self.spec.namespace])
        self.runner.run(
            [
                "ip",
                "link",
                "add",
                self.spec.host_if,
                "type",
                "veth",
                "peer",
                "name",
                self.spec.ns_if,
            ]
        )
        self.runner.run(["ip", "link", "set", self.spec.ns_if, "netns", self.spec.namespace])
        self.runner.run(["ip", "addr", "add", f"{self.spec.host_ip}/24", "dev", self.spec.host_if])
        self.runner.run(["ip", "link", "set", self.spec.host_if, "up"])
        self.runner.run(["ip", "netns", "exec", self.spec.namespace, "ip", "link", "set", "lo", "up"])
        self.runner.run(
            ["ip", "netns", "exec", self.spec.namespace, "ip", "link", "set", "dev", self.spec.ns_if, "address", self.spec.mac]
        )
        self.runner.run(
            ["ip", "netns", "exec", self.spec.namespace, "ip", "addr", "add", f"{self.spec.ns_ip}/24", "dev", self.spec.ns_if]
        )
        self.runner.run(["ip", "netns", "exec", self.spec.namespace, "ip", "link", "set", self.spec.ns_if, "up"])
        self.runner.run(
            [
                "ip",
                "netns",
                "exec",
                self.spec.namespace,
                "ip",
                "route",
                "add",
                "default",
                "via",
                self.spec.host_ip,
            ]
        )
        self.runner.run(["iptables", "-A", "FORWARD", "-i", self.spec.host_if, "-o", self.public_iface, "-j", "ACCEPT"])
        self.runner.run(
            [
                "iptables",
                "-A",
                "FORWARD",
                "-i",
                self.public_iface,
                "-o",
                self.spec.host_if,
                "-m",
                "state",
                "--state",
                "RELATED,ESTABLISHED",
                "-j",
                "ACCEPT",
            ]
        )
        self.runner.run(
            [
                "iptables",
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-s",
                f"{self.spec.subnet}/24",
                "-o",
                self.public_iface,
                "-j",
                "MASQUERADE",
            ]
        )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.runner.run(
            [
                "iptables",
                "-t",
                "nat",
                "-D",
                "POSTROUTING",
                "-s",
                f"{self.spec.subnet}/24",
                "-o",
                self.public_iface,
                "-j",
                "MASQUERADE",
            ],
            check=False,
        )
        self.runner.run(
            [
                "iptables",
                "-D",
                "FORWARD",
                "-i",
                self.public_iface,
                "-o",
                self.spec.host_if,
                "-m",
                "state",
                "--state",
                "RELATED,ESTABLISHED",
                "-j",
                "ACCEPT",
            ],
            check=False,
        )
        self.runner.run(
            ["iptables", "-D", "FORWARD", "-i", self.spec.host_if, "-o", self.public_iface, "-j", "ACCEPT"],
            check=False,
        )
        self.runner.run(["ip", "link", "delete", self.spec.host_if], check=False)
        self.runner.run(["ip", "netns", "delete", self.spec.namespace], check=False)

    def build_unshare_command(self, inner_cmd: list[str]) -> list[str]:
        script = "\n".join(
            [
                "set -euo pipefail",
                "mount --make-rprivate /",
                "mount --bind \"$1\" /sys/devices/virtual/net",
                "hostname \"$2\"",
                "shift 2",
                "exec \"$@\"",
            ]
        )
        cmd = [
            "ip",
            "netns",
            "exec",
            self.spec.namespace,
            "unshare",
            "--mount",
            "--uts",
            "bash",
            "-lc",
            script,
            "bash",
            str(self.fake_virtual_net_dir),
            self.spec.hostname,
        ]
        return cmd + inner_cmd

    def run_machine_info(self) -> subprocess.CompletedProcess[str]:
        inner_cmd = [
            str(self.qoder_path),
            "machine-info",
            "--debug",
            "--workDir",
            str(self.spec.work_dir),
        ]
        if self.region:
            inner_cmd.extend(["--region", self.region])
        return self.runner.run(self.build_unshare_command(inner_cmd))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量构造新的 Qoder 设备环境并跑通 machine-info。")
    parser.add_argument("--qoder", default=str(DEFAULT_QODER), help=f"Qoder 二进制路径，默认 {DEFAULT_QODER}")
    parser.add_argument("--count", type=int, default=5, help="目标成功设备数量，默认 5")
    parser.add_argument("--max-attempts", type=int, help="最大尝试次数，默认与 --count 相同")
    parser.add_argument("--seed", type=int, help="随机种子，默认自动生成")
    parser.add_argument("--region", help="透传给 machine-info 的 region 参数")
    parser.add_argument("--public-iface", help="宿主机出口网卡，默认自动检测 default route")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"输出根目录，默认 {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument(
        "--output-prefix",
        default="qoder_batch_devices",
        help="输出目录前缀，默认 qoder_batch_devices",
    )
    parser.add_argument(
        "--hostname-prefix",
        default="batch-host",
        help="构造设备时使用的 hostname 前缀",
    )
    parser.add_argument("--verbose", action="store_true", help="打印底层 ip/iptables/unshare 命令")
    parser.add_argument("--json", action="store_true", help="最终输出 JSON 摘要")
    return parser.parse_args()


def ensure_root() -> None:
    if os.geteuid() != 0:
        raise BuilderError("该脚本需要 root 权限运行（ip netns / iptables / mount）")


def detect_public_iface(runner: CommandRunner) -> str:
    result = runner.run(["ip", "-o", "route", "show", "default"])
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    raise BuilderError("无法从 default route 自动识别出口网卡，请使用 --public-iface 显式指定")


def random_mac(rng: random.Random) -> str:
    octets = [0x02, rng.randrange(0, 256), rng.randrange(0, 256), rng.randrange(0, 256), rng.randrange(0, 256), rng.randrange(0, 256)]
    return ":".join(f"{octet:02x}" for octet in octets)


def random_hostname(prefix: str, rng: random.Random, attempt: int) -> str:
    suffix = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"{prefix}-{attempt:03d}-{suffix}"


def build_network(attempt: int) -> tuple[str, str, str]:
    if attempt < 1:
        raise BuilderError("attempt 必须从 1 开始")
    second = 20 + ((attempt - 1) // 200)
    third = 1 + ((attempt - 1) % 200)
    if second > 250:
        raise BuilderError("尝试次数过大，当前子网分配策略已耗尽")
    subnet = f"10.{second}.{third}.0"
    return subnet, f"10.{second}.{third}.1", f"10.{second}.{third}.2"


def build_spec(output_dir: Path, hostname_prefix: str, rng: random.Random, attempt: int) -> DeviceSpec:
    token = f"{attempt:04d}{secrets.token_hex(2)}"
    namespace = f"qdb{token}"
    host_if = f"qdh{token[-6:]}"
    ns_if = f"qdn{token[-6:]}"
    subnet, host_ip, ns_ip = build_network(attempt)
    work_dir = output_dir / "devices" / f"device_{attempt:04d}"
    log_dir = work_dir / "logs"
    return DeviceSpec(
        attempt=attempt,
        namespace=namespace,
        host_if=host_if[:15],
        ns_if=ns_if[:15],
        subnet=subnet,
        host_ip=host_ip,
        ns_ip=ns_ip,
        mac=random_mac(rng),
        hostname=random_hostname(hostname_prefix, rng, attempt),
        work_dir=work_dir,
        log_dir=log_dir,
    )


def parse_machine_info_output(spec: DeviceSpec, result: subprocess.CompletedProcess[str]) -> DeviceResult:
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    stdout_log = spec.log_dir / "qoder.stdout.log"
    stderr_log = spec.log_dir / "qoder.stderr.log"
    stdout_log.write_text(result.stdout, encoding="utf-8")
    stderr_log.write_text(result.stderr, encoding="utf-8")

    payload: dict[str, Any] | None = None
    for line in reversed(stdout_lines):
        if line.startswith("{") and line.endswith("}"):
            payload = json.loads(line)
            break

    init_region = None
    init_code = None
    for line in stdout_lines:
        match = INIT_REGION_RE.search(line)
        if match:
            init_region = int(match.group(1))
            init_code = int(match.group(2))
            break

    if payload is None:
        return DeviceResult(
            attempt=spec.attempt,
            success=False,
            classification="parse_failed",
            init_region=init_region,
            init_code=init_code,
            machine_token="",
            machine_type="",
            machine_id=read_machine_id(spec.work_dir),
            mac=spec.mac.replace(":", ""),
            hostname=spec.hostname,
            work_dir=str(spec.work_dir),
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
            elapsed_seconds=0.0,
            error="machine-info 输出中未找到 JSON 结果",
        )

    machine_token = str(payload.get("machineToken", ""))
    machine_type = str(payload.get("machineType", ""))
    classification = classify_result(init_code, machine_token)
    return DeviceResult(
        attempt=spec.attempt,
        success=classification == "server_backed_or_real_umid",
        classification=classification,
        init_region=init_region,
        init_code=init_code,
        machine_token=machine_token,
        machine_type=machine_type,
        machine_id=read_machine_id(spec.work_dir),
        mac=spec.mac.replace(":", ""),
        hostname=spec.hostname,
        work_dir=str(spec.work_dir),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        elapsed_seconds=0.0,
    )


def classify_result(init_code: int | None, machine_token: str) -> str:
    if init_code == 0 and len(machine_token) == TOKEN_LEN_REAL:
        return "server_backed_or_real_umid"
    if init_code == 6 and len(machine_token) == 32:
        return "local_fallback_or_degraded_umid"
    return "mixed_or_unknown"


def read_machine_id(work_dir: Path) -> str:
    path = work_dir / "cache" / "id"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_csv(path: Path, results: list[DeviceResult]) -> None:
    fieldnames = [
        "seed",
        "machine_token",
        "machine_type",
        "machine_id",
        "mac",
        "hostname",
        "init_code",
        "classification",
        "work_dir",
        "stdout_log",
        "stderr_log",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "seed": item.attempt,
                    "machine_token": item.machine_token,
                    "machine_type": item.machine_type,
                    "machine_id": item.machine_id,
                    "mac": item.mac,
                    "hostname": item.hostname,
                    "init_code": "" if item.init_code is None else item.init_code,
                    "classification": item.classification,
                    "work_dir": item.work_dir,
                    "stdout_log": item.stdout_log,
                    "stderr_log": item.stderr_log,
                    "error": item.error or "",
                }
            )


def build_output_dir(root: Path, prefix: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    output_dir = root / f"{prefix}_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


def run_attempt(
    spec: DeviceSpec,
    *,
    public_iface: str,
    fake_virtual_net_dir: Path,
    qoder_path: Path,
    region: str | None,
    runner: CommandRunner,
) -> DeviceResult:
    start = time.monotonic()
    try:
        with NamespaceSession(
            spec,
            public_iface=public_iface,
            fake_virtual_net_dir=fake_virtual_net_dir,
            qoder_path=qoder_path,
            region=region,
            runner=runner,
        ) as session:
            completed = session.run_machine_info()
        parsed = parse_machine_info_output(spec, completed)
        parsed.elapsed_seconds = round(time.monotonic() - start, 3)
        return parsed
    except subprocess.CalledProcessError as exc:
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = spec.log_dir / "qoder.stdout.log"
        stderr_log = spec.log_dir / "qoder.stderr.log"
        stdout_log.write_text(exc.stdout or "", encoding="utf-8")
        stderr_log.write_text(exc.stderr or "", encoding="utf-8")
        return DeviceResult(
            attempt=spec.attempt,
            success=False,
            classification="command_failed",
            init_region=None,
            init_code=None,
            machine_token="",
            machine_type="",
            machine_id=read_machine_id(spec.work_dir),
            mac=spec.mac.replace(":", ""),
            hostname=spec.hostname,
            work_dir=str(spec.work_dir),
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
            elapsed_seconds=round(time.monotonic() - start, 3),
            error=f"{exc.cmd!r} exited with code {exc.returncode}",
        )


def print_human_summary(results: list[DeviceResult], output_dir: Path, csv_path: Path) -> None:
    successes = [item for item in results if item.success]
    print(f"输出目录: {output_dir}")
    print(f"CSV: {csv_path}")
    print(f"成功 {len(successes)}/{len(results)}")
    for item in results:
        marker = "OK" if item.success else "!!"
        print(
            f"[{marker}] seed={item.attempt} init={item.init_code} class={item.classification} "
            f"mac={item.mac} host={item.hostname} token_len={len(item.machine_token)}"
        )


def build_json_summary(
    *,
    output_dir: Path,
    csv_path: Path,
    qoder_path: Path,
    public_iface: str,
    seed: int,
    desired_count: int,
    max_attempts: int,
    results: list[DeviceResult],
) -> dict[str, Any]:
    success_results = [item for item in results if item.success]
    return {
        "outputDir": str(output_dir),
        "csv": str(csv_path),
        "qoder": str(qoder_path),
        "publicIface": public_iface,
        "seed": seed,
        "desiredCount": desired_count,
        "maxAttempts": max_attempts,
        "successCount": len(success_results),
        "attemptCount": len(results),
        "results": [
            {
                **asdict(item),
                "machineTypeAnalysis": (
                    asdict(
                        analyze_machine_type(
                            machine_token=item.machine_token,
                            observed_machine_type=item.machine_type or None,
                            forced_vm_type=None,
                        )
                    )
                    if item.machine_token and item.machine_type
                    else None
                ),
            }
            for item in results
        ],
    }


def main() -> int:
    args = parse_args()
    ensure_root()

    qoder_path = Path(args.qoder).resolve()
    if not qoder_path.exists():
        raise BuilderError(f"Qoder 不存在: {qoder_path}")

    output_root = Path(args.output_root).resolve()
    output_dir = build_output_dir(output_root, args.output_prefix)
    fake_virtual_net_dir = output_dir / "fake_virtual_net"
    csv_path = output_dir / "devices.csv"
    runner = CommandRunner(verbose=args.verbose)
    public_iface = args.public_iface or detect_public_iface(runner)
    rng_seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(rng_seed)
    desired_count = args.count
    max_attempts = args.max_attempts or desired_count

    results: list[DeviceResult] = []
    seen_tokens: set[str] = set()

    with ForwardingGuard(runner):
        attempt = 1
        while len([item for item in results if item.success]) < desired_count and attempt <= max_attempts:
            spec = build_spec(output_dir, args.hostname_prefix, rng, attempt)
            result = run_attempt(
                spec,
                public_iface=public_iface,
                fake_virtual_net_dir=fake_virtual_net_dir,
                qoder_path=qoder_path,
                region=args.region,
                runner=runner,
            )
            if result.machine_token and result.machine_token in seen_tokens:
                result.success = False
                result.classification = "duplicate_machine_token"
                result.error = "machineToken 与之前结果重复"
            elif result.machine_token:
                seen_tokens.add(result.machine_token)
            results.append(result)
            attempt += 1

    write_csv(csv_path, results)

    summary = build_json_summary(
        output_dir=output_dir,
        csv_path=csv_path,
        qoder_path=qoder_path,
        public_iface=public_iface,
        seed=rng_seed,
        desired_count=desired_count,
        max_attempts=max_attempts,
        results=results,
    )
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_human_summary(results, output_dir, csv_path)

    success_count = len([item for item in results if item.success])
    return 0 if success_count >= desired_count else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuilderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
