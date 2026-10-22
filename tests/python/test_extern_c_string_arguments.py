"""Managed string arguments cross the raw C ABI with bounded pin leases."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.pipeline import compile_python
from tests.owned_ir_validation import verify_ir_text


HEADER = '''from pcc.extern import extern, c_str, c_ptr, c_rawptr, c_obj, c_int64, c_abi_export
pair = extern("c_string_pair_probe", (c_str, c_str, c_int64), c_int64)
identity = extern("c_string_identity_probe", (c_str, c_obj), c_obj)
'''


def _compile(tmp_path, body, directive=""):
    source = tmp_path / "string_args.py"
    output = tmp_path / "string_args.ll"
    source.write_text(directive + HEADER + body)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   python_library=True, backend="self", libpython_mode="off",
                   ir_scaffold_mode="on")
    text = output.read_text()
    verify_ir_text(text)
    return text


def _body(text, name):
    match = re.search(r"^define[^\n]*@user_string_args_" + name + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match, name
    return match[1]


def test_c_string_projection_follows_all_argument_evaluation(tmp_path):
    text = _compile(tmp_path, '''
def probe(left: str, make, collect) -> int:
    return pair(left, make(), collect())
''')
    body = _body(text, "probe")
    foreign = body.index("@c_string_pair_probe(")
    callbacks = [m.start() for m in re.finditer("@py_obj_call\\(", body)]
    projections = [m.start() for m in re.finditer("@py_str_utf8\\(", body)]
    assert len(projections) == 2
    assert callbacks and max(callbacks) < min(projections) < foreign
    assert "extern.argument.cleanup" in body
    assert "extern.pin.prior" in body
    assert "@pcc_gc_take_pinned_slot(" in body
    assert "@pcc_gc_store_root(" in body[:min(callbacks)]


def test_c_string_dynamic_value_is_checked_before_layout_access(tmp_path):
    text = _compile(tmp_path, '''
def probe(value, other: str) -> int:
    return pair(value, other, 0)
''')
    body = _body(text, "probe")
    assert body.index("@py_obj_type_tag(") < body.index("@py_str_utf8(")
    assert "extern.string.invalid" in body
    assert "extern.string.valid" in body
    assert "c_str argument must be str" in text or "\\63\\5F\\73\\74\\72" in text


def test_c_string_managed_result_survives_alias_cleanup(tmp_path):
    text = _compile(tmp_path, '''
def probe(value: str):
    return identity(value, value)
''')
    body = _body(text, "probe")
    foreign = body.index("@c_string_identity_probe(")
    assert "extern.result.alias.prior" in body[foreign:]
    assert "extern.result.root" in body[foreign:]
    assert body[foreign:].count("@pcc_gc_take_pinned_slot(") >= 3


@pytest.mark.parametrize("directive", ["__pcc_freestanding__", "__pcc_runtime_port__"])
def test_c_string_pointer_lane_remains_raw(tmp_path, directive):
    text = _compile(tmp_path, '''
from pcc import i64
@c_abi_export("c_string_pair_probe")
def raw_pair(left, right, value: i64) -> i64:
    return value
@c_abi_export("user_string_args_probe")
def probe(left, right) -> i64:
    return pair(left, right, 0)
'''.replace('def probe(left, right) -> i64:',
               'def probe(left, right) -> i64:' if directive == '__pcc_freestanding__'
               else 'def probe(left, right) -> int:'), directive + " = True\n")
    body = _body(text, "probe")
    assert "@py_str_utf8(" not in body
    assert "extern.string.invalid" not in body


@pytest.mark.parametrize("value", ['b"bytes"', '17', 'None', '[1]', '1.5'])
def test_c_string_non_string_values_keep_runtime_validation(tmp_path, value):
    text = _compile(tmp_path, '''
def probe() -> int:
    return pair(''' + value + ''', "other", 0)
''')
    body = _body(text, "probe")
    assert "extern.string.invalid" in body
    assert body.index("@py_obj_type_tag(") < body.index("@py_str_utf8(")


def test_c_string_later_exception_releases_prior_operands(tmp_path):
    text = _compile(tmp_path, '''
def fail() -> int:
    raise ValueError("later operand")
def probe(value: str) -> int:
    try:
        return pair(value, value, fail())
    except ValueError:
        return 7
''')
    body = _body(text, "probe")
    assert "extern.argument.cleanup" in body
    assert body.index("@user_string_args_fail(") < body.index("@py_str_utf8(")
    cleanups = re.findall(r"(?ms)^extern.argument.cleanup[^:]*:\n(.*?)(?=^[\w.]+:|\Z)", body)
    assert any(part.count("@pcc_gc_take_pinned_slot(") == 2 for part in cleanups)


_NATIVE_SOURCE = '''from pcc import i64
from pcc.extern import extern, c_str, c_ptr, c_obj, c_int, c_int64, c_void, c_abi_export
length = extern("strlen", (c_str,), c_int64)
pair = extern("test_cstr_pair", (c_str, c_str, c_int64), c_int64)
callback_read = extern("test_cstr_callback_read", (c_str, c_obj, c_ptr), c_int64)
identity = extern("test_cstr_identity", (c_str, c_obj), c_obj)
object_callback = extern("test_obj_callback_identity", (c_obj, c_ptr), c_obj)
object_error = extern("test_obj_callback_error", (c_obj, c_ptr), c_obj)
state = extern("test_cstr_pin_state", (c_ptr,), c_int64)
pin = extern("pcc_gc_pin", (c_ptr,), c_void)
unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
collect = extern("pcc_gc_collect", (c_int,), c_int64)

def later() -> int:
    collect(0)
    return 3

def failing() -> int:
    collect(0)
    raise ValueError("later argument")

@c_abi_export("test_cstr_callback")
def callback(value) -> i64:
    values = [value]
    collect(0)
    return 0

@c_abi_export("test_cstr_error_callback")
def error_callback(value) -> i64:
    raise ValueError("foreign")

def invalid(value):
    try:
        length(value)
    except TypeError:
        print("type")

def check():
    left = "ab" + str(123)
    right = "é" + str(456)
    print(length(left))
    print(pair(left, right, later()))
    print(pair("q" + str(8), "r" + str(9), later()))
    print(callback_read(left, left, callback))
    tracked = [1, 2]
    returned = object_callback(tracked, callback)
    print(returned is tracked)
    try:
        object_error(tracked, error_callback)
    except ValueError:
        print("foreign")
    pin(left)
    print(state(left) == 64)
    print(pair(left, left, later()))
    print(state(left) == 64)
    result = identity(left, left)
    print(result == left)
    print(state(left) == 64)
    try:
        pair(left, right, failing())
    except ValueError:
        print("later")
    print(state(left) == 64)
    unpin(left)
    before = state(left)
    result = identity(left, left)
    print(result == left)
    print(state(left) == before)
    invalid(b"bytes")
    invalid(17)
    invalid(None)
    invalid([1])
    invalid(1.5)
    print(length("done"))
check()
'''

_NATIVE_CALLEES = '''
declare i64 @strlen(ptr)
declare void @py_incref(ptr)
declare i64 @pcc_gc_object_is_address_pinned(ptr)
declare void @pcc_py_gc_minor_graph_lock()
declare void @pcc_py_gc_minor_graph_unlock()
define ptr @test_obj_callback_error(ptr %owner, ptr %callback) {
entry:
  %ignore = call i64 %callback(ptr %owner)
  ; The callback left ValueError pending. Return a valid owned alias anyway
  ; so the caller must preserve that exception through result/root cleanup.
  call void @py_incref(ptr %owner)
  ret ptr %owner
}
define ptr @test_obj_callback_identity(ptr %owner, ptr %callback) {
entry:
  %ignore = call i64 %callback(ptr %owner)
  call void @pcc_py_gc_minor_graph_lock()
  %pin = call i64 @pcc_gc_object_is_address_pinned(ptr %owner)
  call void @pcc_py_gc_minor_graph_unlock()
  %protected = icmp ne i64 %pin, 0
  br i1 %protected, label %good, label %bad
good:
  call void @py_incref(ptr %owner)
  ret ptr %owner
bad:
  ret ptr null
}
define i64 @test_cstr_pair(ptr %a, ptr %b, i64 %c) {
entry:
  %la = call i64 @strlen(ptr %a)
  %lb = call i64 @strlen(ptr %b)
  %sum = add i64 %la, %lb
  %out = add i64 %sum, %c
  ret i64 %out
}
define i64 @test_cstr_callback_read(ptr %text, ptr %owner, ptr %callback) {
entry:
  %before = call i64 @strlen(ptr %text)
  %ignore = call i64 %callback(ptr %owner)
  %after = call i64 @strlen(ptr %text)
  %same = icmp eq i64 %before, %after
  %result = zext i1 %same to i64
  ret i64 %result
}
define ptr @test_cstr_identity(ptr %text, ptr %owner) {
entry:
  call void @py_incref(ptr %owner)
  ret ptr %owner
}
define i64 @test_cstr_pin_state(ptr %owner) {
entry:
  %p = getelementptr i8, ptr %owner, i64 12
  %flags = load i32, ptr %p
  %pin = and i32 %flags, 64
  %result = zext i32 %pin to i64
  ret i64 %result
}
'''


def test_c_string_native_regression_shape_compiles_to_verified_ir(tmp_path):
    source = tmp_path / "string_native.py"
    output = tmp_path / "string_native.ll"
    source.write_text(_NATIVE_SOURCE)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    verify_ir_text(text)
    assert "@test_cstr_callback_read(" in text
    assert "@pcc_gc_take_pinned_slot(" in text


@pytest.mark.integration
def test_c_string_arguments_execute_all_collectors(tmp_path, pcc_runtime_archive):
    import os
    import subprocess
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline_targets import host_target_triple

    source = tmp_path / "string_native.py"
    output = tmp_path / "string_native"
    callee = tmp_path / "string_callees.o"
    source.write_text(_NATIVE_SOURCE)
    target = host_target_triple()
    callee.write_bytes(emit_owned_object('target triple = "' + target + '"\n' + _NATIVE_CALLEES, target))
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
                   link_args=(str(callee),))
    expected = "5\n13\n7\n1\nTrue\nforeign\nTrue\n13\nTrue\nTrue\nTrue\nlater\nTrue\nTrue\nTrue\n" + "type\n" * 5 + "4\n"
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc), PCC_GC_MINOR_ALLOC_MAX="4096", PATH="", PCC_HOST_PYTHON="/nonexistent/host-python"))
        (tmp_path / ("gc" + str(gc) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(gc) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, (gc, result.returncode, result.stdout, result.stderr)
        assert result.stderr == "", (gc, result.stderr)
        assert result.stdout == expected, (gc, result.stdout)


class _PinModel:
    """Run actual extern release methods against a moving-root/pin model."""
    def __init__(self):
        from pcc.frontends.python.codegen.extern_lowering import ExternScaffoldMixin
        self._extern_repin_root = ExternScaffoldMixin._extern_repin_root.__get__(self)
        self._extern_load_root = ExternScaffoldMixin._extern_load_root.__get__(self)
        self._extern_take_root = ExternScaffoldMixin._extern_take_root.__get__(self)
        self._extern_release_roots = ExternScaffoldMixin._extern_release_roots.__get__(self)
        self.current_func_def = None
        self.runtime = {name: name for name in (
            "pcc_gc_load_ptr", "pcc_gc_take_pinned_slot",
            "pcc_py_gc_minor_graph_lock", "pcc_py_gc_minor_graph_unlock")}
        self.builder = self
        self.slots = {}
        self.events = []
        self.metric = 0

    def _fresh(self, name):
        return name

    def _as_gc_ptr(self, slot):
        return slot

    def _gc_pin(self, value):
        if value is not None:
            value["pinned"] = True
            self.metric += 1

    def _gc_unpin(self, value):
        if value is not None:
            value["pinned"] = False
            self.metric -= 1

    def _emit_gc_frame_leave_lifo_for_slot(self, slot):
        assert self.slots[slot] is None or self.slots[slot]["pinned"]
        self.events.append(("leave", slot))

    def _gc_release(self, value, known_object):
        assert value["refs"] > 0, "double release"
        value["refs"] -= 1
        self.events.append(("release", value["name"]))

    def call(self, function, args, name=None):
        if function == "pcc_gc_load_ptr":
            return self.slots[args[1]]
        if function == "pcc_gc_take_pinned_slot":
            slot, prior = args
            value = self.slots[slot]
            self.slots[slot] = None
            self._gc_unpin(value)
            if value is not None and prior:
                value["pinned"] = True
            self.events.append(("take", slot))
            return value
        assert function in ("pcc_py_gc_minor_graph_lock", "pcc_py_gc_minor_graph_unlock")

    def acquire(self, slot, value, owned=False):
        prior = 64 if value["pinned"] else 0
        self._gc_pin(value)
        value["refs"] += 1  # pcc_gc_store_root retains its own reference
        self.slots[slot] = value
        return (slot, owned, prior)


@pytest.mark.parametrize("initial_pin", [False, True])
def test_extern_root_cleanup_restores_alias_lease_and_metric(initial_pin):
    model = _PinModel()
    obj = {"name": "object", "pinned": initial_pin, "refs": 2}
    first = model.acquire("first", obj)
    second = model.acquire("second", obj, owned=True)
    # A nested operand clears the header flag but balances its own metric.
    obj["pinned"] = False
    model._extern_release_roots((first, second))
    assert obj["pinned"] is initial_pin
    assert obj["refs"] == 1  # caller's borrowed owner survives; temporary is consumed
    assert model.metric == 0
    assert model.slots == {"first": None, "second": None}
    assert [item for item in model.events if item[0] == "take"] == [("take", "second"), ("take", "first")]
    assert ("release", "object") in model.events


def test_extern_root_cleanup_uses_relocated_slot_value():
    model = _PinModel()
    old = {"name": "old", "pinned": False, "refs": 1}
    root = model.acquire("value", old, owned=True)
    moved = {"name": "moved", "pinned": False, "refs": old["refs"]}
    model.slots["value"] = moved
    model._extern_release_roots((root,))
    assert ("release", "moved") in model.events
    assert ("release", "old") not in model.events
    assert model.metric == 0
    assert moved["pinned"] is False
    assert moved["refs"] == 0


def test_c_string_thread_roots_transfer_without_duplicate_frame_leave(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    text = _compile(tmp_path, '''
def probe(value: str, later) -> int:
    return pair(value, value, later())
''')
    body = _body(text, "probe")
    assert "extern.argument.root" in body
    assert "@pcc_gc_frame_enter(" in body
    assert "@pcc_gc_take_pinned_slot(" in body
    assert "extern.pin.prior" in body


def test_c_string_native_callees_emit_owned_object():
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline_targets import host_target_triple
    target = host_target_triple()
    text = 'target triple = "' + target + '"\n' + _NATIVE_CALLEES
    verify_ir_text(text)
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.parametrize("value", ['"hello"', '1.5'])
def test_extern_pin_header_offset_is_bytes_for_typed_constants(tmp_path, value):
    body = _body(_compile(tmp_path, '''
def probe() -> int:
    return pair(''' + value + ''', "other", 0)
'''), "probe")
    headers = re.findall(r"(?ms)^extern.pin.header[^:]*:\n(.*?)(?=^[\w.]+:|\Z)", body)
    if value.startswith('"'):
        # A str literal is a static immortal object: nothing is pinned, so
        # no header is read. The float constant still reaches the header.
        assert headers == []
        assert not re.search(r"@pcc_gc_pin\(ptr @\.pystr\.obj\.", body)
        return
    assert headers
    for header in headers:
        assert re.search(r"getelementptr i8, ptr [^\n]+, i64 12", header), header
        assert "getelementptr {" not in header


@pytest.mark.parametrize("owned", [False, True])
def test_extern_operand_releases_root_reference_and_only_owned_source(owned):
    model = _PinModel()
    obj = {"name": "source", "pinned": False, "refs": 1}
    root = model.acquire("argument", obj, owned)
    assert obj["refs"] == 2
    model._extern_release_roots((root,))
    assert obj["refs"] == (0 if owned else 1)
    assert model.metric == 0


@pytest.mark.parametrize("alias", [False, True])
def test_extern_managed_result_transfers_exactly_one_reference(alias):
    model = _PinModel()
    argument = {"name": "argument", "pinned": False, "refs": 1}
    argument_root = model.acquire("argument", argument)
    result = argument if alias else {"name": "result", "pinned": False, "refs": 0}
    result["refs"] += 1  # Foreign c_obj return is an owned reference.
    result_root = model.acquire("result", result, owned=True)
    if alias:
        result_root = (result_root[0], True, argument_root[2])
    model._extern_release_roots((argument_root,))
    transferred = model._extern_take_root(result_root)
    assert transferred is result
    assert result["refs"] == (2 if alias else 1)
    assert argument["refs"] == (2 if alias else 1)
    assert model.metric == 0
    assert result["pinned"] is False
    model._gc_release(transferred, known_object=True)
    assert result["refs"] == (1 if alias else 0)


def test_callback_alias_shape_exercises_legacy_unpin_before_collection(tmp_path):
    """Keep the independent review's callback reproducer in the native gate.

    This is reachability evidence, not a claim that the foreign pointer is
    safe: counted foreign-address leases are still required across this call.
    """
    source = tmp_path / "callback_alias.py"
    output = tmp_path / "callback_alias.ll"
    source.write_text(_NATIVE_SOURCE)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    verify_ir_text(text)
    match = re.search(r"^define[^\n]*@test_cstr_callback\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match
    body = match[1]
    assert re.search(r"@pcc_gc_unpin\(ptr %value\)", body)
    assert body.index("@pcc_gc_unpin(") < body.index("@pcc_gc_collect(")


def test_c_string_counted_leases_cover_projection_call_and_partial_failure(tmp_path):
    body = _body(_compile(tmp_path, '''
def probe(left: str, right: str) -> int:
    return pair(left, right, 0)
'''), "probe")
    foreign=body.index("@c_string_pair_probe(")
    assert body.index("@pcc_gc_foreign_lease_acquire(") < body.index("@py_str_utf8(") < foreign
    assert "@pcc_gc_foreign_lease_release(" in body[foreign:]
    assert "extern.lease.acquire.error" in body
    assert "extern.lease.overflow" in body
    assert "extern.lease.cleanup.error" in body
    assert "@py_err_occurred(" in body[foreign:]
    assert "@pcc_gc_take_pinned_slot(" in body[foreign:]


def test_foreign_lease_helpers_match_native_host_method_signatures():
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
    expected={
        "_extern_release_foreign_leases":("self","leases"),
        "_extern_check_lease_cleanup":("self","failed","roots"),
        "_extern_cleanup_block":("self","roots","target","leases"),
    }
    static={item['name']:item for item in L1_CODEGEN_STATIC_METHODS}
    exports=_default_native_module_exports('pcc.frontends.python.codegen.layer1')
    methods=exports['pcc.frontends.python.codegen.layer1']['L1CodeGen']['methods']
    native={item['name']:item for item in methods}
    assert len(static) == len(L1_CODEGEN_STATIC_METHODS) == 366
    assert tuple(item['name'] for item in L1_CODEGEN_STATIC_METHODS) == L1_CODEGEN_HOST_METHODS
    for name,parameters in expected.items():
        assert name in L1_CODEGEN_HOST_METHODS
        assert tuple(p['name'] for p in static[name]['call_sig']) == parameters
        assert static[name]['call_sig'] == native[name]['call_sig']
    assert static['_extern_cleanup_block']['call_sig'][-1]['has_default']


def test_mixed_c_string_call_does_not_root_explicit_raw_pointer(tmp_path):
    body = _body(_compile(tmp_path, '''
mixed = extern("c_string_raw_probe", (c_str, c_ptr), c_int64)
def probe(text: str, raw: c_ptr) -> int:
    return mixed(text, raw)
'''), "probe")
    assert "@c_string_raw_probe(" in body
    assert body.count("@pcc_gc_foreign_lease_acquire(") == 1
    assert not re.search(r"ptrtoint ptr %raw",body)
    assert not re.search(r"@pcc_gc_store_root\([^\n]*, ptr %raw",body)


def test_c_obj_only_extern_counts_owner_across_callback(tmp_path):
    body=_body(_compile(tmp_path, '''
only_object = extern("c_obj_callback_probe", (c_obj, c_ptr), c_obj)
def probe(value, callback: c_ptr):
    return only_object(value, callback)
'''),"probe")
    foreign=body.index("@c_obj_callback_probe(")
    assert body.count("@pcc_gc_foreign_lease_acquire(") == 1
    assert body.index("@pcc_gc_foreign_lease_acquire(") < foreign
    assert "@pcc_gc_foreign_lease_release(" in body[foreign:]
    assert "extern.result.alias.prior" in body[foreign:]
    assert "@py_str_utf8(" not in body
