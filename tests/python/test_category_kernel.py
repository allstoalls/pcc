from __future__ import annotations

import pytest

from pcc.diagnostics.contracts.category import (
    AdjunctionWitness,
    CategoryArrow,
    CategoryCompositionError,
    EffectQuantale,
    EqualityProofTerm,
    Functor,
    GradedComputation,
    KanExtensionWitness,
    MonoidalCategory,
    NaturalTransformation,
    TracedMonoidalCategory,
    YonedaEmbedding,
    SmallCategory,
)


def _toy_category() -> SmallCategory[str]:
    return SmallCategory(
        "Toy",
        objects=("A", "B", "C", "D"),
        arrows=(
            CategoryArrow("f", "A", "B"),
            CategoryArrow("g", "B", "C"),
            CategoryArrow("h", "C", "D"),
            CategoryArrow("bad", "A", "C"),
        ),
    )


def test_category_kernel_composes_paths_and_preserves_factors() -> None:
    category = _toy_category()

    path = category.path(["f", "g", "h"], start="A")
    composed = category.compose_path(path, name="hgf")

    assert path.domain == "A"
    assert path.codomain == "D"
    assert path.factors == ("f", "g", "h")
    assert composed.name == "hgf"
    assert composed.domain == "A"
    assert composed.codomain == "D"
    assert composed.factors == ("f", "g", "h")


def test_category_kernel_checks_identity_and_associativity_laws() -> None:
    category = _toy_category()
    f = category.arrow("f")
    g = category.arrow("g")
    h = category.arrow("h")

    assert category.identity_laws_hold(f)
    assert category.identity_laws_hold(g)
    assert category.compose(category.identity("A"), category.identity("A")).is_identity
    assert category.associativity_holds(f, g, h)


def test_functor_and_natural_transformation_laws_are_executable() -> None:
    category = _toy_category()
    functor = Functor.identity(category, name="Id")
    sample_path = category.path(["f", "g"], start="A")

    assert functor.check_laws([sample_path]) == ()
    mapped_path = functor.map_path(sample_path)
    assert mapped_path.domain == "A"
    assert mapped_path.codomain == "C"
    assert mapped_path.factors == ("f", "g")

    identity_transformation = NaturalTransformation(
        "id_to_id",
        source_functor=functor,
        target_functor=functor,
        components={obj: category.identity(obj) for obj in category.objects()},
    )
    assert (
        identity_transformation.check_naturality(
            [category.arrow("f"), category.arrow("g"), category.arrow("bad")]
        )
        == ()
    )


def test_monoidal_and_traced_coherence_checks_are_executable() -> None:
    category = SmallCategory(
        "OneObject",
        objects=("I",),
        arrows=(CategoryArrow("tick", "I", "I"),),
    )

    monoidal = MonoidalCategory(
        category=category,
        unit_object="I",
        tensor_object=lambda _left, _right: "I",
        tensor_arrow=lambda left, right: CategoryArrow(
            f"{left.name}*{right.name}",
            "I",
            "I",
            factors=left.factors + right.factors,
            is_identity=left.is_identity and right.is_identity,
        ),
    )

    assert monoidal.check_object_coherence(["I"]) == ()
    assert monoidal.check_interchange(
        category.arrow("tick"),
        category.arrow("tick"),
        category.arrow("tick"),
        category.arrow("tick"),
    )

    traced = TracedMonoidalCategory(
        monoidal=monoidal,
        trace=lambda _arrow, _feedback: category.identity("I"),
    )
    assert traced.check_yanking(["I"], ["I"]) == ()


def test_yoneda_observers_and_equality_proof_terms_are_checkable() -> None:
    category = SmallCategory(
        "Parallel",
        objects=("A", "B"),
        arrows=(
            CategoryArrow("f", "A", "B"),
            CategoryArrow("g", "A", "B"),
        ),
    )
    f = category.arrow("f")
    g = category.arrow("g")
    observer = category.identity("B")
    yoneda = YonedaEmbedding(category)

    assert yoneda.indistinguishable_by(f, f, [observer])
    assert not yoneda.indistinguishable_by(f, g, [observer])
    assert yoneda.faithful_on(f, g, [observer])

    refl = EqualityProofTerm("f_refl", f, f, "refl")
    assert refl.check(category) == ()

    sym = EqualityProofTerm("f_sym", f, f, "sym", premises=(refl,))
    assert sym.check(category) == ()

    trans = EqualityProofTerm("f_trans", f, f, "trans", premises=(refl, refl))
    assert trans.check(category) == ()

    bad = EqualityProofTerm("bad_refl", f, g, "refl")
    violations = bad.check(category)
    assert len(violations) == 1
    assert violations[0].law == "refl"


def test_adjunction_and_kan_extension_witnesses_check_identity_case() -> None:
    category = _toy_category()
    identity = Functor.identity(category, name="Id")
    id_after_id = identity.then(identity, name="IdAfterId")
    components = {obj: category.identity(obj) for obj in category.objects()}

    unit = NaturalTransformation(
        "unit",
        source_functor=identity,
        target_functor=id_after_id,
        components=components,
    )
    counit = NaturalTransformation(
        "counit",
        source_functor=id_after_id,
        target_functor=identity,
        components=components,
    )

    adjunction = AdjunctionWitness(
        "IdAdjId",
        left_adjoint=identity,
        right_adjoint=identity,
        unit=unit,
        counit=counit,
    )
    assert (
        adjunction.check_triangle_identities(
            category.objects(),
            category.objects(),
        )
        == ()
    )

    kan = KanExtensionWitness(
        "LanIdId",
        along=identity,
        original=identity,
        extension=identity,
        unit=unit,
    )
    assert (
        kan.check_unit_naturality(
            [category.arrow("f"), category.arrow("g"), category.arrow("bad")]
        )
        == ()
    )


def test_category_kernel_rejects_non_composable_paths() -> None:
    category = _toy_category()

    violations = category.check_arrows(
        [category.arrow("f"), category.arrow("bad")],
        start="A",
    )
    assert len(violations) == 1
    assert violations[0].message == "arrow domain does not match previous codomain"
    assert violations[0].index == 1
    assert violations[0].arrow_name == "bad"

    with pytest.raises(CategoryCompositionError, match="does not match"):
        category.path(["f", "bad"], start="A")


def test_category_kernel_rejects_duplicate_or_outside_arrows() -> None:
    with pytest.raises(ValueError, match="duplicate category arrow"):
        SmallCategory(
            "Dup",
            objects=("A",),
            arrows=(
                CategoryArrow("same", "A", "A"),
                CategoryArrow("same", "A", "A"),
            ),
        )

    with pytest.raises(ValueError, match="domain outside category"):
        SmallCategory(
            "Outside",
            objects=("A",),
            arrows=(CategoryArrow("outside", "B", "A"),),
        )


def test_effect_quantale_checks_unit_associativity_and_distributivity() -> None:
    quantale: EffectQuantale[frozenset[str]] = EffectQuantale(
        "PowersetEffects",
        unit=frozenset(),
        bottom=frozenset(),
        compose=lambda left, right: frozenset(set(left) | set(right)),
        join=lambda left, right: frozenset(set(left) | set(right)),
    )

    read = frozenset({"read"})
    write = frozenset({"write"})
    alloc = frozenset({"alloc"})
    samples = (quantale.unit, read, write, alloc, read | write)

    assert quantale.compose(read, write) == frozenset({"read", "write"})
    assert quantale.join(read, write) == frozenset({"read", "write"})
    assert quantale.leq(read, read | write)
    assert not quantale.leq(read | write, read)
    assert quantale.join_all([read, write, alloc]) == frozenset(
        {"read", "write", "alloc"}
    )
    assert quantale.check_laws(samples) == ()


def test_graded_computation_bind_composes_effect_grades() -> None:
    quantale: EffectQuantale[frozenset[str]] = EffectQuantale(
        "PowersetEffects",
        unit=frozenset(),
        bottom=frozenset(),
        compose=lambda left, right: frozenset(set(left) | set(right)),
        join=lambda left, right: frozenset(set(left) | set(right)),
    )

    def read(value: int) -> GradedComputation[frozenset[str], int]:
        return GradedComputation(value + 1, frozenset({"read"}))

    def write(value: int) -> GradedComputation[frozenset[str], int]:
        return GradedComputation(value * 2, frozenset({"write"}))

    pure = GradedComputation.pure(3, quantale)
    left_identity = pure.bind(read, quantale)
    direct = read(3)
    assert left_identity == direct

    m = GradedComputation(3, frozenset({"alloc"}))
    right_identity = m.bind(
        lambda value: GradedComputation.pure(value, quantale), quantale
    )
    assert right_identity == m

    left_assoc = m.bind(read, quantale).bind(write, quantale)
    right_assoc = m.bind(lambda value: read(value).bind(write, quantale), quantale)
    assert left_assoc == right_assoc
    assert left_assoc.value == 8
    assert left_assoc.grade == frozenset({"alloc", "read", "write"})
