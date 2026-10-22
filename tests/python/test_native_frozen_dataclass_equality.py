"""Frozen-dataclass value equality, including tuples of them.

M5-SELFHOST-DATACLASS-TUPLE-EQ: on 2026-08-18 pcc1 answered
``(L(1,True),L(2,False)) == (L(1,True),L(2,False))`` as False (host True),
which cost render_aarch64_stack_map_section a ~2kB key-string fallback for
31946 of 38540 records.  On 2026-08-27 the defect no longer reproduces:
a HEAD-content pcc1 answers every shape below correctly (direct eq, tuple
of dataclasses, nested dataclass, list ``in`` / ``.index``) — receipts in
the task row.  This regression pins the semantics on the host arm; the
pcc1 arm is covered by the stage gates, which compile this same lowering.

Frozen dataclasses also synthesize ``__hash__`` (hash of the field
tuple), matching CPython: frozen=True keeps instances usable as dict keys
while the plain eq-dataclass rule (``__hash__ = None``) still applies to
unfrozen ones.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).absolute().parents[2]


def _run_native(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60
    )


def test_frozen_dataclass_value_equality_shapes(tmp_path):
    native = _run_native(
        tmp_path,
        """
        from dataclasses import dataclass


        @dataclass(frozen=True)
        class L:
            a: int
            b: bool


        @dataclass(frozen=True)
        class Outer:
            name: str
            inner: L


        def main() -> None:
            print(L(1, True) == L(1, True))
            print(L(1, True) == L(2, False))
            tx = (L(1, True), L(2, False))
            ty = (L(1, True), L(2, False))
            print(tx == ty)
            print(Outer("k", L(3, True)) == Outer("k", L(3, True)))
            print(Outer("k", L(3, True)) == Outer("k", L(4, True)))
            xs = [L(9, True), L(8, False)]
            print(L(8, False) in xs)
            print(xs.index(L(8, False)))
            print(L(7, True) in xs)

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == [
        "True", "False", "True", "True", "False", "True", "1", "False",
    ], native.stdout


def test_frozen_dataclass_is_hashable(tmp_path):
    native = _run_native(
        tmp_path,
        """
        from dataclasses import dataclass


        @dataclass(frozen=True)
        class Inner:
            v: int


        def main() -> None:
            d = {}
            d[(Inner(1), Inner(2))] = 5
            print(d.get((Inner(1), Inner(2)), -1))

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["5"], native.stdout


def test_frozen_dataclass_hash_eq_contract(tmp_path):
    """Equal instances hash equal (same dict slot); unequal miss."""
    native = _run_native(
        tmp_path,
        """
        from dataclasses import dataclass


        @dataclass(frozen=True)
        class K:
            a: int
            b: str


        def main() -> None:
            d = {}
            d[K(1, "x")] = 10
            d[K(1, "x")] = 11
            print(len(d))
            print(d.get(K(1, "x"), -1))
            print(d.get(K(2, "x"), -1))
            print(hash(K(3, "z")) == hash(K(3, "z")))

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["1", "11", "-1", "True"], native.stdout


def test_unfrozen_eq_dataclass_stays_unhashable(tmp_path):
    """CPython: eq=True (default) without frozen sets __hash__ = None; the
    synthesis must not over-apply to unfrozen dataclasses."""
    native = _run_native(
        tmp_path,
        """
        from dataclasses import dataclass


        @dataclass
        class U:
            a: int


        def main() -> None:
            d = {}
            try:
                d[U(1)] = 1
                print("hashed")
            except Exception:
                print("caught")

        main()
        """,
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["caught"], native.stdout
