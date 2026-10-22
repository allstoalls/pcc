"""Direct native exercise of authoritative root-copy entry, not the whole ABI.

The unpinned young control must actually move. Independent counted aliases
must reject that same movement before release. GC3 promotion is scheduled
inside the contended handoff; GC4 relocation is prepared outside the graph
lock and its forwarded owning source is healed in the contended handoff.

No stop-the-world wait or allocating GC4 copy runs under a caller-held graph
lock. The worker records a deliberately untraced OLD address as an integer;
the production copy receives the authoritative slot address, never that raw
copy. This complements, rather than replaces, native special-call semantics
and returned-owner/finalizer tests.

The pre-call attempt announcement does not prove the worker has reached the
contended production lock. This native schedule alone therefore cannot reject
every preload-before-lock mutant; the deterministic production-body model
covers that ordering until a real blocked-entry witness is added.
"""
import os
import subprocess

import pytest

from test_foreign_address_leases import MOVEMENT_IR


HANDOFF_IR = MOVEMENT_IR + r'''
@handoff_map = internal constant i32 3
@handoff_ready = internal global i64 0
@handoff_capture_go = internal global i64 0
@handoff_captured = internal global i64 0
@handoff_copy_go = internal global i64 0
@handoff_attempt = internal global i64 0
@handoff_done = internal global i64 0
@handoff_release = internal global i64 0
@handoff_witness = internal global i64 0
@handoff_seen = internal global i64 0
@handoff_token = internal global i64 0
@handoff_status = internal global i64 0
@pcc_gc_foreign_lease_active = external global i64
declare void @pcc_platform_abort()
declare void @pcc_gc_note_slot_write_barrier(ptr, ptr, ptr)
declare ptr @pcc_gc_resolve_root_slot_unlocked(ptr, i64)
declare void @pcc_gc_generational_promote_owned_slot_mode(ptr, i64, i64)
declare i64 @pcc_gc_root_copy_lease(ptr, ptr)
declare i64 @pcc_gc_root_move(ptr, ptr)

define void @handoff_require(i1 %ok) {
entry:
  br i1 %ok, label %good, label %bad
bad:
  call void @pcc_platform_abort()
  unreachable
good:
  ret void
}

define i64 @handoff_active_count() {
entry:
  call void @pcc_py_gc_minor_graph_lock()
  %count = load i64, ptr @pcc_gc_foreign_lease_active
  call void @pcc_py_gc_minor_graph_unlock()
  ret i64 %count
}

define i64 @handoff_address_pinned(ptr %slot) {
entry:
  call void @pcc_py_gc_minor_graph_lock()
  %current = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %slot, i64 0)
  %pinned = call i64 @pcc_gc_object_is_address_pinned(ptr %current)
  call void @pcc_py_gc_minor_graph_unlock()
  ret i64 %pinned
}

define void @handoff_new_young(ptr %slot, i64 %backend) {
entry:
  %is3 = icmp eq i64 %backend, 3
  br i1 %is3, label %string, label %list
string:
  %text = call ptr @py_str_new(ptr @lease_text, i64 6)
  br label %publish
list:
  %items = call ptr @py_list_new(i64 0)
  br label %publish
publish:
  %fresh = phi ptr [%text, %string], [%items, %list]
  ; Slot was registered empty before the allocating call. This first store
  ; transfers its owned result without an intervening call or poll.
  store ptr %fresh, ptr %slot
  %nonnull = icmp ne ptr %fresh, null
  call void @handoff_require(i1 %nonnull)
  call void @pcc_py_gc_minor_graph_lock()
  %current = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %slot, i64 0)
  call void @pcc_gc_note_slot_write_barrier(ptr null, ptr %slot, ptr %current)
  %flag_slot = getelementptr i8, ptr %current, i64 12
  %flags = load i32, ptr %flag_slot
  %young_bits = and i32 %flags, 128
  %young = icmp ne i32 %young_bits, 0
  %arena_mask = select i1 %is3, i32 4096, i32 65536
  %arena_bits = and i32 %flags, %arena_mask
  %arena = icmp ne i32 %arena_bits, 0
  %unpinned = call i64 @pcc_gc_object_is_address_pinned(ptr %current)
  %not_pinned = icmp eq i64 %unpinned, 0
  %young_arena = and i1 %young, %arena
  %valid = and i1 %young_arena, %not_pinned
  call void @handoff_require(i1 %valid)
  call void @pcc_py_gc_minor_graph_unlock()
  ret void
}

define ptr @handoff_copy_thread(ptr %context) {
entry:
  %source_address = getelementptr ptr, ptr %context, i64 0
  %destination_address = getelementptr ptr, ptr %context, i64 1
  %source = load ptr, ptr %source_address
  %destination = load ptr, ptr %destination_address
  store atomic i64 1, ptr @handoff_ready release, align 8
  br label %await_capture
await_capture:
  call void @pcc_thread_safepoint()
  %capture_flag = load atomic i64, ptr @handoff_capture_go acquire, align 8
  %capture = icmp ne i64 %capture_flag, 0
  br i1 %capture, label %capture_old, label %await_capture
capture_old:
  ; The controller cannot move the source until captured is published.
  ; This witness is NEVER passed to a managed API or dereferenced later.
  %old = load ptr, ptr %source
  %old_bits = ptrtoint ptr %old to i64
  store atomic i64 %old_bits, ptr @handoff_witness release, align 8
  store atomic i64 1, ptr @handoff_captured release, align 8
  br label %await_copy
await_copy:
  call void @pcc_thread_safepoint()
  %copy_flag = load atomic i64, ptr @handoff_copy_go acquire, align 8
  %copy = icmp ne i64 %copy_flag, 0
  br i1 %copy, label %copy_entry, label %await_copy
copy_entry:
  ; The controller owns graph lock before granting copy_go. This is the
  ; same contention handshake used by the foreign-address lease probe, but
  ; does not prove the callee has actually reached its lock before promotion.
  store atomic i64 1, ptr @handoff_attempt release, align 8
  %token = call i64 @pcc_gc_root_copy_lease(ptr %destination, ptr %source)
  store atomic i64 %token, ptr @handoff_token release, align 8
  %counted = icmp eq i64 %token, 1
  call void @handoff_require(i1 %counted)
  %current = load ptr, ptr %destination
  %current_bits = ptrtoint ptr %current to i64
  store atomic i64 %current_bits, ptr @handoff_seen release, align 8
  store atomic i64 1, ptr @handoff_done release, align 8
  br label %await_release
await_release:
  call void @pcc_thread_safepoint()
  %release_flag = load atomic i64, ptr @handoff_release acquire, align 8
  %release = icmp ne i64 %release_flag, 0
  br i1 %release, label %finish, label %await_release
finish:
  %status = call i64 @pcc_gc_foreign_lease_release(ptr %destination, i64 %token)
  store atomic i64 %status, ptr @handoff_status release, align 8
  ret ptr null
}

define i64 @test_threaded_root_slot_handoff() {
entry:
  %roots = alloca [3 x ptr]
  %source = getelementptr [3 x ptr], ptr %roots, i64 0, i64 0
  %destination = getelementptr [3 x ptr], ptr %roots, i64 0, i64 1
  %output = getelementptr [3 x ptr], ptr %roots, i64 0, i64 2
  %context = alloca [2 x ptr]
  %context_source = getelementptr [2 x ptr], ptr %context, i64 0, i64 0
  %context_destination = getelementptr [2 x ptr], ptr %context, i64 0, i64 1
  %handle = alloca ptr
  %thread_result = alloca ptr
  store ptr null, ptr %source
  store ptr null, ptr %destination
  store ptr null, ptr %output
  store ptr %source, ptr %context_source
  store ptr %destination, ptr %context_destination
  store ptr null, ptr %handle
  store ptr null, ptr %thread_result
  call void @pcc_gc_frame_enter_lifo(ptr @handoff_map, ptr %roots)
  %backend = call i64 @pcc_gc_backend()
  %is3 = icmp eq i64 %backend, 3
  %is4 = icmp eq i64 %backend, 4
  %moving_backend = or i1 %is3, %is4
  call void @handoff_require(i1 %moving_backend)
  %threads = call i64 @pcc_threads_enabled()
  %threaded = icmp ne i64 %threads, 0
  call void @handoff_require(i1 %threaded)
  %baseline = call i64 @handoff_active_count()

  ; A real young/movable control. Both independent leases must survive a
  ; legacy alias pin/unpin, and releasing one cannot release the other.
  call void @handoff_new_young(ptr %source, i64 %backend)
  %one = call i64 @pcc_gc_root_copy_lease(ptr %destination, ptr %source)
  %two = call i64 @pcc_gc_root_copy_lease(ptr %output, ptr %source)
  %one_ok = icmp eq i64 %one, 1
  %two_ok = icmp eq i64 %two, 1
  %both_counted = and i1 %one_ok, %two_ok
  call void @handoff_require(i1 %both_counted)
  %held = load ptr, ptr %destination
  call void @pcc_gc_pin(ptr %held)
  call void @pcc_gc_unpin(ptr %held)
  %blocked_one = call ptr @lease_try_move(ptr %source, i64 %backend)
  %blocked_one_ok = icmp eq ptr %blocked_one, null
  call void @handoff_require(i1 %blocked_one_ok)
  %release_one = call i64 @pcc_gc_foreign_lease_release(ptr %destination, i64 %one)
  %release_one_ok = icmp eq i64 %release_one, 0
  call void @handoff_require(i1 %release_one_ok)
  call void @pcc_gc_store_root(ptr %destination, ptr null)
  %blocked_two = call ptr @lease_try_move(ptr %source, i64 %backend)
  %blocked_two_ok = icmp eq ptr %blocked_two, null
  call void @handoff_require(i1 %blocked_two_ok)
  %moved_owner = call i64 @pcc_gc_root_move(ptr %destination, ptr %output)
  %move_owner_ok = icmp eq i64 %moved_owner, 0
  %cleared_output = load ptr, ptr %output
  %output_empty = icmp eq ptr %cleared_output, null
  %owner_transferred = and i1 %move_owner_ok, %output_empty
  call void @handoff_require(i1 %owner_transferred)
  %release_two = call i64 @pcc_gc_foreign_lease_release(ptr %destination, i64 %two)
  %release_two_ok = icmp eq i64 %release_two, 0
  call void @handoff_require(i1 %release_two_ok)
  call void @pcc_gc_store_root(ptr %destination, ptr null)
  %active_after_control = call i64 @handoff_active_count()
  %control_balanced = icmp eq i64 %active_after_control, %baseline
  call void @handoff_require(i1 %control_balanced)
  %control_pin = call i64 @handoff_address_pinned(ptr %source)
  %control_unpinned = icmp eq i64 %control_pin, 0
  call void @handoff_require(i1 %control_unpinned)
  %control_before = load ptr, ptr %source
  %control_moved = call ptr @lease_try_move(ptr %source, i64 %backend)
  %control_nonnull = icmp ne ptr %control_moved, null
  %control_different = icmp ne ptr %control_moved, %control_before
  %control_real_movement = and i1 %control_nonnull, %control_different
  call void @handoff_require(i1 %control_real_movement)
  call void @pcc_py_gc_minor_graph_lock()
  %control_current = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %source, i64 0)
  %control_healed = icmp eq ptr %control_current, %control_moved
  call void @handoff_require(i1 %control_healed)
  call void @pcc_py_gc_minor_graph_unlock()
  call void @pcc_gc_store_root(ptr %source, ptr null)

  ; Independent entry-handoff phase, with a fresh young source after the
  ; worker is registered. No timing sleeps or raw from-space dereferences.
  store atomic i64 0, ptr @handoff_ready release, align 8
  store atomic i64 0, ptr @handoff_capture_go release, align 8
  store atomic i64 0, ptr @handoff_captured release, align 8
  store atomic i64 0, ptr @handoff_copy_go release, align 8
  store atomic i64 0, ptr @handoff_attempt release, align 8
  store atomic i64 0, ptr @handoff_done release, align 8
  store atomic i64 0, ptr @handoff_release release, align 8
  store atomic i64 -99, ptr @handoff_status release, align 8
  %started = call i64 @pcc_thread_start(ptr %handle, ptr @handoff_copy_thread, ptr %context)
  %started_ok = icmp eq i64 %started, 0
  call void @handoff_require(i1 %started_ok)
  br label %await_ready
await_ready:
  call void @pcc_thread_safepoint()
  %ready_value = load atomic i64, ptr @handoff_ready acquire, align 8
  %ready = icmp ne i64 %ready_value, 0
  br i1 %ready, label %new_source, label %await_ready
new_source:
  call void @handoff_new_young(ptr %source, i64 %backend)
  store atomic i64 1, ptr @handoff_capture_go release, align 8
  br label %await_captured
await_captured:
  call void @pcc_thread_safepoint()
  %captured_value = load atomic i64, ptr @handoff_captured acquire, align 8
  %captured = icmp ne i64 %captured_value, 0
  br i1 %captured, label %select_movement, label %await_captured
select_movement:
  %witness = load atomic i64, ptr @handoff_witness acquire, align 8
  br i1 %is3, label %prepare_gc3, label %prepare_gc4
prepare_gc3:
  br label %hold_entry
prepare_gc4:
  ; GC4 copy can allocate; do it outside any caller-held graph transaction.
  %gc4_moved = call ptr @lease_try_move(ptr %source, i64 %backend)
  %gc4_nonnull = icmp ne ptr %gc4_moved, null
  %gc4_bits = ptrtoint ptr %gc4_moved to i64
  %gc4_changed = icmp ne i64 %gc4_bits, %witness
  %gc4_movement = and i1 %gc4_nonnull, %gc4_changed
  call void @handoff_require(i1 %gc4_movement)
  br label %hold_entry
hold_entry:
  %prepared_gc4 = phi i64 [0, %prepare_gc3], [%gc4_bits, %prepare_gc4]
  call void @pcc_py_gc_minor_graph_lock()
  store atomic i64 1, ptr @handoff_copy_go release, align 8
  br label %await_entry
await_entry:
  %attempt_value = load atomic i64, ptr @handoff_attempt acquire, align 8
  %attempted = icmp ne i64 %attempt_value, 0
  br i1 %attempted, label %publish_current, label %await_entry
publish_current:
  br i1 %is3, label %promote_gc3, label %heal_gc4
promote_gc3:
  call void @pcc_gc_generational_promote_owned_slot_mode(ptr %source, i64 0, i64 0)
  %promoted = load ptr, ptr %source
  %promoted_bits = ptrtoint ptr %promoted to i64
  %gc3_changed = icmp ne i64 %promoted_bits, %witness
  call void @handoff_require(i1 %gc3_changed)
  br label %release_entry
heal_gc4:
  %healed = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %source, i64 0)
  %healed_bits = ptrtoint ptr %healed to i64
  %gc4_healed = icmp eq i64 %healed_bits, %prepared_gc4
  call void @handoff_require(i1 %gc4_healed)
  br label %release_entry
release_entry:
  %expected = phi i64 [%promoted_bits, %promote_gc3], [%healed_bits, %heal_gc4]
  call void @pcc_py_gc_minor_graph_unlock()
  br label %await_done
await_done:
  call void @pcc_thread_safepoint()
  %done_value = load atomic i64, ptr @handoff_done acquire, align 8
  %done = icmp ne i64 %done_value, 0
  br i1 %done, label %check_entry, label %await_done
check_entry:
  %seen = load atomic i64, ptr @handoff_seen acquire, align 8
  %seen_current = icmp eq i64 %seen, %expected
  %seen_not_raw = icmp ne i64 %seen, %witness
  %entry_ok = and i1 %seen_current, %seen_not_raw
  call void @handoff_require(i1 %entry_ok)
  %current_source = load ptr, ptr %source
  %current_source_bits = ptrtoint ptr %current_source to i64
  %same_source = icmp eq i64 %current_source_bits, %seen
  call void @handoff_require(i1 %same_source)
  %main_token = call i64 @pcc_gc_root_copy_lease(ptr %output, ptr %source)
  %main_counted = icmp eq i64 %main_token, 1
  call void @handoff_require(i1 %main_counted)
  %overlap = call i64 @handoff_active_count()
  %expected_overlap = add i64 %baseline, 2
  %overlap_ok = icmp eq i64 %overlap, %expected_overlap
  call void @handoff_require(i1 %overlap_ok)
  store atomic i64 1, ptr @handoff_release release, align 8
  %thread = load ptr, ptr %handle
  %joined = call i64 @pcc_thread_join(ptr %thread, ptr %thread_result)
  %worker_status = load atomic i64, ptr @handoff_status acquire, align 8
  %joined_ok = icmp eq i64 %joined, 0
  %worker_ok = icmp eq i64 %worker_status, 0
  %thread_ok = and i1 %joined_ok, %worker_ok
  call void @handoff_require(i1 %thread_ok)
  %remaining = call i64 @handoff_active_count()
  %expected_remaining = add i64 %baseline, 1
  %remaining_ok = icmp eq i64 %remaining, %expected_remaining
  call void @handoff_require(i1 %remaining_ok)
  %still_pinned = call i64 @handoff_address_pinned(ptr %output)
  %still_held = icmp ne i64 %still_pinned, 0
  call void @handoff_require(i1 %still_held)
  %last_release = call i64 @pcc_gc_foreign_lease_release(ptr %output, i64 %main_token)
  %last_release_ok = icmp eq i64 %last_release, 0
  call void @handoff_require(i1 %last_release_ok)
  %final_count = call i64 @handoff_active_count()
  %balanced = icmp eq i64 %final_count, %baseline
  call void @handoff_require(i1 %balanced)
  %final_pin = call i64 @handoff_address_pinned(ptr %output)
  %released_all = icmp eq i64 %final_pin, 0
  call void @handoff_require(i1 %released_all)
  call void @pcc_gc_store_root(ptr %output, ptr null)
  call void @pcc_gc_store_root(ptr %destination, ptr null)
  call void @pcc_gc_store_root(ptr %source, ptr null)
  call void @pcc_gc_frame_leave_lifo(ptr %roots)
  ret i64 0
}
'''


def test_threaded_handoff_probe_owned_ir():
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.owned_ir_validation import verify_ir_text

    verify_ir_text('target triple = "' + host_target_triple() + '"\n' + HANDOFF_IR)


@pytest.mark.integration
def test_threaded_root_handoff_native_gc3_gc4(tmp_path, threaded_pcc_runtime_archive):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_targets import host_target_triple

    helper = tmp_path / "root_handoff.o"
    helper.write_bytes(emit_owned_object(
        'target triple = "' + host_target_triple() + '"\n' + HANDOFF_IR,
        host_target_triple(),
    ))
    source = tmp_path / "root_handoff.py"
    source.write_text('''from pcc.extern import extern, c_int64
probe = extern("test_threaded_root_slot_handoff", (), c_int64)
def main():
    assert probe() == 0
    print("ROOT_SLOT_HANDOFF_OK")
main()
''')
    output = source.with_suffix("")
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(threaded_pcc_runtime_archive),
                   link_args=(str(helper),))
    for backend in (3, 4):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend),
                           PCC_GC_MINOR_ALLOC_MAX="4096", PATH="",
                           PCC_HOST_PYTHON="/nonexistent/host-python",
                           PCC_HOST_PCC="/nonexistent/host-pcc")
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=60)
        (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
        (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
        assert result.returncode == 0, (backend, result.returncode, result.stdout, result.stderr)
        assert result.stdout == "ROOT_SLOT_HANDOFF_OK\n" and result.stderr == ""
