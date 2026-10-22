from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")
U = TypeVar("U")
E = TypeVar("E")


@dataclass(frozen=True)
class Result(Generic[T, E]):
    ok: bool
    value: T | E

    @staticmethod
    def Ok(value: T) -> "Result[T, E]":
        return Result(True, value)

    @staticmethod
    def Err(error: E) -> "Result[T, E]":
        return Result(False, error)

    def map(self, fn: Callable[[T], U]) -> "Result[U, E]":
        if not self.ok:
            return Result(False, self.value)  # type: ignore[arg-type]
        return Result.Ok(fn(self.value))  # type: ignore[arg-type]

    def bind(self, fn: Callable[[T], "Result[U, E]"]) -> "Result[U, E]":
        if not self.ok:
            return Result(False, self.value)  # type: ignore[arg-type]
        return fn(self.value)  # type: ignore[arg-type]


def fuse_map_filter(values, map_fn, pred):
    out = []
    for v in values:
        mapped = map_fn(v)
        if pred(mapped):
            out.append(mapped)
    return out
