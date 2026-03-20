#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import random
import socket
import struct
import time
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


BLOCK_KEY = b"650uWT4wglfzY43x"
BLOCK_BLOB = bytes.fromhex(
    "19d6e5fee84e594a2e73d7e5142e781b910b39a740d014a57fab5c11cd5b8452"
    "a715ce2ce62a76bd3ec1d005d77c9af1c34904cf5b6765f340b31ccab937d149"
    "6d5cb19937517b4962172852e03947b5022a222ce3fc5a707b7509d62c70644b"
    "296df7e8dda507c39b6e644be53e088ee663f2f9a4dddc3b5d1110a6af76dcd6"
    "481031bd92b338b5bcda63eb449dd7aa9b2aecb65bcdede588956ac8e6468009"
    "2cc0f8c760da1b984a5eba711850d433"
)
FIELD26_MAGIC = bytes.fromhex("cfcd")
HEADER_MAGIC = bytes.fromhex("cfcd002b")
ALG26_XOR = bytes.fromhex("9e666c394f33b61ca7195b8ed38c2dc7") + (0xE396C669).to_bytes(4, "little")
ALG27_XOR = bytes.fromhex("2f7b991297bd4c89cb5ea6c4652f53e2")
ALG28_XOR = (0x2D4B751D).to_bytes(4, "little") + bytes.fromhex("8ebaa59647dd524ba36ea9a551b7d4d2")
ALG29_XOR = bytes.fromhex("1d754b2d8ebaa59647dd524ba36ea9a5")

FIELD27_PROVIDER_IDS = [
    14,
    26,
    51,
    52,
    27,
    35,
    31,
    28,
    1,
    0,
    38,
    39,
    18,
    24,
    3,
    4,
    11,
    20,
    6,
    5,
    91,
    23,
    22,
    40,
    21,
    47,
    41,
    174,
    13,
    16,
    15,
    17,
    92,
    72,
    75,
    54,
    53,
]
FIELD28_PROVIDER_IDS = [56, 57, 58, 59, 55]


def _aes_cbc_encrypt(key: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size:
        raise ValueError("invalid padded payload length")
    pad_len = data[-1]
    if pad_len == 0 or pad_len > block_size or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid PKCS#7 padding")
    return data[:-pad_len]


def _u16be(value: int) -> bytes:
    return struct.pack(">H", value)


def _u32be(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def _u32le(value: int) -> bytes:
    return struct.pack("<I", value & 0xFFFFFFFF)


def _words32_le(data: bytes) -> list[int]:
    if len(data) % 4:
        raise ValueError("data length must be a multiple of 4")
    return list(struct.unpack("<" + "I" * (len(data) // 4), data))


def _bytes_from_words(words: list[int]) -> bytes:
    return struct.pack("<" + "I" * len(words), *[word & 0xFFFFFFFF for word in words])


def _shuffle_4e(words: list[int]) -> list[int]:
    if len(words) != 4:
        raise ValueError("4-word shuffle expects 4 words")
    return [words[2], words[3], words[0], words[1]]


def _shuffle_39(words: list[int]) -> list[int]:
    if len(words) != 4:
        raise ValueError("4-word shuffle expects 4 words")
    return [words[1], words[2], words[3], words[0]]


def _rotl_160(data: bytes, bits: int) -> bytes:
    value = int.from_bytes(data, "little")
    mask = (1 << 160) - 1
    rotated = ((value << bits) & mask) | (value >> (160 - bits))
    return rotated.to_bytes(20, "little")


def _rotr_160(data: bytes, bits: int) -> bytes:
    value = int.from_bytes(data, "little")
    mask = (1 << 160) - 1
    rotated = ((value >> bits) | ((value << (160 - bits)) & mask)) & mask
    return rotated.to_bytes(20, "little")


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    if len(left) != len(right):
        raise ValueError("xor inputs must have the same length")
    return bytes(a ^ b for a, b in zip(left, right, strict=True))


def _shrd32(dest: int, src: int, bits: int) -> int:
    return ((dest >> bits) | ((src << (32 - bits)) & 0xFFFFFFFF)) & 0xFFFFFFFF


def _shufps(dst: list[int], src: list[int], imm8: int) -> list[int]:
    return [
        dst[(imm8 >> 0) & 3],
        dst[(imm8 >> 2) & 3],
        src[(imm8 >> 4) & 3],
        src[(imm8 >> 6) & 3],
    ]


def _pshufd(src: list[int], imm8: int) -> list[int]:
    return [src[(imm8 >> (2 * index)) & 3] for index in range(4)]


def _pslld(words: list[int], bits: int) -> list[int]:
    return [((word << bits) & 0xFFFFFFFF) for word in words]


def _psrld(words: list[int], bits: int) -> list[int]:
    return [((word >> bits) & 0xFFFFFFFF) for word in words]


def _por(left: list[int], right: list[int]) -> list[int]:
    return [((a | b) & 0xFFFFFFFF) for a, b in zip(left, right, strict=True)]


def _pxor_words(left: list[int], right: list[int]) -> list[int]:
    return [((a ^ b) & 0xFFFFFFFF) for a, b in zip(left, right, strict=True)]


def _movd_to_xmm(value: int) -> list[int]:
    return [value & 0xFFFFFFFF, 0, 0, 0]


def _alg26_block_encode(block: bytes) -> bytes:
    a, b, c, d, e = _words32_le(block)
    xmm0 = [b, c, d, e]
    xmm1 = _pshufd(xmm0, 0xEE)
    ecx = _shrd32(xmm1[0], a, 0x19)
    xmm1 = _movd_to_xmm(a)
    xmm1 = _shufps(xmm1, xmm0, 0xF4)
    xmm1 = _shufps(xmm1, xmm0, 0x42)
    xmm1 = _psrld(xmm1, 0x19)
    xmm0 = _por(_pslld(xmm0, 0x07), xmm1)
    xmm1 = _pshufd(xmm0, 0xEE)
    eax = _shrd32(xmm1[0], ecx, 0x03)
    xmm1 = _movd_to_xmm(ecx)
    xmm1 = _shufps(xmm1, xmm0, 0xF4)
    xmm1 = _shufps(xmm1, xmm0, 0x42)
    xmm1 = _psrld(xmm1, 0x03)
    xmm0 = _por(_pslld(xmm0, 0x1D), xmm1)
    xmm1 = _movd_to_xmm(eax)
    xmm1 = _shufps(xmm1, xmm0, 0xF4)
    xmm1 = _shufps(xmm1, xmm0, 0x42)
    xmm1 = _psrld(xmm1, 0x0D)
    xmm2 = _pshufd(xmm0, 0xEE)
    xmm0 = _por(_pslld(xmm0, 0x13), xmm1)
    ecx = _shrd32(xmm2[0], eax, 0x0D)
    xor_words = _words32_le(ALG26_XOR)
    xmm0 = _pxor_words(xmm0, xor_words[:4])
    return _bytes_from_words(xmm0 + [ecx ^ xor_words[4]])


def _alg26_block_decode(block: bytes) -> bytes:
    a, b, c, d, e = _words32_le(block)
    xor_words = _words32_le(ALG26_XOR)
    eax = xor_words[4] ^ e
    xmm1 = _pxor_words([a, b, c, d], xor_words[:4])
    xmm0 = _pshufd(xmm1, 0xEE)
    ecx = _shrd32(xmm0[0], eax, 0x13)
    xmm0 = _movd_to_xmm(eax)
    xmm0 = _shufps(xmm0, xmm1, 0xF4)
    xmm0 = _shufps(xmm0, xmm1, 0x42)
    xmm0 = _psrld(xmm0, 0x13)
    xmm1 = _pslld(xmm1, 0x0D)
    xmm0 = _por(xmm0, xmm1)
    xmm2 = _pshufd(xmm1, 0xEE)
    eax = _shrd32(xmm2[0], ecx, 0x1D)
    xmm2 = _movd_to_xmm(ecx)
    xmm2 = _shufps(xmm2, xmm1, 0xF4)
    xmm2 = _shufps(xmm2, xmm1, 0x42)
    xmm0 = _pslld(xmm0, 0x03)
    xmm2 = _psrld(xmm2, 0x1D)
    xmm2 = _por(xmm2, xmm0)
    xmm1 = _pshufd(xmm0, 0xEE)
    ecx = _shrd32(xmm1[0], eax, 0x07)
    xmm1 = _movd_to_xmm(eax)
    xmm1 = _shufps(xmm1, xmm0, 0xF4)
    xmm1 = _shufps(xmm1, xmm0, 0x42)
    xmm1 = _psrld(xmm1, 0x07)
    xmm2 = _por(_pslld(xmm2, 0x19), xmm1)
    return _bytes_from_words(xmm2 + [ecx])


def _alg27_block_encode(block: bytes) -> bytes:
    xmm0 = _words32_le(block)
    xmm1 = _pshufd(xmm0, 0x4E)
    xmm0 = _pslld(xmm0, 0x11)
    xmm1 = _psrld(xmm1, 0x0F)
    xmm0 = _por(xmm1, xmm0)
    xmm0 = _pshufd(xmm0, 0x39)
    return _bytes_from_words(_pxor_words(xmm0, _words32_le(ALG27_XOR)))


def _alg27_block_decode(block: bytes) -> bytes:
    xmm0 = _pxor_words(_words32_le(block), _words32_le(ALG27_XOR))
    xmm1 = _pshufd(xmm0, 0x4E)
    xmm0 = _pslld(xmm0, 0x0F)
    xmm1 = _psrld(xmm1, 0x11)
    xmm1 = _por(xmm1, xmm0)
    return _bytes_from_words(_pshufd(xmm1, 0x39))


def _alg28_block_encode(block: bytes) -> bytes:
    a, b, c, d, e = _words32_le(block)
    xor_words = _words32_le(ALG28_XOR)
    eax = xor_words[0] ^ a
    xmm0 = _pxor_words([b, c, d, e], xor_words[1:])
    xmm1 = _movd_to_xmm(eax)
    xmm1 = _shufps(xmm1, xmm0, 0xF4)
    xmm1 = _shufps(xmm1, xmm0, 0x42)
    xmm1 = _psrld(xmm1, 0x12)
    xmm2 = _pshufd(xmm0, 0xEE)
    xmm0 = _pslld(xmm0, 0x0E)
    xmm0 = _por(xmm0, xmm1)
    ecx = _shrd32(xmm2[0], eax, 0x12)
    return _bytes_from_words(xmm0 + [ecx])


def _alg28_block_decode(block: bytes) -> bytes:
    a, b, c, d, e = _words32_le(block)
    xmm0 = [b, c, d, e]
    xmm1 = _pshufd(xmm0, 0xEE)
    ecx = _shrd32(xmm1[0], a, 0x0E)
    xmm1 = _movd_to_xmm(a)
    xmm1 = _shufps(xmm1, xmm0, 0xF4)
    xmm1 = _shufps(xmm1, xmm0, 0x42)
    xmm1 = _psrld(xmm1, 0x0E)
    xmm0 = _pslld(xmm0, 0x12)
    xmm0 = _por(xmm0, xmm1)
    xor_words = _words32_le(ALG28_XOR)
    xmm0 = _pxor_words(xmm0, xor_words[1:])
    return _bytes_from_words([ecx ^ 0xD2D4B751] + xmm0)


def _alg29_block_encode(block: bytes) -> bytes:
    xmm0 = _pxor_words(_words32_le(block), _words32_le(ALG29_XOR))
    xmm1 = _pshufd(xmm0, 0x4E)
    xmm0 = _pslld(xmm0, 0x0E)
    xmm1 = _psrld(xmm1, 0x12)
    xmm1 = _por(xmm1, xmm0)
    return _bytes_from_words(xmm1)


def _alg29_block_decode(block: bytes) -> bytes:
    xmm0 = _words32_le(block)
    xmm1 = _pshufd(xmm0, 0x4E)
    xmm0 = _psrld(xmm0, 0x0E)
    xmm1 = _pslld(xmm1, 0x12)
    xmm1 = _por(xmm1, xmm0)
    return _bytes_from_words(_pxor_words(xmm1, _words32_le(ALG29_XOR)))


def _algo_params(algo_id: int) -> tuple[int, callable[[bytes], bytes], callable[[bytes], bytes]]:
    if algo_id == 26:
        return 20, _alg26_block_encode, _alg26_block_decode
    if algo_id == 27:
        return 16, _alg27_block_encode, _alg27_block_decode
    if algo_id == 28:
        return 20, _alg28_block_encode, _alg28_block_decode
    if algo_id == 29:
        return 16, _alg29_block_encode, _alg29_block_decode
    raise ValueError(f"unsupported algorithm id: {algo_id}")


def encode_type3(payload: bytes, algo_id: int, *, prefix_value: int | None = None) -> bytes:
    block_size, transform, _ = _algo_params(algo_id)
    padded = _pkcs7_pad(payload, block_size)
    transformed = b"".join(transform(padded[offset : offset + block_size]) for offset in range(0, len(padded), block_size))
    if prefix_value is None:
        prefix_value = random.getrandbits(32)
    prefix = _u32le(prefix_value)
    masked = bytearray(transformed)
    for index in range(len(masked)):
        masked[index] ^= prefix[(index + 2) % 4]
    return bytes([algo_id]) + prefix + bytes(masked)


def decode_type4(payload: bytes) -> bytes:
    if not payload:
        raise ValueError("type4 payload is empty")
    algo_id = payload[0]
    block_size, _, inverse = _algo_params(algo_id)
    encoded = bytearray(payload[1:])
    if len(encoded) < 4:
        raise ValueError("type4 payload is shorter than its 4-byte prefix")
    prefix = bytes(encoded[:4])
    transformed = bytearray(encoded[4:])
    for index in range(len(transformed)):
        transformed[index] ^= prefix[(index + 2) % 4]
    transformed = bytes(transformed)
    if len(transformed) % block_size:
        raise ValueError("type4 payload block size mismatch")
    padded = b"".join(inverse(transformed[offset : offset + block_size]) for offset in range(0, len(transformed), block_size))
    return _pkcs7_unpad(padded, block_size)


def _decrypt_block_blob() -> bytes:
    return _pkcs7_unpad(_aes_cbc_decrypt(BLOCK_KEY, BLOCK_BLOB))


def get_block_table() -> list[bytes]:
    plaintext = _decrypt_block_blob()
    if len(plaintext) != 160:
        raise ValueError(f"unexpected block blob plaintext length: {len(plaintext)}")
    return [plaintext[offset : offset + 16] for offset in range(0, len(plaintext), 16)]


def select_wrap_key(index: int) -> bytes:
    table = get_block_table()
    if not 0 <= index < len(table):
        raise ValueError(f"invalid wrap key index: {index}")
    return table[index]


def wrap_cipher_payload(payload: bytes, *, key_index: int | None = None) -> bytes:
    if key_index is None:
        key_index = random.randrange(10)
    key = select_wrap_key(key_index)
    ciphertext = _aes_cbc_encrypt(key, _pkcs7_pad(payload))
    return bytes([key_index + 1]) + ciphertext


def unwrap_cipher_payload(payload: bytes) -> bytes:
    if not payload:
        raise ValueError("cipher payload is empty")
    key_index = payload[0] - 1
    key = select_wrap_key(key_index)
    return _pkcs7_unpad(_aes_cbc_decrypt(key, payload[1:]))


def encode_inner_payload(payload: bytes, *, key_index: int | None = None) -> bytes:
    return wrap_cipher_payload(zlib.compress(payload), key_index=key_index)


def decode_response_dt(dt_value: str) -> bytes:
    return unwrap_cipher_payload(base64.b64decode(dt_value))


def decode_response_dt_json(dt_value: str) -> dict[str, object]:
    return json.loads(decode_response_dt(dt_value).decode("utf-8"))


def extract_machine_token_from_dt(dt_value: str) -> str:
    payload = decode_response_dt_json(dt_value)
    eeid = payload.get("eeid")
    if not isinstance(eeid, str) or not eeid:
        raise ValueError("repPc dt 中未找到有效 eeid")
    return eeid


def _length_prefixed_text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return _u16be(len(raw)) + raw


def _length_prefixed_blob(value: bytes) -> bytes:
    return _u16be(len(value)) + value


def _read_first_line(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
    except (FileNotFoundError, IndexError, OSError):
        return ""


def _mac_hex() -> str:
    for name in sorted(os.listdir("/sys/class/net")):
        if name == "lo":
            continue
        address = _read_first_line(f"/sys/class/net/{name}/address").replace(":", "").strip()
        if address and address != "000000000000":
            return address
    return ""


def _bios_triplet() -> str:
    vendor = _read_first_line("/sys/class/dmi/id/bios_vendor")
    version = _read_first_line("/sys/class/dmi/id/bios_version")
    date = _read_first_line("/sys/class/dmi/id/bios_date")
    parts = [part for part in [vendor, version, date] if part]
    return "-".join(parts)


def _cpu_id() -> str:
    sysfs_value = _read_first_line("/sys/class/dmi/id/product_uuid")
    if sysfs_value:
        return sysfs_value.replace(" ", "-")
    return ""


def _root_mount_source() -> str:
    try:
        with open("/etc/mtab", "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    return parts[0].rstrip("0123456789")
    except OSError:
        return ""
    return ""


def _all_mac_hex() -> str:
    values: list[str] = []
    for name in sorted(os.listdir("/sys/class/net")):
        if name == "lo":
            continue
        address = _read_first_line(f"/sys/class/net/{name}/address").replace(":", "").strip()
        if address and address != "000000000000":
            values.append(address)
    return "    ".join(values)


def _proc_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


@dataclass(slots=True)
class ProviderContext:
    event_code: int = 1000
    os_code: int = 8
    region_id: int = 3
    counter: int = 0
    product_name: str = "Qoder"
    product_version: str = "NA"
    sdk_version: str = "1.5.0"
    product_type: int = 1
    field27_mode: int = 1
    field27_platform_code: int = 4
    field27_persisted_timestamp_ms: int | None = None
    cached_field26: bytes = b""
    cached_field29_prefix: bytes = b""
    provider_overrides: dict[int, str] = field(default_factory=dict)

    def provider_value(self, provider_id: int) -> str:
        if provider_id in self.provider_overrides:
            return self.provider_overrides[provider_id]
        if provider_id in {0, 31, 35}:
            return _mac_hex()
        if provider_id == 1:
            return _mac_hex()[:8]
        if provider_id in {11, 20}:
            mac = _mac_hex()
            return f"00000000-0000-0000-0000-{mac}" if mac else ""
        if provider_id == 14:
            return "Linux"
        if provider_id == 18:
            return ""
        if provider_id == 23:
            return ""
        if provider_id == 24:
            return socket.gethostname()
        if provider_id == 26:
            return _proc_text("/proc/version")
        if provider_id == 38:
            return ""
        if provider_id == 41:
            return _all_mac_hex()
        if provider_id == 51:
            return self.product_name
        if provider_id == 52:
            return ""
        if provider_id == 4:
            return ""
        if provider_id == 5:
            return ""
        if provider_id == 6:
            return ""
        if provider_id == 22:
            return _proc_text("/proc/meminfo")
        if provider_id == 82:
            return _read_first_line("/sys/class/dmi/id/board_name")
        if provider_id == 106:
            return _proc_text("/proc/devices")
        return ""


def _gather_provider_blob(provider_ids: list[int], context: ProviderContext) -> bytes:
    return b"".join(_length_prefixed_text(context.provider_value(provider_id)) for provider_id in provider_ids)


def build_field26(context: ProviderContext, *, prefix_value: int | None = None) -> bytes:
    raw = _length_prefixed_blob(context.cached_field26)
    payload = encode_type3(raw, 26, prefix_value=prefix_value)
    return FIELD26_MAGIC + _u32be(len(payload) + 6) + payload


def build_field27(
    context: ProviderContext,
    *,
    now_ms: int | None = None,
    prefix_value: int | None = None,
) -> bytes:
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    persisted_timestamp_ms = context.field27_persisted_timestamp_ms
    if persisted_timestamp_ms is None:
        persisted_timestamp_ms = now_ms
    raw = b"".join(
        [
            _length_prefixed_text(str(context.event_code)),
            _length_prefixed_text(str(now_ms)),
            _length_prefixed_text(str(persisted_timestamp_ms)),
            _length_prefixed_text(str(context.field27_mode)),
            _length_prefixed_text(str(context.field27_platform_code)),
            _length_prefixed_text(context.sdk_version),
            _gather_provider_blob(FIELD27_PROVIDER_IDS, context),
        ]
    )
    payload = encode_type3(raw, 27, prefix_value=prefix_value)
    return FIELD26_MAGIC + _u32be(len(payload) + 6) + payload


def build_field28(context: ProviderContext, *, prefix_value: int | None = None) -> bytes:
    payload = encode_type3(_gather_provider_blob(FIELD28_PROVIDER_IDS, context), 28, prefix_value=prefix_value)
    return FIELD26_MAGIC + _u32be(len(payload) + 6) + payload


def build_field29(context: ProviderContext, *, prefix_value: int | None = None) -> bytes:
    provider29 = context.provider_value(29)
    provider78 = context.provider_value(78)
    provider76 = context.provider_value(76)
    raw = b"".join(
        [
            _length_prefixed_blob(context.cached_field29_prefix),
            _length_prefixed_text(f"{provider29}##{provider78}##{provider76}"),
            b"\x00\x00",
        ]
    )
    payload = encode_type3(raw, 29, prefix_value=prefix_value)
    return FIELD26_MAGIC + _u32be(len(payload) + 6) + payload


def build_pre_zlib_request(
    context: ProviderContext,
    *,
    now_ms: int | None = None,
    random_seed: int | None = None,
    field_prefixes: dict[int, int] | None = None,
) -> bytes:
    if random_seed is None:
        random_seed = random.getrandbits(32)
    if field_prefixes is None:
        field_prefixes = {}
    fields = b"".join(
        [
            build_field26(context, prefix_value=field_prefixes.get(26)),
            build_field27(context, now_ms=now_ms, prefix_value=field_prefixes.get(27)),
            build_field28(context, prefix_value=field_prefixes.get(28)),
            build_field29(context, prefix_value=field_prefixes.get(29)),
        ]
    )
    seed_be = _u32be(random_seed)
    header = HEADER_MAGIC + seed_be + _u32be(len(fields) + 12)
    request = bytearray(header + fields)
    for index in range(len(header), len(request)):
        request[index] ^= seed_be[(index - len(header)) % 4]
    return bytes(request)


def build_rep_pc_body(
    context: ProviderContext,
    *,
    now_ms: int | None = None,
    random_seed: int | None = None,
    key_index: int | None = None,
    field_prefixes: dict[int, int] | None = None,
) -> bytes:
    wrapped = encode_inner_payload(
        build_pre_zlib_request(
            context,
            now_ms=now_ms,
            random_seed=random_seed,
            field_prefixes=field_prefixes,
        ),
        key_index=key_index,
    )
    return base64.b64encode(wrapped)


def build_rep_pc_url(context: ProviderContext) -> str:
    region_map = {
        1: "https://pre-umid.alibaba-inc.com/repPc.json",
        2: "https://umid.daily.alibaba.net/repPc.json",
        3: "https://us-pum.alibabachengdun.com/repPc.json",
        4: "https://sg-pum.alibabachengdun.com/repPc.json",
        5: "https://sg-pum.alibabachengdun.com/repPc.json",
    }
    base_url = region_map.get(context.region_id, "https://pum.m.alibaba.com/repPc.json")
    return (
        f"{base_url}?e={context.event_code}"
        f"&pn={context.product_name}"
        f"&os=8"
        f"&sv={context.sdk_version}"
        f"&pv={context.product_version}"
        f"&pt={context.product_type}"
    )


def post_rep_pc(
    context: ProviderContext,
    *,
    now_ms: int | None = None,
    random_seed: int | None = None,
    key_index: int | None = None,
    field_prefixes: dict[int, int] | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    url = build_rep_pc_url(context)
    body = build_rep_pc_body(
        context,
        now_ms=now_ms,
        random_seed=random_seed,
        key_index=key_index,
        field_prefixes=field_prefixes,
    )
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Connection": "close",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _self_test() -> None:
    sample = b"qoder-pure-protocol-self-test"
    for algo_id in [26, 27, 29]:
        encoded = encode_type3(sample, algo_id, prefix_value=0x11223344)
        decoded = decode_type4(encoded)
        if decoded != sample:
            raise AssertionError(f"type3/type4 roundtrip failed for algo {algo_id}")
    field28 = build_field28(ProviderContext(), prefix_value=0x11223344)
    expected_field28 = bytes.fromhex("cfcd0000001f1c4433221115a7e75d70da15048b746eea74c1129cca7a03ee")
    if field28 != expected_field28:
        raise AssertionError("field28 deterministic encode check failed")
    for key_index in range(10):
        wrapped = wrap_cipher_payload(sample, key_index=key_index)
        if unwrap_cipher_payload(wrapped) != sample:
            raise AssertionError(f"cipher wrap roundtrip failed for key {key_index}")


if __name__ == "__main__":
    _self_test()
    print("qoder_pure_protocol self-test ok")
