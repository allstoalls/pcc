"""pcc.stdlib.io — StringIO + BytesIO (minimal)."""
from __future__ import annotations


class StringIO:
    def __init__(self, initial: str = "") -> None:
        self._buf = initial
        self._pos = 0

    def write(self, s: str) -> int:
        self._buf = self._buf[:self._pos] + s + self._buf[self._pos + len(s):]
        self._pos += len(s)
        return len(s)

    def getvalue(self) -> str:
        return self._buf

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._buf = ""

    def read(self, n: int = -1) -> str:
        v = self.getvalue()
        if n is None or n < 0:
            r = v[self._pos:]
            self._pos = len(v)
            return r
        r = v[self._pos:self._pos + n]
        self._pos += len(r)
        return r

    def readline(self, size: int = -1) -> str:
        v = self.getvalue()
        i = self._pos
        stop = len(v)
        if size is not None and size >= 0 and self._pos + size < stop:
            stop = self._pos + size
        while i < stop:
            i += 1
            if v[i - 1] == "\n":
                break
        r = v[self._pos:i]
        self._pos = i
        return r

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = len(self._buf) + offset
        if self._pos < 0:
            self._pos = 0
        return self._pos

    def tell(self) -> int:
        return self._pos


class BytesIO:
    def __init__(self, initial: bytes = b"") -> None:
        self._buf = bytearray(initial)
        self._pos = 0

    def write(self, b: bytes) -> int:
        end = self._pos + len(b)
        if self._pos > len(self._buf):
            # ``bytes(N)`` materialises N zero bytes; the closed-world
            # codegen has no ``bytes * int`` repeat helper today (no
            # ``py_bytes_repeat`` in runtime), so this avoids tripping
            # the unsupported BinOp lowering on ``b'\x00' * N``.
            self._buf.extend(bytes(self._pos - len(self._buf)))
        self._buf[self._pos:end] = b
        self._pos = end
        return len(b)

    def getvalue(self) -> bytes:
        return bytes(self._buf)

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            r = bytes(self._buf[self._pos:])
            self._pos = len(self._buf)
            return r
        r = bytes(self._buf[self._pos:self._pos + n])
        self._pos += len(r)
        return r

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        # ``bytearray()`` (no-arg) routes through builtins via
        # py_cpy_*; ``bytearray(b"")`` lowers natively via the
        # bytes-arg constructor.
        self._buf = bytearray(b"")

    def readline(self, size: int = -1) -> bytes:
        i = self._pos
        stop = len(self._buf)
        if size is not None and size >= 0 and self._pos + size < stop:
            stop = self._pos + size
        while i < stop:
            i += 1
            if self._buf[i - 1] == 10:
                break
        r = bytes(self._buf[self._pos:i])
        self._pos = i
        return r

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = len(self._buf) + offset
        if self._pos < 0:
            self._pos = 0
        return self._pos

    def tell(self) -> int:
        return self._pos
