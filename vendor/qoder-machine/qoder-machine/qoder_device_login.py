#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


AUTH_STATUS_NAMES = {
    1: "NOT_LOGIN",
    2: "LOGIN",
    3: "LOGIN_EXPIRED",
    4: "ERROR",
    5: "NETWORK_ERROR",
    6: "IP_BANNED_ERROR",
    7: "APP_DISABLED_ERROR",
    8: "LOGGING_IN",
    9: "CHECKING",
}

WHITELIST_NAMES = {
    1: "NOT_WHITELIST",
    2: "WAIT_PASS",
    3: "PASS",
    4: "UNKNOWN",
    5: "NO_LICENCE",
    6: "ORG_ORDER_EXPIRED",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QODER = REPO_ROOT / "temp" / "Qoder"
DEFAULT_WORK_DIR = Path.home() / ".qoder" / "shared_client"
DEFAULT_PROBE_REGIONS = ("", "1", "2", "3")
MASK64 = 0xFFFFFFFFFFFFFFFF
XXH64_PRIME1 = 11400714785074694791
XXH64_PRIME2 = 14029467366897019727
XXH64_PRIME3 = 1609587929392839161
XXH64_PRIME4 = 9650029242287828579
XXH64_PRIME5 = 2870177450012600261
INIT_REGION_RE = re.compile(r"Initialized region (\d+) code (\d+)")


class QoderError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="复用 Qoder 原生后端生成登录 URL / 设备登录参数，并可等待登录成功。",
    )
    parser.add_argument(
        "--qoder",
        default=str(DEFAULT_QODER),
        help=f"Qoder 原生二进制路径，默认 {DEFAULT_QODER}",
    )
    parser.add_argument(
        "--work-dir",
        default=str(DEFAULT_WORK_DIR),
        help=f"Qoder 工作目录，默认 {DEFAULT_WORK_DIR}",
    )
    parser.add_argument(
        "--socket-port",
        type=int,
        help="启动新 Qoder 时使用的 websocket 端口，默认自动找空闲端口",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        help="启动新 Qoder 时使用的本地回调 HTTP 端口，默认自动找空闲端口",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="只连接已存在的 Qoder 服务，不自动启动",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="生成登录 URL 后轮询 auth/status，直到登录完成或超时",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="等待登录的超时时间（秒），默认 300",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="轮询 auth/status 的时间间隔（秒），默认 1",
    )
    parser.add_argument(
        "--require-whitelist-pass",
        action="store_true",
        help="等待条件升级为 status=LOGIN 且 whitelist=PASS",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出结构化 JSON",
    )
    parser.add_argument(
        "--region",
        help="machine-info 使用的 region 参数，可选空/1/2/3",
    )
    parser.add_argument(
        "--machine-info-only",
        action="store_true",
        help="只运行 Qoder machine-info，不连接 IPC / 不生成登录 URL",
    )
    parser.add_argument(
        "--probe-machine-info",
        action="store_true",
        help="批量探测 machine-info 在 default/1/2/3 region 下的行为",
    )
    parser.add_argument(
        "--probe-runs",
        type=int,
        default=1,
        help="--probe-machine-info 时每个 region 运行次数，默认 1",
    )
    parser.add_argument(
        "--risk-precheck",
        action="store_true",
        help="执行风控预检：要求拿到真实/稳定的 UMID machineToken，否则返回非 0",
    )
    parser.add_argument(
        "--require-real-umid",
        action="store_true",
        help="默认登录流前先做风控预检；若仍是 fallback/degraded token 则直接失败",
    )
    parser.add_argument(
        "--dump-user-plan",
        action="store_true",
        help="额外查询一次 user/plan，并输出其结果或错误",
    )
    parser.add_argument(
        "--simulate-device-login",
        action="store_true",
        help="直接调用本地 auth/device_login，模拟设备登录回调落地",
    )
    parser.add_argument(
        "--device-token",
        help="--simulate-device-login 使用的 token；默认 a",
    )
    parser.add_argument(
        "--device-user-id",
        help="--simulate-device-login 使用的 userId；默认 b",
    )
    parser.add_argument(
        "--device-expire-time",
        help="--simulate-device-login 使用的 expireTime（毫秒字符串）；默认当前时间+1小时",
    )
    parser.add_argument(
        "--device-refresh-token",
        help="--simulate-device-login 可选 refreshToken",
    )
    parser.add_argument(
        "--device-username",
        help="--simulate-device-login 可选 username",
    )
    parser.add_argument(
        "--device-expires-in",
        help="--simulate-device-login 可选 expiresIn",
    )
    parser.add_argument(
        "--compute-only",
        action="store_true",
        help="只做本地 machineType 计算，不连接 Qoder",
    )
    parser.add_argument(
        "--machine-token",
        help="手工指定 machineToken；配合 --compute-only 使用，或用于覆盖 native 返回值",
    )
    parser.add_argument(
        "--vm-type",
        type=int,
        help="手工指定 VMType（如 91/13/50）；未指定时优先复用 native machineType 中的中间两位",
    )
    return parser.parse_args()


def log(message: str) -> None:
    print(message, file=sys.stderr)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def wait_for_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return path.exists()


@dataclass(slots=True)
class QoderInfo:
    pid: int | None
    websocket_port: int | None
    ipc_server_path: Path | None

    @classmethod
    def from_file(cls, info_path: Path) -> "QoderInfo | None":
        payload = read_json_file(info_path)
        if not payload:
            return None
        socket_path = payload.get("ipcServerPath")
        return cls(
            pid=payload.get("pid"),
            websocket_port=payload.get("websocketPort"),
            ipc_server_path=Path(socket_path) if socket_path else None,
        )


@dataclass(slots=True)
class MachineTypeAnalysis:
    machine_token: str
    machine_hash: str
    computed_machine_type: str
    vm_type: int
    vm_type_source: str
    observed_machine_type: str | None = None
    observed_vm_type: int | None = None
    observed_hash: str | None = None
    matches_observed: bool | None = None


@dataclass(slots=True)
class MachineInfoResult:
    machine_token: str
    machine_type: str
    stdout_lines: list[str]
    stderr_lines: list[str]
    init_region: int | None
    init_code: int | None
    region_arg: str | None
    work_dir: str


class JsonRpcStreamClient:
    def __init__(self, socket_path: Path, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._buffer = bytearray()
        self._next_id = 1
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(socket_path))

    def close(self) -> None:
        self._sock.close()

    def request(self, method: str, params: Any = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        while True:
            message = self._recv_message()
            message_id = message.get("id")
            if message_id == request_id:
                if "error" in message:
                    raise QoderError(f"{method} failed: {json.dumps(message['error'], ensure_ascii=False)}")
                return message.get("result")
            if "method" in message and message_id is not None:
                self._send({"jsonrpc": "2.0", "id": message_id, "result": None})

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._sock.sendall(header + body)

    def _recv_message(self) -> dict[str, Any]:
        header_end = self._buffer.find(b"\r\n\r\n")
        while header_end == -1:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise QoderError("Qoder IPC closed unexpectedly")
            self._buffer.extend(chunk)
            header_end = self._buffer.find(b"\r\n\r\n")

        header = bytes(self._buffer[:header_end]).decode("ascii", errors="replace")
        body_start = header_end + 4
        content_length = None
        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())
                break
        if content_length is None:
            raise QoderError(f"Invalid LSP header: {header!r}")

        total_needed = body_start + content_length
        while len(self._buffer) < total_needed:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise QoderError("Qoder IPC closed while receiving JSON-RPC body")
            self._buffer.extend(chunk)

        body = bytes(self._buffer[body_start:total_needed])
        del self._buffer[:total_needed]
        return json.loads(body.decode("utf-8"))


def ensure_socket_path(path: Path | None, base_dir: Path) -> Path:
    if path is None:
        raise QoderError(".info.json 中没有 ipcServerPath")
    return path if path.is_absolute() else (base_dir / path).resolve()


def rotl64(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & MASK64


def xxh64_round(accumulator: int, lane: int) -> int:
    accumulator = (accumulator + (lane * XXH64_PRIME2)) & MASK64
    accumulator = rotl64(accumulator, 31)
    return (accumulator * XXH64_PRIME1) & MASK64


def xxh64_merge_round(accumulator: int, lane: int) -> int:
    accumulator ^= xxh64_round(0, lane)
    accumulator = (accumulator * XXH64_PRIME1 + XXH64_PRIME4) & MASK64
    return accumulator


def xxh64_avalanche(value: int) -> int:
    value ^= value >> 33
    value = (value * XXH64_PRIME2) & MASK64
    value ^= value >> 29
    value = (value * XXH64_PRIME3) & MASK64
    value ^= value >> 32
    return value & MASK64


def xxh64_int(data: bytes, seed: int = 0) -> int:
    length = len(data)
    offset = 0

    if length >= 32:
        v1 = (seed + XXH64_PRIME1 + XXH64_PRIME2) & MASK64
        v2 = (seed + XXH64_PRIME2) & MASK64
        v3 = seed & MASK64
        v4 = (seed - XXH64_PRIME1) & MASK64

        limit = length - 32
        while offset <= limit:
            v1 = xxh64_round(v1, int.from_bytes(data[offset : offset + 8], "little"))
            v2 = xxh64_round(v2, int.from_bytes(data[offset + 8 : offset + 16], "little"))
            v3 = xxh64_round(v3, int.from_bytes(data[offset + 16 : offset + 24], "little"))
            v4 = xxh64_round(v4, int.from_bytes(data[offset + 24 : offset + 32], "little"))
            offset += 32

        result = (
            rotl64(v1, 1)
            + rotl64(v2, 7)
            + rotl64(v3, 12)
            + rotl64(v4, 18)
        ) & MASK64
        result = xxh64_merge_round(result, v1)
        result = xxh64_merge_round(result, v2)
        result = xxh64_merge_round(result, v3)
        result = xxh64_merge_round(result, v4)
    else:
        result = (seed + XXH64_PRIME5) & MASK64

    result = (result + length) & MASK64

    limit = length - 8
    while offset <= limit:
        lane = int.from_bytes(data[offset : offset + 8], "little")
        result ^= xxh64_round(0, lane)
        result = (rotl64(result, 27) * XXH64_PRIME1 + XXH64_PRIME4) & MASK64
        offset += 8

    if offset + 4 <= length:
        lane = int.from_bytes(data[offset : offset + 4], "little")
        result ^= (lane * XXH64_PRIME1) & MASK64
        result = (rotl64(result, 23) * XXH64_PRIME2 + XXH64_PRIME3) & MASK64
        offset += 4

    while offset < length:
        result ^= (data[offset] * XXH64_PRIME5) & MASK64
        result = (rotl64(result, 11) * XXH64_PRIME1) & MASK64
        offset += 1

    return xxh64_avalanche(result)


def xxh64_hex(text: str) -> str:
    return f"{xxh64_int(text.encode('utf-8')):016x}"


def split_machine_type(machine_type: str) -> tuple[str, int]:
    if len(machine_type) != 18:
        raise QoderError(f"machineType 长度异常: {machine_type!r}")
    prefix = machine_type[:8]
    middle = machine_type[8:10]
    suffix = machine_type[10:]
    if not middle.isdigit():
        raise QoderError(f"machineType 中间 VMType 不是两位数字: {machine_type!r}")
    try:
        int(prefix + suffix, 16)
    except ValueError as exc:
        raise QoderError(f"machineType hash 片段不是十六进制: {machine_type!r}") from exc
    return prefix + suffix, int(middle)


def build_machine_type(machine_token: str, vm_type: int) -> str:
    if not 0 <= vm_type <= 99:
        raise QoderError(f"VMType 必须在 0..99 之间: {vm_type}")
    machine_hash = xxh64_hex(machine_token)
    return f"{machine_hash[:8]}{vm_type:02d}{machine_hash[8:]}"


def analyze_machine_type(
    machine_token: str,
    observed_machine_type: str | None,
    forced_vm_type: int | None,
) -> MachineTypeAnalysis:
    observed_hash: str | None = None
    observed_vm_type: int | None = None
    if observed_machine_type:
        observed_hash, observed_vm_type = split_machine_type(observed_machine_type)

    if forced_vm_type is not None:
        vm_type = forced_vm_type
        vm_type_source = "user"
    elif observed_vm_type is not None:
        vm_type = observed_vm_type
        vm_type_source = "native_machineType"
    else:
        vm_type = 91
        vm_type_source = "default_91"

    machine_hash = xxh64_hex(machine_token)
    computed_machine_type = build_machine_type(machine_token, vm_type)
    matches_observed = None
    if observed_machine_type is not None:
        matches_observed = observed_machine_type == computed_machine_type

    return MachineTypeAnalysis(
        machine_token=machine_token,
        machine_hash=machine_hash,
        computed_machine_type=computed_machine_type,
        vm_type=vm_type,
        vm_type_source=vm_type_source,
        observed_machine_type=observed_machine_type,
        observed_vm_type=observed_vm_type,
        observed_hash=observed_hash,
        matches_observed=matches_observed,
    )


def machine_info(
    qoder_path: Path,
    work_dir: Path,
    *,
    region: str | None = None,
    debug: bool = False,
) -> MachineInfoResult:
    cmd = [str(qoder_path), "machine-info", "--workDir", str(work_dir)]
    if debug:
        cmd.append("--debug")
    if region:
        cmd.extend(["--region", region])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    lines = stdout_lines
    if not lines:
        raise QoderError("machine-info 没有输出")
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            payload = json.loads(line)
            init_region = None
            init_code = None
            for log_line in stdout_lines:
                match = INIT_REGION_RE.search(log_line)
                if match:
                    init_region = int(match.group(1))
                    init_code = int(match.group(2))
                    break
            return MachineInfoResult(
                machine_token=payload["machineToken"],
                machine_type=payload["machineType"],
                stdout_lines=[item for item in stdout_lines if not item.startswith("{")],
                stderr_lines=stderr_lines,
                init_region=init_region,
                init_code=init_code,
                region_arg=region,
                work_dir=str(work_dir),
            )
    raise QoderError(f"machine-info 输出无法解析为 JSON: {result.stdout!r}")


def classify_machine_info_runs(runs: list[MachineInfoResult]) -> str:
    init_codes = {item.init_code for item in runs}
    tokens = {item.machine_token for item in runs}
    token_lengths = {len(item.machine_token) for item in runs}
    if init_codes == {0} and len(tokens) == 1:
        return "server_backed_or_real_umid"
    if init_codes == {6} and len(tokens) == len(runs) and token_lengths == {32}:
        return "local_fallback_or_degraded_umid"
    return "mixed_or_unknown"


def is_real_umid_classification(classification: str) -> bool:
    return classification == "server_backed_or_real_umid"


def build_probe_dirs(base_work_dir: Path, probe_runs: int) -> list[tuple[str, Path]]:
    stamp = int(time.time())
    probe_root = base_work_dir.parent / f"{base_work_dir.name}_probe_{stamp}"
    probe_root.mkdir(parents=True, exist_ok=True)
    probe_targets: list[tuple[str, Path]] = []
    for region in DEFAULT_PROBE_REGIONS:
        region_name = region or "default"
        for index in range(probe_runs):
            probe_targets.append(
                (
                    region,
                    probe_root / f"{region_name}_{index}",
                )
            )
    return probe_targets


def run_machine_info_probe(
    qoder_path: Path,
    work_dir: Path,
    probe_runs: int,
    forced_vm_type: int | None,
) -> dict[str, Any]:
    probe_results: list[MachineInfoResult] = []
    for region, probe_dir in build_probe_dirs(work_dir, probe_runs):
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_results.append(
            machine_info(
                qoder_path,
                probe_dir,
                region=region or None,
                debug=True,
            )
        )
    return {
        "qoder": str(qoder_path),
        "probe_root": str(Path(probe_results[0].work_dir).parent),
        "classification": classify_machine_info_runs(probe_results),
        "results": [
            {
                "region": item.region_arg or "default",
                "work_dir": item.work_dir,
                "machineToken": item.machine_token,
                "machineTokenLength": len(item.machine_token),
                "machineType": item.machine_type,
                "machineTypeAnalysis": asdict(
                    analyze_machine_type(
                        machine_token=item.machine_token,
                        observed_machine_type=item.machine_type,
                        forced_vm_type=forced_vm_type,
                    )
                ),
                "initRegion": item.init_region,
                "initCode": item.init_code,
                "stdoutLines": item.stdout_lines,
                "stderrLines": item.stderr_lines,
            }
            for item in probe_results
        ],
    }


def start_qoder_if_needed(args: argparse.Namespace, qoder_path: Path, work_dir: Path) -> tuple[QoderInfo, bool]:
    info_path = work_dir / ".info.json"
    info = QoderInfo.from_file(info_path)
    if info is not None:
        socket_path = ensure_socket_path(info.ipc_server_path, Path.cwd())
        if is_process_alive(info.pid) and socket_path.exists():
            return info, False

    if args.no_start:
        raise QoderError("未发现可用的 Qoder 服务，且指定了 --no-start")

    work_dir.mkdir(parents=True, exist_ok=True)
    socket_port = args.socket_port or find_free_port()
    http_port = args.http_port or find_free_port()
    cmd = [
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
    log(f"[+] 启动 Qoder: {' '.join(cmd)}")
    subprocess.Popen(
        cmd,
        cwd=str(Path.cwd()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    if not wait_for_file(info_path, timeout=15):
        raise QoderError("启动后未生成 .info.json")

    deadline = time.monotonic() + 20
    last_info: QoderInfo | None = None
    while time.monotonic() < deadline:
        last_info = QoderInfo.from_file(info_path)
        if last_info is not None:
            socket_path = ensure_socket_path(last_info.ipc_server_path, Path.cwd())
            if socket_path.exists():
                return last_info, True
        time.sleep(0.2)
    raise QoderError(f"启动后未发现可用 IPC socket: {last_info}")


def connect_client(socket_path: Path, timeout: float = 10.0) -> tuple[JsonRpcStreamClient, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = JsonRpcStreamClient(socket_path, timeout=timeout)
            ping = client.request("ping")
            return client, ping
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.2)
    raise QoderError(f"连接 Qoder IPC 失败: {last_error}")


def status_name(status: dict[str, Any]) -> str:
    return AUTH_STATUS_NAMES.get(status.get("status"), f"UNKNOWN({status.get('status')})")


def whitelist_name(status: dict[str, Any]) -> str:
    return WHITELIST_NAMES.get(status.get("whitelist"), f"UNKNOWN({status.get('whitelist')})")


def wait_for_login(
    client: JsonRpcStreamClient,
    timeout: float,
    interval: float,
    require_whitelist_pass: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_seen: tuple[int | None, int | None] | None = None
    while time.monotonic() < deadline:
        status = client.request("auth/status")
        marker = (status.get("status"), status.get("whitelist"))
        if marker != last_seen:
            log(
                f"[*] auth/status => {status_name(status)} / whitelist={whitelist_name(status)}"
            )
            last_seen = marker
        logged_in = status.get("status") == 2
        allow = logged_in and status.get("whitelist") == 3
        if allow or (logged_in and not require_whitelist_pass):
            return status
        time.sleep(interval)
    raise QoderError("等待登录超时")


def try_request(client: JsonRpcStreamClient, method: str, params: Any = None) -> tuple[Any | None, str | None]:
    try:
        return client.request(method, params), None
    except QoderError as exc:
        return None, str(exc)


def build_device_login_params(args: argparse.Namespace) -> dict[str, str]:
    expire_time = args.device_expire_time or str(int(time.time() * 1000) + 3600_000)
    params = {
        "token": args.device_token or "a",
        "userId": args.device_user_id or "b",
        "expireTime": expire_time,
    }
    if args.device_refresh_token:
        params["refreshToken"] = args.device_refresh_token
    if args.device_username:
        params["username"] = args.device_username
    if args.device_expires_in:
        params["expiresIn"] = args.device_expires_in
    return params


def main() -> int:
    args = parse_args()
    qoder_path = Path(args.qoder).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    if args.compute_only:
        if not args.machine_token:
            raise QoderError("--compute-only 模式必须提供 --machine-token")
        analysis = analyze_machine_type(
            machine_token=args.machine_token,
            observed_machine_type=None,
            forced_vm_type=args.vm_type,
        )
        output = {
            "machine_token": analysis.machine_token,
            "machine_hash": analysis.machine_hash,
            "vm_type": analysis.vm_type,
            "vm_type_source": analysis.vm_type_source,
            "computed_machine_type": analysis.computed_machine_type,
        }
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        print(f"Machine token:    {analysis.machine_token}")
        print(f"Machine hash:     {analysis.machine_hash}")
        print(f"VM type:          {analysis.vm_type} ({analysis.vm_type_source})")
        print(f"Machine type(py): {analysis.computed_machine_type}")
        return 0

    if not qoder_path.exists():
        raise QoderError(f"Qoder 二进制不存在: {qoder_path}")

    if args.machine_info_only:
        machine = machine_info(
            qoder_path,
            work_dir,
            region=args.region,
            debug=True,
        )
        machine_analysis = analyze_machine_type(
            machine_token=args.machine_token or machine.machine_token,
            observed_machine_type=machine.machine_type,
            forced_vm_type=args.vm_type,
        )
        output = {
            "qoder": str(qoder_path),
            "work_dir": str(work_dir),
            "region": args.region or "default",
            "machine_info": {
                "machineToken": machine.machine_token,
                "machineType": machine.machine_type,
                "machineTypePy": machine_analysis.computed_machine_type,
                "machineHashPy": machine_analysis.machine_hash,
                "machineTypeVmType": machine_analysis.vm_type,
                "machineTypeVmTypeSource": machine_analysis.vm_type_source,
                "machineTypeMatch": machine_analysis.matches_observed,
                "initRegion": machine.init_region,
                "initCode": machine.init_code,
                "stdoutLines": machine.stdout_lines,
                "stderrLines": machine.stderr_lines,
            },
        }
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        print(f"Qoder:        {qoder_path}")
        print(f"Work dir:     {work_dir}")
        print(f"Region arg:   {args.region or 'default'}")
        print(f"Machine token:{machine.machine_token}")
        print(f"Machine type: {machine.machine_type}")
        print(f"Machine type(py): {machine_analysis.computed_machine_type}")
        print(
            "Machine type match: "
            f"{machine_analysis.matches_observed} "
            f"(vmType={machine_analysis.vm_type}, source={machine_analysis.vm_type_source})"
        )
        print(f"Init region/code: {machine.init_region}/{machine.init_code}")
        if machine.stdout_lines:
            print("STDOUT logs:")
            for line in machine.stdout_lines:
                print(f"  {line}")
        if machine.stderr_lines:
            print("STDERR logs:")
            for line in machine.stderr_lines:
                print(f"  {line}")
        return 0

    if args.probe_machine_info or args.risk_precheck:
        probe_runs = max(args.probe_runs, 1)
        output = run_machine_info_probe(qoder_path, work_dir, probe_runs, args.vm_type)
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
            if args.risk_precheck and not is_real_umid_classification(output["classification"]):
                return 2
            return 0
        print(f"Probe root:    {output['probe_root']}")
        print(f"Classification:{output['classification']}")
        for item in output["results"]:
            print("---")
            print(f"Region:        {item['region']}")
            print(f"Work dir:      {item['work_dir']}")
            print(f"Token len:     {item['machineTokenLength']}")
            print(f"Machine token: {item['machineToken']}")
            print(f"Machine type:  {item['machineType']}")
            print(f"Init region/code: {item['initRegion']}/{item['initCode']}")
        if args.risk_precheck and not is_real_umid_classification(output["classification"]):
            print("[!] 风控预检失败：当前仍是 fallback/degraded UMID", file=sys.stderr)
            return 2
        return 0

    if args.require_real_umid:
        precheck = run_machine_info_probe(qoder_path, work_dir, probe_runs=1, forced_vm_type=args.vm_type)
        if not is_real_umid_classification(precheck["classification"]):
            raise QoderError(
                f"风控预检失败：{precheck['classification']}，当前 machine-info 仍未进入真实 UMID 链"
            )

    info, started = start_qoder_if_needed(args, qoder_path, work_dir)
    socket_path = ensure_socket_path(info.ipc_server_path, Path.cwd())
    client, ping = connect_client(socket_path)
    login_result: dict[str, Any] | None = None
    device_login_params: dict[str, str] | None = None
    device_login_result: Any = None
    user_plan_result: Any = None
    user_plan_error: str | None = None
    try:
        if not ping or ping.get("success") is not True:
            raise QoderError(f"ping 失败: {ping}")
        status = client.request("auth/status")
        if args.simulate_device_login:
            device_login_params = build_device_login_params(args)
            device_login_result, device_login_error = try_request(
                client,
                "auth/device_login",
                device_login_params,
            )
            if device_login_error:
                raise QoderError(device_login_error)
        else:
            login_result = client.request("login/generateUrl")
        machine = machine_info(
            qoder_path,
            work_dir,
            region=args.region,
            debug=False,
        )
        machine_analysis = analyze_machine_type(
            machine_token=args.machine_token or machine.machine_token,
            observed_machine_type=machine.machine_type,
            forced_vm_type=args.vm_type,
        )
        final_status = status
        if args.simulate_device_login:
            final_status = client.request("auth/status")
        if args.wait:
            final_status = wait_for_login(
                client,
                timeout=args.timeout,
                interval=args.interval,
                require_whitelist_pass=args.require_whitelist_pass,
            )
        if args.dump_user_plan:
            user_plan_result, user_plan_error = try_request(client, "user/plan")
    finally:
        client.close()

    output = {
        "qoder": str(qoder_path),
        "work_dir": str(work_dir),
        "started_qoder": started,
        "pid": info.pid,
        "websocket_port": info.websocket_port,
        "ipc_socket": str(socket_path),
        "machine_info": {
            "machineToken": machine.machine_token,
            "machineType": machine.machine_type,
            "initRegion": machine.init_region,
            "initCode": machine.init_code,
            "stdoutLines": machine.stdout_lines,
            "stderrLines": machine.stderr_lines,
            "machineTypePy": machine_analysis.computed_machine_type,
            "machineHashPy": machine_analysis.machine_hash,
            "machineTypeVmType": machine_analysis.vm_type,
            "machineTypeVmTypeSource": machine_analysis.vm_type_source,
            "machineTypeMatch": machine_analysis.matches_observed,
        },
        "login": login_result,
        "device_login_params": device_login_params,
        "device_login_result": device_login_result,
        "initial_status": {
            **status,
            "_status_name": status_name(status),
            "_whitelist_name": whitelist_name(status),
        },
        "final_status": {
            **final_status,
            "_status_name": status_name(final_status),
            "_whitelist_name": whitelist_name(final_status),
        },
    }
    if args.dump_user_plan:
        output["user_plan"] = user_plan_result
        output["user_plan_error"] = user_plan_error

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print(f"Qoder:        {qoder_path}")
    print(f"Work dir:     {work_dir}")
    print(f"Started now:  {started}")
    print(f"PID:          {info.pid}")
    print(f"IPC socket:   {socket_path}")
    print(f"WS port:      {info.websocket_port}")
    print(f"Machine type: {machine.machine_type}")
    print(f"Machine type(py): {machine_analysis.computed_machine_type}")
    print(f"Machine hash(py): {machine_analysis.machine_hash}")
    print(
        "Machine type match: "
        f"{machine_analysis.matches_observed} "
        f"(vmType={machine_analysis.vm_type}, source={machine_analysis.vm_type_source})"
    )
    print(f"Machine token:{machine.machine_token}")
    if login_result is not None:
        print(f"Login URL:    {login_result.get('loginUrl', '')}")
        print(f"Nonce:        {login_result.get('nonce', '')}")
        print(f"Verifier:     {login_result.get('verifier', '')}")
        print(f"Challenge:    {login_result.get('challenge', '')}")
        print(f"Method:       {login_result.get('challengeMethod', '')}")
    if device_login_params is not None:
        print(f"Device login: {json.dumps(device_login_params, ensure_ascii=False)}")
        print(f"Device result:{json.dumps(device_login_result, ensure_ascii=False)}")
    print(
        "Auth status:  "
        f"{status_name(final_status)} / whitelist={whitelist_name(final_status)}"
    )
    if args.dump_user_plan:
        if user_plan_error:
            print(f"User plan err:{user_plan_error}")
        else:
            print(f"User plan:    {json.dumps(user_plan_result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QoderError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        raise SystemExit(1)
