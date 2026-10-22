"""Fallback route recording for ``--explain-fallback``.

The pipeline already has import classification functions. This module turns
those individual decisions into user-visible events with stable reasons so the
CLI can report why libpython was required or avoided.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class FallbackRoute:
    module: str
    classification: str
    reason: str
    native: bool

    def to_json(self) -> dict[str, object]:
        return dict(module=self.module, classification=self.classification, reason=self.reason, native=self.native)


def route_from_classification(module: str, classification: str) -> FallbackRoute:
    native = classification in {"compile_time_only", "native_user_module", "builtin_native_dispatch", "native_stdlib"}
    reason = {
        "compile_time_only": "erased at compile time",
        "native_user_module": "compiled as part of source closure",
        "builtin_native_dispatch": "lowered by built-in native dispatch",
        "native_stdlib": "resolved to pcc/stdlib provider",
        "cpython_fallback": "no native provider found; libpython required unless disabled",
    }.get(classification, "unknown import route")
    return FallbackRoute(module, classification, reason, native)


def explain_routes(routes: list[FallbackRoute], *, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps({"schema": "pcc.diagnostics.fallback_routes.v1", "routes": [r.to_json() for r in routes]}, indent=2, sort_keys=True)
    return "\n".join(f"{r.module}: {r.classification}: {r.reason}" for r in routes)
