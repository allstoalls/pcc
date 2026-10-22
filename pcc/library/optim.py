"""User-visible optimization markers.

Compiler support can recognize these markers later; the decorators are useful
now because tests and libraries can declare intent without changing syntax.
"""
from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)


def tailcall(fn: F) -> F:
    """Mark a function as eligible for traceback-relaxed tail-call lowering."""
    setattr(fn, "__pcc_tailcall__", True)
    return fn


def is_tailcall_enabled(fn: Callable) -> bool:
    return bool(getattr(fn, "__pcc_tailcall__", False))


class OptimizationLog:
    def __init__(self) -> None:
        self._events: list[tuple[str, str, int]] = []

    def record_tailcall(self, function_name: str, count: int = 1) -> None:
        self._events.append(("tailcall_eliminated", function_name, count))

    def events(self) -> tuple[tuple[str, str, int], ...]:
        return tuple(self._events)

    def as_text(self) -> str:
        return "\n".join(
            f"{kind}={name} count={count}" for kind, name, count in self._events
        )
