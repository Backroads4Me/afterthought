"""Stable identifiers: a prefix plus a time-ordered, lowercase ULID."""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase


def new_id(prefix: str) -> str:
    millis = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (millis << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"
