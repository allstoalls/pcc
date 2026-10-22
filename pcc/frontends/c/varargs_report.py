from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class VarargsRewrite:
    helper: str
    return_type: str
    argument_type: str
    argument_value: str
    line: int = 0

    def to_json(self) -> dict[str, object]:
        return dict(self.__dict__)


class VarargsRewriteReport:
    def __init__(self) -> None:
        self.rewrites: list[VarargsRewrite] = []

    def add(self, rewrite: VarargsRewrite) -> None:
        self.rewrites.append(rewrite)

    def to_json(self) -> dict[str, object]:
        return {
            "schema": "pcc.varargs_rewrite.v1",
            "count": len(self.rewrites),
            "rewrites": [r.to_json() for r in self.rewrites],
        }

    def format_json(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=True)
