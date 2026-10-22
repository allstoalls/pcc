"""A DynType attribute read's ``py_obj_getattr`` result must be owned.

``py_obj_getattr`` returns a NEW reference, but the generic attribute
emitter never registered its result, so every consumer's
``_gc_release_if_owned`` answered not-owned and the reference leaked:
``print(o.n)`` leaked one object per call
(docs/goal/evidence/2026-08-25-attr-getattr-ownership-investigation.md,
2026-08-25-print-consumer-ownership-investigation.md).

The fix registers the result at the EMITTER
(``_note_owned_dynamic_call_value``, the same mechanism the dynamic-call
and exact-int paths already use) — the emitter is the only place that
knows whether the raising getattr or the borrowed field read ran.

Guard in both directions: a leak keeps the canary alive (0 finalizers);
an over-release fires early or crashes the moving collectors, so the
typed borrowed field-read shape runs under the same canary discipline.
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


_CANARY_PRELUDE = """
DELS: list = [0]


class Canary:
    def __init__(self) -> None:
        self.tag = 7

    def __del__(self) -> None:
        DELS[0] = DELS[0] + 1

    def __str__(self) -> str:
        return "canary"


class Box:
    def __init__(self) -> None:
        self.c = Canary()
"""


def test_dyn_attr_print_releases_getattr_result(tmp_path):
    """print(o.c) through a dyn receiver: the getattr result is owned and
    print borrows, so the canary must die when the box is dropped."""
    native = _run_native(
        tmp_path,
        _CANARY_PRELUDE
        + textwrap.dedent("""
        def probe(o: object) -> None:
            print(o.c)

        def main() -> None:
            b = Box()
            probe(b)
            b = None
            import gc
            gc.collect()
            print(DELS[0])

        main()
        """),
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["canary", "1"], native.stdout


def test_dyn_attr_chain_releases_intermediate(tmp_path):
    """o.inner.c: the intermediate getattr result is the outer read's
    receiver; both owned results must be released exactly once."""
    native = _run_native(
        tmp_path,
        _CANARY_PRELUDE
        + textwrap.dedent("""
        class Outer:
            def __init__(self) -> None:
                self.inner = Box()

        def probe(o: object) -> None:
            print(o.inner.c)

        def main() -> None:
            b = Outer()
            probe(b)
            b = None
            import gc
            gc.collect()
            print(DELS[0])

        main()
        """),
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["canary", "1"], native.stdout


def test_typed_field_read_stays_borrowed(tmp_path):
    """The typed borrowed field-read shape must gain NO release: exactly
    one finalizer when the owner drops, and no early fire before it."""
    native = _run_native(
        tmp_path,
        _CANARY_PRELUDE
        + textwrap.dedent("""
        def probe(b: Box) -> None:
            print(b.c)
            print(DELS[0])

        def main() -> None:
            b = Box()
            probe(b)
            b = None
            import gc
            gc.collect()
            print(DELS[0])

        main()
        """),
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["canary", "0", "1"], native.stdout


def test_dyn_attr_assigned_then_dropped_balances(tmp_path):
    """x = o.c; x = None — whatever the assignment consumer does with the
    owned result, the total balance must be exactly one finalizer."""
    native = _run_native(
        tmp_path,
        _CANARY_PRELUDE
        + textwrap.dedent("""
        def probe(o: object) -> int:
            x = o.c
            got = x.tag
            x = None
            return got

        def main() -> None:
            b = Box()
            t = probe(b)
            b = None
            import gc
            gc.collect()
            print(t)
            print(DELS[0])

        main()
        """),
    )
    assert native.returncode == 0, native.stderr
    assert native.stdout.split() == ["7", "1"], native.stdout
