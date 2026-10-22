from __future__ import annotations

from dataclasses import dataclass


PyBUF_SIMPLE = 0
PyBUF_WRITABLE = 0x0001
PyBUF_FORMAT = 0x0004
PyBUF_ND = 0x0008
PyBUF_STRIDES = 0x0010


@dataclass(frozen=True)
class BufferView:
    obj: object
    length: int
    itemsize: int = 1
    readonly: bool = True
    format: str = "B"
    shape: tuple[int, ...] = ()
    strides: tuple[int, ...] = ()

    def check_flags(self, flags: int) -> None:
        if flags & PyBUF_WRITABLE and self.readonly:
            raise BufferError("buffer is read-only")
        if flags & PyBUF_ND and not self.shape:
            raise BufferError("shape requested but unavailable")
        if flags & PyBUF_STRIDES and not self.strides:
            raise BufferError("strides requested but unavailable")
