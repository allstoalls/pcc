import shlex

import pytest

from pcc.stdlib import shlex as owned


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


@pytest.mark.parametrize("text", [
    "", "abcXYZ019_@%+=:,./-", "a b", "a'b", 'a"b', "$value", "`command`",
    "line\nline", "tab\tvalue", "é", "日本語", "a;b", "a\\b",
])
def test_owned_quote_matches_cpython_and_round_trips(text):
    quoted = owned.quote(text)
    assert quoted == shlex.quote(text)
    assert shlex.split(quoted) == [text]
    assert owned.split(quoted) == [text]


@pytest.mark.parametrize("parts", [
    [], [""], ["a b", "a'b", "", "é", "$value"],
    ["PCC_X=a b", "/path with spaces/program", "--flag=a@b+c,d"],
])
def test_owned_join_matches_cpython_and_preserves_argument_boundaries(parts):
    assert owned.join(parts) == shlex.join(parts)
    assert owned.split(owned.join(parts)) == parts
