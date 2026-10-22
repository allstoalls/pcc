from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TraitMethod:
    name: str
    signature: str


@dataclass
class Trait:
    name: str
    methods: dict[str, TraitMethod] = field(default_factory=dict)

    def requires(self, name: str, signature: str) -> "Trait":
        self.methods[name] = TraitMethod(name, signature)
        return self


def check_trait(obj: object, trait: Trait) -> list[str]:
    return [name for name in trait.methods if not hasattr(obj, name)]
