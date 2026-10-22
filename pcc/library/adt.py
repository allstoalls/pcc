
"""Small ADT/pattern helper library for roadmap §5.4."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Variant:
    tag: str
    values: tuple[Any, ...] = ()

    def __iter__(self):
        return iter(self.values)


class MatchError(Exception):
    pass


def variant(tag: str, *values: Any) -> Variant:
    return Variant(tag=tag, values=tuple(values))


def match(value: Variant, cases: dict[str, Callable[..., Any]], *, default: Callable[[Variant], Any] | None = None) -> Any:
    if value.tag in cases:
        return cases[value.tag](*value.values)
    if default is not None:
        return default(value)
    raise MatchError(f"no case for variant {value.tag!r}")


def check_exhaustive(known_tags: set[str], cases: set[str]) -> set[str]:
    return set(known_tags) - set(cases)


# Conventional names for functional libraries.
def Some(value: Any) -> Variant:
    return variant("Some", value)


Nothing = variant("Nothing")


def Ok(value: Any) -> Variant:
    return variant("Ok", value)


def Err(error: Any) -> Variant:
    return variant("Err", error)
