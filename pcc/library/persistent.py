
"""Tiny persistent data structures used by functional roadmap experiments."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Iterable, Iterator, TypeVar

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True)
class PVector(Generic[T]):
    _items: tuple[T, ...] = ()

    def append(self, value: T) -> "PVector[T]":
        return PVector(self._items + (value,))

    def set(self, index: int, value: T) -> "PVector[T]":
        items = list(self._items)
        items[index] = value
        return PVector(tuple(items))

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index: int) -> T:
        return self._items[index]


@dataclass(frozen=True)
class PMap(Generic[K, V]):
    _items: tuple[tuple[K, V], ...] = ()

    @staticmethod
    def from_items(items: Iterable[tuple[K, V]]) -> "PMap[K, V]":
        out = PMap()
        for k, v in items:
            out = out.set(k, v)
        return out

    def set(self, key: K, value: V) -> "PMap[K, V]":
        out: list[tuple[K, V]] = []
        replaced = False
        for k, v in self._items:
            if k == key:
                out.append((key, value))
                replaced = True
            else:
                out.append((k, v))
        if not replaced:
            out.append((key, value))
        return PMap(tuple(out))

    def get(self, key: K, default: V | None = None):
        for k, v in self._items:
            if k == key:
                return v
        return default

    def items(self) -> tuple[tuple[K, V], ...]:
        return self._items
