"""Qoder 独立登录与签名相关的加密工具。"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from functools import lru_cache
from typing import Any, Mapping

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_AUTH_VERSION = "v1"
_DEFAULT_CLIENT_VERSION = "0.1.32"
_DEFAULT_COSY_VERSION = "0.14.2"
_RSA_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----
"""


def derive_machine_cipher_key(machine_id: str) -> str:
    """取 Qoder machine_id 的前 16 个字符作为 AES key/iv。"""
    normalized = (machine_id or "").strip()
    if len(normalized) < 16:
        raise ValueError("machine_id 长度不足 16，无法解密 Qoder 登录态")
    return normalized[:16]


def _as_aes_key(key_text: str) -> bytes:
    key = derive_machine_cipher_key(key_text).encode("utf-8")
    if len(key) != 16:
        raise ValueError("Qoder AES key 必须为 16 字节")
    return key


def aes_encrypt_base64(text: str, key_text: str) -> str:
    """使用 AES-CBC + PKCS7 加密后输出 base64。"""
    raw_key = _as_aes_key(key_text)
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(text.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(raw_key), modes.CBC(raw_key)).encryptor()
    cipher_bytes = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(cipher_bytes).decode("utf-8")


def aes_decrypt_base64(cipher_text: str, key_text: str) -> str:
    """解密 Qoder `.auth/user` 风格的密文。"""
    raw_key = _as_aes_key(key_text)
    try:
        cipher_bytes = base64.b64decode(cipher_text.strip())
    except Exception as exc:  # pragma: no cover - 只在坏输入时触发
        raise ValueError("Qoder 用户文件不是合法 base64") from exc

    decryptor = Cipher(algorithms.AES(raw_key), modes.CBC(raw_key)).decryptor()
    padded = decryptor.update(cipher_bytes) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        plain_bytes = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise ValueError("Qoder 用户文件解密失败，可能 machine_id 不匹配") from exc
    return plain_bytes.decode("utf-8")


@lru_cache(maxsize=1)
def _load_public_key():
    return serialization.load_pem_public_key(_RSA_PUBLIC_KEY_PEM)


def rsa_encrypt_base64(raw: bytes) -> str:
    """使用 Qoder 内置 RSA 公钥加密后输出 base64。"""
    encrypted = _load_public_key().encrypt(raw, asym_padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


def _normalize_mapping(payload: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(by_alias=True, exclude_none=True)
    raise TypeError(f"不支持的 payload 类型: {type(payload)!r}")


def _generate_temp_aes_key() -> str:
    return uuid.uuid4().hex[:16]


def generate_auth_fields(cached_user: Mapping[str, Any] | Any) -> tuple[str, str]:
    """生成 `encrypt_user_info` 和 `key`。"""
    payload = _normalize_mapping(cached_user)
    temp_key = _generate_temp_aes_key()
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encrypt_user_info = aes_encrypt_base64(plain, temp_key)
    encrypted_key = rsa_encrypt_base64(temp_key.encode("utf-8"))
    return encrypt_user_info, encrypted_key


def build_auth_payload(
    encrypt_user_info: str,
    *,
    ide_version: str = "",
    client_version: str = _DEFAULT_CLIENT_VERSION,
    request_id: str | None = None,
    cosy_version: str = _DEFAULT_COSY_VERSION,
    version: str = _AUTH_VERSION,
) -> str:
    """构造官方 COSY payload，并按 Qoder 规则做 base64。"""
    del client_version  # 兼容旧调用签名；官方 infer 鉴权不再使用该字段。
    raw = {
        "cosyVersion": cosy_version,
        "ideVersion": ide_version,
        "info": encrypt_user_info,
        "requestId": request_id or str(uuid.uuid4()),
        "version": version,
    }
    encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(encoded).decode("utf-8")


def build_auth_signature(
    payload: str,
    *,
    key: str,
    date: str,
    body: str,
    path: str,
) -> str:
    """按 Qoder COSY 规则生成签名。"""
    normalized_path = path.split("?", 1)[0]
    sign_input = f"{payload}\n{key}\n{date}\n{body}\n{normalized_path}"
    return hashlib.md5(sign_input.encode("utf-8")).hexdigest()


def build_bearer_token(
    *,
    encrypt_user_info: str,
    key: str,
    date: str,
    body: str,
    path: str,
    ide_version: str = "",
    client_version: str = _DEFAULT_CLIENT_VERSION,
) -> tuple[str, str]:
    """返回 `(payload, authorization)`。"""
    payload = build_auth_payload(
        encrypt_user_info,
        ide_version=ide_version,
        client_version=client_version,
    )
    signature = build_auth_signature(payload, key=key, date=date, body=body, path=path)
    return payload, f"Bearer COSY.{payload}.{signature}"


def build_pkce_challenge(verifier: str) -> str:
    """构造 S256 PKCE challenge。"""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def generate_pkce_verifier() -> str:
    """生成 PKCE verifier。"""
    return base64.urlsafe_b64encode(
        uuid.uuid4().bytes + uuid.uuid4().bytes,
    ).decode("utf-8").rstrip("=")
