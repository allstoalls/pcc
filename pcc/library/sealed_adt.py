from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ExhaustivenessError(ValueError):
    pass


@dataclass(frozen=True)
class VariantSpec:
    name: str
    fields: tuple[str, ...] = ()


class SealedADT:
    def __init__(self, name: str, variants: list[VariantSpec]) -> None:
        if not variants:
            raise ValueError("sealed ADT needs at least one variant")
        self.name = name
        self.variants = tuple(variants)
        self._variant_names = {v.name for v in variants}

    def construct(self, variant: str, **fields: Any) -> tuple[str, dict[str, Any]]:
        spec = next((v for v in self.variants if v.name == variant), None)
        if spec is None:
            raise ValueError(f"unknown variant {variant}")
        missing = set(spec.fields) - set(fields)
        extra = set(fields) - set(spec.fields)
        if missing or extra:
            raise TypeError(f"field mismatch missing={sorted(missing)} extra={sorted(extra)}")
        return (variant, dict(fields))

    def check_exhaustive(self, handled: set[str]) -> None:
        missing = self._variant_names - set(handled)
        if missing:
            raise ExhaustivenessError(f"non-exhaustive match on {self.name}: missing {sorted(missing)}")


def decision_tree_order(adt: SealedADT, hot_order: list[str] | None = None) -> list[str]:
    hot = [v for v in (hot_order or []) if v in adt._variant_names]
    rest = [v.name for v in adt.variants if v.name not in hot]
    return hot + rest
