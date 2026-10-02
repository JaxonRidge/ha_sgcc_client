"""SGCC 特征摘要算法."""
from __future__ import annotations

from collections.abc import Mapping

_MULTIPLIER = 0xC6A4A7935BD1E995
_LCG_MULTIPLIER = 214_013
_LCG_INCREMENT = 2_531_011
_MASK_64 = 0xFFFFFFFFFFFFFFFF

def _u64(value: int) -> int:
    return value & _MASK_64

def _mix47(value: int) -> int:
    value = _u64(value)
    return _u64(value ^ (value >> 47))

def turing_hash64(data: bytes, seed: int = 0) -> int:
    # 移植自 App 原生 sub_40f154, 逐位对齐
    raw = bytes(data)
    length = len(raw)
    state = _u64(_u64(length) * _MULTIPLIER) ^ _u64(seed)
    block_count = length // 8

    for index in range(block_count):
        word = int.from_bytes(raw[index * 8 : index * 8 + 8], "little")
        word = _u64(_mix47(_u64(word * _MULTIPLIER)) * _MULTIPLIER)
        state = _u64((word ^ state) * _MULTIPLIER)

    tail = raw[block_count * 8 :]
    if len(tail) >= 7:
        state ^= tail[6] << 48
    if len(tail) >= 6:
        state ^= tail[5] << 40
    if len(tail) >= 5:
        state ^= tail[4] << 32
    if len(tail) >= 4:
        state ^= tail[3] << 24
    if len(tail) >= 3:
        state ^= tail[2] << 16
    if len(tail) >= 2:
        # This unusual AND is present in the ARM64 implementation.
        state ^= 0x118727C03FB9FFC6 & (tail[1] << 8)
    if tail:
        state ^= tail[0]
        state = _u64(state * _MULTIPLIER)

    state = _mix47(state)
    state = _u64(state * _MULTIPLIER)
    return _mix47(state)

def build_feature_hash_buffer(features: Mapping[int, str], timestamp_ms: int) -> bytes:
    # 原生 sub_439e3c: 按整数键升序拼接 UTF-8, 每 4 项推进 15 位 LCG 取末态
    if timestamp_ms < 0:
        raise ValueError("timestamp_ms must be non-negative")

    entries = sorted(features.items())
    parts = [str(value) for _key, value in entries]
    state = int(timestamp_ms)
    for _ in range((len(entries) + 3) // 4):
        state = ((state * _LCG_MULTIPLIER + _LCG_INCREMENT) >> 16) & 0x7FFF
        parts.append(f"{state:x}")
    return "".join(parts).encode("utf-8")

def generate_feature_hash_tags(
    features: Mapping[int, str], timestamp_ms: int
) -> tuple[str, str]:
    digest = f"{turing_hash64(build_feature_hash_buffer(features, timestamp_ms)):x}"
    return "01" + digest, digest