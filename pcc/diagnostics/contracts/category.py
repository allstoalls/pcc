"""Small category kernel used by PCC checker/protocol layers.

This is not a proof assistant.  It is a compact executable model of the parts
of category structure PCC needs before a checker can claim that some runtime or
codegen path is compositional: objects, arrows, identities, composition, paths,
and law checks.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

ObjT = TypeVar("ObjT", bound=Hashable)
GradeT = TypeVar("GradeT", bound=Hashable)
ValueT = TypeVar("ValueT")


class CategoryCompositionError(ValueError):
    """Raised when arrows cannot be composed in a category."""


@dataclass(frozen=True)
class CategoryViolation:
    """A structural category checker finding."""

    message: str
    index: int
    arrow_name: str


@dataclass(frozen=True)
class QuantaleViolation:
    """A failed executable law for a sampled effect quantale."""

    law: str
    message: str
    operands: tuple[object, ...]


@dataclass(frozen=True)
class ProofViolation:
    """A failed executable proof obligation."""

    law: str
    message: str
    witness: str


@dataclass(frozen=True)
class CategoryArrow(Generic[ObjT]):
    """A morphism from ``domain`` to ``codomain``."""

    name: str
    domain: ObjT
    codomain: ObjT
    factors: tuple[str, ...] = ()
    is_identity: bool = False

    def __post_init__(self) -> None:
        if self.factors:
            return
        if self.is_identity:
            return
        object.__setattr__(self, "factors", (self.name,))


@dataclass(frozen=True)
class CategoryPath(Generic[ObjT]):
    """A checked path through a category."""

    start: ObjT
    arrows: tuple[CategoryArrow[ObjT], ...]

    @property
    def domain(self) -> ObjT:
        return self.start

    @property
    def codomain(self) -> ObjT:
        if not self.arrows:
            return self.start
        return self.arrows[-1].codomain

    @property
    def factors(self) -> tuple[str, ...]:
        out: list[str] = []
        for arrow in self.arrows:
            out.extend(arrow.factors)
        return tuple(out)


class SmallCategory(Generic[ObjT]):
    """Finite category surface for PCC checker code."""

    def __init__(
        self,
        name: str,
        *,
        objects: Iterable[ObjT],
        arrows: Iterable[CategoryArrow[ObjT]] = (),
    ) -> None:
        self.name = name
        self._objects = frozenset(objects)
        arrow_by_name: dict[str, CategoryArrow[ObjT]] = {}
        for arrow in arrows:
            if arrow.domain not in self._objects:
                raise ValueError(f"arrow {arrow.name!r} has domain outside category")
            if arrow.codomain not in self._objects:
                raise ValueError(f"arrow {arrow.name!r} has codomain outside category")
            if arrow.name in arrow_by_name:
                raise ValueError(f"duplicate category arrow {arrow.name!r}")
            arrow_by_name[arrow.name] = arrow
        self._arrows = arrow_by_name

    def objects(self) -> tuple[ObjT, ...]:
        return tuple(sorted(self._objects, key=str))

    def arrow_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._arrows))

    def arrow(self, name: str) -> CategoryArrow[ObjT]:
        try:
            return self._arrows[name]
        except KeyError as exc:
            raise KeyError(f"no arrow {name!r} in category {self.name!r}") from exc

    def identity(self, obj: ObjT) -> CategoryArrow[ObjT]:
        if obj not in self._objects:
            raise KeyError(f"no object {obj!r} in category {self.name!r}")
        return CategoryArrow(
            name=f"id[{obj}]",
            domain=obj,
            codomain=obj,
            factors=(),
            is_identity=True,
        )

    def compose(
        self,
        first: CategoryArrow[ObjT],
        second: CategoryArrow[ObjT],
        *,
        name: str | None = None,
    ) -> CategoryArrow[ObjT]:
        if first.codomain != second.domain:
            raise CategoryCompositionError(
                "cannot compose "
                f"{first.name!r}: {first.domain!r}->{first.codomain!r} "
                "then "
                f"{second.name!r}: {second.domain!r}->{second.codomain!r}"
            )
        if first.is_identity:
            if name is None:
                return second
            return CategoryArrow(
                name=name,
                domain=second.domain,
                codomain=second.codomain,
                factors=second.factors,
                is_identity=second.is_identity,
            )
        if second.is_identity:
            if name is None:
                return first
            return CategoryArrow(
                name=name,
                domain=first.domain,
                codomain=first.codomain,
                factors=first.factors,
                is_identity=first.is_identity,
            )
        return CategoryArrow(
            name=name or f"{first.name};{second.name}",
            domain=first.domain,
            codomain=second.codomain,
            factors=first.factors + second.factors,
        )

    def check_arrows(
        self,
        arrows: Sequence[CategoryArrow[ObjT]],
        *,
        start: ObjT | None = None,
    ) -> tuple[CategoryViolation, ...]:
        violations: list[CategoryViolation] = []
        if start is not None and start not in self._objects:
            violations.append(
                CategoryViolation(
                    "path start object is not in category",
                    0,
                    "<start>",
                )
            )
        previous = start
        for index, arrow in enumerate(arrows):
            if arrow.domain not in self._objects:
                violations.append(
                    CategoryViolation(
                        "arrow domain is not in category",
                        index,
                        arrow.name,
                    )
                )
            if arrow.codomain not in self._objects:
                violations.append(
                    CategoryViolation(
                        "arrow codomain is not in category",
                        index,
                        arrow.name,
                    )
                )
            if previous is not None and arrow.domain != previous:
                violations.append(
                    CategoryViolation(
                        "arrow domain does not match previous codomain",
                        index,
                        arrow.name,
                    )
                )
            previous = arrow.codomain
        return tuple(violations)

    def path(
        self,
        names: Sequence[str],
        *,
        start: ObjT | None = None,
    ) -> CategoryPath[ObjT]:
        arrows = tuple(self.arrow(name) for name in names)
        if start is None:
            if not arrows:
                raise ValueError("empty category path requires a start object")
            start = arrows[0].domain
        violations = self.check_arrows(arrows, start=start)
        if violations:
            first = violations[0]
            raise CategoryCompositionError(
                f"{first.message} at index {first.index}: {first.arrow_name}"
            )
        return CategoryPath(start=start, arrows=arrows)

    def compose_path(
        self,
        path: CategoryPath[ObjT],
        *,
        name: str | None = None,
    ) -> CategoryArrow[ObjT]:
        if not path.arrows:
            return self.identity(path.start)
        result = path.arrows[0]
        for arrow in path.arrows[1:]:
            result = self.compose(result, arrow)
        if name is not None:
            return CategoryArrow(
                name=name,
                domain=result.domain,
                codomain=result.codomain,
                factors=result.factors,
            )
        return result

    def equivalent_arrows(
        self,
        left: CategoryArrow[ObjT],
        right: CategoryArrow[ObjT],
    ) -> bool:
        return (
            left.domain == right.domain
            and left.codomain == right.codomain
            and left.factors == right.factors
        )

    def identity_laws_hold(self, arrow: CategoryArrow[ObjT]) -> bool:
        left_identity = self.compose(self.identity(arrow.domain), arrow)
        right_identity = self.compose(arrow, self.identity(arrow.codomain))
        return self.equivalent_arrows(
            left_identity,
            arrow,
        ) and self.equivalent_arrows(right_identity, arrow)

    def associativity_holds(
        self,
        first: CategoryArrow[ObjT],
        second: CategoryArrow[ObjT],
        third: CategoryArrow[ObjT],
    ) -> bool:
        left = self.compose(self.compose(first, second), third)
        right = self.compose(first, self.compose(second, third))
        return self.equivalent_arrows(left, right)


@dataclass(frozen=True)
class Functor(Generic[ObjT]):
    """A finite functor between two small categories.

    The checker treats identities and composed arrows structurally, so a
    functor can map generated identity/path arrows even when they are not
    present as named entries in ``arrow_map``.
    """

    name: str
    source: SmallCategory[ObjT]
    target: SmallCategory[ObjT]
    object_map: Mapping[ObjT, ObjT]
    arrow_map: Mapping[str, str]

    @classmethod
    def identity(
        cls,
        category: SmallCategory[ObjT],
        *,
        name: str | None = None,
    ) -> "Functor[ObjT]":
        return cls(
            name=name or f"Id[{category.name}]",
            source=category,
            target=category,
            object_map={obj: obj for obj in category.objects()},
            arrow_map={arrow_name: arrow_name for arrow_name in category.arrow_names()},
        )

    def map_object(self, obj: ObjT) -> ObjT:
        try:
            return self.object_map[obj]
        except KeyError as exc:
            raise KeyError(
                f"functor {self.name!r} does not map object {obj!r}"
            ) from exc

    def map_arrow(self, arrow: CategoryArrow[ObjT]) -> CategoryArrow[ObjT]:
        if arrow.is_identity:
            return self.target.identity(self.map_object(arrow.domain))
        if len(arrow.factors) > 1:
            path = self.target.path(
                [self.arrow_map[factor] for factor in arrow.factors],
                start=self.map_object(arrow.domain),
            )
            return self.target.compose_path(path, name=f"{self.name}({arrow.name})")
        try:
            mapped = self.target.arrow(self.arrow_map[arrow.name])
        except KeyError as exc:
            raise KeyError(
                f"functor {self.name!r} does not map arrow {arrow.name!r}"
            ) from exc
        expected_domain = self.map_object(arrow.domain)
        expected_codomain = self.map_object(arrow.codomain)
        if mapped.domain != expected_domain or mapped.codomain != expected_codomain:
            raise CategoryCompositionError(
                f"functor {self.name!r} maps {arrow.name!r} to incompatible "
                f"{mapped.domain!r}->{mapped.codomain!r}; expected "
                f"{expected_domain!r}->{expected_codomain!r}"
            )
        return mapped

    def map_path(self, path: CategoryPath[ObjT]) -> CategoryPath[ObjT]:
        mapped_arrows = tuple(self.map_arrow(arrow) for arrow in path.arrows)
        violations = self.target.check_arrows(
            mapped_arrows,
            start=self.map_object(path.domain),
        )
        if violations:
            first = violations[0]
            raise CategoryCompositionError(
                f"{first.message} at index {first.index}: {first.arrow_name}"
            )
        return CategoryPath(start=self.map_object(path.domain), arrows=mapped_arrows)

    def then(
        self, next_functor: "Functor[ObjT]", *, name: str | None = None
    ) -> "Functor[ObjT]":
        if self.target is not next_functor.source:
            raise ValueError("functor composition requires matching categories")
        return Functor(
            name=name or f"{next_functor.name}∘{self.name}",
            source=self.source,
            target=next_functor.target,
            object_map={
                obj: next_functor.map_object(self.map_object(obj))
                for obj in self.source.objects()
            },
            arrow_map={
                arrow_name: next_functor.map_arrow(
                    self.map_arrow(self.source.arrow(arrow_name))
                ).name
                for arrow_name in self.source.arrow_names()
            },
        )

    def check_laws(
        self,
        sample_paths: Sequence[CategoryPath[ObjT]],
    ) -> tuple[ProofViolation, ...]:
        violations: list[ProofViolation] = []
        for obj in self.source.objects():
            mapped = self.map_object(obj)
            if mapped not in self.target.objects():
                violations.append(
                    ProofViolation(
                        "functor-object",
                        "mapped object is not in target category",
                        str(obj),
                    )
                )
        for arrow_name in self.source.arrow_names():
            try:
                self.map_arrow(self.source.arrow(arrow_name))
            except (CategoryCompositionError, KeyError) as exc:
                violations.append(ProofViolation("functor-arrow", str(exc), arrow_name))
        for path in sample_paths:
            mapped_path = self.map_path(path)
            if mapped_path.domain != self.map_object(path.domain):
                violations.append(
                    ProofViolation(
                        "functor-path-domain",
                        "mapped path domain does not match mapped source domain",
                        ";".join(path.factors),
                    )
                )
            if mapped_path.codomain != self.map_object(path.codomain):
                violations.append(
                    ProofViolation(
                        "functor-path-codomain",
                        "mapped path codomain does not match mapped source codomain",
                        ";".join(path.factors),
                    )
                )
        return tuple(violations)


@dataclass(frozen=True)
class NaturalTransformation(Generic[ObjT]):
    """A finite natural transformation between two functors."""

    name: str
    source_functor: Functor[ObjT]
    target_functor: Functor[ObjT]
    components: Mapping[ObjT, CategoryArrow[ObjT]]

    def component(self, obj: ObjT) -> CategoryArrow[ObjT]:
        try:
            arrow = self.components[obj]
        except KeyError as exc:
            raise KeyError(
                f"natural transformation {self.name!r} has no component for {obj!r}"
            ) from exc
        expected_domain = self.source_functor.map_object(obj)
        expected_codomain = self.target_functor.map_object(obj)
        if arrow.domain != expected_domain or arrow.codomain != expected_codomain:
            raise CategoryCompositionError(
                f"component {obj!r} has type {arrow.domain!r}->{arrow.codomain!r}; "
                f"expected {expected_domain!r}->{expected_codomain!r}"
            )
        return arrow

    def check_naturality(
        self,
        sample_arrows: Sequence[CategoryArrow[ObjT]],
    ) -> tuple[ProofViolation, ...]:
        if self.source_functor.source is not self.target_functor.source:
            return (
                ProofViolation(
                    "naturality-source",
                    "source and target functors must share source category",
                    self.name,
                ),
            )
        if self.source_functor.target is not self.target_functor.target:
            return (
                ProofViolation(
                    "naturality-target",
                    "source and target functors must share target category",
                    self.name,
                ),
            )
        category = self.source_functor.target
        violations: list[ProofViolation] = []
        for arrow in sample_arrows:
            try:
                left = category.compose(
                    self.source_functor.map_arrow(arrow),
                    self.component(arrow.codomain),
                )
                right = category.compose(
                    self.component(arrow.domain),
                    self.target_functor.map_arrow(arrow),
                )
            except (CategoryCompositionError, KeyError) as exc:
                violations.append(ProofViolation("naturality", str(exc), arrow.name))
                continue
            if not category.equivalent_arrows(left, right):
                violations.append(
                    ProofViolation(
                        "naturality",
                        "naturality square does not commute",
                        arrow.name,
                    )
                )
        return tuple(violations)


@dataclass(frozen=True)
class MonoidalCategory(Generic[ObjT]):
    """Strict monoidal structure over a small category.

    This first kernel intentionally checks strict coherence.  Non-strict
    associators/unitors can be layered on later as explicit natural isomorphism
    proof terms.
    """

    category: SmallCategory[ObjT]
    unit_object: ObjT
    tensor_object: Callable[[ObjT, ObjT], ObjT]
    tensor_arrow: Callable[
        [CategoryArrow[ObjT], CategoryArrow[ObjT]], CategoryArrow[ObjT]
    ]

    def check_object_coherence(
        self,
        samples: Sequence[ObjT],
    ) -> tuple[ProofViolation, ...]:
        violations: list[ProofViolation] = []
        for obj in samples:
            if self.tensor_object(self.unit_object, obj) != obj:
                violations.append(
                    ProofViolation("left-unitor", "I tensor A must equal A", str(obj))
                )
            if self.tensor_object(obj, self.unit_object) != obj:
                violations.append(
                    ProofViolation("right-unitor", "A tensor I must equal A", str(obj))
                )
        for first in samples:
            for second in samples:
                for third in samples:
                    left = self.tensor_object(
                        self.tensor_object(first, second),
                        third,
                    )
                    right = self.tensor_object(
                        first,
                        self.tensor_object(second, third),
                    )
                    if left != right:
                        violations.append(
                            ProofViolation(
                                "associator",
                                "(A tensor B) tensor C must equal A tensor (B tensor C)",
                                f"{first},{second},{third}",
                            )
                        )
        return tuple(violations)

    def check_interchange(
        self,
        first: CategoryArrow[ObjT],
        second: CategoryArrow[ObjT],
        third: CategoryArrow[ObjT],
        fourth: CategoryArrow[ObjT],
    ) -> bool:
        left = self.category.compose(
            self.tensor_arrow(first, third),
            self.tensor_arrow(second, fourth),
        )
        right = self.tensor_arrow(
            self.category.compose(first, second),
            self.category.compose(third, fourth),
        )
        return self.category.equivalent_arrows(left, right)


@dataclass(frozen=True)
class TracedMonoidalCategory(Generic[ObjT]):
    """A traced monoidal checker surface for feedback-style paths."""

    monoidal: MonoidalCategory[ObjT]
    trace: Callable[[CategoryArrow[ObjT], ObjT], CategoryArrow[ObjT]]

    def check_yanking(
        self,
        feedback_objects: Sequence[ObjT],
        carried_objects: Sequence[ObjT],
    ) -> tuple[ProofViolation, ...]:
        violations: list[ProofViolation] = []
        for feedback in feedback_objects:
            for carried in carried_objects:
                tensor_obj = self.monoidal.tensor_object(feedback, carried)
                traced = self.trace(
                    self.monoidal.category.identity(tensor_obj),
                    feedback,
                )
                expected = self.monoidal.category.identity(carried)
                if not self.monoidal.category.equivalent_arrows(traced, expected):
                    violations.append(
                        ProofViolation(
                            "trace-yanking",
                            "trace of identity feedback must be identity",
                            f"{feedback},{carried}",
                        )
                    )
        return tuple(violations)


@dataclass(frozen=True)
class EqualityProofTerm(Generic[ObjT]):
    """A tiny proof term for category-arrow equality.

    This is deliberately a proof-checking object, not a theorem prover.  PCC can
    attach these terms to rewrites/lowering steps and reject a claim if the term
    no longer checks.
    """

    name: str
    left: CategoryArrow[ObjT]
    right: CategoryArrow[ObjT]
    rule: str
    premises: tuple["EqualityProofTerm[ObjT]", ...] = ()

    def check(self, category: SmallCategory[ObjT]) -> tuple[ProofViolation, ...]:
        if (
            self.left.domain != self.right.domain
            or self.left.codomain != self.right.codomain
        ):
            return (
                ProofViolation(
                    self.rule,
                    "proof endpoints have different domain/codomain",
                    self.name,
                ),
            )
        if self.rule in {"refl", "category-equivalence"}:
            if category.equivalent_arrows(self.left, self.right):
                return ()
            return (
                ProofViolation(
                    self.rule,
                    "arrows are not structurally equal",
                    self.name,
                ),
            )
        if self.rule == "sym":
            if len(self.premises) != 1:
                return (ProofViolation("sym", "sym requires one premise", self.name),)
            premise = self.premises[0]
            violations = list(premise.check(category))
            if not category.equivalent_arrows(self.left, premise.right):
                violations.append(
                    ProofViolation(
                        "sym", "left side is not premise right side", self.name
                    )
                )
            if not category.equivalent_arrows(self.right, premise.left):
                violations.append(
                    ProofViolation(
                        "sym", "right side is not premise left side", self.name
                    )
                )
            return tuple(violations)
        if self.rule == "trans":
            if len(self.premises) != 2:
                return (
                    ProofViolation("trans", "trans requires two premises", self.name),
                )
            first, second = self.premises
            violations = list(first.check(category)) + list(second.check(category))
            if not category.equivalent_arrows(first.right, second.left):
                violations.append(
                    ProofViolation(
                        "trans", "premises do not meet in the middle", self.name
                    )
                )
            if not category.equivalent_arrows(self.left, first.left):
                violations.append(
                    ProofViolation(
                        "trans", "left side is not first premise left", self.name
                    )
                )
            if not category.equivalent_arrows(self.right, second.right):
                violations.append(
                    ProofViolation(
                        "trans", "right side is not second premise right", self.name
                    )
                )
            return tuple(violations)
        return (ProofViolation(self.rule, "unknown proof rule", self.name),)


@dataclass(frozen=True)
class YonedaEmbedding(Generic[ObjT]):
    """Finite Yoneda-style observer checker for a small category."""

    category: SmallCategory[ObjT]

    def observations(
        self,
        arrow: CategoryArrow[ObjT],
        observers: Sequence[CategoryArrow[ObjT]],
    ) -> tuple[CategoryArrow[ObjT], ...]:
        return tuple(self.category.compose(arrow, observer) for observer in observers)

    def indistinguishable_by(
        self,
        left: CategoryArrow[ObjT],
        right: CategoryArrow[ObjT],
        observers: Sequence[CategoryArrow[ObjT]],
    ) -> bool:
        if left.domain != right.domain or left.codomain != right.codomain:
            return False
        left_obs = self.observations(left, observers)
        right_obs = self.observations(right, observers)
        return all(
            self.category.equivalent_arrows(left_arrow, right_arrow)
            for left_arrow, right_arrow in zip(left_obs, right_obs)
        )

    def faithful_on(
        self,
        left: CategoryArrow[ObjT],
        right: CategoryArrow[ObjT],
        observers: Sequence[CategoryArrow[ObjT]],
    ) -> bool:
        if not self.indistinguishable_by(left, right, observers):
            return True
        return self.category.equivalent_arrows(left, right)


@dataclass(frozen=True)
class AdjunctionWitness(Generic[ObjT]):
    """Executable witness for an adjunction through unit/counit triangles."""

    name: str
    left_adjoint: Functor[ObjT]
    right_adjoint: Functor[ObjT]
    unit: NaturalTransformation[ObjT]
    counit: NaturalTransformation[ObjT]

    def check_triangle_identities(
        self,
        left_samples: Sequence[ObjT],
        right_samples: Sequence[ObjT],
    ) -> tuple[ProofViolation, ...]:
        violations: list[ProofViolation] = []
        left_category = self.left_adjoint.source
        right_category = self.left_adjoint.target
        for obj in left_samples:
            try:
                unit_component = self.unit.component(obj)
                mapped_unit = self.left_adjoint.map_arrow(unit_component)
                counit_component = self.counit.component(
                    self.left_adjoint.map_object(obj)
                )
                triangle = right_category.compose(mapped_unit, counit_component)
                expected = right_category.identity(self.left_adjoint.map_object(obj))
            except (CategoryCompositionError, KeyError) as exc:
                violations.append(
                    ProofViolation("adjunction-left-triangle", str(exc), str(obj))
                )
                continue
            if not right_category.equivalent_arrows(triangle, expected):
                violations.append(
                    ProofViolation(
                        "adjunction-left-triangle",
                        "left triangle identity does not commute",
                        str(obj),
                    )
                )
        for obj in right_samples:
            try:
                counit_component = self.counit.component(obj)
                mapped_counit = self.right_adjoint.map_arrow(counit_component)
                unit_component = self.unit.component(self.right_adjoint.map_object(obj))
                triangle = left_category.compose(unit_component, mapped_counit)
                expected = left_category.identity(self.right_adjoint.map_object(obj))
            except (CategoryCompositionError, KeyError) as exc:
                violations.append(
                    ProofViolation("adjunction-right-triangle", str(exc), str(obj))
                )
                continue
            if not left_category.equivalent_arrows(triangle, expected):
                violations.append(
                    ProofViolation(
                        "adjunction-right-triangle",
                        "right triangle identity does not commute",
                        str(obj),
                    )
                )
        return tuple(violations)


@dataclass(frozen=True)
class KanExtensionWitness(Generic[ObjT]):
    """Minimal checked witness for a left Kan-extension-style unit."""

    name: str
    along: Functor[ObjT]
    original: Functor[ObjT]
    extension: Functor[ObjT]
    unit: NaturalTransformation[ObjT]

    def check_unit_naturality(
        self,
        sample_arrows: Sequence[CategoryArrow[ObjT]],
    ) -> tuple[ProofViolation, ...]:
        return self.unit.check_naturality(sample_arrows)


class EffectQuantale(Generic[GradeT]):
    """Executable finite-sample effect quantale surface.

    PCC uses this for checker-grade composition.  The structure is only as
    strong as the supplied operations and sampled law checks; it is not a proof
    assistant or an exhaustive theorem prover.
    """

    def __init__(
        self,
        name: str,
        *,
        unit: GradeT,
        bottom: GradeT,
        compose: Callable[[GradeT, GradeT], GradeT],
        join: Callable[[GradeT, GradeT], GradeT],
    ) -> None:
        self.name = name
        self.unit = unit
        self.bottom = bottom
        self._compose = compose
        self._join = join

    def compose(self, left: GradeT, right: GradeT) -> GradeT:
        return self._compose(left, right)

    def join(self, left: GradeT, right: GradeT) -> GradeT:
        return self._join(left, right)

    def join_all(self, grades: Iterable[GradeT]) -> GradeT:
        result = self.bottom
        for grade in grades:
            result = self.join(result, grade)
        return result

    def leq(self, left: GradeT, right: GradeT) -> bool:
        return self.join(left, right) == right

    def check_laws(
        self,
        samples: Sequence[GradeT],
    ) -> tuple[QuantaleViolation, ...]:
        violations: list[QuantaleViolation] = []

        for grade in samples:
            if self.compose(self.unit, grade) != grade:
                violations.append(
                    QuantaleViolation(
                        "compose-left-unit",
                        "unit ; grade must equal grade",
                        (grade,),
                    )
                )
            if self.compose(grade, self.unit) != grade:
                violations.append(
                    QuantaleViolation(
                        "compose-right-unit",
                        "grade ; unit must equal grade",
                        (grade,),
                    )
                )
            if self.join(grade, self.bottom) != grade:
                violations.append(
                    QuantaleViolation(
                        "join-bottom-right",
                        "grade join bottom must equal grade",
                        (grade,),
                    )
                )
            if self.join(self.bottom, grade) != grade:
                violations.append(
                    QuantaleViolation(
                        "join-bottom-left",
                        "bottom join grade must equal grade",
                        (grade,),
                    )
                )
            if self.join(grade, grade) != grade:
                violations.append(
                    QuantaleViolation(
                        "join-idempotent",
                        "grade join grade must equal grade",
                        (grade,),
                    )
                )

        for left in samples:
            for right in samples:
                if self.join(left, right) != self.join(right, left):
                    violations.append(
                        QuantaleViolation(
                            "join-commutative",
                            "join must be commutative",
                            (left, right),
                        )
                    )

        for first in samples:
            for second in samples:
                for third in samples:
                    if self.compose(
                        self.compose(first, second),
                        third,
                    ) != self.compose(first, self.compose(second, third)):
                        violations.append(
                            QuantaleViolation(
                                "compose-associative",
                                "sequential composition must be associative",
                                (first, second, third),
                            )
                        )
                    if self.compose(
                        first,
                        self.join(second, third),
                    ) != self.join(
                        self.compose(first, second),
                        self.compose(first, third),
                    ):
                        violations.append(
                            QuantaleViolation(
                                "left-distributive",
                                "composition must left-distribute over join",
                                (first, second, third),
                            )
                        )
                    if self.compose(
                        self.join(first, second),
                        third,
                    ) != self.join(
                        self.compose(first, third),
                        self.compose(second, third),
                    ):
                        violations.append(
                            QuantaleViolation(
                                "right-distributive",
                                "composition must right-distribute over join",
                                (first, second, third),
                            )
                        )

        return tuple(violations)


@dataclass(frozen=True)
class GradedComputation(Generic[GradeT, ValueT]):
    """A value annotated with an effect grade."""

    value: ValueT
    grade: GradeT

    @classmethod
    def pure(
        cls,
        value: ValueT,
        quantale: EffectQuantale[GradeT],
    ) -> "GradedComputation[GradeT, ValueT]":
        return cls(value=value, grade=quantale.unit)

    def bind(
        self,
        fn: Callable[[ValueT], "GradedComputation[GradeT, object]"],
        quantale: EffectQuantale[GradeT],
    ) -> "GradedComputation[GradeT, object]":
        result = fn(self.value)
        return GradedComputation(
            value=result.value,
            grade=quantale.compose(self.grade, result.grade),
        )


__all__ = [
    "AdjunctionWitness",
    "CategoryArrow",
    "CategoryCompositionError",
    "CategoryPath",
    "CategoryViolation",
    "EffectQuantale",
    "EqualityProofTerm",
    "Functor",
    "GradedComputation",
    "KanExtensionWitness",
    "MonoidalCategory",
    "NaturalTransformation",
    "ProofViolation",
    "QuantaleViolation",
    "SmallCategory",
    "TracedMonoidalCategory",
    "YonedaEmbedding",
]
