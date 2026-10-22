"""Prepared threaded index controls; require a centrally matched archive.

The direct slot control forces actual heap-integer movement before worker
entry on GC3/4. GC0/1/2 still start/join a real runtime thread. It does not
claim deterministic movement *inside* the compiled entry poll: the model and
inspected threaded provider IR establish that separate ordering property.
The Python control exercises callbacks/descriptors with a collector thread;
its enclosing container/producer paths remain wider qualification boundaries.
"""
import contextlib
import io
import os
import subprocess

import pytest

from test_foreign_address_leases import MOVEMENT_IR


INDEX_IR = MOVEMENT_IR + r'''
@index_map = internal constant i32 2
@index_text = internal constant [20 x i8] c"4611686018427387941\00"
@index_phase = internal global i64 0
@index_status = internal global i64 0
@index_witness = internal global i64 0
declare ptr @py_int_from_cstr(ptr, i32)
declare i64 @py_obj_index_slots(ptr, ptr)
declare i64 @py_obj_index_i64_slots(ptr)
declare i64 @py_index_i64_checked_slots(ptr)
declare i64 @py_err_occurred()
declare void @pcc_platform_abort()
declare void @pcc_gc_note_slot_write_barrier(ptr, ptr, ptr)
declare ptr @pcc_gc_resolve_root_slot_unlocked(ptr, i64)

define void @index_require(i1 %ok) {
entry:
  br i1 %ok, label %good, label %bad
bad:
  call void @pcc_platform_abort()
  unreachable
good:
  ret void
}

define ptr @index_thread(ptr %roots) {
entry:
  %output = getelementptr ptr, ptr %roots, i64 1
  store atomic i64 1, ptr @index_phase release, align 8
  br label %await_capture
await_capture:
  call void @pcc_thread_safepoint()
  %capture_flag = load atomic i64, ptr @index_phase acquire, align 8
  %capture = icmp eq i64 %capture_flag, 2
  br i1 %capture, label %capture_old, label %await_capture
capture_old:
  ; Witness is an untraced integer, never a managed operand or dereference.
  %old = load ptr, ptr %roots
  %old_bits = ptrtoint ptr %old to i64
  store atomic i64 %old_bits, ptr @index_witness release, align 8
  store atomic i64 3, ptr @index_phase release, align 8
  br label %await_entry
await_entry:
  call void @pcc_thread_safepoint()
  %entry_flag = load atomic i64, ptr @index_phase acquire, align 8
  %go = icmp eq i64 %entry_flag, 4
  br i1 %go, label %call_index, label %await_entry
call_index:
  ; Both addresses belong to the controller's already registered frame.
  ; No raw object value is passed across any compiled runtime entry poll.
  %status = call i64 @py_obj_index_slots(ptr %roots, ptr %output)
  %status_ok = icmp eq i64 %status, 0
  call void @index_require(i1 %status_ok)
  %container = call i64 @py_obj_index_i64_slots(ptr %output)
  %container_ok = icmp eq i64 %container, 4611686018427387941
  call void @index_require(i1 %container_ok)
  %checked = call i64 @py_index_i64_checked_slots(ptr %roots)
  %checked_ok = icmp eq i64 %checked, 4611686018427387941
  call void @index_require(i1 %checked_ok)
  %error = call i64 @py_err_occurred()
  store atomic i64 %error, ptr @index_status release, align 8
  store atomic i64 5, ptr @index_phase release, align 8
  ret ptr null
}

define i64 @test_threaded_index_slots() {
entry:
  %roots = alloca [2 x ptr]
  %source = getelementptr [2 x ptr], ptr %roots, i64 0, i64 0
  %output = getelementptr [2 x ptr], ptr %roots, i64 0, i64 1
  %handle = alloca ptr
  %joined_value = alloca ptr
  store ptr null, ptr %source
  store ptr null, ptr %output
  store ptr null, ptr %handle
  store ptr null, ptr %joined_value
  call void @pcc_gc_frame_enter_lifo(ptr @index_map, ptr %roots)
  %backend = call i64 @pcc_gc_backend()
  %threads = call i64 @pcc_threads_enabled()
  %threaded = icmp ne i64 %threads, 0
  call void @index_require(i1 %threaded)
  store atomic i64 0, ptr @index_phase release, align 8
  %started = call i64 @pcc_thread_start(ptr %handle, ptr @index_thread, ptr %roots)
  %start_ok = icmp eq i64 %started, 0
  call void @index_require(i1 %start_ok)
  br label %await_ready
await_ready:
  call void @pcc_thread_safepoint()
  %ready_flag = load atomic i64, ptr @index_phase acquire, align 8
  %ready = icmp eq i64 %ready_flag, 1
  br i1 %ready, label %new_source, label %await_ready
new_source:
  %fresh = call ptr @py_int_from_cstr(ptr @index_text, i32 10)
  ; Immediate NEW publication, before null check, registration or any poll.
  store ptr %fresh, ptr %source
  %token = call i64 @pcc_gc_foreign_lease_acquire(ptr %source)
  %token_ok = icmp sge i64 %token, 0
  call void @index_require(i1 %token_ok)
  %current = load ptr, ptr %source
  call void @pcc_gc_note_slot_write_barrier(ptr null, ptr %source, ptr %current)
  %released = call i64 @pcc_gc_foreign_lease_release(ptr %source, i64 %token)
  %release_ok = icmp eq i64 %released, 0
  call void @index_require(i1 %release_ok)
  store atomic i64 2, ptr @index_phase release, align 8
  br label %await_captured
await_captured:
  call void @pcc_thread_safepoint()
  %captured_flag = load atomic i64, ptr @index_phase acquire, align 8
  %captured = icmp eq i64 %captured_flag, 3
  br i1 %captured, label %choose_movement, label %await_captured
choose_movement:
  %is3 = icmp eq i64 %backend, 3
  %is4 = icmp eq i64 %backend, 4
  %moving = or i1 %is3, %is4
  br i1 %moving, label %move_source, label %collect_source
move_source:
  %old_bits = load atomic i64, ptr @index_witness acquire, align 8
  %moved = call ptr @lease_try_move(ptr %source, i64 %backend)
  %moved_bits = ptrtoint ptr %moved to i64
  %moved_nonnull = icmp ne ptr %moved, null
  %changed = icmp ne i64 %moved_bits, %old_bits
  %real_move = and i1 %moved_nonnull, %changed
  call void @index_require(i1 %real_move)
  call void @pcc_py_gc_minor_graph_lock()
  %healed = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %source, i64 0)
  %is_healed = icmp eq ptr %healed, %moved
  call void @index_require(i1 %is_healed)
  call void @pcc_py_gc_minor_graph_unlock()
  br label %enter_worker
collect_source:
  %collection = call i64 @pcc_gc_collect(i32 1)
  br label %enter_worker
enter_worker:
  store atomic i64 4, ptr @index_phase release, align 8
  br label %await_done
await_done:
  call void @pcc_thread_safepoint()
  %done_flag = load atomic i64, ptr @index_phase acquire, align 8
  %done = icmp eq i64 %done_flag, 5
  br i1 %done, label %join, label %await_done
join:
  %thread = load ptr, ptr %handle
  %joined = call i64 @pcc_thread_join(ptr %thread, ptr %joined_value)
  %join_ok = icmp eq i64 %joined, 0
  call void @index_require(i1 %join_ok)
  %error = load atomic i64, ptr @index_status acquire, align 8
  %error_ok = icmp eq i64 %error, 0
  call void @index_require(i1 %error_ok)
  call void @pcc_gc_store_root(ptr %output, ptr null)
  call void @pcc_gc_store_root(ptr %source, ptr null)
  call void @pcc_gc_frame_leave_lifo(ptr %roots)
  ret i64 0
}
'''


PROGRAM = '''import gc
import threading
events = []
class Index:
    def __index__(self):
        gc.collect()
        events.append("index")
        return 1
class Bound:
    def __call__(self):
        gc.collect()
        events.append("call")
        return 2
class Descriptor:
    def __get__(self, obj, owner):
        gc.collect()
        events.append("get")
        return Bound()
class Described:
    __index__ = Descriptor()
class Huge:
    def __index__(self):
        gc.collect()
        return 1237940039285380274899124224
class Raises:
    def __index__(self):
        gc.collect()
        raise OverflowError("callback overflow")
def pick(index):
    values = [10, 20, 30]
    return values[index]
def collect():
    for _ in range(8):
        gc.collect()
def main():
    worker = threading.Thread(target=collect)
    worker.start()
    assert pick(Index()) == 20
    assert pick(Described()) == 30
    caught = False
    try:
        pick(Huge())
    except IndexError:
        caught = True
    assert caught
    caught = False
    try:
        pick(Raises())
    except OverflowError as error:
        caught = str(error) == "callback overflow"
    assert caught
    worker.join()
    assert events == ["index", "get", "call"]
    print("ROOTED_INDEX_CALLBACKS_OK")
main()
'''


def test_threaded_index_probe_owned_ir():
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.owned_ir_validation import verify_ir_text
    verify_ir_text('target triple = "' + host_target_triple() + '"\n' + INDEX_IR)


def test_rooted_index_callbacks_reference():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAM, {})
    assert output.getvalue() == "ROOTED_INDEX_CALLBACKS_OK\n"


@pytest.mark.integration
@pytest.mark.parametrize("control", ["direct_slots", "callbacks"])
def test_rooted_index_native_gc0_to_gc4(tmp_path, monkeypatch, threaded_pcc_runtime_archive, control):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_targets import host_target_triple

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    source = tmp_path / (control + ".py")
    link_args = ()
    expected = "ROOTED_INDEX_CALLBACKS_OK\n"
    if control == "direct_slots":
        target = host_target_triple()
        helper = tmp_path / "index_slots.o"
        helper.write_bytes(emit_owned_object('target triple = "' + target + '"\n' + INDEX_IR, target))
        link_args = (str(helper),)
        source.write_text('''from pcc.extern import extern, c_int64
probe = extern("test_threaded_index_slots", (), c_int64)
def main():
    assert probe() == 0
    print("ROOTED_INDEX_ENTRY_OK")
main()
''')
        expected = "ROOTED_INDEX_ENTRY_OK\n"
    else:
        source.write_text(PROGRAM)
    output = source.with_suffix("")
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(threaded_pcc_runtime_archive),
                   link_args=link_args)
    for backend in range(5):
        environment = dict(os.environ, PCC_WITH_THREADS="1", PCC_GC_BACKEND=str(backend),
                           PCC_GC_MINOR_ALLOC_MAX="4096", PATH="",
                           PCC_HOST_PYTHON="/nonexistent/host-python", PCC_HOST_PCC="/nonexistent/host-pcc")
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True, text=True, timeout=60)
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, (control, backend, result.returncode, result.stdout, result.stderr)
        assert result.stdout == expected and result.stderr == ""
