"""Generator frames contain function locals, never explicit module globals."""

import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.py_ast import FuncDef
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


PROGRAM = '''import pcc.virtual_thread as vt
counter = 10
outside = 0
def values(seed):
    global counter
    local = seed
    counter = counter + 1
    yield local
    counter += 2
    yield counter + local
def outer(seed):
    outside = seed
    def nested():
        global outside
        outside += 1
    yield outside
def parked(seed):
    global counter
    local = seed
    counter += 1
    vt.yield_now()
    counter = counter + local
    return counter
def main():
    iterator = values(7)
    assert next(iterator) == 7
    assert counter == 11
    counter_before_resume = counter
    assert next(iterator) == counter_before_resume + 2 + 7
    assert counter == 13
    assert next(outer(9)) == 9
    assert outside == 0
    thread = vt.spawn(parked, 5)
    vt.run(1, 32)
    assert vt.result(thread) == 19
    assert counter == 19
    print("GENERATOR_GLOBALS_OK")
main()
'''


def _typed(source=PROGRAM):
    return infer_module(parse_and_lift(source, "generator_globals.py", "generator_globals"))


def _body(text, name):
    match = re.search(r"^define[^\n]*@user_generator_globals_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, name
    return match[1]


def test_frame_inventory_filters_current_globals_and_retains_nested_scope_locals():
    typed = _typed()
    host = L1CodeGen(typed, ir_scaffold_mode="on")
    functions = {stmt.name: stmt for stmt in typed.body if isinstance(stmt, FuncDef)}
    names = host._collect_generator_frame_names(functions["values"])
    assert "counter" not in names
    assert "seed" in names and "local" in names
    exact_names = []
    host._collect_generator_exact_int_frame_names(exact_names, functions["values"].body)
    assert exact_names and set(exact_names).issubset(names)
    assert "counter" not in exact_names
    outer_names = host._collect_generator_frame_names(functions["outer"])
    assert "outside" in outer_names and "seed" in outer_names
    nested = next(stmt for stmt in functions["outer"].body if isinstance(stmt, FuncDef))
    assert "outside" not in host._collect_generator_frame_names(nested)


@pytest.mark.parametrize("first_entry", ["0", "1"])
def test_wrapper_resume_global_reads_and_writes_reach_owned_emitter(tmp_path, monkeypatch, first_entry):
    monkeypatch.setenv("PCC_GENERATOR_FIRST_ENTRY_INIT", first_entry)
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "generator_globals.py"
    output = tmp_path / "generator_globals.ll"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    for name in ("values__gen_resume", "parked__gen_resume"):
        body = _body(text, name)
        assert not re.search(r"%counter\.addr[^\n]* = alloca", body)
        assert "gen.frame.counter" not in body
        assert re.search(r"(?:load|pcc_gc_load_ptr|pcc_gc_load_borrowed_ptr)[^\n]*counter", body)
        assert re.search(r"(?:store|pcc_gc_store_root)[^\n]*counter", body)
        assert "gen.frame.seed" in body
        assert "local.addr" in body
    assert emit_owned_object(text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


def test_parameter_global_conflict_is_rejected():
    typed = _typed('''def invalid(counter):
    global counter
    yield counter
''')
    host = L1CodeGen(typed, ir_scaffold_mode="on")
    function = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    with pytest.raises(SyntaxError, match="parameter and global"):
        host._collect_generator_frame_names(function)


def test_original_condition_barrier_globals_and_caller_reach_owned_emitter(tmp_path, monkeypatch):
    from tests.python.test_concurrent_list_append import concurrent_append_source

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "append_gate.py"
    output = tmp_path / "append_gate.ll"
    source.write_text(concurrent_append_source())
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    match = re.search(r"^define[^\n]*append_barrier_wait__gen_resume[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None
    body = match[1]
    for name in ("barrier_arrived", "barrier_generation"):
        assert not re.search(r"%" + name + r"\.addr[^\n]* = alloca", body)
        assert "gen.frame." + name not in body
        assert re.search(r"load ptr, ptr @\.modvar\.append_gate\." + name, body)
        # Besides the load, the global slot itself is addressed (directly or
        # through an identity alias) rather than a generator-frame copy.
        assert [line for line in body.splitlines()
                if "ptr @.modvar.append_gate." + name in line and " = load " not in line]
    assert "append_worker__gen_resume" in text
    assert emit_owned_object(text, "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.integration
def test_native_global_bindings_survive_yield_and_park_resume(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "generator_globals.py"
    output = tmp_path / "generator_globals"
    source.write_text(PROGRAM)
    python_program_compiler(str(source), str(output), backend="self", libpython_mode="off",
                            ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "GENERATOR_GLOBALS_OK\n", (backend, result.stdout)
