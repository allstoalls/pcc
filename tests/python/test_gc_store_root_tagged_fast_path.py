"""``pcc_gc_store_root`` must not call refcount helpers for unrefcountable values.

A tagged immediate and NULL both make ``py_incref``/``py_decref`` return at
once, so calling them from ``store_root`` is pure overhead — and codegen emits
roughly 47000 ``store_root`` sites. Profiling a `lst.append(i)` loop under pcc1
put ``pcc_gc_store_root`` at 17.5% of samples against 5.6% for the append
itself; eliding the two no-op calls moved the loop 48 ms -> 30 ms (a 2.8x gap
against CPython narrowed to 1.8x).

The slot store itself is unconditional. These tests exist to pin that: skipping
the *store* for a tagged value would silently drop the root.
"""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python

BACKENDS = ("0", "1", "2", "3", "4")


def _run(tmp_path, source: str, backend: str) -> str:
    src = tmp_path / f"probe{backend}.py"
    exe = tmp_path / f"probe{backend}.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    done = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=240,
        env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("backend", BACKENDS)
def test_rooted_tagged_and_pointer_values_both_survive(tmp_path, backend):
    """Mix tagged ints, a bignum, strings and containers through rooted slots,
    then force a collection: anything whose root was dropped would be gone."""
    assert _run(
        tmp_path,
        '''
        import gc


        def main() -> None:
            box = []
            i = 0
            while i < 400:
                box.append(i)
                box.append("s" + str(i))
                box.append([i])
                i = i + 1
            box.append(1 << 200)
            gc.collect()
            total = 0
            j = 0
            while j < 400:
                total = total + box[j * 3]
                total = total + len(box[j * 3 + 1])
                total = total + box[j * 3 + 2][0]
                j = j + 1
            print(total)
            print(box[1200] == (1 << 200))
            print(len(box))


        main()
        ''',
        backend,
    ) == "\n".join([
        str(sum(range(400)) + sum(len("s" + str(i)) for i in range(400))
            + sum(range(400))),
        "True",
        "1201",
    ])


@pytest.mark.parametrize("backend", BACKENDS)
def test_root_slot_still_holds_a_tagged_value_after_store(tmp_path, backend):
    """The elision covers only the refcount calls -- the slot write must still
    happen, so a tagged value stored into a root must read back."""
    assert _run(
        tmp_path,
        '''
        import gc


        def main() -> None:
            keep = []
            n = 0
            while n < 50:
                keep.append(n * 7)
                n = n + 1
            gc.collect()
            print(keep[0], keep[49], len(keep))


        main()
        ''',
        backend,
    ) == "0 343 50"
