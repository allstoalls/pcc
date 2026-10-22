"""`from x import NoneType` must make `isinstance(v, NoneType)` mean that class.

The builtin tag tables map the bare name `NoneType` to `PY_TYPE_NONE`, so
`isinstance(v, NoneType)` compiled to "is v None?".  A guard already existed to
let a user class of the same name win, but it recognised the name only through
`env` or `_module_globals` -- and `from py_ast import NoneType` binds it
through neither.  The guard therefore missed precisely the case its own
comment described.

pcc's `type_infer` is that case.  `_class_fields_from_def` decides whether a
field first seen as `self.x = None` may be widened by a later `self.x = <str>`
with `isinstance(known_field_ty, NoneType)`, where `NoneType` is the py_ast
descriptor class.  Compiled, that test answered False for a `NoneType()`
descriptor, so the widening never happened and the field stayed `NoneType`.
Run under CPython the same line is real Python and answers True.  Host pcc
widened; pcc1 did not.  Stage2 died with `Layer 1 slice on type NoneType not
supported` compiling `pcc/frontends/c/ply/lex.py`, whose `self.lexdata = None` in
`__init__` becomes a string in `input()` and is sliced in `token()` -- a
bootstrap divergence produced entirely by this one lowering.

The builtin path has to survive too: the same guard once fired too eagerly and
made `isinstance("x", str)`, `isinstance(7, int)` and `isinstance(b"", bytes)`
all answer False.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

TYPES_MODULE = textwrap.dedent(
    '''
    from dataclasses import dataclass


    @dataclass(frozen=True)
    class Type:
        name: str


    @dataclass(frozen=True)
    class IntType(Type):
        width: int = 64


    @dataclass(frozen=True)
    class NoneType(Type):
        pass


    @dataclass(frozen=True)
    class StrType(Type):
        pass
    '''
)

MAIN = textwrap.dedent(
    '''
    from shadow_types import IntType, NoneType, StrType, Type

    TYPE_NONE: NoneType = NoneType(name="None")


    def describe(ty) -> str:
        return (
            type(ty).__name__
            + " " + str(isinstance(ty, NoneType))
            + " " + str(isinstance(ty, Type))
            + " " + str(isinstance(ty, StrType))
        )


    def widens(known) -> int:
        # The type_infer shape: a field first seen as None may be widened.
        return 0 if not isinstance(known, NoneType) else 1


    def main() -> None:
        print(describe(TYPE_NONE))
        print(describe(StrType(name="str")))
        print(describe(IntType(name="int")))
        print(widens(TYPE_NONE), widens(StrType(name="str")))
        # The builtin meanings must be untouched.
        print(isinstance("x", str), isinstance(7, int), isinstance(b"", bytes))
        print(isinstance(None, type(None)))


    main()
    '''
)


def _compile_and_run(tmp_path: Path) -> str:
    from pcc.frontends.python.pipeline import compile_python

    (tmp_path / "shadow_types.py").write_text(TYPES_MODULE, encoding="utf-8")
    src = tmp_path / "shadow_main.py"
    src.write_text(MAIN, encoding="utf-8")
    exe = tmp_path / "shadow_main.out"
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        compile_python(
            str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off"
        )
    finally:
        os.chdir(cwd)
    env = dict(os.environ)
    env["PCC_GC_BACKEND"] = "0"
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, env=env
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_an_imported_class_named_NoneType_wins_over_the_builtin(tmp_path) -> None:
    lines = _compile_and_run(tmp_path).splitlines()
    # A NoneType() descriptor is an instance of NoneType and of Type only.
    assert lines[0] == "NoneType True True False"
    assert lines[1] == "StrType False True True"
    assert lines[2] == "IntType False True False"
    # The widening test type_infer relies on.
    assert lines[3] == "1 0"
    # And the builtin meanings still hold.
    assert lines[4] == "True True True"
    assert lines[5] == "True"


def test_the_guard_reads_import_bindings(tmp_path) -> None:
    source = (
        Path(__file__).absolute().parents[2]
        / "pcc" / "frontends" / "python"
        / "codegen"
        / "isinstance_lowering.py"
    ).read_text(encoding="utf-8")
    assert "_name_is_imported_into_module(host, class_arg.ident)" in source
    helper = source.split("def _name_is_imported_into_module", 1)[1]
    # Both `from x import N` and `import x as N` bind the name.
    assert '("Import", "ImportFrom")' in helper
    assert "entry[1]" in helper
