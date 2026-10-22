"""Small typed-functional primitives for pcc user and compiler code.

The multi-year roadmap asks for category-theory-inspired libraries to start as
ordinary Python libraries before the compiler recognizes and optimizes them.
This module intentionally stays dependency-light and CPython-compatible: it is
usable from host Python, pcc-compiled Python, and later typed fast paths.
"""
from __future__ import annotations

from typing import Callable, Generic, Iterator, TypeVar, Union, overload

T = TypeVar("T")
U = TypeVar("U")
E = TypeVar("E")
F = TypeVar("F")


class Option(Generic[T]):
    """A value that is either ``Some(value)`` or ``Nothing``."""

    def is_some(self) -> bool:
        raise NotImplementedError

    def is_none(self) -> bool:
        return not self.is_some()

    def unwrap(self) -> T:
        raise NotImplementedError

    def unwrap_or(self, default: T) -> T:
        return self.unwrap() if self.is_some() else default

    def map(self, fn: Callable[[T], U]) -> "Option[U]":
        if self.is_some():
            return Some(fn(self.unwrap()))
        return NOTHING

    def and_then(self, fn: Callable[[T], "Option[U]"]) -> "Option[U]":
        if self.is_some():
            return fn(self.unwrap())
        return NOTHING

    def __iter__(self) -> Iterator[T]:
        if self.is_some():
            yield self.unwrap()


class Some(Option[T]):
    __slots__ = ("value",)

    def __init__(self, value: T) -> None:
        self.value = value

    def is_some(self) -> bool:
        return True

    def unwrap(self) -> T:
        return self.value

    def __repr__(self) -> str:
        return f"Some({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Some) and self.value == other.value


class _Nothing(Option[object]):
    __slots__ = ()

    def is_some(self) -> bool:
        return False

    def unwrap(self):
        raise ValueError("called unwrap() on Nothing")

    def __repr__(self) -> str:
        return "Nothing"

    def __bool__(self) -> bool:
        return False


NOTHING: Option[object] = _Nothing()
Nothing = NOTHING


def option(value: T | None) -> Option[T]:
    return NOTHING if value is None else Some(value)


class Result(Generic[T, E]):
    """A value that is either ``Ok(value)`` or ``Err(error)``."""

    def is_ok(self) -> bool:
        raise NotImplementedError

    def is_err(self) -> bool:
        return not self.is_ok()

    def unwrap(self) -> T:
        raise NotImplementedError

    def unwrap_err(self) -> E:
        raise NotImplementedError

    def map(self, fn: Callable[[T], U]) -> "Result[U, E]":
        if self.is_ok():
            return Ok(fn(self.unwrap()))
        return Err(self.unwrap_err())

    def map_err(self, fn: Callable[[E], F]) -> "Result[T, F]":
        if self.is_ok():
            return Ok(self.unwrap())
        return Err(fn(self.unwrap_err()))

    def and_then(self, fn: Callable[[T], "Result[U, E]"]) -> "Result[U, E]":
        if self.is_ok():
            return fn(self.unwrap())
        return Err(self.unwrap_err())


class Ok(Result[T, E]):
    __slots__ = ("value",)

    def __init__(self, value: T) -> None:
        self.value = value

    def is_ok(self) -> bool:
        return True

    def unwrap(self) -> T:
        return self.value

    def unwrap_err(self) -> E:
        raise ValueError("called unwrap_err() on Ok")

    def __repr__(self) -> str:
        return f"Ok({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ok) and self.value == other.value


class Err(Result[T, E]):
    __slots__ = ("error",)

    def __init__(self, error: E) -> None:
        self.error = error

    def is_ok(self) -> bool:
        return False

    def unwrap(self) -> T:
        raise ValueError(f"called unwrap() on Err({self.error!r})")

    def unwrap_err(self) -> E:
        return self.error

    def __repr__(self) -> str:
        return f"Err({self.error!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Err) and self.error == other.error


class Left(Generic[T]):
    __slots__ = ("value",)

    def __init__(self, value: T) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"Left({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Left) and self.value == other.value


class Right(Generic[T]):
    __slots__ = ("value",)

    def __init__(self, value: T) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"Right({self.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Right) and self.value == other.value


Either = Union[Left[T], Right[U]]
