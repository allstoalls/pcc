"""Spatial memory safety for the no-libpython Python frontend.

Source: *Low-Level Software Security for Compiler Developers* — out-of-bounds
reads are a primary information-disclosure primitive. In Python the safe
behavior is that an out-of-range subscript raises ``IndexError`` (or
``KeyError`` for a mapping) instead of reading adjacent memory. pcc lowers
container subscripts to native runtime calls; a path that skips the bounds
check turns ``s[i]`` into an unchecked load of whatever lies past the object.

Verified behavior (no-libpython, LLVM backend) at the time of writing:
  * list out-of-range -> IndexError        (checked, PASS)
  * list negative index               -> OK (PASS)
  * dict missing key -> KeyError            (checked, PASS)
  * slice out of range -> clamped/empty     (PASS)
  * str  out-of-range -> IndexError        (checked, PASS)
  * bytes out-of-range -> IndexError       (checked, PASS)
  * tuple out-of-range -> IndexError       (checked, PASS)
"""
from __future__ import annotations

import pytest

PROGRAM = r"""
def main() -> int:
    xs = [10, 20, 30]
    try:
        _ = xs[99]
        print("LIST_OOB_NORAISE")
    except IndexError:
        print("LIST_OOB_RAISES")
    print("NEG_INDEX:" + str(xs[-1]))
    d = {"k": 1}
    try:
        _ = d["missing"]
        print("DICT_MISS_NORAISE")
    except KeyError:
        print("DICT_MISS_RAISES")
    s = "abc"
    try:
        _ = s[99]
        print("STR_OOB_NORAISE")
    except IndexError:
        print("STR_OOB_RAISES")
    b = b"xy"
    try:
        _ = b[99]
        print("BYTES_OOB_NORAISE")
    except IndexError:
        print("BYTES_OOB_RAISES")
    print("SLICE:" + s[1:99] + "|" + s[99:200])
    # Adversarial variants: a literal index takes the exact-int lowering path,
    # a variable index takes the general subscript path, and a large negative
    # index must not read backwards past the object either. bytearray shares
    # the bytes-like path.
    i = 99
    try:
        _ = s[i]
        print("STR_VAR_OOB_NORAISE")
    except IndexError:
        print("STR_VAR_OOB_RAISES")
    try:
        _ = s[-99]
        print("STR_NEG_OOB_NORAISE")
    except IndexError:
        print("STR_NEG_OOB_RAISES")
    try:
        _ = b[i]
        print("BYTES_VAR_OOB_NORAISE")
    except IndexError:
        print("BYTES_VAR_OOB_RAISES")
    ba = bytearray(b"xy")
    try:
        _ = ba[99]
        print("BYTEARRAY_OOB_NORAISE")
    except IndexError:
        print("BYTEARRAY_OOB_RAISES")
    # Tuple subscript OOB must raise IndexError too (was py_tuple_get, which
    # returned NULL silently). Literal index takes the exact-int lowering path;
    # a variable index takes the general subscript path; both call py_tuple_get*
    # so both must bounds-check. A large negative index must not read backwards.
    t = (10, 20, 30)
    try:
        _ = t[99]
        print("TUPLE_OOB_NORAISE")
    except IndexError:
        print("TUPLE_OOB_RAISES")
    print("TUPLE_NEG:" + str(t[-1]))
    print("TUPLE_IN:" + str(t[1]))
    ti = 99
    try:
        _ = t[ti]
        print("TUPLE_VAR_OOB_NORAISE")
    except IndexError:
        print("TUPLE_VAR_OOB_RAISES")
    try:
        _ = t[-99]
        print("TUPLE_NEG_OOB_NORAISE")
    except IndexError:
        print("TUPLE_NEG_OOB_RAISES")
    return 0


main()
"""


@pytest.fixture(scope="module")
def out(compile_and_run):
    r = compile_and_run(PROGRAM, backend="self")
    # The program is written to always run to completion (it never propagates
    # an exception), so a non-zero exit is itself a failure to surface.
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


@pytest.fixture(scope="module")
def out_self(compile_and_run):
    # Same program through pcc's own LLVM-free `self` backend: the bounds check
    # must not depend on a silent fallback to the LLVM path (S-track obligation).
    r = compile_and_run(PROGRAM, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_list_index_out_of_range_raises(out):
    assert "LIST_OOB_RAISES" in out


def test_list_negative_index(out):
    assert "NEG_INDEX:30" in out


def test_dict_missing_key_raises(out):
    assert "DICT_MISS_RAISES" in out


def test_slice_out_of_range_is_clamped(out):
    # Python never errors on an out-of-range slice; it clamps. s[1:99]=="bc",
    # s[99:200]=="".
    assert "SLICE:bc|" in out


def test_str_index_out_of_range_raises(out):
    assert "STR_OOB_RAISES" in out


def test_bytes_index_out_of_range_raises(out):
    assert "BYTES_OOB_RAISES" in out


def test_str_variable_index_out_of_range_raises(out):
    # Variable index -> general subscript path (not the exact-int literal path).
    assert "STR_VAR_OOB_RAISES" in out


def test_str_negative_index_out_of_range_raises(out):
    # A large negative index must raise, not read backwards past the object.
    assert "STR_NEG_OOB_RAISES" in out


def test_bytes_variable_index_out_of_range_raises(out):
    assert "BYTES_VAR_OOB_RAISES" in out


def test_bytearray_index_out_of_range_raises(out):
    assert "BYTEARRAY_OOB_RAISES" in out


def test_tuple_index_out_of_range_raises(out):
    # Literal index -> exact-int lowering path.
    assert "TUPLE_OOB_RAISES" in out


def test_tuple_negative_and_in_range_index(out):
    # Negative and in-range indices still return the right element (not raise).
    assert "TUPLE_NEG:30" in out
    assert "TUPLE_IN:20" in out


def test_tuple_variable_index_out_of_range_raises(out):
    # Variable index -> general subscript path (not the exact-int literal path).
    assert "TUPLE_VAR_OOB_RAISES" in out


def test_tuple_negative_index_out_of_range_raises(out):
    # A large negative index must raise, not read backwards past the object.
    assert "TUPLE_NEG_OOB_RAISES" in out


def test_str_bytes_oob_raises_on_self_backend(out_self):
    # No silent fallback to LLVM: the self backend must bounds-check too.
    assert "STR_OOB_RAISES" in out_self
    assert "STR_VAR_OOB_RAISES" in out_self
    assert "STR_NEG_OOB_RAISES" in out_self
    assert "BYTES_OOB_RAISES" in out_self
    assert "BYTEARRAY_OOB_RAISES" in out_self


def test_tuple_oob_raises_on_self_backend(out_self):
    # No silent fallback to LLVM: the self backend must bounds-check tuple too.
    assert "TUPLE_OOB_RAISES" in out_self
    assert "TUPLE_VAR_OOB_RAISES" in out_self
    assert "TUPLE_NEG_OOB_RAISES" in out_self
    assert "TUPLE_NEG:30" in out_self
    assert "TUPLE_IN:20" in out_self
