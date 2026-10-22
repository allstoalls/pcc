"""The searching operand splitter agrees with the character scan.

The scan visited every character of every uncached instruction; under pcc1
each step is a boxed string index. Flat operands, the only kind the emitter
writes, are now split with native searches, and any other text (nested or
unbalanced brackets, a comma inside brackets) still takes the scan.
"""

import random

import pytest

from pcc.backend import x86_64_encode as encoder


def _outcome(split, text):
    try:
        return ("ok", split(text))
    except encoder.X86EncodeError as error:
        return ("error", str(error))


@pytest.mark.parametrize("text", [
    "", " ", "rax", "rax, rbx", "  rax ,  rbx  ", "rax,", "rax,,rbx", ",rax",
    "QWORD PTR [rbp - 8], rax", "rax, QWORD PTR [rbp + rcx*8 - 16]",
    "DWORD PTR value[rip], 9", "[rax], [rbx]", "[a, b]", "[[a]]", "a]", "[a",
    "] [", "[a] b] [c", "[,]", "x [y] , z",
])
def test_known_shapes_match_the_scan(text):
    assert _outcome(encoder._split_operands, text) == _outcome(encoder._scan_operands, text)


def test_random_text_matches_the_scan():
    rng = random.Random(20261020)
    alphabet = "ab [],"
    for _ in range(20000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
        assert _outcome(encoder._split_operands, text) == _outcome(encoder._scan_operands, text), text
