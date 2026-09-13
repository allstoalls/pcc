"""Owned hashlib algorithms with native incremental SHA-256 under pcc.

CPython uses the Python implementation; pcc uses the same native context
operations for constructor data, update(), copy(), and digest().
"""
from __future__ import annotations

from pcc.extern import c_obj, extern
from pcc.unsafe import null, ptr_is_null

_native_sha256_new = extern("py_sha256_state_new", (), c_obj)
_native_sha256_update = extern("py_sha256_state_update", (c_obj, c_obj), c_obj)
_native_sha256_digest = extern("py_sha256_state_digest", (c_obj,), c_obj)


def _native_transform_available() -> bool:
    try:
        return ptr_is_null(null()) != 0
    except NotImplementedError:
        return False


_NATIVE_TRANSFORM_AVAILABLE = _native_transform_available()

_K = [
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
]

_K_SHA1 = [
    0x5A827999,
    0x6ED9EBA1,
    0x8F1BBCDC,
    0xCA62C1D6,
]


def _rotr(x, n):
    return ((x >> n) | ((x << (32 - n)) & 0xffffffff)) & 0xffffffff


def _as_bytes(data):
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return data.encode()
    return bytes(data)


class _SHA256:
    digest_size = 32
    block_size = 64
    name = "sha256"

    def __init__(self, data=b""):
        self._h = [
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
        ]
        self._buf = b""
        self._counter = 0
        self._native_state = b""
        if _NATIVE_TRANSFORM_AVAILABLE:
            self._native_state = _native_sha256_new()
        if data:
            self.update(data)

    def copy(self):
        other = _SHA256()
        other._h = list(self._h)
        other._buf = self._buf
        other._counter = self._counter
        # Snapshots are immutable; update replaces only this instance's state.
        other._native_state = self._native_state
        return other

    def update(self, data):
        data = _as_bytes(data)
        if self._native_state:
            self._native_state = _native_sha256_update(self._native_state, data)
            return None
        self._counter += len(data)
        data = self._buf + data
        i = 0
        while i + 64 <= len(data):
            self._compress(data[i:i+64])
            i += 64
        self._buf = data[i:]
        return None

    def _compress(self, block):
        w = []
        for i in range(16):
            j = i * 4
            w.append((block[j] << 24) | (block[j+1] << 16) | (block[j+2] << 8) | block[j+3])
        for i in range(16, 64):
            s0 = _rotr(w[i-15], 7) ^ _rotr(w[i-15], 18) ^ (w[i-15] >> 3)
            s1 = _rotr(w[i-2], 17) ^ _rotr(w[i-2], 19) ^ (w[i-2] >> 10)
            w.append((w[i-16] + s0 + w[i-7] + s1) & 0xffffffff)

        a,b,c,d,e,f,g,h = self._h
        for i in range(64):
            S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
            ch = (e & f) ^ ((~e) & g)
            temp1 = (h + S1 + ch + _K[i] + w[i]) & 0xffffffff
            S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            temp2 = (S0 + maj) & 0xffffffff
            h = g
            g = f
            f = e
            e = (d + temp1) & 0xffffffff
            d = c
            c = b
            b = a
            a = (temp1 + temp2) & 0xffffffff
        self._h = [
            (self._h[0] + a) & 0xffffffff,
            (self._h[1] + b) & 0xffffffff,
            (self._h[2] + c) & 0xffffffff,
            (self._h[3] + d) & 0xffffffff,
            (self._h[4] + e) & 0xffffffff,
            (self._h[5] + f) & 0xffffffff,
            (self._h[6] + g) & 0xffffffff,
            (self._h[7] + h) & 0xffffffff,
        ]

    def digest(self):
        if self._native_state:
            return _native_sha256_digest(self._native_state)
        clone = self.copy()
        bit_len = clone._counter * 8
        clone.update(b"\x80")
        while len(clone._buf) != 56:
            if len(clone._buf) > 56:
                clone.update(b"\x00" * (64 - len(clone._buf)))
            else:
                clone.update(b"\x00")
        clone.update(bit_len.to_bytes(8, "big"))
        return b"".join(x.to_bytes(4, "big") for x in clone._h)

    def hexdigest(self):
        return "".join(f"{b:02x}" for b in self.digest())


def sha256(data=b""):
    return _SHA256(data)


class _SHA224(_SHA256):
    digest_size = 28
    block_size = 64
    name = "sha224"

    def __init__(self, data=b""):
        self._h = [
            0xc1059ed8, 0x367cd507, 0x3070dd17, 0xf70e5939,
            0xffc00b31, 0x68581511, 0x64f98fa7, 0xbefa4fa4,
        ]
        self._buf = b""
        self._counter = 0
        self._native_state = b""
        if data:
            self.update(data)

    def copy(self):
        other = _SHA224()
        other._h = list(self._h)
        other._buf = self._buf
        other._counter = self._counter
        return other

    def digest(self):
        clone = self.copy()
        bit_len = clone._counter * 8
        clone.update(b"\x80")
        while len(clone._buf) != 56:
            if len(clone._buf) > 56:
                clone.update(b"\x00" * (64 - len(clone._buf)))
            else:
                clone.update(b"\x00")
        clone.update(bit_len.to_bytes(8, "big"))
        return b"".join(x.to_bytes(4, "big") for x in clone._h[:7])


def sha224(data=b""):
    return _SHA224(data)


def _rotl32(x, n):
    return (((x << n) & 0xffffffff) | (x >> (32 - n))) & 0xffffffff


class _SHA1:
    digest_size = 20
    block_size = 64
    name = "sha1"

    def __init__(self, data=b""):
        self._h = [
            0x67452301,
            0xEFCDAB89,
            0x98BADCFE,
            0x10325476,
            0xC3D2E1F0,
        ]
        self._buf = b""
        self._counter = 0
        if data:
            self.update(data)

    def copy(self):
        other = _SHA1()
        other._h = list(self._h)
        other._buf = self._buf
        other._counter = self._counter
        return other

    def update(self, data):
        data = _as_bytes(data)
        self._counter += len(data)
        data = self._buf + data
        i = 0
        while i + 64 <= len(data):
            self._compress(data[i:i+64])
            i += 64
        self._buf = data[i:]
        return None

    def _compress(self, block):
        w = []
        for i in range(16):
            j = i * 4
            w.append((block[j] << 24) | (block[j+1] << 16) | (block[j+2] << 8) | block[j+3])
        for i in range(16, 80):
            w.append(_rotl32(w[i-3] ^ w[i-8] ^ w[i-14] ^ w[i-16], 1))
        a, b, c, d, e = self._h
        for i in range(80):
            if i < 20:
                f = (b & c) | ((~b) & d)
                k = _K_SHA1[0]
            elif i < 40:
                f = b ^ c ^ d
                k = _K_SHA1[1]
            elif i < 60:
                f = (b & c) | (b & d) | (c & d)
                k = _K_SHA1[2]
            else:
                f = b ^ c ^ d
                k = _K_SHA1[3]
            temp = (_rotl32(a, 5) + f + e + k + w[i]) & 0xffffffff
            e = d
            d = c
            c = _rotl32(b, 30)
            b = a
            a = temp
        self._h = [
            (self._h[0] + a) & 0xffffffff,
            (self._h[1] + b) & 0xffffffff,
            (self._h[2] + c) & 0xffffffff,
            (self._h[3] + d) & 0xffffffff,
            (self._h[4] + e) & 0xffffffff,
        ]

    def digest(self):
        clone = self.copy()
        bit_len = clone._counter * 8
        clone.update(b"\x80")
        while len(clone._buf) != 56:
            if len(clone._buf) > 56:
                clone.update(b"\x00" * (64 - len(clone._buf)))
            else:
                clone.update(b"\x00")
        clone.update(bit_len.to_bytes(8, "big"))
        return b"".join(x.to_bytes(4, "big") for x in clone._h)

    def hexdigest(self):
        return "".join(f"{b:02x}" for b in self.digest())


def sha1(data=b""):
    return _SHA1(data)


# RFC 1321 section 3.4: floor(2**32 * abs(sin(i))), i=1..64.
# Stored as integers so native execution never needs a host math provider.
# https://www.rfc-editor.org/rfc/rfc1321.html#section-3.4
_K_MD5 = [
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x2441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x4881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391,
]
_S_MD5 = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
]


class _MD5(_SHA1):
    digest_size = 16
    block_size = 64
    name = "md5"

    def __init__(self, data=b""):
        self._h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476]
        self._buf = b""
        self._counter = 0
        if data:
            self.update(data)

    def copy(self):
        other = _MD5()
        other._h = list(self._h)
        other._buf = self._buf
        other._counter = self._counter
        return other

    def _compress(self, block):
        words = []
        for index in range(16):
            offset = index * 4
            words.append(block[offset] | (block[offset + 1] << 8)
                         | (block[offset + 2] << 16) | (block[offset + 3] << 24))
        a, b, c, d = self._h
        for index in range(64):
            if index < 16:
                mixed = (b & c) | ((~b) & d)
                word = index
            elif index < 32:
                mixed = (d & b) | ((~d) & c)
                word = (5 * index + 1) % 16
            elif index < 48:
                mixed = b ^ c ^ d
                word = (3 * index + 5) % 16
            else:
                mixed = c ^ (b | (~d))
                word = (7 * index) % 16
            value = (a + mixed + _K_MD5[index] + words[word]) & 0xffffffff
            rotated = _rotl32(value, _S_MD5[index])
            a, d, c, b = d, c, b, (b + rotated) & 0xffffffff
        self._h = [(self._h[0] + a) & 0xffffffff,
                   (self._h[1] + b) & 0xffffffff,
                   (self._h[2] + c) & 0xffffffff,
                   (self._h[3] + d) & 0xffffffff]

    def digest(self):
        clone = self.copy()
        bit_length = (clone._counter * 8) & 0xffffffffffffffff
        padding = (55 - len(clone._buf)) % 64
        clone.update(b"\x80" + b"\x00" * padding + bit_length.to_bytes(8, "little"))
        return b"".join(word.to_bytes(4, "little") for word in clone._h)


def md5(data=b""):
    return _MD5(data)


def new(name, data=b""):
    n = name.lower().replace("-", "")
    if n == "sha1":
        return sha1(data)
    if n == "sha224":
        return sha224(data)
    if n == "sha256":
        return sha256(data)
    if n == "md5":
        return md5(data)
    raise ValueError("unsupported hash type: " + name)


algorithms_guaranteed = {"md5", "sha1", "sha224", "sha256"}
algorithms_available = algorithms_guaranteed
