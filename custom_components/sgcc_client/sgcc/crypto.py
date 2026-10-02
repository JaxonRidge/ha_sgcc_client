"""SGCC 加解密."""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from gmssl import func, sm2, sm3, sm4
from .const import CLIENT_PUBLIC_KEY_HEX

HEX_32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
HEX_64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
HEX_128_RE = re.compile(r"^[0-9a-fA-F]{128}$")
HEX_130_RE = re.compile(r"^04[0-9a-fA-F]{128}$")

def compact_json(value: Any) -> str:
    # 对齐 App Gson 紧凑序列化
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

def generate_sm4_key_text() -> str:
    return uuid.uuid4().hex

def sm4_key_bytes(key_text: str) -> bytes:
    # App 不 hex 解码密钥, 取前 16 ASCII 字节
    if not HEX_32_RE.fullmatch(key_text):
        raise ValueError("SM4 key text must be exactly 32 hexadecimal characters")
    return key_text[:16].encode("ascii")

def sm4_encrypt_hex(plaintext: str | bytes, key_text: str) -> str:
    raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    cipher = sm4.CryptSM4()
    cipher.set_key(sm4_key_bytes(key_text), sm4.SM4_ENCRYPT)
    return cipher.crypt_ecb(raw).hex().upper()

def sm4_decrypt_hex(ciphertext_hex: str, key_text: str) -> bytes:
    cipher = sm4.CryptSM4()
    cipher.set_key(sm4_key_bytes(key_text), sm4.SM4_DECRYPT)
    return cipher.crypt_ecb(bytes.fromhex(ciphertext_hex))

def normalize_public_key(public_key_hex: str) -> str:
    if HEX_130_RE.fullmatch(public_key_hex):
        return public_key_hex[2:]
    if HEX_128_RE.fullmatch(public_key_hex):
        return public_key_hex
    raise ValueError("SM2 public key must be 128 hex chars, optionally prefixed by 04")

def sm2_encrypt_key(key_text: str, server_public_key_hex: str) -> str:
    if not HEX_32_RE.fullmatch(key_text):
        raise ValueError("request key must be a 32-character hexadecimal string")
    crypt = sm2.CryptSM2(
        private_key="",
        public_key=normalize_public_key(server_public_key_hex),
        mode=1,
    )
    return "04" + crypt.encrypt(key_text.encode("ascii")).hex().lower()

def derive_public_key(private_key_hex: str) -> str:
    if not HEX_64_RE.fullmatch(private_key_hex):
        raise ValueError("SM2 private key must be exactly 64 hexadecimal characters")
    probe = sm2.CryptSM2(private_key=private_key_hex, public_key="", mode=1)
    return probe._kg(int(private_key_hex, 16), probe.ecc_table["g"])

def sm2_decrypt_key(wrapped_key_hex: str, client_private_key_hex: str) -> str:
    if not HEX_64_RE.fullmatch(client_private_key_hex):
        raise ValueError("SM2 private key must be exactly 64 hexadecimal characters")
    wire = (
        wrapped_key_hex[2:]
        if wrapped_key_hex.lower().startswith("04")
        else wrapped_key_hex
    )
    # 公钥由私钥固定派生, 预置常量省去每次响应解密的椭圆曲线点乘
    crypt = sm2.CryptSM2(
        private_key=client_private_key_hex,
        public_key=CLIENT_PUBLIC_KEY_HEX,
        mode=1,
    )
    plaintext = crypt.decrypt(bytes.fromhex(wire))
    key_text = plaintext.decode("ascii")
    if not HEX_32_RE.fullmatch(key_text):
        raise ValueError("decrypted response key is not 32 hexadecimal characters")
    return key_text

def sm3_hex(text: str) -> str:
    return sm3.sm3_hash(func.bytes_to_list(text.encode("utf-8"))).lower()

def envelope_sign(skey: str, data: str, timestamp: str) -> str:
    return sm3_hex(skey + data + timestamp)

def build_request_envelope(
    payload: Mapping[str, Any],
    server_public_key_hex: str,
    *,
    timestamp: str | None = None,
    key_text: str | None = None,
) -> dict[str, str]:
    key_text = key_text or generate_sm4_key_text()
    timestamp = timestamp or str(int(time.time() * 1000))
    data = sm4_encrypt_hex(compact_json(payload), key_text)
    skey = sm2_encrypt_key(key_text, server_public_key_hex)
    return {
        "data": data,
        "sign": envelope_sign(skey, data, timestamp),
        "skey": skey,
        "timestamp": timestamp,
    }

def decrypt_response_envelope(
    envelope: Mapping[str, Any], client_private_key_hex: str
) -> Any:
    key_text = sm2_decrypt_key(str(envelope["respKey"]), client_private_key_hex)
    plaintext = sm4_decrypt_hex(str(envelope["encryptData"]), key_text)
    return json.loads(plaintext.decode("utf-8"))