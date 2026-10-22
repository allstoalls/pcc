from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HotObjectCandidate:
    name: str
    allocations: int
    bytes_allocated: int
    suggested_layout: str

    @property
    def score(self) -> int:
        return self.allocations * max(1, self.bytes_allocated)


def rank_hot_objects(items: list[HotObjectCandidate]) -> list[HotObjectCandidate]:
    return sorted(items, key=lambda item: item.score, reverse=True)


def recommend_slots(candidate: HotObjectCandidate) -> bool:
    return candidate.allocations >= 1000 or candidate.bytes_allocated >= 1_000_000


@dataclass(frozen=True)
class ValueModelMigration:
    name: str
    valueclass_marker: str
    fields: tuple[str, ...]
    reason: str


def migrated_value_model_hot_objects() -> tuple[ValueModelMigration, ...]:
    """V6 migration list for compiler-owned immutable hot objects.

    These are intentionally metadata-first: the frontend can consume the same
    shape to decide which dataclass-like compiler objects are safe to flatten
    without changing public Python semantics for identity-bearing classes.
    """
    return (
        ValueModelMigration(
            "SourceSpan",
            "@pcc.valueclass",
            ("file", "line", "col", "end_line", "end_col"),
            "high-volume immutable diagnostic coordinate",
        ),
        ValueModelMigration(
            "ValuePayload",
            "@pcc.valueclass",
            ("descriptor", "values"),
            "value projection carrier for boxed interop",
        ),
    )
