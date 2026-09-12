import shlex

import pytest

from pcc.py_stdlib import shlex as owned


@pytest.mark.parametrize("text", [
    "'' a \"\"", "PCC_X='a b' '/path with spaces/program' ''",
    "'a'\"'\"'b'", r'"a\"b" "c\\d" "e\q"', "one # comment\ntwo",
    "a\\ b", "'line\nline' last", r'"$value `name` \$literal"',
])
@pytest.mark.parametrize("comments", [False, True])
def test_owned_split_matches_posix_argument_boundaries(text, comments):
    assert owned.split(text, comments=comments) == shlex.split(text, comments=comments)


@pytest.mark.parametrize("text", ["'unterminated", '"unterminated', "escape\\"])
def test_owned_split_rejects_incomplete_quoting(text):
    with pytest.raises(ValueError):
        owned.split(text)
