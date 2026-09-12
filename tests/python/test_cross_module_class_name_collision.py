"""A class name imported from one module never resolves to another's.

Cross-module class lookup in ``_resolve_method_mro`` is keyed by bare class
name and scanned the whole export table, so the first module that happened to
define the name won.  ``pcc/ast/c_ast.py`` defines ``Enum``, ``If``, ``For``,
``Union``, ``Struct``, ``Return``, ``While``, ``ID``, ``Constant``, ``Cast``,
``Decl``, ``Label``, ``Case``, ``Default``, ``Break``, ``Continue``,
``Switch``, ``Typename`` and ``Assignment``.  The moment the C frontend joined
the self-host closure, stage1 failed with

    codegen[pcc.diagnostics]: L1CodegenError: missing required argument
    'values' (positional=1, raw_positional=1, raw_first=Attr, kwargs=0,
    formals=3; signature=name<none>,values<none>,coord=<NoneLit>; supplied=)

``pcc/diagnostics.py`` does ``from enum import Enum`` and
``class DiagnosticSeverity(str, Enum)``; the base resolved to the C AST's
``Enum``, whose ``__init__(self, name, values, coord=None)`` then rejected
``DiagnosticSeverity(self.severity)``.

A name bound by ``from X import Y`` can only be ``X``'s, so the importing
module's own import statements decide -- and when that module has no such
export the answer is "not found", not "try everyone else".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_COLLIDING_PROVIDER = '''
class Enum:
    """Same leaf name as ``enum.Enum``, unrelated shape."""

    def __init__(self, name, values, coord=None):
        self.name = name
        self.values = values
        self.coord = coord

    def describe(self) -> str:
        return "cnode:" + str(self.name)


def make(name: str) -> Enum:
    return Enum(name, (), None)
'''

_CONSUMER = '''
from enum import Enum

from colliding_provider import make


class Level(Enum):
    LOW = 1
    HIGH = 2


def main() -> None:
    print(Level.LOW.name)
    print(Level(2).name)
    print(Level.LOW == Level.HIGH)
    print(make("decl").describe())


main()
'''


# Same module without ``Level(2)``: by-value enum construction is a separate
# gap (see ``test_string_valued_enum_members_expose_value``), so the runtime
# check reads members by attribute instead.
_RUNTIME_CONSUMER = '''
from enum import Enum

from colliding_provider import make


class Level(Enum):
    LOW = 1
    HIGH = 2


def main() -> None:
    print(Level.LOW.name)
    print(Level.HIGH.name)
    print(Level.LOW == Level.HIGH)
    print(make("decl").describe())


main()
'''


def test_calling_a_class_whose_base_name_collides_still_compiles(tmp_path):
    """The compile-time shape of the stage1 failure.

    ``Level(2)`` resolves the constructor through ``Level``'s declared base.
    With the base taken from whichever module defined ``Enum`` first, that was
    the C AST node's ``__init__(self, name, values, coord=None)`` and the call
    was rejected outright.
    """
    from pcc.py_frontend.pipeline import compile_python_multi

    provider = tmp_path / "colliding_provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text(_COLLIDING_PROVIDER, encoding="utf-8")
    consumer.write_text(_CONSUMER, encoding="utf-8")
    compile_python_multi(
        [str(consumer), str(provider)],
        str(tmp_path / "consumer.ll"),
        entry_module="consumer",
        module_names=["consumer", "colliding_provider"],
        libpython_mode="off",
        emit_llvm_only=True,
        ir_scaffold_mode="on",
    )


def test_both_same_named_classes_keep_their_own_behaviour(tmp_path):
    """The provider's ``Enum`` and ``enum.Enum`` coexist at runtime."""
    from pcc.py_frontend.pipeline import compile_python_multi

    provider = tmp_path / "colliding_provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text(_COLLIDING_PROVIDER, encoding="utf-8")
    consumer.write_text(_RUNTIME_CONSUMER, encoding="utf-8")
    exe = tmp_path / "consumer.out"
    compile_python_multi(
        [str(consumer), str(provider)],
        str(exe),
        entry_module="consumer",
        module_names=["consumer", "colliding_provider"],
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(consumer)],
        text=True,
        capture_output=True,
        timeout=180,
        check=True,
        cwd=str(tmp_path),
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == ["LOW", "HIGH", "False", "cnode:decl"]


def test_c_ast_leaf_names_that_collide_are_still_present():
    """The collision is a property of the tree, not of one release.

    If these names ever stop colliding the test above loses its subject, so
    say so here rather than let it quietly become a tautology.
    """
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "pcc" / "ast" / "c_ast.py").read_text(encoding="utf-8")
    for name in ("Enum", "If", "For", "Union", "Return", "While", "Constant"):
        assert "\nclass " + name + "(" in source, name


_STRING_ENUM = '''
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    ERROR = "error"


def main() -> None:
    print(Severity("error").value)
    print(Severity.ERROR.value)
    print(Severity.INFO.name)


main()
'''


@pytest.mark.xfail(
    strict=True,
    reason=(
        "pcc models enum members as integers only: "
        "class_gen._enum_member_value accepts BoolLit, IntLit and auto(), so a "
        "string-valued member is not registered in ClassInfo.enum_members at "
        "all and the class becomes an ordinary class with string class "
        "attributes.  Both spellings then fail with \"'Severity' object has no "
        "attribute 'value'\".  pcc/diagnostics.py needs this -- it declares "
        "class DiagnosticSeverity(str, Enum) and reads self.severity.value in "
        "to_json and format_compact -- so it is a runtime gap in a closure "
        "member, independent of the name collision above."
    ),
)
def test_string_valued_enum_members_expose_value(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "string_enum.py"
    exe = tmp_path / "string_enum.out"
    src.write_text(_STRING_ENUM, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == ["error", "error", "INFO"]
