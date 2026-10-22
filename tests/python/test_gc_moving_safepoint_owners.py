"""Moving-GC owners at generator, iterator and runtime callback boundaries.

IR checks do not qualify execution. Integration cases require the rebuilt
owned runtime and execute the same native program under all five collectors.
"""

import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python


SHAPES = '''
def values(xs: list):
    yield xs
    raise ValueError("generator exit")

def scan_dict(d: dict) -> int:
    total = 0
    try:
        for key, value in d.items():
            d = {}
            total += value
    except ValueError:
        return -1
    return total

class Cursor:
    def __init__(self):
        self.index = 0
    def __iter__(self) -> "Cursor":
        return self
    def __next__(self) -> int:
        if self.index == 3:
            raise StopIteration()
        self.index += 1
        return self.index

def scan_cursor() -> int:
    total = 0
    try:
        for value in Cursor():
            total += value
            if value == 2:
                raise ValueError("body exit")
    except ValueError:
        return total
    return -1
'''


def _owner_slot_released(body, owner):
    """The owner's root slot is cleared, directly or via an identity cast."""
    slots = set(re.findall(r"(%" + re.escape(owner) + r"[-\w.]*\.for\.obj\.addr[-\w.]*)", body))
    names = slots | {
        alias for alias, slot in re.findall(r"(%[-\w.]+) = bitcast ptr (%[-\w.]+) to ptr", body)
        if slot in slots
    }
    return any(
        re.search(r"@pcc_gc_store_root\(ptr " + re.escape(name) + r", ptr null\)", body)
        for name in names
    )


def _body(text, symbol):
    match = re.search(r"^define[^\n]*@" + re.escape(symbol) + r"\([^\n]*\) [^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, symbol
    return match[1]


@pytest.fixture
def shapes_ir(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "moving_owners.py"
    output = tmp_path / "moving_owners.ll"
    source.write_text(SHAPES)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   python_library=True, backend="self", libpython_mode="off",
                   ir_scaffold_mode="on")
    return output.read_text()


def test_generator_wrapper_roots_inputs_before_poll_and_reloads_them(shapes_ir):
    body = _body(shapes_ir, "user_moving_owners_values")
    assert body.index("@pcc_gc_frame_enter_lifo") < body.index("@pcc_thread_safepoint")
    assert "gen.arguments.borrowed" in body
    assert re.search(r"%gen.argument.current[^\n]* = load ptr, ptr %gen.argument.slot", body)
    assert body.count("@pcc_gc_frame_enter_lifo") == body.count("@pcc_gc_frame_leave_lifo")
    assert "@pcc_gc_take_pinned_slot" in body


def test_generator_resume_roots_both_owners_and_uses_root_derived_ssa(shapes_ir):
    body = _body(shapes_ir, "user_moving_owners_values__gen_resume")
    assert ".pcc.gc.frame.map.borrowed.2" in shapes_ir
    assert "gen.owner.borrowed.slots" in body
    assert body.index("@pcc_gc_frame_enter") < body.index("@pcc_thread_safepoint")
    assert re.search(r"%gen.borrowed.current[^\n]* = load ptr, ptr %gen.borrowed.slot", body)
    assert re.search(r"@py_gen_state\(ptr %gen.borrowed.current", body)
    assert "@py_gen_state(ptr %gen)" not in body


def test_dict_items_owns_source_independently_and_reloads_before_both_reads(shapes_ir):
    body = _body(shapes_ir, "user_moving_owners_scan_dict")
    assert "for.dict.items.source" in body
    assert body.count("for.dict.items.source.current") >= 4
    assert re.search(r"@py_dict_entry_key_at\(ptr %for.dict.items.source.current", body)
    assert re.search(r"@py_dict_entry_value_at\(ptr %for.dict.items.source.current", body)
    assert "for.dict.items.error" in body
    assert _owner_slot_released(body, "for.dict.items.source")


def test_custom_iterator_has_owned_root_reload_and_body_error_cleanup(shapes_ir):
    body = _body(shapes_ir, "user_moving_owners_scan_cursor")
    assert "for.iter.owner" in body
    assert "for.iter.owner.error" in body
    assert re.search(r"%for.iter.cur[^\n]* = call ptr (?:\([^\n)]*\) )?@pcc_gc_load_ptr", body)
    assert re.search(r"@user_moving_owners_Cursor___next__\(ptr %for.iter.cur", body)
    assert _owner_slot_released(body, "for.iter.owner")


@pytest.mark.parametrize("module,symbol,callback", [
    ("py_dunder", "py_user_del_dispatch", "@user_py_dunder__call_user_unary_method_void"),
    ("py_exc_traceback", "user_py_exc_traceback__write_user_exception_heading", "@py_obj_str"),
    ("py_exc_traceback", "user_py_exc_traceback__tb_append_user_exc_heading", "@py_obj_str"),
])
def test_runtime_saved_exception_stays_pinned_across_callback(tmp_path, monkeypatch, module, symbol, callback):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = Path(__file__).resolve().parents[2] / "pcc/runtime/py" / (module + ".py")
    output = tmp_path / (module + ".ll")
    compile_python(str(source), str(output), emit_llvm_only=True,
                   python_library=True, backend="self", libpython_mode="off",
                   ir_scaffold_mode="on")
    body = _body(output.read_text(), symbol)
    pin = body.index("@pcc_gc_pin")
    call = body.index(callback)
    unpin = body.index("@pcc_gc_unpin")
    assert pin < call < unpin
    assert body.index("@py_tls_exc_set", call) < unpin
    assert "atomicrmw or" in body[unpin:]


NATIVE_PROGRAM = '''
import weakref
from pcc.extern import c_int64, extern
from pcc.unsafe import load_i32
step = extern("pcc_gc_step", (c_int64,), c_int64)
metric = extern("pcc_gc_telemetry", (c_int64,), c_int64)
events = []
keeper = []
watched = ValueError("keep")

def drive():
    index = 0
    while index < 8:
        step(64)
        index += 1

def values(value):
    drive()
    yield value
    drive()
    yield value

class Cursor:
    def __init__(self):
        self.index = 0
    def __iter__(self) -> "Cursor":
        if self.index == -1:
            raise ValueError("iterator entry")
        return self
    def __next__(self) -> int:
        if self.index == 3:
            raise StopIteration()
        self.index += 1
        return self.index
    def __del__(self):
        events.append(1)

class Victim:
    def __del__(self):
        assert load_i32(watched, 12) & 64
        drive()
        events.append(2)
        keeper.append(self)

def fail():
    victim = Victim()
    raise watched

def scan():
    total = 0
    for value in Cursor():
        drive()
        total += value
    assert events == [1]
    for key, value in {"a": [1], "b": [2]}.items():
        drive()
        total += value[0]
    source = {"a": [3], "b": [4]}
    for key, value in source.items():
        source = {}
        drive()
        total += value[0]
    return total

def rejecting_cursor():
    cursor = Cursor()
    cursor.index = -1
    return cursor

def body_exit():
    drive()
    raise ValueError("body exit")

def return_from_loops():
    for outer in Cursor():
        for key, value in {"a": [19]}.items():
            for inner in Cursor():
                drive()
                return value[0]
    return -1

def exercise_loop_exits():
    before = len(events)
    try:
        for value in rejecting_cursor():
            assert False
    except ValueError:
        pass
    assert len(events) == before + 1
    try:
        for outer in Cursor():
            for key, value in {"a": [1]}.items():
                body_exit()
    except ValueError:
        pass
    assert len(events) == before + 2
    try:
        for key, value in {"a": [1]}.items():
            for inner in Cursor():
                drive()
                raise ValueError("explicit body exit")
    except ValueError:
        pass
    assert len(events) == before + 3
    for outer in Cursor():
        for inner in Cursor():
            drive()
            break
        break
    assert len(events) == before + 5
    assert return_from_loops() == 19
    assert len(events) == before + 7
    for value in Cursor():
        try:
            body_exit()
        except ValueError:
            continue
    assert len(events) == before + 8

def main():
    held = [17]
    alias = held
    identity = id(held)
    iterator = values(held)
    drive()
    assert next(iterator) is held
    drive()
    assert next(iterator) is alias
    assert id(held) == identity
    assert held == [17]
    assert scan() == 16
    before = load_i32(watched, 12) & 64
    try:
        fail()
    except ValueError as error:
        assert error is watched
        assert str(error) == "keep"
    assert load_i32(watched, 12) & 64 == before
    assert events == [1, 2]
    assert len(keeper) == 1
    exercise_loop_exits()
    print("moving-owners-ok", metric(45))
main()
'''


@pytest.mark.parametrize("threads", ["0", "1"])
@pytest.mark.parametrize("passes", ["default", "off"])
def test_moving_loop_exits_reach_owned_object_emitter(tmp_path, monkeypatch, threads, passes):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline_targets import host_target_triple

    monkeypatch.setenv("PCC_WITH_THREADS", threads)
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", passes)
    source = tmp_path / "moving.py"
    output = tmp_path / "moving.ll"
    source.write_text(NATIVE_PROGRAM)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   python_library=True, backend="self", libpython_mode="off",
                   ir_scaffold_mode="on")
    ir_text = output.read_text()
    body = _body(ir_text, "user_moving_exercise_loop_exits")
    assert "for.iter.source.error" in body
    assert "@user_moving_body_exit" in body
    assert "for.dict.items.error" in body
    # Execute the precise root-state analysis and actual encoder, rather than
    # accepting an IR fixture whose exceptional joins have never been checked.
    data = emit_owned_object(ir_text, host_target_triple())
    assert len(data) > 0
    (tmp_path / "moving.o").write_bytes(data)


@pytest.mark.integration
def test_native_moving_generator_iterator_and_finalizer_owners(tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "moving_owner_execution.py"
    binary = tmp_path / "moving_owner_execution"
    source.write_text(NATIVE_PROGRAM)
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", ir_scaffold_mode="on",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        run = subprocess.run([str(binary)], capture_output=True, text=True,
                             timeout=30, env=environment)
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        assert run.stderr == "", (backend, run.stdout, run.stderr)
        marker, copied = run.stdout.strip().split()
        assert marker == "moving-owners-ok"
        if backend == 4:
            assert int(copied) > 0
