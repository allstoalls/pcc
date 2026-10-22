"""Type confusion in the no-libpython Python frontend codegen.

Source: *Low-Level Software Security for Compiler Developers* — the "JIT
compiler vulnerabilities" chapter: a miscompilation that confuses a value's
type breaks the memory safety of the language being implemented. A type
confusion between a floating-point payload and a pointer is the canonical
exploitation primitive (write a controlled ``double`` where the runtime
expects a heap pointer, or vice versa).

This pins a concrete instance: when one local variable is assigned the result
of true division ``/`` (a ``float``) on one path and an ``int`` on another, the
no-libpython lowering emits invalid LLVM IR that stores the raw ``double``
value into the object (``ptr``) slot of that local:

    store ptr %iobj2f.<n>, ptr %v.obj.addr.<n>
    error: '%iobj2f.<n>' defined with type 'double' but expected 'ptr'

The regression test asserts the safe outcome: the program compiles, runs, and
prints the boxed values correctly.
"""
from __future__ import annotations

# A local reused for a float (truediv) result and then an int result. The
# type inferencer must give the local a single boxed-object representation and
# box the float, not store the raw double into the pointer slot.
PROGRAM = r"""
def main() -> int:
    v = 7 / 2          # float -> 3.5
    print("FLOAT:" + str(v))
    v = 7 // 2         # int -> 3 (reuses the same local)
    print("INT:" + str(v))
    return 0


main()
"""


def test_local_reused_for_float_then_int_is_boxed_consistently(compile_and_run):
    r = compile_and_run(PROGRAM, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert "FLOAT:3.5" in lines
    assert "INT:3" in lines


# Adversarial generality: the type-confusion fix must be a real join over a
# reused local's assigned types (-> a consistent boxed-object slot), not a
# special case of the float-then-int order.
PROGRAM_REVERSE = r"""
def main() -> int:
    v = 7 // 2         # int -> 3
    print("INT:" + str(v))
    v = 7 / 2          # float -> 3.5 (reuses the same local, reverse order)
    print("FLOAT:" + str(v))
    return 0


main()
"""

PROGRAM_STR_INT = r"""
def main() -> int:
    v = "hi"           # str
    print("STR:" + v)
    v = 5              # int (reuses the same local -> must widen to object)
    print("INT:" + str(v))
    return 0


main()
"""

PROGRAM_BRANCH = r"""
def main() -> int:
    n = 3
    if n > 0:
        v = 7 / 2      # float on one branch
    else:
        v = 1          # int on the other
    print("V:" + str(v))
    return 0


main()
"""


def test_local_reused_int_then_float_is_boxed_consistently(compile_and_run):
    r = compile_and_run(PROGRAM_REVERSE, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert "INT:3" in lines
    assert "FLOAT:3.5" in lines


def test_local_reused_str_then_int_widens_to_object(compile_and_run):
    r = compile_and_run(PROGRAM_STR_INT, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert "STR:hi" in lines
    assert "INT:5" in lines


def test_local_typed_across_branches_is_boxed_consistently(compile_and_run):
    # SEC-P1-TYPECONF branch-join fix: a local assigned float on one branch and
    # int on another must be unified to a consistent boxed-object slot at the
    # control-flow join, so the merged read yields a real value rather than the
    # <null> a raw double aliased through a ptr slot produced before. The fix is
    # a forced-object widening layered on the existing shared-scope inference in
    # type_infer.py (no scope-propagation change — that variant broke the
    # pcc1->pcc2->pcc3 self-host).
    r = compile_and_run(PROGRAM_BRANCH, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "V:3.5" in r.stdout.splitlines()


# Adversarial generality for the branch-join fix: the widening must be a real
# storage-class join, symmetric in branch order, and must also cover the
# reverse (int on the taken branch, float on the fall-through) and the
# scalar-vs-object mix (int on one branch, str on the other).
PROGRAM_BRANCH_REVERSE = r"""
def main() -> int:
    n = 3
    if n > 0:
        v = 1          # int on one branch
    else:
        v = 7 / 2      # float on the other
    print("V:" + str(v))
    return 0


main()
"""

PROGRAM_BRANCH_STR_INT = r"""
def main() -> int:
    n = 3
    if n > 0:
        v = "hi"       # str (object) on one branch
    else:
        v = 5          # int on the other
    print("V:" + str(v))
    return 0


main()
"""


def test_branch_join_reverse_order_is_boxed_consistently(compile_and_run):
    r = compile_and_run(PROGRAM_BRANCH_REVERSE, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    # n = 3 > 0, so the int branch is taken; the widened boxed slot must read
    # back the int, not a type-confused float bit-pattern.
    assert "V:1" in r.stdout.splitlines()


def test_branch_join_str_vs_int_widens_to_object(compile_and_run):
    r = compile_and_run(PROGRAM_BRANCH_STR_INT, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "V:hi" in r.stdout.splitlines()
