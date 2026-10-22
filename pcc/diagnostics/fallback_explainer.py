"""Explain why imports/features fall back to CPython/libpython."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable


@dataclass(frozen=True)
class FallbackReason:
    feature: str
    phase: str
    reason: str
    suggestion: str = ""
    source: str = ""

    def to_json(self) -> dict[str, str]:
        return dict(self.__dict__)


class FallbackExplainer:
    def __init__(self) -> None:
        self._items: list[FallbackReason] = []

    def add(self, feature: str, phase: str, reason: str,
            *, suggestion: str = "", source: str = "") -> None:
        self._items.append(FallbackReason(feature, phase, reason, suggestion, source))

    def extend(self, items: Iterable[FallbackReason]) -> None:
        self._items.extend(items)

    def to_json(self) -> dict[str, object]:
        return {
            "schema": "pcc.fallback.v1",
            "count": len(self._items),
            "fallbacks": [item.to_json() for item in self._items],
        }

    def format_json(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=True)

    def format_text(self) -> str:
        if not self._items:
            return "no fallback reasons recorded"
        return "\n".join(
            f"{item.phase}: {item.feature}: {item.reason}"
            for item in self._items
        )


def explain_import(module: str, classification: str) -> FallbackReason | None:
    if classification != "cpython_fallback":
        return None
    return FallbackReason(
        feature=f"import {module}",
        phase="import-resolution",
        reason="module is not native-builtin, native-user, or pcc stdlib",
        suggestion="add pcc/stdlib port or enable --python-libpython=auto",
    )
