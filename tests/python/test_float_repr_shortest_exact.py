"""``repr(float)`` must be the shortest string that round-trips.

pcc1 printed `4503599627370496.0` (exactly 2**52) as `4503599627370499.9`, and
`float(str(v))` then gave a third value. The stored double was fine — `int(v)`
and `v == 2**52` both agreed — so only the decimal *formatting* was wrong.

That single literal is the one `pcc/ir/support/_float_bits.py` scales by to extract
a mantissa, so a pcc-built compiler inherited the error: `1000.0` came out as
`0x408F400000000004` instead of `0x408F400000000000`, which needs one extra
`movk` to materialise. Across 13 numeric/time-formatting functions that is +100
bytes of `__TEXT,__text`, which is exactly why `cmp pcc2 pcc3` failed and the
five-GC bootstrap matrix was red on all five backends.

See docs/investigations/pcc1-float-repr-strtod-17-digit-defect.md
"""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python

# Values chosen for the ways float formatting breaks, not for coverage volume.
CASES = [
    "0.0", "-0.0", "1.0", "2.0", "100.0", "1000.0", "0.5", "0.1", "0.3",
    "2.675", "3.14159265358979",
    "123456789012345.0",
    # the 2**52 family -- where the defect showed up
    "4503599627370496.0", "4503599627370498.0", "9007199254740992.0",
    # exponent-form boundaries
    "1e16", "1e22", "1e23", "1e-300",
    # extremes
    "1.7976931348623157e308", "5e-324",
    # a value whose shortest form needs all 17 digits
    "0.30000000000000004",
]


def _run(tmp_path, source: str) -> list[str]:
    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.splitlines()


def test_repr_matches_cpython_for_every_case(tmp_path):
    body = "\n".join(f"    print({c})" for c in CASES)
    got = _run(tmp_path, f"def main() -> None:\n{body}\n\n\nmain()\n")
    want = [repr(eval(c)) for c in CASES]
    mismatches = [
        (c, w, g) for c, w, g in zip(CASES, want, got) if w != g
    ]
    assert not mismatches, "\n".join(
        f"{c}: CPython {w!r} vs pcc {g!r}" for c, w, g in mismatches
    )


def test_repr_round_trips_through_float(tmp_path):
    """Independent of matching CPython's spelling: whatever is printed must
    parse back to the same double. This is the property the shortest-repr loop
    exists to guarantee and the one it could not verify."""
    body = "\n".join(f"    print(float(str({c})) == {c})" for c in CASES)
    got = _run(tmp_path, f"def main() -> None:\n{body}\n\n\nmain()\n")
    assert got == ["True"] * len(CASES), [
        (c, g) for c, g in zip(CASES, got) if g != "True"
    ]


def test_two_pow_52_scaling_constant_is_exact(tmp_path):
    """The specific expression `_float64_to_bits` relies on. A wrong low bit
    here is what propagated into every float literal a pcc-built compiler
    emitted."""
    got = _run(
        tmp_path,
        '''
        def main() -> None:
            big = 4503599627370496.0
            print(big)
            print(int(big))
            print(big == 4503599627370496.0)
            print(0.5 * big)
            print((1.953125 - 1.0) * big)


        main()
        ''',
    )
    assert got == [
        "4503599627370496.0",
        "4503599627370496",
        "True",
        repr(0.5 * 4503599627370496.0),
        repr((1.953125 - 1.0) * 4503599627370496.0),
    ]
