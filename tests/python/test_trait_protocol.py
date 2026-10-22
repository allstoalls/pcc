from pcc.library.trait_protocol import Trait, check_trait


def test_trait_reports_missing_method():
    trait = Trait("Show").requires("__str__", "() -> str").requires("format", "(str) -> str")

    class X:
        def __str__(self):
            return "x"

    assert check_trait(X(), trait) == ["format"]
