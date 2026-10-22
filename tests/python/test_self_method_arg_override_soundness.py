"""Unannotated method parameters are typed from ``self.method(...)`` calls
only when every call agrees.

Class-body inference joins the argument types of the ``self.method(...)``
calls in a class and types an unannotated parameter with the common type.
Arguments of unknown (dyn) type used to be skipped, so a single typed caller
decided the parameter: pcc's own comprehension lowering has
``_comp_expr_references_names(self, node, names)``, whose only typed caller
passes an ``Expr`` while its recursive calls pass field values and tuple
elements.  ``node`` became ``Expr``, ``isinstance(node, tuple)`` folded to
False, and pcc1 compiled ``[values for i, values in enumerate(values)]``
into a NameError.  A dyn argument now widens the parameter, a starred call
or unknown keyword poisons the method, and keyword arguments join by name.
"""

import os
import subprocess
import sys


PROGRAM = '''from dataclasses import dataclass


@dataclass(frozen=True)
class Expr:
    span: int


@dataclass(frozen=True)
class Name(Expr):
    ident: str


@dataclass(frozen=True)
class Call(Expr):
    func: Expr
    args: tuple
    kwargs: tuple


def field_names(obj):
    if isinstance(obj, Name):
        return ("span", "ident")
    if isinstance(obj, Call):
        return ("span", "func", "args", "kwargs")
    return ()


class Walker:
    def refs(self, node, names: set) -> bool:
        if isinstance(node, Name):
            return node.ident in names
        if isinstance(node, Expr):
            for field in field_names(node):
                if self.refs(getattr(node, field, None), names):
                    return True
            return False
        if isinstance(node, tuple):
            return any(self.refs(e, names) for e in node)
        return False

    def prehoist(self, iter_e: Expr, bound: set) -> bool:
        return self.refs(iter_e, bound)

    def kind(self, value) -> str:
        if isinstance(value, tuple):
            return "tuple"
        if isinstance(value, str):
            return "str"
        return "other"

    def typed_then_starred(self, head: Expr, rest: tuple) -> tuple:
        return (self.kind(head), self.kind(*rest))

    def typed_then_keyword(self, head: Expr) -> tuple:
        return (self.kind(head), self.kind(value=("a", "b")))


def main():
    w = Walker()
    call = Call(0, Name(0, "enumerate"), (Name(0, "values"),), ())
    print(w.prehoist(call, {"values", "i"}), w.prehoist(call, {"zzz"}))
    print(w.typed_then_starred(Name(0, "x"), (("t",),)))
    print(w.typed_then_keyword(Name(0, "x")))


main()
'''


def test_dyn_starred_and_keyword_calls_keep_parameters_dynamic(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "self_method_overrides.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert expected.returncode == 0, expected.stderr
    assert expected.stdout == (
        "True False\n('other', 'tuple')\n('other', 'tuple')\n"
    )
    output = tmp_path / "self_method_overrides"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"
