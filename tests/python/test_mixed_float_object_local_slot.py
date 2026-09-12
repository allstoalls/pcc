"""A local written as both a float and an object gets the object slot.

The slot took the shape of the name's first binding and every later store was
*coerced* into it.  For a name bound to a ``float`` on one branch and to a
class instance on another, that emitted ``py_float_to_f64`` on the instance --
a silently wrong double -- and a later attribute read passed that double to
``py_instance_get_field``.  Nothing rejected it until the self backend's IR
verifier saw the operand type:

    self IR verifier [operand-type] in
    'user_pcc_codegen_c_codegen_LLVMCodeGenerator__eval_const_expr':
    'call.cont.145681'/call expects void* for 'lhs.145688.3290', got double

``pcc/codegen/c_codegen.py::_eval_const_expr`` is that function: it writes
``lhs = float(...)`` on the floating branch and ``lhs = integer_promotion(...)``
(a ``ConstIntValue``) on the integer one, then reads ``lhs.width``.

``int`` already had this planner (a raw i64 and a PyInt cannot share a slot);
``float``/``bool`` did not.  The plan is function-level because the slot is
allocated in the entry block, before either branch is seen.
"""

from __future__ import annotations

import re
import subprocess

import pytest


_SOURCE = '''
class Boxed:
    def __init__(self, value: int) -> None:
        self.value = value


def evaluate(node_op: str, left, right):
    def numeric(value):
        if isinstance(value, Boxed):
            return value.value
        return value

    def divide(lhs, rhs):
        if rhs == 0.0:
            return 0.0
        return lhs / rhs

    use_float = isinstance(left, float) or isinstance(right, float)
    if use_float:
        lhs = float(numeric(left))
        rhs = float(numeric(right))
        if node_op == "/":
            return divide(lhs, rhs)
        return lhs + rhs
    lhs = Boxed(numeric(left))
    rhs = Boxed(numeric(right))
    return lhs.value + rhs.value


def main() -> None:
    print(evaluate("/", 3.0, 2.0))
    print(evaluate("+", 3.0, 2.0))
    print(evaluate("+", 3, 4))
    print(evaluate("/", 1.0, 0.0))


main()
'''


def test_mixed_float_object_local_runs_and_matches_cpython(tmp_path):
    """backend="self" so the self IR verifier runs over the emitted function."""
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "mixed_local.py"
    exe = tmp_path / "mixed_local.out"
    src.write_text(_SOURCE, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == ["1.5", "5.0", "7", "0.0"]


def test_the_mixed_local_never_unboxes_an_instance_as_a_float(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "mixed_local.py"
    out = tmp_path / "mixed_local.ll"
    src.write_text(_SOURCE, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    body = re.search(
        r"(?ms)^define [^\n]*@user_mixed_local_evaluate\(.*?\n\}",
        out.read_text(encoding="utf-8"),
    )
    assert body is not None
    text = body.group(0)
    # The instance must reach the slot as a pointer, never through a float
    # unbox, and the slot itself is the object one.
    assert re.search(r"%lhs\.addr[^ ]* = alloca ptr", text), text[:1500]
    assert not re.search(
        r"@py_float_to_f64\(ptr %inst\.Boxed", text
    ), text[:2500]


_BOOL_MIXED = '''
class Holder:
    def __init__(self, flag: int) -> None:
        self.flag = flag


def probe(as_object: int) -> str:
    value = 1 > 0
    if as_object:
        value = Holder(1)
        return "holder:" + str(value.flag)
    return "bool:" + str(value)


def main() -> None:
    print(probe(1))
    print(probe(0))


main()
'''


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bool is not in mixed_scalar_object_local_names.  Including it made "
        "two owned-local store paths reachable that still write a raw i1 into "
        "the object slot -- stage1 failed with \"'is_cpy.owned.cont'/store "
        "expects void* for 'm.bool_unbox', got i1\" -- so the planner covers "
        "float only until those stores box.  Today the class instance is "
        "coerced into the bool slot through py_obj_truthy and the attribute "
        "read then fails."
    ),
)
def test_mixed_bool_object_local_matches_cpython(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "bool_mixed.py"
    exe = tmp_path / "bool_mixed.out"
    src.write_text(_BOOL_MIXED, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout.splitlines() == ["holder:1", "bool:True"]


@pytest.mark.parametrize(
    "source,expected",
    [
        # float and object, reading the object's attribute after the join
        (
            '''
class Cell:
    def __init__(self, width: int) -> None:
        self.width = width


def probe(as_cell: int) -> str:
    item = 2.5
    if as_cell:
        item = Cell(64)
        return "cell:" + str(item.width)
    return "float:" + str(item + 0.5)


def main() -> None:
    print(probe(1))
    print(probe(0))


main()
''',
            ["cell:64", "float:3.0"],
        ),
    ],
)
def test_other_mixed_scalar_object_locals_match_cpython(tmp_path, source, expected):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "shape.py"
    exe = tmp_path / "shape.out"
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == expected
