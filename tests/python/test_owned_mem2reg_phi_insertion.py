"""Contract for ``pcc.native_ir.mem2reg``: the owned, llvmlite-free mem2reg.

pcc has three mem2reg implementations and only one of them can ship.
``pcc/ir_passes/mem2reg.py`` reads the function through ``llvmlite.binding``,
so a self-hosted ``pcc1`` can never run it.  The textual subset in
``pcc/py_frontend/compiled_default_passes.py`` needs no dominance information
and therefore cannot promote anything that requires a phi node.
``pcc/native_ir/mem2reg.py`` is the full algorithm -- dominance frontiers,
phi placement, dominator-tree renaming -- over pcc's own IR model.

These tests pin what the frontend actually gets.  Because the self backend is
the default, this is the pass every self compile and every runtime archive
member goes through, so a regression here silently un-optimizes the runtime
that the whole bootstrap executes.

llvmlite appears only as an oracle: it verifies our output and supplies the
reference counts.  The pass under test never imports it.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import llvmlite.binding as llvm
import pytest

from pcc.native_ir import mem2reg as owned
from pcc.native_ir.ir_mutator import MutableModule
from pcc.py_frontend.compiled_owned_passes import run_owned_passes
from pcc.py_frontend.pipeline import compile_python


def _counts(text: str) -> tuple[int, int, int]:
    return (
        text.count(" = alloca "),
        text.count(" = load "),
        text.count("  store "),
    )


def _verify(text: str) -> None:
    module = llvm.parse_assembly(text)
    module.verify()


def _run(source: str) -> tuple[str, bool]:
    text = textwrap.dedent(source).lstrip()
    out, changed = owned.mem2reg_text(text)
    if changed:
        _verify(out)
    return out, changed


_DIAMOND = """
    define i64 @pick(i1 %c) {
    entry:
      %slot = alloca i64, align 8
      br i1 %c, label %yes, label %no

    yes:
      store i64 11, ptr %slot, align 8
      br label %join

    no:
      store i64 22, ptr %slot, align 8
      br label %join

    join:
      %value = load i64, ptr %slot, align 8
      ret i64 %value
    }
    """


def test_a_branch_join_gets_one_phi_and_loses_the_slot() -> None:
    out, changed = _run(_DIAMOND)
    assert changed
    assert _counts(out) == (0, 0, 0), out
    assert " = phi i64 " in out
    assert "[ 11, %yes ]" in out and "[ 22, %no ]" in out
    # The phi must be the value returned, not a leftover name.
    returned = [line for line in out.splitlines() if line.strip().startswith("ret ")]
    assert len(returned) == 1
    phi_name = None
    for line in out.splitlines():
        if " = phi " in line:
            phi_name = line.strip().split(" = ")[0]
    assert phi_name is not None
    assert returned[0].strip() == "ret i64 " + phi_name


_LOOP = """
    define i64 @total(i64 %n) {
    entry:
      %sum = alloca i64, align 8
      %i = alloca i64, align 8
      store i64 0, ptr %sum, align 8
      store i64 0, ptr %i, align 8
      br label %head

    head:
      %cur = load i64, ptr %i, align 8
      %more = icmp slt i64 %cur, %n
      br i1 %more, label %body, label %done

    body:
      %acc = load i64, ptr %sum, align 8
      %next = add i64 %acc, %cur
      store i64 %next, ptr %sum, align 8
      %step = add i64 %cur, 1
      store i64 %step, ptr %i, align 8
      br label %head

    done:
      %out = load i64, ptr %sum, align 8
      ret i64 %out
    }
    """


def test_a_loop_carried_slot_gets_a_phi_in_the_header() -> None:
    """Two back-edge-carried variables need two phis in the loop header.

    This is the shape the textual subset can never reach: the store that
    supplies the next iteration is in a block the header does not dominate a
    single-value view of, so no store dominates every load.
    """
    out, changed = _run(_LOOP)
    assert changed
    assert _counts(out) == (0, 0, 0), out
    header = out.split("head:", 1)[1].split("body:", 1)[0]
    assert header.count(" = phi i64 ") == 2, header
    # Each phi carries the entry initializer and the body's update.
    assert "[ 0, %entry ]" in header
    assert "%entry ]" in header and "%body ]" in header


def test_an_unreachable_predecessor_does_not_force_a_phi() -> None:
    """A join whose only reachable predecessor is one block needs no phi.

    The unreachable arm still branches in, so a phi placed there would have to
    name it; not placing one at all is both correct and smaller.
    """
    out, changed = _run(
        """
        define i64 @orphan(i1 %c) {
        entry:
          %slot = alloca i64, align 8
          store i64 4, ptr %slot, align 8
          br label %join

        lost:
          br label %join

        join:
          %v = load i64, ptr %slot, align 8
          ret i64 %v
        }
        """
    )
    assert changed
    assert _counts(out) == (0, 0, 0), out
    assert " = phi " not in out
    assert "ret i64 4" in out


@pytest.mark.parametrize(
    "name,source",
    [
        (
            "address escapes into a call",
            """
            declare void @sink(ptr)

            define void @escapes() {
            entry:
              %slot = alloca i64, align 8
              store i64 1, ptr %slot, align 8
              call void @sink(ptr %slot)
              ret void
            }
            """,
        ),
        (
            "volatile access",
            """
            define i64 @vol() {
            entry:
              %slot = alloca i64, align 8
              store volatile i64 5, ptr %slot, align 8
              %v = load volatile i64, ptr %slot, align 8
              ret i64 %v
            }
            """,
        ),
        (
            "atomic access",
            """
            define i64 @atom() {
            entry:
              %slot = alloca i64, align 8
              store atomic i64 5, ptr %slot seq_cst, align 8
              %v = load atomic i64, ptr %slot seq_cst, align 8
              ret i64 %v
            }
            """,
        ),
        (
            "aggregate slot reached through a gep",
            """
            define i64 @agg() {
            entry:
              %slot = alloca { i64, i64 }, align 8
              %f = getelementptr inbounds { i64, i64 }, ptr %slot, i32 0, i32 0
              store i64 3, ptr %f, align 8
              %v = load i64, ptr %f, align 8
              ret i64 %v
            }
            """,
        ),
        (
            "slot allocated outside the entry block",
            """
            define i64 @late() {
            entry:
              br label %tail

            tail:
              %slot = alloca i64, align 8
              store i64 9, ptr %slot, align 8
              %v = load i64, ptr %slot, align 8
              ret i64 %v
            }
            """,
        ),
        (
            "the slot address is itself stored",
            """
            define ptr @leak() {
            entry:
              %slot = alloca ptr, align 8
              store ptr %slot, ptr %slot, align 8
              %v = load ptr, ptr %slot, align 8
              ret ptr %v
            }
            """,
        ),
    ],
)
def test_unprovable_shapes_are_left_exactly_as_they_were(name: str, source: str) -> None:
    text = textwrap.dedent(source).lstrip()
    out, changed = owned.mem2reg_text(text)
    assert changed is False, name
    assert out == text, name


def test_an_unreadable_terminator_leaves_the_function_alone() -> None:
    """The CFG builder covers br/switch/indirectbr/invoke/callbr and the
    path-ending terminators.  An exception-handling terminator would give an
    incomplete CFG, so the function is skipped rather than analysed."""
    text = textwrap.dedent(
        """
        define i64 @ehpad(i1 %c) personality ptr null {
        entry:
          %slot = alloca i64, align 8
          store i64 1, ptr %slot, align 8
          br label %pad

        pad:
          %cp = cleanuppad within none []
          cleanupret from %cp unwind to caller
        }
        """
    ).lstrip()
    out, changed = owned.mem2reg_text(text)
    assert changed is False
    assert out == text


def test_a_switch_join_is_promoted() -> None:
    out, changed = _run(
        """
        define i64 @by_case(i64 %k) {
        entry:
          %slot = alloca i64, align 8
          switch i64 %k, label %other [
            i64 1, label %one
            i64 2, label %two
          ]

        one:
          store i64 10, ptr %slot, align 8
          br label %join

        two:
          store i64 20, ptr %slot, align 8
          br label %join

        other:
          store i64 30, ptr %slot, align 8
          br label %join

        join:
          %v = load i64, ptr %slot, align 8
          ret i64 %v
        }
        """
    )
    assert changed
    assert _counts(out) == (0, 0, 0), out
    phis = [line for line in out.splitlines() if " = phi " in line]
    assert len(phis) == 1
    assert phis[0].count("[ ") == 3, phis[0]


def test_a_repeated_branch_edge_gets_one_phi_entry_per_edge() -> None:
    """``br i1 %c, label %join, label %join`` is two edges into ``%join``.

    LLVM requires a phi to name every incoming edge, so a block reached twice
    from the same predecessor must appear twice in the phi.  Deduplicating the
    predecessor list produces IR that fails verification.
    """
    out, changed = _run(
        """
        define i64 @twice(i1 %c) {
        entry:
          %slot = alloca i64, align 8
          store i64 1, ptr %slot, align 8
          br i1 %c, label %a, label %b

        a:
          store i64 2, ptr %slot, align 8
          br i1 %c, label %join, label %join

        b:
          store i64 3, ptr %slot, align 8
          br label %join

        join:
          %v = load i64, ptr %slot, align 8
          ret i64 %v
        }
        """
    )
    assert changed
    assert _counts(out) == (0, 0, 0), out
    phis = [line for line in out.splitlines() if " = phi " in line]
    assert len(phis) == 1, out
    assert phis[0].count("[ ") == 3, phis[0]
    assert phis[0].count("%a ]") == 2, phis[0]
    assert phis[0].count("%b ]") == 1, phis[0]


def test_a_load_with_no_reaching_store_becomes_undef() -> None:
    out, changed = _run(
        """
        define i64 @never_written() {
        entry:
          %slot = alloca i64, align 8
          %v = load i64, ptr %slot, align 8
          ret i64 %v
        }
        """
    )
    assert changed
    assert _counts(out) == (0, 0, 0), out
    assert "ret i64 undef" in out


_VARIADIC = """
    declare i64 @printf(ptr, ...)

    define i64 @shout(ptr %fmt, ...) {
    entry:
      %slot = alloca i64, align 8
      store i64 7, ptr %slot, align 8
      %v = load i64, ptr %slot, align 8
      ret i64 %v
    }
    """


def test_a_variadic_signature_survives_the_owned_round_trip() -> None:
    """Regression: the owned IR model used to invent a name for ``...``.

    ``Argument`` gave every unnamed parameter a synthesized name, so
    serializing rewrote ``@shout(ptr %fmt, ...)`` as ``(ptr %fmt, ... %anon1)``
    and LLVM refused to parse the module.  A bare parse/serialize round trip
    was enough to corrupt it, which put every owned pass that serializes at
    risk on the six runtime modules that declare variadic C entry points.
    """
    text = textwrap.dedent(_VARIADIC).lstrip()
    _verify(MutableModule.parse(text).serialize())
    out, changed = _run(text)
    assert changed
    assert "define i64 @shout(ptr %fmt, ...)" in out
    assert "declare i64 @printf(ptr, ...)" in out
    assert "%anon" not in out


@pytest.mark.parametrize("source", [_DIAMOND, _LOOP, _VARIADIC])
def test_the_owned_pass_matches_llvms_own_mem2reg(source: str) -> None:
    """LLVM is the oracle, not the owner: same slots promoted, same counts."""
    text = textwrap.dedent(source).lstrip()
    ours, _changed = owned.mem2reg_text(text)
    from pcc.llvm_capi import binding as capi

    theirs = capi.run_passes_on_ir(text, "function(mem2reg)")
    assert _counts(ours) == _counts(theirs), (ours, theirs)


def test_the_owned_dispatcher_uses_the_owned_kernel() -> None:
    """The versioned ``mem2reg,sroa`` manifest must reach the real algorithm.

    The dispatcher used to send that exact tuple to the textual tier, which is
    why the self backend becoming the default silently un-optimized every
    compile.  A phi-requiring shape is the discriminator: the textual tier
    cannot promote it, the owned kernel must.
    """
    text = textwrap.dedent(_DIAMOND).lstrip()
    out = run_owned_passes(text, ["mem2reg", "sroa"], True)
    assert _counts(out) == (0, 0, 0), out
    assert " = phi i64 " in out


def test_branch_and_loop_shapes_still_execute_after_promotion(tmp_path: Path) -> None:
    """End-to-end: the pass runs inside the default self-backend compile.

    A structural assertion cannot catch a phi wired to the wrong predecessor.
    This program's answers depend on exactly that wiring.
    """
    src = tmp_path / "phi_shapes.py"
    src.write_text(
        textwrap.dedent(
            """
            def pick(flag: int) -> int:
                if flag != 0:
                    value = 11
                else:
                    value = 22
                return value


            def total(n: int) -> int:
                sum_so_far = 0
                i = 0
                while i < n:
                    sum_so_far += i
                    i += 1
                return sum_so_far


            def nested(n: int) -> int:
                out = 0
                i = 0
                while i < n:
                    if i % 2 == 0:
                        step = i
                    else:
                        step = -i
                    out += step
                    i += 1
                return out


            print(pick(1), pick(0))
            print(total(0), total(1), total(10))
            print(nested(0), nested(5), nested(10))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "phi_shapes"
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "11 22\n0 0 45\n0 2 -5\n"
