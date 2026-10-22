
from __future__ import annotations

import pytest

from pcc.library.adt import Err, MatchError, Nothing, Ok, Some, check_exhaustive, match


def test_option_result_variants_and_match():
    assert match(Some(3), {"Some": lambda x: x + 1, "Nothing": lambda: 0}) == 4
    assert match(Nothing, {"Some": lambda x: x, "Nothing": lambda: 0}) == 0
    assert match(Ok("v"), {"Ok": lambda x: x, "Err": lambda e: "bad"}) == "v"


def test_match_reports_missing_case_and_exhaustiveness():
    assert check_exhaustive({"Some", "Nothing"}, {"Some"}) == {"Nothing"}
    with pytest.raises(MatchError):
        match(Err("boom"), {"Ok": lambda x: x})
