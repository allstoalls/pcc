from __future__ import annotations

import pytest

from pcc.library.functional import Err, Left, Nothing, Ok, Right, Some, option


def test_option_map_and_and_then_are_real_values():
    assert Some(2).map(lambda x: x + 3) == Some(5)
    assert Nothing.map(lambda x: x).is_none()
    assert Some(4).and_then(lambda x: Some(x * 2)) == Some(8)
    assert Nothing.unwrap_or(9) == 9


def test_option_unwrap_none_raises_actionable_error():
    with pytest.raises(ValueError, match="Nothing"):
        Nothing.unwrap()


def test_result_map_error_and_chain():
    assert Ok(10).map(lambda x: x + 1) == Ok(11)
    assert Err("bad").map(lambda x: x + 1) == Err("bad")
    assert Err("bad").map_err(str.upper) == Err("BAD")
    assert Ok(3).and_then(lambda x: Ok(x * 4)) == Ok(12)


def test_either_values_are_comparable_and_repr_stable():
    assert Left("err") == Left("err")
    assert Right(42) == Right(42)
    assert repr(Left("x")) == "Left('x')"
    assert repr(Right(1)) == "Right(1)"


def test_option_constructor_turns_nullable_into_explicit_shape():
    assert option(None).is_none()
    assert option("v") == Some("v")
