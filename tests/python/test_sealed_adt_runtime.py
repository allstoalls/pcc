import pytest

from pcc.library.sealed_adt import ExhaustivenessError, SealedADT, VariantSpec, decision_tree_order


def test_sealed_adt_construct_and_exhaustive():
    option = SealedADT("Option", [VariantSpec("Some", ("value",)), VariantSpec("None")])
    assert option.construct("Some", value=1) == ("Some", {"value": 1})
    option.check_exhaustive({"Some", "None"})


def test_sealed_adt_rejects_missing_case():
    option = SealedADT("Option", [VariantSpec("Some", ("value",)), VariantSpec("None")])
    with pytest.raises(ExhaustivenessError):
        option.check_exhaustive({"Some"})


def test_decision_tree_order_respects_hot_case():
    adt = SealedADT("R", [VariantSpec("Err"), VariantSpec("Ok")])
    assert decision_tree_order(adt, ["Ok"]) == ["Ok", "Err"]
