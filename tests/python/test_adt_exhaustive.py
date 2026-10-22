from pcc.library.adt_exhaustive import SealedADT, Variant, check_exhaustive, missing_patterns


def test_missing_patterns():
    maybe = SealedADT("Maybe", (Variant("Some"), Variant("None_")))
    assert missing_patterns(maybe, {"Some"}) == {"None_"}


def test_wildcard_is_exhaustive():
    check_exhaustive(SealedADT("X", (Variant("A"),)), set(), wildcard=True)
