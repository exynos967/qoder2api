#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import random
import string
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from qoder_pure_protocol import ProviderContext, extract_machine_token_from_dt, post_rep_pc

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "temp"
MASK64 = 0xFFFFFFFFFFFFFFFF
XXH64_PRIME1 = 0x9E3779B185EBCA87
XXH64_PRIME2 = 0xC2B2AE3D27D4EB4F
XXH64_PRIME3 = 0x165667B19E3779F9
XXH64_PRIME4 = 0x85EBCA77C2B2AE63
XXH64_PRIME5 = 0x27D4EB2F165667C5


@dataclass(slots=True)
class PureDeviceResult:
    attempt: int
    ec: int
    success: bool
    machine_token: str
    machine_type: str
    mac: str
    hostname: str
    persisted_timestamp_ms: int
    now_ms: int
    response_path: str
    error: str | None = None


def build_output_dir(root: Path, prefix: str) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    output_dir = root / f"{prefix}_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


def random_mac(rng: random.Random) -> str:
    octets = [0x02, rng.randrange(0, 256), rng.randrange(0, 256), rng.randrange(0, 256), rng.randrange(0, 256), rng.randrange(0, 256)]
    return ":".join(f"{octet:02x}" for octet in octets)


def random_hostname(prefix: str, rng: random.Random, attempt: int) -> str:
    suffix = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"{prefix}-{attempt:03d}-{suffix}"


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
        result = (rotl64(v1, 1) + rotl64(v2, 7) + rotl64(v3, 12) + rotl64(v4, 18)) & MASK64
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


def build_machine_type(machine_token: str, vm_type: int) -> str:
    machine_hash = f"{xxh64_int(machine_token.encode('utf-8')):016x}"
    return f"{machine_hash[:8]}{vm_type:02d}{machine_hash[8:]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="纯协议批量生成新的 Qoder 设备身份。")
    parser.add_argument("--count", type=int, default=5, help="目标设备数量，默认 5")
    parser.add_argument("--seed", type=int, help="随机种子，默认使用当前时间戳")
    parser.add_argument("--region-id", type=int, default=0, help="repPc 区域 ID，默认 0（pum.m.alibaba.com）")
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"输出根目录，默认 {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument("--output-prefix", default="qoder_xieyi_pure_batch", help="输出目录前缀")
    parser.add_argument("--hostname-prefix", default="pure-host", help="生成 hostname 时使用的前缀")
    parser.add_argument("--persisted-min-ms", type=int, default=60_000, help="持久时间戳最小回退毫秒数，默认 60000")
    parser.add_argument("--persisted-max-ms", type=int, default=3_600_000, help="持久时间戳最大回退毫秒数，默认 3600000")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次 repPc 请求超时秒数，默认 30")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    return parser.parse_args()


def build_context(region_id: int, hostname: str, mac_compact: str, persisted_timestamp_ms: int) -> ProviderContext:
    return ProviderContext(
        region_id=region_id,
        field27_persisted_timestamp_ms=persisted_timestamp_ms,
        provider_overrides={
            0: mac_compact,
            1: mac_compact[:8],
            11: f"00000000-0000-0000-0000-{mac_compact}",
            20: f"00000000-0000-0000-0000-{mac_compact}",
            24: hostname,
            31: mac_compact,
            35: mac_compact,
            41: f"{mac_compact}    ",
        },
    )


def write_csv(path: Path, results: list[PureDeviceResult]) -> None:
    fieldnames = [
        "attempt",
        "ec",
        "success",
        "machine_token",
        "machine_type",
        "mac",
        "hostname",
        "persisted_timestamp_ms",
        "now_ms",
        "response_path",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            writer.writerow(asdict(item))


def run_attempt(
    output_dir: Path,
    attempt: int,
    *,
    hostname_prefix: str,
    region_id: int,
    rng: random.Random,
    persisted_min_ms: int,
    persisted_max_ms: int,
    timeout: float,
) -> PureDeviceResult:
    mac = random_mac(rng).replace(":", "")
    hostname = random_hostname(hostname_prefix, rng, attempt)
    now_ms = int(time.time() * 1000)
    persisted_timestamp_ms = now_ms - rng.randint(persisted_min_ms, persisted_max_ms)
    response_path = output_dir / "responses" / f"device_{attempt:04d}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        context = build_context(region_id, hostname, mac, persisted_timestamp_ms)
        payload = post_rep_pc(
            context,
            now_ms=now_ms,
            random_seed=rng.getrandbits(32),
            key_index=rng.randrange(10),
            timeout=timeout,
        )
        response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        ec = int(payload.get("ec", -1))
        if ec != 200 or "dt" not in payload:
            return PureDeviceResult(
                attempt=attempt,
                ec=ec,
                success=False,
                machine_token="",
                machine_type="",
                mac=mac,
                hostname=hostname,
                persisted_timestamp_ms=persisted_timestamp_ms,
                now_ms=now_ms,
                response_path=str(response_path),
                error=f"repPc ec={ec}",
            )
        machine_token = extract_machine_token_from_dt(str(payload["dt"]))
        return PureDeviceResult(
            attempt=attempt,
            ec=ec,
            success=True,
            machine_token=machine_token,
            machine_type=build_machine_type(machine_token, 91),
            mac=mac,
            hostname=hostname,
            persisted_timestamp_ms=persisted_timestamp_ms,
            now_ms=now_ms,
            response_path=str(response_path),
        )
    except Exception as exc:
        return PureDeviceResult(
            attempt=attempt,
            ec=-1,
            success=False,
            machine_token="",
            machine_type="",
            mac=mac,
            hostname=hostname,
            persisted_timestamp_ms=persisted_timestamp_ms,
            now_ms=now_ms,
            response_path=str(response_path),
            error=str(exc),
        )


def main() -> int:
    args = parse_args()
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)

    output_root = Path(args.output_root).resolve()
    output_dir = build_output_dir(output_root, args.output_prefix)
    results = [
        run_attempt(
            output_dir,
            attempt,
            hostname_prefix=args.hostname_prefix,
            region_id=args.region_id,
            rng=rng,
            persisted_min_ms=args.persisted_min_ms,
            persisted_max_ms=args.persisted_max_ms,
            timeout=args.timeout,
        )
        for attempt in range(1, args.count + 1)
    ]

    csv_path = output_dir / "devices.csv"
    write_csv(csv_path, results)
    summary = {
        "output_dir": str(output_dir),
        "csv": str(csv_path),
        "seed": seed,
        "count": args.count,
        "success_count": sum(1 for item in results if item.success),
        "results": [asdict(item) for item in results],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"summary: {summary_path}")
        print(f"success_count: {summary['success_count']}/{args.count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
