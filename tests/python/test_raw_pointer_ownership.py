"""Explicit foreign addresses must never enter the managed object protocol."""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.export_meta import decode_type, encode_type
from pcc.frontends.python.py_ast import ClassType, FuncDef, IntType, RawPointerType
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


HEADER = '''from pcc.extern import c_ptr as Handle
from pcc.unsafe import int_to_ptr, ptr_to_int
__pcc_runtime_port__ = True
'''
POINTER_PROGRAM = '''
def echo(value: Handle) -> Handle:
    copied = value
    return copied

def choose(value: Handle, flag: bool) -> Handle:
    copied = echo(value)
    return copied if flag else echo(value)

def run() -> int:
    first = int_to_ptr(1)
    second: Handle = choose(echo(first), True)
    return ptr_to_int(second)

def run_opaque() -> int:
    first = int_to_ptr(4096)
    second: Handle = choose(echo(first), False)
    return ptr_to_int(second)
'''


def _infer(source, name="raw_probe"):
    return infer_module(parse_and_lift(source, name + ".py", name))


def _functions(module):
    return {stmt.name: stmt for stmt in module.body if isinstance(stmt, FuncDef)}


def _compile(tmp_path, source, name="raw_probe"):
    path = tmp_path / (name + ".py")
    path.write_text(source)
    out = path.with_suffix(".ll")
    pipeline.compile_python(str(path), str(out), emit_llvm_only=True,
                            python_library=True, libpython_mode="off", backend="self",
                            target_triple="x86_64-unknown-linux-gnu")
    return out.read_text()


def _body(text, name):
    match = re.search(r"^define[^\n]*@" + re.escape(name) + r"\([^\n]*\{\n(.*?)^}", text, re.M | re.S)
    assert match, name
    return match.group(1)


@pytest.mark.parametrize("imports,marker", [
    ("from pcc.extern import c_ptr", "c_ptr"),
    ("from pcc.extern import c_ptr as Handle", "Handle"),
    ("from pcc.extern import c_rawptr as Handle", "Handle"),
    ("import pcc.extern as ffi", "ffi.c_ptr"),
    ("import pcc.extern", "pcc.extern.c_rawptr"),
    ("from pcc import extern as ffi", "ffi.c_rawptr"),
], ids=["c-ptr", "ptr-alias", "rawptr-alias", "module-alias", "qualified", "from-module"])
def test_verified_pointer_annotation_propagates(imports, marker):
    source = imports + f'''\n__pcc_runtime_port__ = True
def echo(value: {marker}) -> {marker}:
    copied = value
    return copied
def invoke(value: {marker}) -> {marker}:
    return echo(value)
'''
    functions = _functions(_infer(source))
    for fn in functions.values():
        assert isinstance(fn.return_ty, RawPointerType)
        assert isinstance(fn.args[0].annotation, RawPointerType)
        assert isinstance(fn.body[-1].value.ty, RawPointerType)
    assert isinstance(functions["echo"].body[0].value.ty, RawPointerType)


@pytest.mark.parametrize("prefix,marker", [
    ("class c_ptr:\n    pass\n", "c_ptr"),
    ("from pcc.extern import c_ptr as Handle\nclass Handle:\n    pass\n", "Handle"),
    ("from pcc.extern import c_ptr as Handle\nclass c_ptr:\n    pass\n", "c_ptr"),
], ids=["user-name", "shadowed-import", "distinct-alias"])
def test_user_class_pointer_spelling_stays_managed(prefix, marker):
    fn = _functions(_infer(prefix + f"def echo(value: {marker}) -> {marker}:\n    return value\n"))["echo"]
    assert isinstance(fn.return_ty, ClassType)
    assert isinstance(fn.args[0].annotation, ClassType)


def test_raw_pointer_export_wire_roundtrip():
    raw = RawPointerType("pcc.extern.c_rawptr")
    assert encode_type(raw) == ("raw_pointer",)
    assert isinstance(decode_type(encode_type(raw)), RawPointerType)


def test_raw_pointer_export_metadata_is_canonical(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    provider = tmp_path / "provider.py"
    provider.write_text(HEADER + POINTER_PROGRAM)
    _, exports, _ = build_closed_world_context([str(provider)], ["provider"])
    assert exports["provider"]["echo"]["param_types"] == (("raw_pointer",),)
    assert exports["provider"]["echo"]["return_ty"] == ("raw_pointer",)


def test_ordinary_unsafe_address_projection_remains_integer():
    fn = _functions(_infer("from pcc.unsafe import int_to_ptr\ndef make():\n    return int_to_ptr(1)\n"))["make"]
    assert isinstance(fn.body[-1].value.ty, IntType)


def test_raw_pointer_params_calls_locals_returns_have_no_gc_ownership(tmp_path):
    text = _compile(tmp_path, HEADER + POINTER_PROGRAM)
    for name in ("echo", "choose", "run", "run_opaque"):
        body = _body(text, "user_raw_probe_" + name)
        assert not re.search(r"call[^\n]*@pcc_gc_(?!safepoint)", body), body
    assert "inttoptr i64 1 to ptr" in text


def test_managed_and_scalar_ownership_controls(tmp_path):
    text = _compile(tmp_path, '''def managed(value: str) -> str:
    return value
def scalar(value: float) -> float:
    return value
''')
    managed = _body(text, "user_raw_probe_managed")
    scalar = _body(text, "user_raw_probe_scalar")
    assert "@pcc_gc_retain" in managed
    assert not re.search(r"call[^\n]*@pcc_gc_(?!safepoint)", scalar)
    assert "ret double" in scalar


def test_opaque_raw_pointer_executes_on_all_gc_backends(tmp_path, pcc_runtime_archive):
    source = tmp_path / "raw_native.py"
    controls = "\ndef managed(value: str) -> str:\n    return value\ndef scalar(value: float) -> float:\n    return value + 0.25\nprint(run(), run_opaque(), managed(str(run())), scalar(2.0))\n"
    source.write_text(HEADER + POINTER_PROGRAM + controls)
    output = tmp_path / "raw_native"
    pipeline.compile_python(str(source), str(output), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH=""))
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == "1 4096 1 2.25\n", (gc, result.stdout)
        assert result.stderr == "", (gc, result.stderr)


def test_raw_pointer_typed_ast_wire_roundtrip(tmp_path):
    from pcc.frontends.python.pipeline_ast_wire import _write_py_ast_wire, _read_py_ast_wire
    module = _infer(HEADER + POINTER_PROGRAM)
    wire = tmp_path / "raw.ast.json"
    _write_py_ast_wire(str(wire), module)
    restored = _read_py_ast_wire(str(wire))
    assert isinstance(_functions(restored)["echo"].return_ty, RawPointerType)
    assert isinstance(_functions(restored)["echo"].args[0].annotation, RawPointerType)


def test_cross_module_pointer_signature_reaches_ordinary_caller(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    provider = tmp_path / "provider.py"
    provider.write_text(HEADER + POINTER_PROGRAM)
    consumer = tmp_path / "consumer.py"
    consumer.write_text("from provider import echo\ndef bridge(value):\n    return echo(value)\n")
    parsed, exports, _ = build_closed_world_context([str(provider), str(consumer)], ["provider", "consumer"])
    typed = infer_module(parse_and_lift(consumer.read_text(), str(consumer), "consumer"), external_exports=exports)
    call = _functions(typed)["bridge"].body[-1].value
    assert isinstance(call.ty, RawPointerType)
    assert isinstance(call.func.ty.params[0], RawPointerType)


@pytest.mark.parametrize("body", [
    "def reject(value: Handle):\n    return [value]\n",
    "def reject(value: Handle):\n    return {'value': value}\n",
    "def reject(value: Handle) -> object:\n    copy: object = value\n    return copy\n",
    "class Box:\n    def __init__(self, value: Handle):\n        self.value = value\n",
    "raw: Handle = int_to_ptr(1)\n",
], ids=["list", "dict", "managed-local", "class-field", "published-global"])
def test_raw_pointer_cannot_cross_managed_storage(tmp_path, body):
    with pytest.raises((NotImplementedError, ValueError), match="raw pointer|RawPointer"):
        _compile(tmp_path, HEADER + body)


@pytest.mark.parametrize("imports,marker", [
    ("from pcc.extern import c_ptr as Handle", "Handle"),
    ("import pcc.extern as ffi\nclass c_ptr:\n    pass", "ffi.c_ptr"),
    ("import pcc.extern\nclass c_rawptr:\n    pass", "pcc.extern.c_rawptr"),
], ids=["direct-alias", "module-alias-collision", "qualified-collision"])
def test_host_native_parsers_preserve_raw_marker_identity(imports, marker):
    from pcc.frontends.python.parser import parse
    source = imports + f"\ndef echo(value: {marker}) -> {marker}:\n    return value\n"
    host = _functions(infer_module(parse(source, "parser_probe.py")))["echo"]
    native = _functions(_infer(source))["echo"]
    assert isinstance(host.return_ty, RawPointerType)
    assert isinstance(native.return_ty, RawPointerType)


def test_dynamic_raw_adapter_fails_before_reading_argument_objects(tmp_path):
    text = _compile(tmp_path, HEADER + POINTER_PROGRAM)
    adapter = _body(text, "user_raw_probe_echo_native_adapter")
    assert "@py_raise" in adapter
    assert "@py_tuple_get" not in adapter
    assert "@user_raw_probe_echo(" not in adapter
    message = "raw pointer ABI is unavailable through a dynamic function boundary"
    assert "".join("\\%02X" % byte for byte in message.encode()) in text


def test_cross_module_raw_call_ir_uses_callee_ownership_signature(tmp_path):
    provider = tmp_path / "provider.py"
    provider.write_text(HEADER + POINTER_PROGRAM + "\ndef make() -> Handle:\n    return int_to_ptr(1)\n")
    consumer = tmp_path / "consumer.py"
    consumer.write_text("from provider import echo, make\nfrom pcc.extern import c_ptr as Foreign\ndef bridge() -> Foreign:\n    result = echo(make())\n    return result\n")
    output = tmp_path / "cross.ll"
    pipeline.compile_python_multi([str(provider), str(consumer)], str(output),
        module_names=["provider", "consumer"], entry_module="consumer",
        backend="self", libpython_mode="off", emit_llvm_only=True)
    body = _body(output.read_text(), "user_consumer_bridge")
    assert not re.search(r"call[^\n]*@pcc_gc_(?!safepoint)", body), body


def test_freestanding_implicit_pointer_abi_stays_ptr(tmp_path):
    source = """from pcc import i64
from pcc.extern import c_abi_export, c_ptr
__pcc_freestanding__ = True
@c_abi_export("memcpy_shape")
def copy(dst, src, size: i64) -> c_ptr:
    return dst
@c_abi_export("raw_roundtrip")
def echo(value: c_ptr):
    return value
"""
    text = _compile(tmp_path, source)
    assert re.search(r"define external ptr @memcpy_shape\(ptr %dst, ptr %src, i64 %size\)", text)
    assert re.search(r"define external ptr @raw_roundtrip\(ptr %value\)", text)
    assert not re.search(r"call[^\n]*@pcc_gc_", text)


def test_explicit_return_annotation_survives_lift_inference_clone_and_wire(tmp_path):
    from pcc.frontends.python.parser import parse
    from pcc.frontends.python.pipeline_ast_wire import _write_py_ast_wire, _read_py_ast_wire
    from pcc.frontends.python.codegen.hoist_analysis import clone_funcdef
    source = "def implicit(value):\n    return value\ndef explicit(value) -> object:\n    return value\n"
    for module in (parse(source, "annotations.py"), parse_and_lift(source, "annotations.py", "annotations")):
        typed = infer_module(module)
        functions = _functions(typed)
        assert not functions["implicit"].has_return_annotation
        explicit = functions["explicit"]
        assert explicit.has_return_annotation
        cloned = clone_funcdef(explicit, "clone", explicit.args, explicit.return_ty, explicit.body)
        assert cloned.has_return_annotation
        wire = tmp_path / "annotations.json"
        _write_py_ast_wire(str(wire), typed)
        restored = _functions(_read_py_ast_wire(str(wire)))
        assert restored["explicit"].has_return_annotation
        assert not restored["implicit"].has_return_annotation


@pytest.mark.parametrize("shallow", [False, True], ids=["full", "shallow"])
def test_explicit_return_annotation_survives_export_reconstruction(tmp_path, shallow):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.codegen.extern_func_info_lowering import ExternFuncInfoLoweringMixin
    source = tmp_path / "annotations.py"
    source.write_text("def implicit(value):\n    return value\ndef explicit(value) -> object:\n    return value\n")
    _, exports, _ = build_closed_world_context([str(source)], ["annotations"], lift_indices=[] if shallow else None)
    host = ExternFuncInfoLoweringMixin()
    for name, expected in (("explicit", True), ("implicit", False)):
        info = exports["annotations"][name]
        assert info["has_return_annotation"] is expected
        assert host._extern_info_to_funcdef(name, info).has_return_annotation is expected


def test_legacy_raw_c_export_return_keeps_native_pointer_abi(tmp_path):
    source = HEADER + '''from pcc.extern import c_abi_export
@c_abi_export("legacy_raw")
def exported():
    return raw()
def raw() -> Handle:
    return int_to_ptr(4096)
'''
    text = _compile(tmp_path, source)
    body = _body(text, "legacy_raw")
    assert "ret ptr" in body
    assert "@user_raw_probe_raw(" in body
    assert not re.search(r"call[^\n]*@pcc_gc_(?!safepoint)", body)


@pytest.mark.parametrize("declaration", [
    '@c_abi_export("managed")\ndef reject() -> object:',
    'def reject():',
], ids=["explicit-object-export", "ordinary-helper"])
def test_raw_return_cannot_become_managed_or_unknown_python_value(tmp_path, declaration):
    source = HEADER + "from pcc.extern import c_abi_export\ndef raw() -> Handle:\n    return int_to_ptr(1)\n" + declaration + "\n    return raw()\n"
    with pytest.raises(NotImplementedError, match="raw pointer cannot cross"):
        _compile(tmp_path, source)


def test_runtime_managed_pointer_or_null_helper_preserves_null_sentinel(tmp_path):
    source = HEADER + '''from pcc.unsafe import null
def nullable(value, flag: bool):
    if flag:
        return value
    return null()
'''
    text = _compile(tmp_path, source)
    body = _body(text, "user_raw_probe_nullable")
    assert "ret ptr null" in body
    assert re.search(r"@pcc_gc_retain\(ptr %value(?:\.|\))", body)
    assert not re.search(r"call[^\n]*@pcc_gc_[^(]+\(ptr null\)", body)



def test_managed_c_export_return_ownership_is_unchanged(tmp_path):
    source = HEADER + """from pcc.extern import c_abi_export
@c_abi_export("managed_export")
def managed(value: str) -> str:
    return value
"""
    body = _body(_compile(tmp_path, source), "managed_export")
    assert "@pcc_gc_retain" in body
    assert "ret ptr" in body



def test_raw_and_annotation_static_native_mirrors_match_schema():
    from pcc.frontends.python.py_ast_contract import PY_AST_FIELD_NAME_OVERRIDES
    from pcc.frontends.python.pipeline_ast_wire import _py_ast_field_type_override
    from pcc.frontends.python.codegen.layer1_support import _PY_AST_STATIC_CLASS_FIELDS, _dataclass_field_names, _type_kind_key
    for kind in ("RawPointerType", "FuncDef"):
        assert _PY_AST_STATIC_CLASS_FIELDS[kind] == PY_AST_FIELD_NAME_OVERRIDES[kind]
    assert _py_ast_field_type_override("RawPointerType", "name") == "str"
    assert _py_ast_field_type_override("FuncDef", "has_return_annotation") == "bool"
    fn = _functions(_infer("def explicit() -> object:\n    return None\n"))["explicit"]
    assert _dataclass_field_names(fn) == PY_AST_FIELD_NAME_OVERRIDES["FuncDef"]
    assert _type_kind_key(RawPointerType("pcc.extern.c_rawptr")) == "RawPointerType"



@pytest.mark.parametrize("body", [
    "value: object = []\n    consume(value)",
    "value = []\n    alias = value\n    consume(alias)",
    "value = int_to_ptr(4096)\n    if flag:\n        value = []\n    consume(value)",
    "value = int_to_ptr(4096)\n    del value\n    consume(value)",
    "value = int_to_ptr(4096)\n    for value in [1]:\n        pass\n    consume(value)",
], ids=["dyn-list", "managed-alias", "mixed-branch", "deleted", "loop-shadow"])
def test_ordinary_dyn_values_do_not_gain_raw_ownership_by_pointer_shape(tmp_path, body):
    source = """from pcc.extern import c_ptr as Handle
from pcc.unsafe import int_to_ptr
def consume(value: Handle) -> None:
    pass
def reject(flag: bool):
    """ + body + "\n"
    with pytest.raises(NotImplementedError, match="raw pointer|RawPointer"):
        _compile(tmp_path, source)


def test_runtime_manual_c_api_pointer_views_keep_raw_return_abi(tmp_path):
    source = HEADER + """from pcc.extern import extern, c_ptr, c_int64, c_abi_export
from pcc.unsafe import global_load_ptr, cstr
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
def none_view() -> c_ptr:
    return global_load_ptr("py_None")
@c_abi_export("string_view")
def string_view() -> c_ptr:
    return py_str_new(cstr(""), 0)
"""
    text = _compile(tmp_path, source)
    for name in ("user_raw_probe_none_view", "string_view"):
        body = _body(text, name)
        assert "ret ptr" in body
        assert not re.search(r"call[^\n]*@pcc_gc_(?!safepoint)", body), body


@pytest.mark.parametrize("parameters,result,expression,arguments", [
    ("obj", "int", "ptr_to_int(obj)", "make()"),
    ("obj", "bool", "ptr_is_null(obj)", "make()"),
    ("left, right", "int", "ptr_to_int(right)", "make(), make()"),
    ("result", "Handle", "result", "make()"),
    ("definition, module", "int", "ptr_to_int(module)", "make(), make()"),
], ids=["header-int", "header-bool", "unicode-compare", "require-result", "module-slots"])
def test_resolved_manual_callee_accepts_legacy_pointer_formals(tmp_path, parameters, result, expression, arguments):
    wrap = "ptr_to_int(consume(" + arguments + "))" if result == "Handle" else "consume(" + arguments + ")"
    return_type = "bool" if result == "bool" else "int"
    source = HEADER + f'''from pcc.extern import c_abi_export
from pcc.unsafe import ptr_is_null
@c_abi_export("legacy_consumer")
def consume({parameters}) -> {result}:
    return {expression}
def make() -> Handle:
    return int_to_ptr(4096)
def invoke() -> {return_type}:
    return {wrap}
'''
    text = _compile(tmp_path, source)
    for name in ("legacy_consumer", "user_raw_probe_invoke"):
        body = _body(text, name)
        assert not re.search(r"call[^\n]*@pcc_gc_(?!safepoint)", body), body


def test_private_runtime_port_callee_with_rooted_dyn_formal_is_rejected(tmp_path):
    source = HEADER + '''from pcc.extern import c_abi_export
@c_abi_export("anchor")
def anchor() -> None:
    pass
def private(value) -> int:
    return ptr_to_int(value)
def make() -> Handle:
    return int_to_ptr(4096)
'''
    before = _compile(tmp_path, source, "private_policy")
    assert "@pcc_gc_frame_enter" in _body(before, "user_private_policy_private")
    with pytest.raises(NotImplementedError, match="raw pointer cannot cross"):
        _compile(tmp_path, source + "\ndef invoke() -> int:\n    return private(make())\n", "private_rejected")


def test_manual_pointer_comparison_and_join_use_unmanaged_views(tmp_path):
    source = HEADER + '''from pcc.extern import c_abi_export
@c_abi_export("raw_compare")
def compare(value, foreign: Handle) -> bool:
    return value == foreign
@c_abi_export("raw_join")
def choose(value, foreign: Handle, flag: bool) -> Handle:
    return value if flag else foreign
'''
    text = _compile(tmp_path, source)
    assert "icmp eq ptr" in _body(text, "raw_compare")
    assert "phi ptr" in _body(text, "raw_join")
    for name in ("raw_compare", "raw_join"):
        body = _body(text, name)
        assert not re.search(r"call[^\n]*@pcc_gc_(?!safepoint)", body), body
        assert "@py_obj_" not in body
        assert "@py_int_to_" not in body


@pytest.mark.parametrize("expression", ["value == foreign", "value if flag else foreign"], ids=["compare", "join"])
def test_ordinary_dyn_and_raw_pointer_views_cannot_mix(tmp_path, expression):
    source = f'''from pcc.extern import c_ptr as Handle
def reject(value: object, foreign: Handle, flag: bool):
    result = {expression}
'''
    with pytest.raises(NotImplementedError, match="raw pointer"):
        _compile(tmp_path, source)


def test_manual_abi_metadata_tracks_defining_module_and_symbol(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.pipeline_ast_wire import _write_py_ast_wire, _read_py_ast_wire
    from pcc.frontends.python.codegen.hoist_analysis import clone_funcdef
    provider = tmp_path / "manual_provider.py"
    provider.write_text('''from pcc.extern import c_abi_export
__pcc_runtime_port__ = True
@c_abi_export("user_manual_provider_consume")
def consume(value) -> int:
    return 1
def private(value) -> int:
    return 1
''')
    parsed, exports, _ = build_closed_world_context([str(provider)], ["manual_provider"])
    fn = _functions(infer_module(parsed[0]))["consume"]
    assert fn.manual_pointer_abi
    assert clone_funcdef(fn, fn.name, fn.args, fn.return_ty, fn.body).manual_pointer_abi
    wire = tmp_path / "manual.json"
    _write_py_ast_wire(str(wire), fn)
    assert _read_py_ast_wire(str(wire)).manual_pointer_abi
    assert exports["manual_provider"]["consume"]["manual_pointer_abi"]
    assert exports["manual_provider"]["consume"]["manual_pointer_abi_symbol"] == "user_manual_provider_consume"
    assert exports["manual_provider"]["private"]["manual_pointer_abi_symbol"] == ""
    provider.write_text("""from pcc.extern import c_abi_typed_export, c_ptr
__pcc_runtime_port__ = True
@c_abi_typed_export("user_manual_provider_good", "ptr", ("ptr",))
def good(value) -> c_ptr:
    return value
@c_abi_typed_export("user_manual_provider_bad", "ptr", ("i64",))
def bad(value) -> c_ptr:
    return value
""")
    _, typed_exports, _ = build_closed_world_context([str(provider)], ["manual_provider"])
    assert typed_exports["manual_provider"]["good"]["manual_pointer_abi_symbol"] == "user_manual_provider_good"
    assert typed_exports["manual_provider"]["bad"]["manual_pointer_abi_symbol"] == ""
