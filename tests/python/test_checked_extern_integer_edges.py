"""Checked ordinary Python integers at explicit C and address boundaries."""

from pathlib import Path
import os
import re
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python


ROOT = Path(__file__).resolve().parents[2]
_CONTEXT = (ROOT / "tests/fixtures/contextual_class_method_extern_args.py").read_text().rsplit(
    "ContextualFillApp().exercise()", 1
)[0]
EDGE_SOURCE = _CONTEXT + '''
def edge_main():
    app = ContextualFillApp()
    buf = stack_alloc(24)
    store_i64(buf, 0, 0)
    store_i64(buf, 8, 0)
    store_i64(buf, 16, 0)
    _setg(0, ptr_to_int(buf))
    assert app.fill(255, 8, 8) == 8
    assert load_i64(buf, 8) == -1
    caught = 0
    try:
        app.fill(7, 1 << 80, 0)
    except OverflowError:
        caught += 1
    try:
        app.fill(7, 1, 1 << 80)
    except OverflowError:
        caught += 1
    try:
        app.fill(1 << 80, 1, 0)
    except OverflowError:
        caught += 1
    try:
        app.fill(1 << 31, 1, 0)
    except OverflowError:
        caught += 1
    try:
        app.fill(-(1 << 31) - 1, 1, 0)
    except OverflowError:
        caught += 1
    assert caught == 5
    assert last() == 8
    assert load_i64(buf, 0) == 0
    assert load_i64(buf, 8) == -1
    assert load_i64(buf, 16) == 0
    print("CHECKED_EXTERN_EDGE_OK")
edge_main()
'''


def _compile_ir(tmp_path, monkeypatch, source, threads):
    monkeypatch.setenv("PCC_WITH_THREADS", threads)
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "default")
    path = tmp_path / "checked_extern.py"
    ir_path = tmp_path / "checked_extern.ll"
    path.write_text(source)
    compile_python(str(path), str(ir_path), emit_llvm_only=True,
                   python_library=True, backend="self", libpython_mode="off",
                   ir_scaffold_mode="on")
    return ir_path.read_text()


def _body(text, suffix):
    match = re.search(r"^define[^\n]*@user_[^\n]*" + suffix + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, suffix
    return match[1]


@pytest.mark.parametrize("threads", ["0", "1"])
def test_conversion_errors_are_checked_before_raw_addresses_and_foreign_call(
    tmp_path, monkeypatch, threads,
):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline_targets import host_target_triple

    text = _compile_ir(tmp_path, monkeypatch, EDGE_SOURCE, threads)
    body = _body(text, "ContextualFillApp_fill")
    # Contextual business arguments remain objects; this boundary check must
    # not replace their Python semantics with a machine-integer method ABI.
    header = next(line for line in text.splitlines() if line.startswith("define") and "ContextualFillApp_fill(" in line)
    assert "ptr %byte_value" in header and "ptr %count" in header and "ptr %offset" in header
    address = re.search(r"%unsafe\.int\.to\.ptr[^\n]* = inttoptr ", body).start()
    offset_conversion = body.index("@py_int_to_i64_lane(")
    assert "@py_err_occurred(" in body[offset_conversion:address]
    foreign = body.index("@memset(")
    conversions = [match.end() for match in re.finditer(r"@py_int_to_i64_lane\(", body[:foreign])]
    assert len(conversions) >= 3
    for start in conversions:
        assert "@py_err_occurred(" in body[start:foreign]
    assert "extern.integer.overflow" in body
    assert "extern.argument.cleanup" in body
    assert "unsafe.integer.cleanup" in body
    data = emit_owned_object(text, host_target_triple())
    assert data
    (tmp_path / "checked_extern.o").write_bytes(data)


def test_numeric_temporaries_do_not_change_c_obj_argument_root_mapping(tmp_path, monkeypatch):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline_targets import host_target_triple

    text = _compile_ir(tmp_path, monkeypatch, '''
from pcc.extern import extern, c_obj, c_int32, c_int64, c_void
numeric_first = extern("checked_numeric_first", (c_int32, c_obj, c_int64), c_void)
object_first = extern("checked_object_first", (c_obj, c_int32), c_void)
def first(value, obj):
    numeric_first(value, obj, 7)
def second(value):
    object_first((object(),), value)
''', "0")
    body = _body(text, "first")
    definitions = {match[1]: match[2] for match in re.finditer(r"^\s*(%[^ ]+) = ([^\n]+)", body, re.M)}
    current = next(value for value, definition in definitions.items()
                   if "extern.object.argument.current" in value and "@pcc_gc_load_ptr" in definition)
    root_pointer = re.findall(r"%[^ ,)]+", definitions[current])[-1]
    assert "extern.object.argument" in definitions[root_pointer]
    assert "extern.integer.argument" not in definitions[root_pointer]
    assert "extern.argument.cleanup" in _body(text, "second")
    assert emit_owned_object(text, host_target_triple())


def test_checked_unsafe_helper_is_admitted_to_native_static_method_inventory():
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS

    name = "_unsafe_checked_i64_value"
    assert name in L1_CODEGEN_HOST_METHODS
    entry = next(item for item in L1_CODEGEN_STATIC_METHODS if item["name"] == name)
    assert tuple(parameter["name"] for parameter in entry["call_sig"]) == ("self", "value", "expr")


@pytest.mark.integration
def test_native_checked_integer_edges_preserve_callee_and_buffer_state(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "checked_extern.py"
    binary = tmp_path / ("checked_extern.exe" if os.name == "nt" else "checked_extern")
    source.write_text(EDGE_SOURCE)
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "CHECKED_EXTERN_EDGE_OK\n", (backend, result.stdout, result.stderr)
