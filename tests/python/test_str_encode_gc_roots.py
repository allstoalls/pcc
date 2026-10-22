"""Counted encoder address leases, direct-entry roots, and failure teardown.

The fixture-free memory model executes the checked-in guard bodies. Both
moving collectors honor legacy Boolean pins, but an overlapping callback can
clear that flag while another operation still borrows the address. Independent
counted leases must survive that interleave. Native controls remain deferred.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots
from test_int_bytes_gc_roots import _BytesMemory

ROOT = Path(__file__).resolve().parents[2]
PORT = ROOT / "pcc/runtime/py/py_str_accessors.py"


class _EncoderMemory(_BytesMemory):
    def __init__(self, phase="acquire", fail_acquire=0, acquire_code=-1,
                 pending=None, fail_release=False, alias=False, backend=3):
        super().__init__(phase=phase, pending=pending, fail_release=fail_release)
        self.backend = backend
        self.fail_acquire = fail_acquire
        self.acquire_code = acquire_code
        self.acquires = 0
        self.releases = []
        self.frames = {}
        self.cached = []
        self.value.tag = abi.PY_TYPE_STR
        self.value.movable = True
        self.signed.tag = abi.PY_TYPE_STR
        if alias:
            self.order = self.value
            self.signed = self.value
        namespace = self.namespace
        namespace.update({
            "global_addr": lambda name: name,
            "memset": self.memset,
            "pcc_gc_frame_enter": self.frame_enter,
            "pcc_gc_frame_leave": self.frame_leave,
            "pcc_gc_foreign_lease_acquire": self.acquire,
            "pcc_gc_foreign_lease_release": self.release,
            "pcc_gc_load_ptr": lambda _owner, slot: self.read(slot, 0),
        })
        tree = ast.parse(PORT.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(PORT), "exec"), namespace)
        # Conversion bytes/error ranges have their own source-body oracle.
        # These hooks force GC precisely while the real guard holds raw views.
        namespace["_str_encode_names_leased"] = self.encode_names
        namespace["_str_encode_codec"] = self.encode_legacy
        self.namespace = namespace

    def memset(self, slots, _byte, size):
        for offset in range(0, size, 8):
            self.write(slots, offset, None)

    def frame_enter(self, frame_map, slots):
        self.gc("frame_before")
        count = 3 if "borrowed" in frame_map else 4
        handles = []
        for index in range(count):
            handle = object()
            self.handles[handle] = self.add(slots, index * 8)
            handles.append(handle)
        self.frames[slots] = handles
        # Publication must precede this park. Incoming raw SSA copies are
        # deliberately stale afterward, so the implementation must reload.
        self.gc("frame_enter")

    def frame_leave(self, slots):
        # The shared final transfer protocol has separate runtime gates. Here
        # force relocation while these slots are still authoritative.
        self.gc("frame_leave")
        for handle in self.frames.pop(slots):
            del self.handles[handle]

    def gc(self, phase):
        if self.depth or phase != self.phase:
            return
        for old in list(self.objects):
            if not old.alive or not old.movable or old.leases:
                continue
            # Mirror the shared address-pinned predicate used by GC3
            # oldification and GC4 relocation. Alias-unpin is injected by the
            # explicit callback hooks, never by ignoring a live Boolean pin.
            if old.flags & abi.PY_FLAG_GC_PINNED:
                continue
            moved = self.make(old.name, old.tag)
            moved.references, moved.fields = old.references, old.fields.copy()
            moved.flags = old.flags
            moved.leases = old.leases
            for slot in self.handles.values():
                if self.read(slot, 0) is old:
                    self.write(slot, 0, moved)
            for value in self.objects:
                if value.alive:
                    value.fields = {key: moved if item is old else item
                                    for key, item in value.fields.items()}
            old.alive, old.references = False, 0
            self.moves += 1

    def acquire(self, slot):
        self.acquires += 1
        self.gc("acquire")
        if self.acquires == self.fail_acquire:
            return self.acquire_code
        value = self.read(slot, 0)
        if value is None or isinstance(value, int):
            return 0
        assert value.alive, "lease acquired from a stale root"
        value.leases += 1
        return 1

    def release(self, slot, acquired):
        if acquired:
            value = self.read(slot, 0)
            assert value.alive and value.leases > 0
            value.leases -= 1
            self.releases.append(value.name)
        self.gc("release")
        return -3 if self.fail_release else 0

    def encode_names(self, value, encoding, errors):
        for obj in (value, encoding, errors):
            assert obj.alive and obj.leases > 0, "raw codec input lacks a counted lease"
            self.cached.append(obj)
            # Simulate an overlapping/nested operation balancing pin/unpin.
            obj.flags &= ~abi.PY_FLAG_GC_PINNED
        self.gc("encode")
        assert all(obj.alive for obj in self.cached), "cached UTF8 pointer moved"
        if self.pending:
            self.error = (59, "unicode")
            return None
        result = self.make("result", abi.PY_TYPE_BYTES)
        return result

    def encode_legacy(self, value, _codec, _mode):
        assert value.alive and value.leases > 0
        self.cached.append(value)
        value.flags &= ~abi.PY_FLAG_GC_PINNED
        self.gc("encode")
        assert value.alive
        return self.make("result", abi.PY_TYPE_BYTES)

    def encode_call(self, symbol="py_str_encode_with_encoding"):
        if symbol == "py_str_encode_with_encoding":
            return self.namespace[symbol](self.value, self.order, self.signed)
        return self.namespace[symbol](self.value)

    def assert_closed(self):
        assert not self.frames and not self.handles
        assert self.depth == self.pins == 0
        for value in self.objects:
            if value.alive:
                assert value.leases == 0, value.name
                assert value.references == 1, value.name


@pytest.mark.parametrize("phase", ("frame_enter", "lock", "acquire"))
@pytest.mark.parametrize("backend", (3, 4))
def test_direct_entry_reloads_before_leasing(phase, backend):
    memory = _EncoderMemory(phase=phase, backend=backend)
    result = memory.encode_call()
    assert result is memory.latest["result"] and result.alive
    assert memory.error is None and memory.moves > 0
    memory.assert_closed()


def test_direct_entry_frame_prepublication_can_park():
    # This remains a required red until shared entry publication is reviewed.
    # A real frame_enter may park before linking slots under graph contention.
    memory = _EncoderMemory(phase="frame_before", backend=3)
    result = memory.encode_call()
    assert result is memory.latest["result"] and result.alive
    memory.assert_closed()


@pytest.mark.parametrize("alias", (False, True))
@pytest.mark.parametrize("backend", (3, 4))
def test_counted_leases_survive_alias_unpin(alias, backend):
    memory = _EncoderMemory(phase="encode", alias=alias, backend=backend)
    result = memory.encode_call()
    assert result.alive and memory.error is None
    assert all(value.alive for value in memory.cached)
    assert memory.acquires == 3 and len(memory.releases) == 3
    memory.assert_closed()


@pytest.mark.parametrize("failure", (1, 2, 3))
@pytest.mark.parametrize("code", (-1, -2))
def test_partial_lease_failure_balances_roots(failure, code):
    memory = _EncoderMemory(fail_acquire=failure, acquire_code=code)
    assert memory.encode_call() is None
    assert memory.error[0] == (15 if code == -2 else 7)
    assert len(memory.releases) == failure - 1
    memory.assert_closed()


def test_pending_error_survives_lease_cleanup():
    memory = _EncoderMemory(pending="unicode", fail_release=True, phase="release")
    assert memory.encode_call() is None
    assert memory.error == (59, "unicode")
    memory.assert_closed()
    memory = _EncoderMemory(fail_release=True, phase="close")
    assert memory.encode_call() is None
    assert memory.error[0] == 7
    assert not memory.latest["result"].alive
    memory.assert_closed()


@pytest.mark.parametrize("phase", ("release", "close", "frame_leave"))
@pytest.mark.parametrize("backend", (3, 4))
def test_result_survives_owner_teardown(phase, backend):
    memory = _EncoderMemory(phase=phase, backend=backend)
    result = memory.encode_call()
    assert result is memory.latest["result"] and result.alive
    assert memory.error is None and memory.moves > 0
    memory.assert_closed()


@pytest.mark.parametrize("symbol", ("py_str_utf8_encode", "py_str_latin1_encode", "py_str_ascii_encode"))
def test_legacy_exports_use_local_lifetime_guard(symbol):
    memory = _EncoderMemory(phase="encode")
    result = memory.encode_call(symbol)
    assert result.alive and memory.error is None
    assert memory.acquires == 3 and len(memory.releases) == 1
    memory.assert_closed()


def test_encoder_binders_capture_results_before_releasing_leases(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source = tmp_path / "encode_leases.py"
    source.write_text('''
def method(value: str, encoding, errors):
    return value.encode(encoding, errors)
def immutable(value: str):
    return bytes(value, "utf8")
def mutable(value: str):
    return bytearray(value, "latin1")
''')
    output = tmp_path / "encode_leases.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    for name, count in (("method", 3), ("immutable", 1), ("mutable", 2)):
        match = re.search(r"^define[^\n]*@user_encode_leases_" + name +
                          r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
        assert match, name
        body = match[1]
        assert "strict.nolib.stub" not in body and "@py_cpy_" not in body
        assert body.count("@pcc_gc_foreign_lease_acquire(") == count
        assert "@pcc_gc_foreign_lease_release(" in body
        assert "@pcc_gc_take_pinned_slot(" in body
        assert "encode.lease.cleanup.error" not in body


_GC3_SOURCE = r'''
import gc
from pcc.extern import c_abi_export, c_obj
from pcc.unsafe import ptr_to_int, load_i32, store_i32

@c_abi_export("encode_gc_address")
def address(value: c_obj) -> int:
    return ptr_to_int(value)

@c_abi_export("encode_gc_flags")
def flags(value: c_obj) -> int:
    return load_i32(value, 12)

@c_abi_export("encode_gc_legacy_pin")
def legacy_pin(value: c_obj, enabled: int) -> None:
    old = load_i32(value, 12)
    store_i32(value, 12, (old | 64) if enabled else (old & ~64))

def fresh(value):
    return ("@" + value)[1:]

def move(holder, result):
    assert flags(holder[0]) & 4096, "source is not nursery-backed"
    before = address(holder[0])
    alias = [holder[0]]
    legacy_pin(alias[0], 0)
    gc.collect()
    assert address(holder[0]) != before, "GC3 nursery movement was not exercised"
    return result

finished = 0
class InvalidError:
    def __init__(self, holder):
        self.holder = holder
    def __del__(self):
        global finished
        alias = [self.holder[0]]
        legacy_pin(alias[0], 0)
        gc.collect()
        assert self.holder[0] == "café"
        finished += 1

def moving_failure(holder):
    move(holder, "unused")
    raise ValueError("later argument")

def main():
    holder = [fresh("control")]
    move(holder, "unused")
    holder = [fresh("café")]
    assert holder[0].encode(encoding=move(holder, "ascii"), errors="replace") == b"caf?"
    codec = [fresh("ascii")]
    assert "café".encode(encoding=codec[0], errors=move(codec, "replace")) == b"caf?"
    errors = [fresh("replace")]
    assert "café".encode(errors=errors[0], encoding=move(errors, "ascii")) == b"caf?"
    alias = [fresh("utf8")]
    legacy_pin(alias[0], 1)
    assert alias[0].encode(encoding=alias[0], errors=move(alias, "strict")) == b"utf8"
    assert flags(alias[0]) & 64, "earliest prior pin was not restored"
    legacy_pin(alias[0], 0)
    for index in range(16):
        holder = [fresh("café")]
        try:
            holder[0].encode(errors=InvalidError(holder))
        except TypeError:
            pass
        else:
            raise AssertionError("missing encoding argument error")
        assert holder[0] == "café"
        holder = [fresh("café")]
        try:
            holder[0].encode(encoding=moving_failure(holder), errors="replace")
        except ValueError:
            pass
        else:
            raise AssertionError("missing later-argument failure")
        assert holder[0] == "café"
    gc.collect()
    assert finished == 16, "temporary error operands were not released once"
    print("encoder nursery movement and finalizers", finished)
main()
'''


@pytest.mark.integration
def test_native_gc3_encoder_movement_and_finalizers(
    tmp_path, pcc_diagnostic_runtime_archive, python_program_compiler, monkeypatch,
):
    import os
    import subprocess
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "encoder_gc3.py"
    source.write_text(_GC3_SOURCE)
    binary = tmp_path / "encoder_gc3"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PCC_GC_BACKEND="3", PCC_GC_MINOR_ALLOC_MAX="4096",
                 PATH="", PCC_HOST_PYTHON="/nonexistent/host-python"))
    (tmp_path / "gc3.stdout").write_text(result.stdout)
    (tmp_path / "gc3.stderr").write_text(result.stderr)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == "encoder nursery movement and finalizers 16\n"


_DIRECT_IR = r'''
@encoder_direct_map = internal constant i32 2
@encoder_direct_text = internal constant [5 x i8] c"caf\C3\A9"
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @pcc_gc_store_root(ptr, ptr)
declare ptr @pcc_gc_load_ptr(ptr, ptr)
declare ptr @py_str_new(ptr, i64)
declare ptr @py_str_utf8_encode(ptr)
declare i64 @py_bytes_len(ptr)
declare i64 @py_err_occurred()

define i64 @test_encoder_direct_entry() {
entry:
  %slots = alloca [2 x ptr]
  %source = getelementptr [2 x ptr], ptr %slots, i64 0, i64 0
  %result = getelementptr [2 x ptr], ptr %slots, i64 0, i64 1
  store ptr null, ptr %source
  store ptr null, ptr %result
  call void @pcc_gc_frame_enter(ptr @encoder_direct_map, ptr %slots)
  br label %loop
loop:
  %index = phi i64 [0, %entry], [%next, %again]
  %string = call ptr @py_str_new(ptr @encoder_direct_text, i64 5)
  store ptr %string, ptr %source
  ; There is deliberately no counted caller lease. The runtime export must
  ; publish local roots and lease its own raw view before allocation.
  %encoded = call ptr @py_str_utf8_encode(ptr %string)
  store ptr %encoded, ptr %result
  %error = call i64 @py_err_occurred()
  %ok = icmp eq i64 %error, 0
  br i1 %ok, label %check, label %bad
check:
  %current = call ptr @pcc_gc_load_ptr(ptr null, ptr %result)
  %length = call i64 @py_bytes_len(ptr %current)
  %lenok = icmp eq i64 %length, 5
  br i1 %lenok, label %bytes, label %bad
bytes:
  %stable = call ptr @pcc_gc_load_ptr(ptr null, ptr %result)
  %data = getelementptr i8, ptr %stable, i64 24
  %a = load i8, ptr %data
  %lastp = getelementptr i8, ptr %data, i64 4
  %last = load i8, ptr %lastp
  %aok = icmp eq i8 %a, 99
  %lastok = icmp eq i8 %last, -87
  %bytesok = and i1 %aok, %lastok
  br i1 %bytesok, label %again, label %bad
again:
  call void @pcc_gc_store_root(ptr %result, ptr null)
  call void @pcc_gc_store_root(ptr %source, ptr null)
  %next = add i64 %index, 1
  %done = icmp eq i64 %next, 2000
  br i1 %done, label %finish, label %loop
finish:
  call void @pcc_gc_frame_leave(ptr %slots)
  ret i64 0
bad:
  call void @pcc_gc_store_root(ptr %result, ptr null)
  call void @pcc_gc_store_root(ptr %source, ptr null)
  call void @pcc_gc_frame_leave(ptr %slots)
  ret i64 -1
}
'''


@pytest.mark.integration
def test_native_legacy_encoder_direct_entry(tmp_path, pcc_diagnostic_runtime_archive):
    import os
    import subprocess
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_targets import host_target_triple
    target = host_target_triple()
    helper = tmp_path / "encoder_direct.o"
    helper.write_bytes(emit_owned_object('target triple = "' + target + '"\n' + _DIRECT_IR, target))
    source = tmp_path / "encoder_direct.py"
    source.write_text('''from pcc.extern import extern, c_int64
probe = extern("test_encoder_direct_entry", (), c_int64)
def main():
    print(probe())
main()
''')
    binary = tmp_path / "encoder_direct"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_diagnostic_runtime_archive), link_args=(str(helper),))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_MINOR_ALLOC_MAX="4096",
                     PATH="", PCC_HOST_PYTHON="/nonexistent/host-python"))
        assert result.returncode == 0 and result.stdout == "0\n", (backend, result.stdout, result.stderr)


def test_deferred_native_controls_have_verified_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.owned_ir_validation import verify_ir_text
    source = tmp_path / "encoder_movement_control.py"
    source.write_text(_GC3_SOURCE)
    output = tmp_path / "encoder_movement_control.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)
    verify_ir_text('target triple = "' + host_target_triple() + '"\n' + _DIRECT_IR)
    assert "@pcc_gc_foreign_lease_acquire" not in _DIRECT_IR
