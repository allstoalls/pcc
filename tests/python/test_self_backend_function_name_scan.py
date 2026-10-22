"""The str-based declaration name scan matches _FUNCTION_NAME_RE exactly."""
import random

import pytest

from pcc.backend import self_backend_parse as parse


def _regex(text, position):
    match = parse._FUNCTION_NAME_RE.search(text, position)
    return match.end() - 1 if match is not None else -1


CASES = [
    "declare ptr @py_obj_call(ptr, ptr)",
    'declare void @"quoted name"(i64)',
    "declare i64 @a.b$c-d_9(ptr)",
    "declare void @9bad(i64) @good(i64)",
    'declare void @""(i64) @next(i64)',
    "declare void @name (i64)",
    "declare void @name",
    "@",
    "@(",
    '@"x"',
    '@"x"(',
    "x @.dot( @$d(",
    "declare { ptr, i64 } @pair(ptr) #3",
    "declare void @né(i64)",
    "declare void @é(i64) @ok(i64)",
    "",
]


@pytest.mark.parametrize("text", CASES)
def test_function_name_scan_matches_regex_at_every_start(text):
    for position in range(len(text) + 1):
        assert parse._function_name_paren(text, position) == _regex(text, position), (text, position)


def test_function_name_scan_matches_regex_on_random_text():
    rng = random.Random(20261019)
    alphabet = '@"( ).$-_aZ9é\n'
    for _ in range(4000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 24)))
        for position in range(len(text) + 1):
            assert parse._function_name_paren(text, position) == _regex(text, position), (text, position)
