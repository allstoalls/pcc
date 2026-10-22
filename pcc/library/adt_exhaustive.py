from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    name: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SealedADT:
    name: str
    variants: tuple[Variant, ...]

    def variant_names(self) -> set[str]:
        return {v.name for v in self.variants}


def missing_patterns(adt: SealedADT, seen: set[str], *, wildcard: bool = False) -> set[str]:
    return set() if wildcard else adt.variant_names() - set(seen)


def check_exhaustive(adt: SealedADT, seen: set[str], *, wildcard: bool = False) -> None:
    missing = missing_patterns(adt, seen, wildcard=wildcard)
    if missing:
        raise ValueError(
            f"non-exhaustive match for {adt.name}: missing "
            + ", ".join(sorted(missing))
        )
