"""A cell freed twice is leaked and counted, not linked onto the free list twice.

`free()` routed an object-family cell to `pcc_allocator_put_small_object`
without asking whether it was already free.  A second free therefore pushed
one cell onto its size-class list twice, so a later pair of allocations
returned the same address to two owners.  The symptom always landed somewhere
else: a misaligned release store inside `pcc_allocator_alloc_object`, or a
`TypeError` in code that never touched the object.

`free()` now checks a poison word first.  A cell on a free list carries
FREE_LIST_POISON in its second word; the pop path clears it and every live
object overwrites it with its own (tag, flags) pair.  The lifecycle word
cannot serve: the carve path links fresh cells that already read FREE, and the
GC retires a granule to FREE before calling free() on it.  A cell that already reads
FREE is counted in `pcc_allocator_double_frees`
(PCC_GC_COUNTER_ALLOCATOR_DOUBLE_FREES, telemetry metric 118) and dropped, so
the heap stays self-consistent and the run reaches a diagnosable point.  Under
the ownership-audit mode (`PCC_GC_REFCOUNT_PROVENANCE_PROBE` 2 or 3) the first
one is reported on stderr, and mode 3 aborts so the crash report names the
caller.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from pcc.frontends.python.codegen import runtime_abi

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME = REPO_ROOT / "pcc" / "runtime"
ALLOCATOR = (RUNTIME / "py" / "freestanding_allocator.py").read_text(encoding="utf-8")
TELEMETRY = (RUNTIME / "py" / "py_gc_telemetry.py").read_text(encoding="utf-8")
HEADER = (RUNTIME / "include" / "py_runtime.h").read_text(encoding="utf-8")

DOUBLE_FREE_MESSAGE = "pcc runtime: object cell freed twice"
METRIC = 118


def _section(name):
    return ALLOCATOR.split('@c_abi_export("%s")' % name, 1)[1].split(
        "\n@c_abi_export", 1
    )[0]


def test_the_poison_is_written_on_push_and_cleared_on_every_pop() -> None:
    put = _section("pcc_allocator_put_small_object")
    assert "store_i64(ptr, 8, FREE_LIST_POISON)" in put
    take = _section("pcc_allocator_take_small_object")
    # Every size class must clear it, or the next free of that cell reads a
    # stale poison and reports a double free that did not happen.
    assert take.count("store_i64(head, 8, 0)") == take.count("return head")
    # And every clear must sit inside its own "the list was not empty" guard.
    # A clear one level out stores through NULL whenever that size class is
    # exhausted, which is a segfault in the first allocation of a fresh
    # process, not a diagnosable one later.
    lines = take.splitlines()
    for index, line in enumerate(lines):
        if "store_i64(head, 8, 0)" not in line:
            continue
        previous = lines[index - 1]
        assert previous.lstrip().startswith("global_store_ptr("), previous
        assert len(line) - len(line.lstrip()) == len(previous) - len(
            previous.lstrip()
        ), (previous, line)


def test_free_checks_the_poison_before_linking() -> None:
    free_body = _section("free")
    guard = "if load_i64(ptr, 8) == FREE_LIST_POISON:"
    assert guard in free_body
    assert free_body.index(guard) < free_body.index(
        "pcc_allocator_put_small_object(ptr, usable)"
    )


def test_the_double_free_is_counted_and_auditable() -> None:
    assert 'define_global_i64("pcc_allocator_double_frees", 0)' in ALLOCATOR
    assert 'define_global_i32("pcc_allocator_double_free_reported", 0)' in ALLOCATOR
    # The audit gate is the same knob as the refcount provenance probe.
    assert 'global_addr("pcc_gc_refcount_provenance_probe"), 0' in ALLOCATOR
    assert "if audit >= 2:" in ALLOCATOR
    assert "if audit == 3:" in ALLOCATOR
    assert "pcc_platform_abort()" in ALLOCATOR
    assert DOUBLE_FREE_MESSAGE in ALLOCATOR


def test_the_counter_is_readable_through_telemetry() -> None:
    assert f"PCC_GC_COUNTER_ALLOCATOR_DOUBLE_FREES = {METRIC}" in HEADER
    assert (
        f"if metric == {METRIC}:\n"
        '        return load_i64(global_addr("pcc_allocator_double_frees"), 0)'
        in TELEMETRY
    )


def test_the_new_globals_are_registered_for_the_freestanding_closure() -> None:
    assert "pcc_allocator_double_frees" in runtime_abi.FREESTANDING_GC_I64_GLOBALS
    assert (
        "pcc_allocator_double_free_reported"
        in runtime_abi.FREESTANDING_GC_I32_GLOBALS
    )


def test_a_bootstrap_stage_smoke_runs_the_ownership_audit() -> None:
    from scripts import bootstrap

    options = bootstrap.Options({})
    assert options.smoke_refcount_probe_mode == "2"
    assert options.smoke_refcount_audit == "1"
    # Both audit reports have to fail the stage, not only the refcount one.
    assert set(bootstrap._OWNERSHIP_FAULT_MESSAGES) == {
        "refcount operation on an unmanaged pointer",
        "object cell freed twice",
    }


def test_an_ordinary_program_reports_no_double_free(tmp_path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(
        textwrap.dedent(
            f"""
            from pcc.extern import extern, c_int64
            pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)


            class Node:
                def __init__(self, name: str) -> None:
                    self.name = name


            def churn(n: int) -> int:
                total = 0
                items = []
                for i in range(n):
                    items.append(Node("n" + str(i)))
                for node in items:
                    head, tail = node.name, node
                    total += len(head) + len(tail.name)
                return total


            def main() -> None:
                total = 0
                for _ in range(40):
                    total += churn(25)
                print(total)
                print(pcc_gc_telemetry({METRIC}))


            main()
            """
        ),
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    env = dict(os.environ)
    env["PCC_GC_BACKEND"] = "0"
    env["PCC_GC_REFCOUNT_PROVENANCE_PROBE"] = "2"
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-1] == "0", result.stdout
    assert DOUBLE_FREE_MESSAGE not in result.stderr


def _double_free_program(metric: int) -> str:
    """A program that really frees one object-family cell twice.

    Everything above this point asserts on the allocator's *source text*, so
    it would keep passing if the poison branch were dead at runtime.  This
    one exercises the branch: it allocates through the object lane, frees the
    cell, frees it again, and reads both the counter and the live-byte
    accounting across each step.
    """
    return textwrap.dedent(
        f"""
        from pcc.extern import extern, c_int64, c_rawptr, c_void

        # c_rawptr, not c_ptr: these are raw cell addresses, not PyObject*.
        alloc_object = extern("pcc_allocator_alloc_object", (c_int64,), c_rawptr)
        free_cell = extern("free", (c_rawptr,), c_void)
        live_usable = extern("pcc_allocator_live_usable_bytes", (), c_int64)
        live_requested = extern("pcc_allocator_live_requested_bytes", (), c_int64)
        telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)


        def main() -> None:
            cell = alloc_object(64)
            held_usable = live_usable()
            free_cell(cell)
            freed_usable = live_usable()
            freed_requested = live_requested()
            free_cell(cell)
            again_usable = live_usable()
            again_requested = live_requested()
            print(telemetry({metric}))
            print(again_usable - freed_usable)
            print(again_requested - freed_requested)
            print(held_usable - freed_usable)


        main()
        """
    )


def _compile_and_run(tmp_path, probe_mode: str, metric: int = METRIC):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "double_free.py"
    exe = tmp_path / "double_free.out"
    src.write_text(_double_free_program(metric), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    env = dict(os.environ)
    env["PCC_GC_BACKEND"] = "0"
    env["PCC_GC_REFCOUNT_PROVENANCE_PROBE"] = probe_mode
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, env=env
    )


def test_the_second_free_is_counted_and_leaves_accounting_unchanged(tmp_path) -> None:
    result = _compile_and_run(tmp_path, "1")
    assert result.returncode == 0, result.stderr
    counted, usable_delta, requested_delta, accounted = result.stdout.split()
    # The branch really ran.
    assert counted == "1", result.stdout
    # free() debits at entry, so the double free must re-credit: a second
    # debit for a cell that was never re-allocated underflows these counters
    # for the rest of the run and skews every heap-size-driven GC decision.
    assert usable_delta == "0", result.stdout
    assert requested_delta == "0", result.stdout
    # Guards the test itself: if the cell were never accounted, the deltas
    # above would be trivially zero.
    assert int(accounted) > 0, result.stdout


def test_the_audit_mode_reports_the_second_free_on_stderr(tmp_path) -> None:
    result = _compile_and_run(tmp_path, "2")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[0] == "1", result.stdout
    assert DOUBLE_FREE_MESSAGE in result.stderr, result.stderr


def _run_audit_block(returncode: int, stderr_text: str) -> int:
    """Drive the shipped reclassification rule (a Python function now)."""

    from scripts import bootstrap

    return bootstrap.audit_rerun_returncode(returncode, stderr_text, audit="1")


_AUDIT_STDERR = "pcc runtime: object cell freed twice (x)\n"


def test_a_passing_smoke_with_an_audit_hit_becomes_the_audit_code() -> None:
    assert _run_audit_block(0, _AUDIT_STDERR) == 97


def test_a_failing_smoke_keeps_its_own_exit_code() -> None:
    # A segfault or a link failure is a different class from an ownership
    # audit; stage gating and CI tell them apart by exit code.  Reclassifying
    # it as 97 hid the real failure.
    assert _run_audit_block(139, _AUDIT_STDERR) == 139
    assert _run_audit_block(1, _AUDIT_STDERR) == 1


def test_a_clean_smoke_is_untouched() -> None:
    assert _run_audit_block(0, "nothing interesting\n") == 0


def test_the_audit_rule_can_be_disabled() -> None:
    from scripts import bootstrap

    assert (
        bootstrap.audit_rerun_returncode(0, _AUDIT_STDERR, audit="0") == 0
    )


def test_the_double_free_report_length_matches_its_literal() -> None:
    """The stderr write passes a literal byte count; nothing else checks it.

    Editing one word of the message silently truncates the report or reads
    past it, and the message assertions elsewhere in this file only look for
    a substring.
    """
    import re

    call = re.search(
        r"pcc_platform_write\(\s*2,\s*cstr\(\s*(.*?)\s*\),\s*(\d+),",
        ALLOCATOR,
        re.S,
    )
    assert call, "double-free report call not found in the allocator"
    literal = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', call.group(1)))
    text = literal.encode("utf-8").decode("unicode_escape")
    assert len(text.encode("utf-8")) == int(call.group(2)), (
        text, call.group(2)
    )
