import base64
import hashlib
import json

from qoder2api.crypto import (
    build_auth_payload,
    build_auth_signature,
    generate_auth_fields,
)


def test_build_auth_signature_matches_cosy_formula() -> None:
    payload = "payload-demo"
    key = "key-demo"
    date = "1700000000"
    body = '{"hello":"world"}'
    path = "/api/v1/demo?x=1"

    expected = hashlib.md5(
        f"{payload}\n{key}\n{date}\n{body}\n/api/v1/demo".encode("utf-8"),
    ).hexdigest()

    assert build_auth_signature(
        payload,
        key=key,
        date=date,
        body=body,
        path=path,
    ) == expected


def test_build_auth_payload_is_base64_json() -> None:
    payload = build_auth_payload("encrypted-user", request_id="req-1", client_version="0.1.32")
    decoded = json.loads(base64.b64decode(payload).decode("utf-8"))

    assert decoded["requestId"] == "req-1"
    assert decoded["info"] == "encrypted-user"
    assert decoded["cosyVersion"] == "0.14.2"
    assert decoded["version"] == "v1"
    assert "cli_version" not in decoded


def test_generate_auth_fields_returns_cipher_and_rsa_key() -> None:
    encrypt_user_info, key = generate_auth_fields({"uid": "u-1", "name": "demo"})

    assert encrypt_user_info
    assert key
    assert encrypt_user_info != key
