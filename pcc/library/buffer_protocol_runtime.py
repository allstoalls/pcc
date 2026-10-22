from __future__ import annotations

from dataclasses import dataclass

PyBUF_SIMPLE = 0
PyBUF_WRITABLE = 0x0001
PyBUF_FORMAT = 0x0004
PyBUF_ND = 0x0008
PyBUF_STRIDES = 0x0010


@dataclass(frozen=True)
class PyBufferView:
    obj: object
    nbytes: int
    itemsize: int = 1
    readonly: bool = True
    ndim: int = 1
    shape: tuple[int, ...] = ()
    strides: tuple[int, ...] = ()
    format: str = "B"

    def validate(self) -> None:
        if self.itemsize <= 0:
            raise ValueError("itemsize must be positive")
        if self.nbytes < 0:
            raise ValueError("nbytes must be non-negative")
        if self.shape and self.ndim != len(self.shape):
            raise ValueError("ndim/shape mismatch")
        if self.strides and self.ndim != len(self.strides):
            raise ValueError("ndim/strides mismatch")


def request_buffer(obj: object, *, flags: int = PyBUF_SIMPLE) -> PyBufferView:
    if isinstance(obj, (bytes, bytearray, memoryview)):
        view = memoryview(obj)
        readonly = view.readonly
        if (flags & PyBUF_WRITABLE) and readonly:
            raise BufferError("writable buffer requested from readonly object")
        out = PyBufferView(obj=obj, nbytes=view.nbytes, itemsize=view.itemsize, readonly=readonly, ndim=view.ndim, shape=tuple(view.shape or ()), strides=tuple(view.strides or ()), format=view.format)
        out.validate()
        return out
    raise TypeError("object does not expose pcc buffer protocol")
