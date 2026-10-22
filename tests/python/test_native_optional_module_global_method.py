"""A guarded optional module global dispatches to its native class method."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python_multi


def _source_modules(tmp_path):
    entry = tmp_path / "optional_entry.py"
    arena = tmp_path / "optional_arena.py"
    state = tmp_path / "optional_state.py"
    entry.write_text(
        "from pcc.backend.optional_state import begin, end\n"
        "def main() -> None:\n"
        "    begin()\n"
        "    end()\n"
        "    print('capture-ok')\n"
        "main()\n",
        encoding="utf-8",
    )
    arena.write_text(
        "class Arena:\n"
        "    def close(self) -> None:\n"
        "        print('closed')\n",
        encoding="utf-8",
    )
    state.write_text(
        "from pcc.backend.optional_arena import Arena\n"
        "_RECORDS: Arena | None = None\n"
        "def begin() -> None:\n"
        "    global _RECORDS\n"
        "    _RECORDS = Arena()\n"
        "def end() -> None:\n"
        "    global _RECORDS\n"
        "    if _RECORDS is not None:\n"
        "        _RECORDS.close()\n"
        "    _RECORDS = None\n",
        encoding="utf-8",
    )
    return entry, arena, state


# native execution requires an explicit immutable runtime archive
@pytest.mark.pcc_gate(env="PCC_RUNTIME_ARCHIVE")
def test_guarded_optional_imported_class_global_method_runs_natively(tmp_path):
    archive_name = os.environ.get("PCC_RUNTIME_ARCHIVE")
    archive = Path(archive_name).resolve()
    assert archive.is_file()
    entry, arena, state = _source_modules(tmp_path)
    executable = tmp_path / "optional_global"
    compile_python_multi(
        [str(entry), str(arena), str(state)],
        str(executable),
        entry_module="pcc.backend.optional_entry",
        module_names=[
            "pcc.backend.optional_entry",
            "pcc.backend.optional_arena",
            "pcc.backend.optional_state",
        ],
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        recursive_stdlib=False,
        target_triple="arm64-apple-darwin23.6.0",
        runtime_archive=str(archive),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "closed\ncapture-ok\n"


def test_none_typed_module_global_method_uses_native_dispatch(tmp_path):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.py_ast import Attr, Call, ExprStmt, FuncDef, If, Name, NoneType

    _entry, arena, state = _source_modules(tmp_path)
    modules, exports, _classes = build_closed_world_context(
        [str(arena), str(state)],
        ["pcc.backend.optional_arena", "pcc.backend.optional_state"],
    )
    typed = modules[1]
    end = next(
        stmt for stmt in typed.body
        if isinstance(stmt, FuncDef) and stmt.name == "end"
    )
    guarded = next(stmt for stmt in end.body if isinstance(stmt, If))
    call_stmt = next(stmt for stmt in guarded.body if isinstance(stmt, ExprStmt))
    assert isinstance(call_stmt.expr, Call)
    assert isinstance(call_stmt.expr.func, Attr)
    receiver = call_stmt.expr.func.obj
    assert isinstance(receiver, Name) and receiver.ident == "_RECORDS"
    # The recorded Stage2 worker has exactly this flow-insensitive type at
    # method lowering, although its serialized receiver began as DynType.
    object.__setattr__(receiver, "ty", NoneType("None"))

    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    ir_text = str(codegen.generate(typed))
    match = re.search(
        r"(?m)^define[^\n]*@user_pcc_backend_optional_state_end\([^\n]*\{\n([\s\S]*?)^\}",
        ir_text,
    )
    assert match is not None
    body = match.group(1)
    assert "strict.nolib.stub" not in body
    assert "@py_cpy_" not in body
    assert "@py_obj_load_method" in body
    assert "@py_obj_call_method" in body
