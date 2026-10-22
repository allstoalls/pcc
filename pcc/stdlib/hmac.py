"""pcc.stdlib.hmac - small HMAC implementation."""
from __future__ import annotations

import hashlib


def _as_bytes(data):
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return data.encode()
    return bytes(data)


class HMAC:
    def __init__(self, key, msg=None, digestmod=None):
        self.digest_name = "sha256" if digestmod is None else "sha1"
        self.digest_cons = digestmod
        block_size = 64
        key_b = _as_bytes(key)
        if len(key_b) > block_size:
            key_b = self._new_hash(key_b).digest()
        if len(key_b) < block_size:
            key_b = key_b + (b"\x00" * (block_size - len(key_b)))
        ipad = bytearray()
        opad = bytearray()
        i = 0
        while i < len(key_b):
            b = key_b[i]
            ipad.append(b ^ 0x36)
            opad.append(b ^ 0x5C)
            i += 1
        self.inner = self._new_hash(bytes(ipad))
        self.outer_key = bytes(opad)
        if msg is not None:
            self.update(msg)

    def _new_hash(self, data=b""):
        if self.digest_name == "sha1":
            return hashlib.sha1(data)
        return hashlib.sha256(data)

    def update(self, msg):
        self.inner.update(_as_bytes(msg))

    def digest(self):
        outer = self._new_hash(self.outer_key)
        outer.update(self.inner.digest())
        return outer.digest()

    def hexdigest(self):
        return "".join(f"{b:02x}" for b in self.digest())


def new(key, msg=None, digestmod=None):
    return HMAC(key, msg, digestmod)
