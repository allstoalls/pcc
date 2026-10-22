"""A set/dict must never lose an element while a free slot exists.

``py_set``/``py_dict`` probe with CPython's perturb recurrence
``j = (j * 5 + perturb + 1) & mask`` and bounded the loop at ``capacity * 2``
probes.  That bound is not sufficient: ``perturb`` needs **13** shifts to decay
from a 64-bit value to zero, and only once it is zero does the recurrence
become a full-period generator over the table (a=5, c=1, m=2**k satisfies
Hull-Dobell).  At capacity 8 that left three full-period probes.

A run of **negative pointer-aligned** keys is the worst case: they all start at
the same slot, and the sequence could then oscillate over a handful of slots
until the budget ran out — with free slots never visited.  The element was then
dropped **with no error**.

Measured before the fix, at capacity 8 inserting -48 after -8..-40::

    probe slots visited:    [0, 7, 3, 7, 3, 7, 3, 7, 3, 7, 3, 7, 3, 0, 1, 6]
    distinct slots reached: {0, 1, 3, 6, 7}
    table:                  [-8, -16, None, -40, None, None, -24, -32]

Both the pcc-Python port and the C mirror had it, so host-side tests could
never see it: host pcc runs on CPython's own containers.

Why it mattered: frame offsets are negative and pointer-aligned, so
``active_offsets`` in the precise stack-map planner is exactly this shape.  A
dropped offset made a live GC root look inactive and rejected 19 functions
during Stage2.  See
``docs/investigations/pcc1-stage2-stale-managed-self-outlives-root.md``.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest


# Sizes 1..12 walk across the capacity-8 growth boundary (a set/dict grows when
# fill exceeds (capacity * 2) // 3, i.e. on the 6th insert) and the capacity-16
# one after it.  n=6 is where this first failed.
_MAX_N = 12


_SET_PROGRAM = """
def report(n: int, got: int, missing: list) -> None:
    print(str(n) + ":" + str(got) + ":" + ",".join([str(x) for x in missing]))


def run(n: int) -> None:
    s: set = set()
    i: int = 0
    while i < n:
        s.add(-8 * (i + 1))
        i = i + 1
    missing: list = []
    i = 0
    while i < n:
        if (-8 * (i + 1)) not in s:
            missing.append(-8 * (i + 1))
        i = i + 1
    report(n, len(s), missing)


def main() -> None:
    n: int = 1
    while n <= 12:
        run(n)
        n = n + 1


main()
"""


_DICT_PROGRAM = """
def report(n: int, got: int, missing: list) -> None:
    print(str(n) + ":" + str(got) + ":" + ",".join([str(x) for x in missing]))


def run(n: int) -> None:
    d: dict = {}
    i: int = 0
    while i < n:
        d[-8 * (i + 1)] = i
        i = i + 1
    missing: list = []
    i = 0
    while i < n:
        if (-8 * (i + 1)) not in d:
            missing.append(-8 * (i + 1))
        i = i + 1
    report(n, len(d), missing)


def main() -> None:
    n: int = 1
    while n <= 12:
        run(n)
        n = n + 1


main()
"""


_PROGRAMS = {"set": _SET_PROGRAM, "dict": _DICT_PROGRAM}


@pytest.mark.parametrize("kind", ["set", "dict"])
def test_negative_aligned_keys_are_never_dropped(tmp_path, kind):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(_PROGRAMS[kind].lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120,
    )
    assert native.returncode == 0, native.stderr
    lines = native.stdout.split()
    assert len(lines) == _MAX_N, native.stdout
    for n, line in enumerate(lines, start=1):
        want_n, got, missing = line.split(":", 2)
        assert int(want_n) == n
        assert int(got) == n, (
            f"{kind} with {n} negative pointer-aligned keys reported "
            f"len {got}; dropped {missing or '(unknown)'}\n{native.stdout}"
        )
        assert missing == "", (
            f"{kind} lost {missing} at n={n}\n{native.stdout}"
        )


_PROBE_SOURCES = ("pcc/runtime/py/py_set.py", "pcc/runtime/py/py_dict.py")

# ceil(64 / 5): shifts for a 64-bit perturb to reach zero.  Only once it IS
# zero is `j = (j * 5 + 1) & mask` full-period, so a budget below
# `_MIN_DECAY_SHIFTS + capacity` can give up while a free slot exists.
_MIN_DECAY_SHIFTS = 13


def _probe_budget_addends(text: str) -> list[int]:
    """Every ``capacity + N`` / ``capacity * N`` used as a probe bound."""
    found: list[int] = []
    for match in re.finditer(
        r"(?:probes\s*<\s*|limit\s*=\s*)(?:s->)?capacity\s*([+*])\s*(\d+)",
        text,
    ):
        operator, number = match.group(1), int(match.group(2))
        # A multiplicative bound is only sufficient if it dominates
        # `capacity + 13` at *every* capacity, and `capacity * k` does not:
        # at capacity 8, `capacity * 2` is 16 against the 21 required.
        found.append(number if operator == "+" else -number)
    return found


def test_probe_budget_is_at_least_decay_plus_a_full_period():
    """Pin the bound itself, not merely the absence of the old string.

    Asserting ``"capacity * 2" not in text`` would also pass for
    ``capacity + 1``, which is just as broken.  Assert the actual addend.
    """
    shifts = 0
    perturb = (1 << 64) - 1
    while perturb:
        perturb >>= 5
        shifts += 1
    assert shifts == _MIN_DECAY_SHIFTS

    for source in _PROBE_SOURCES:
        text = Path(source).read_text(encoding="utf-8")
        addends = _probe_budget_addends(text)
        assert addends, f"{source}: found no capacity-derived probe bound"
        for addend in addends:
            assert addend > 0, (
                f"{source}: a multiplicative probe bound (capacity * "
                f"{-addend}) is insufficient — at capacity 8 it gives "
                f"{8 * -addend} probes against the "
                f"{_MIN_DECAY_SHIFTS + 8} required"
            )
            assert addend >= _MIN_DECAY_SHIFTS, (
                f"{source}: probe budget capacity + {addend} is below the "
                f"required capacity + {_MIN_DECAY_SHIFTS}; the loop can give "
                f"up while a free slot exists"
            )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["set", "dict"])
def test_negative_aligned_keys_survive_the_c_mirror(tmp_path, monkeypatch, kind):
    """The C runtime had the identical defect, so it needs the identical gate.

    Marked integration because selecting it rebuilds the runtime archive; it
    is deselectable, never skipped.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(_PROGRAMS[kind].lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=300,
    )
    assert native.returncode == 0, native.stderr
    lines = native.stdout.split()
    assert len(lines) == _MAX_N, native.stdout
    for n, line in enumerate(lines, start=1):
        want_n, got, missing = line.split(":", 2)
        assert int(want_n) == n
        assert int(got) == n and missing == "", (
            f"C mirror {kind} lost {missing} at n={n}\n{native.stdout}"
        )
