"""``a, b = x, 0`` must give ``a`` its own reference to ``x``'s object.

The tuple-unpack store protocol defaults to "any non-Dyn value is owned",
which is right for ``py_tuple_get`` results and wrong for a destructured
literal whose element is a plain ``Name``: the target was marked owned without
a retain, so two locals owned one reference.  In
``arm64_asm_driver.AArch64ModuleBuilder._append_line``,
``symbol, offset = item, 0`` followed by ``symbol = symbol.strip()`` freed the
string ``item`` still held; ``item``'s loop cleanup then released a dead cell.
With the refcount provenance probe on that was silently counted in
PCC_GC_COUNTER_UNMANAGED_REFCOUNT_OPS; with it off the freed cell's free-list
link was decremented and pcc1 died in ``pcc_allocator_alloc_object``.

These programs run under ``PCC_GC_REFCOUNT_PROVENANCE_PROBE=1`` and assert
that the counter stays at zero and the results are right.  They are compiled
with the four codegen fast paths forced off so the counter can see every
refcount, then again with the tree defaults.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

FLAGS_OFF = {
    "PCC_KNOWN_OBJECT_REFS": "0",
    "PCC_FAST_COMPLETED_CONTINUATIONS": "0",
    "PCC_GENERATOR_FIRST_ENTRY_INIT": "0",
    "PCC_DIRECT_GENERATOR_TASKS": "0",
}

PROGRAM = textwrap.dedent(
    '''
    from pcc.extern import extern, c_int64
    pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)


    def alias_then_rebind(rest: str) -> int:
        # The _append_line shape: alias a loop item, then rebind the alias.
        total = 0
        for item in [part.strip() for part in rest.split(",") if part.strip()]:
            symbol, offset = item, 0
            symbol = symbol.strip()
            total += len(symbol) + offset + len(item)
        return total


    def swap(n: int) -> int:
        a = "left" + str(n)
        b = "right" + str(n)
        a, b = b, a
        a, b = b, a
        return len(a) + len(b) * 3


    def same_source(rest: str) -> int:
        first, second = rest, rest
        first = first.upper()
        return len(first) + len(second)


    def rebound_targets(rest: str) -> int:
        head, tail = rest, rest[1:]
        head, tail = tail, head
        head, tail = tail.strip(), head.strip()
        return len(head) + len(tail)


    def main() -> None:
        before = pcc_gc_telemetry(116)
        total = 0
        for i in range(40):
            total += alias_then_rebind("_sym_a, _sym_b ,_sym_c")
            total += swap(i)
            total += same_source("abcdef")
            total += rebound_targets(" xyz ")
        print(total)
        print(pcc_gc_telemetry(116) - before)


    main()
    '''
)

EXPECTED_TOTAL = 40 * (
    (len("_sym_a") * 2 + len("_sym_b") * 2 + len("_sym_c") * 2)
    + 0  # swap: computed per i below
    + (len("ABCDEF") + len("abcdef"))
    + (len("xyz") + len("xyz"))  # rebound_targets: both end up as "xyz"
) + sum(len("left" + str(i)) + len("right" + str(i)) * 3 for i in range(40))


def _compile(tmp_path: Path, name: str, extra_env: dict[str, str]) -> Path:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    exe = tmp_path / f"{name}.out"
    src.write_text(PROGRAM, encoding="utf-8")
    saved = {key: os.environ.get(key) for key in extra_env}
    try:
        os.environ.update(extra_env)
        compile_python(
            str(src),
            str(exe),
            ir_scaffold_mode="on",
            libpython_mode="off",
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return exe


@pytest.mark.parametrize("variant", ["flags_off", "tree_defaults"])
def test_destructured_literal_elements_take_their_own_reference(tmp_path, variant):
    exe = _compile(tmp_path, variant, FLAGS_OFF if variant == "flags_off" else {})
    env = dict(os.environ)
    env["PCC_GC_BACKEND"] = "0"
    env["PCC_GC_REFCOUNT_PROVENANCE_PROBE"] = "1"
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    total, unmanaged = result.stdout.split()
    assert int(total) == EXPECTED_TOTAL
    assert unmanaged == "0"
    # And the program survives with the probe off, i.e. no double release
    # reaches the allocator's free list.
    env.pop("PCC_GC_REFCOUNT_PROVENANCE_PROBE")
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[0] == str(EXPECTED_TOTAL)
