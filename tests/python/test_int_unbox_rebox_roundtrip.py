"""`py_int_from_i64(py_int_to_i64(x))` truncating a bignum to 0 -- KNOWN GAP.

These are `xfail(strict=True)`: the behaviour is wrong today and the fix was
attempted and reverted, so they must stay red-as-expected and will FAIL LOUDLY
the moment someone fixes it, prompting removal of the markers.

Attempt and measured outcome (2026-08-19), recorded so it is not retried the
same way: recovering the source object at the box site works for the shapes it
covers, but is unsound at `marshal_to_object` in both directions -- with a
retain it leaks one reference per call (20k iters 17 MB, 200k iters 162 MB,
against a flat 1 MB baseline) because the release side dedups by value identity
and releases once; without the retain the value is freed early. The correct
design REGISTERS the recovered value with the ownership lowering as that call's
owned result instead of increfing it. Tracked as M5-SELFHOST-BIG-INT-LITERAL.


`marshal_from_object` unboxes an int-typed object to i64 and the overflow slot
it passes was documented "caller can ignore". For a value above 2**63-1
`py_int_to_i64` yields 0, so an immediate re-box produced 0 instead of the
original bignum. Because the parser lifts literals with `int(e.text, 0)`
(`pcc/frontends/python/py_lift.py:583`), that also made every over-i64 *literal* in
compiled source become 0 -- which is what left pcc2 unable to print any integer
(a mask spelled `& 0xFFFFFFFFFFFFFFFF` became `& 0`).

The box direction now hands back the object that was unboxed, retained to match
`py_int_from_i64`'s new-owned-reference contract.

Still open, tracked as INT-P0-PROJ / M5-SELFHOST-BIG-INT-LITERAL: a value that
passes through a local inferred as exact-int is stored in an i64 slot, so it is
lost there regardless of this elision. Only the adjacent unbox/re-box pair is
recovered here.
"""

from __future__ import annotations

import subprocess

import pytest

from pcc1_gate import repo_root

REPO = repo_root()

_OVER_I64 = "9223372036854775808"  # 2**63, one above the signed i64 maximum


def _compile_and_run(tmp_path, name: str, source: str) -> str:
    src = tmp_path / (name + ".py")
    src.write_text(source, encoding="utf-8")
    binary = tmp_path / name
    compile_proc = subprocess.run(
        [
            "uv", "run", "pcc",
            "--backend", "self",
            "--python-libpython", "off",
            "--ir-scaffold=on",
            str(src), "-o", str(binary),
        ],
        cwd=str(REPO),
        check=False,
        text=True,
        capture_output=True,
        timeout=600,
    )
    assert compile_proc.returncode == 0, compile_proc.stdout + compile_proc.stderr
    run_proc = subprocess.run(
        [str(binary)],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
        env={"PCC_DEBUG_RUNTIME": "1", "PATH": "/usr/bin:/bin"},
    )
    assert run_proc.returncode == 0, run_proc.stderr
    assert "BAD_INCREF" not in run_proc.stderr, run_proc.stderr
    return run_proc.stdout


@pytest.mark.xfail(strict=True, reason="M5-SELFHOST-BIG-INT-LITERAL: box(unbox(x)) truncates above 2**63-1; fix attempted and reverted as unsound")
@pytest.mark.integration
def test_over_i64_int_survives_an_adjacent_unbox_rebox(tmp_path):
    out = _compile_and_run(
        tmp_path,
        "roundtrip",
        "def take(v) -> str:\n"
        "    return str(v)\n"
        "\n"
        'print(take(int("' + _OVER_I64 + '", 0)))\n',
    )
    assert out.strip() == _OVER_I64, out


@pytest.mark.xfail(strict=True, reason="M5-SELFHOST-BIG-INT-LITERAL: depends on the reverted recovery")
@pytest.mark.integration
def test_recovered_object_is_retained_not_borrowed(tmp_path):
    """Returning the borrowed original would over-release under repetition."""
    out = _compile_and_run(
        tmp_path,
        "retain",
        "def take(v) -> str:\n"
        "    return str(v)\n"
        "\n"
        "i = 0\n"
        "total = 0\n"
        "while i < 20000:\n"
        '    total = total + len(take(int("' + _OVER_I64 + '", 0)))\n'
        "    i = i + 1\n"
        "print(total)\n",
    )
    assert out.strip() == str(len(_OVER_I64) * 20000), out
