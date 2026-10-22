"""Native returned-owner handoff through the production special-call binder.

The movement phase returns an intentionally stale *owned* address. Its mirror
stays authoritative while GC4 allocates every copy/forwarding plan. After the
callback announces capture it makes no call or poll before returning; the
controller performs only the preallocated commit and publishes return-go
before unlocking. No pre-call announcement is treated as contention proof.
The controller stays nonparking during this transition and fails immediately
on an unexpected stop request; arbitrary third-collector schedules are outside
this controlled probe's scope.

The managed variant also exercises PyFunc's distinct legacy inner-result
handling. Neither it nor supporting raw fixture construction is presumed safe.
A separate finalizer phase checks lifetime after output publication without
claiming that user instances can use GC3's leaf-only copying primitive.
"""
import os
import subprocess

import pytest

from test_threaded_root_slot_handoff import HANDOFF_IR


RESULT_IR = HANDOFF_IR + r'''
@result_map = internal constant i32 7
@result_class_name = internal constant [13 x i8] c"ResultCaller\00"
@result_final_name = internal constant [12 x i8] c"FinalResult\00"
@result_call_name = internal constant [9 x i8] c"__call__\00"
@result_del_name = internal constant [8 x i8] c"__del__\00"
@result_mirror_slot = internal global ptr null
@result_final_class_slot = internal global ptr null
@result_receiver_slot = internal global ptr null
@result_output_slot = internal global ptr null
@result_backend = internal global i64 0
@result_phase = internal global i64 0
@result_ready = internal global i64 0
@result_capture_go = internal global i64 0
@result_captured = internal global i64 0
@result_return_go = internal global i64 0
@result_done = internal global i64 0
@result_witness = internal global i64 0
@result_final_lease = internal global i64 0
@result_finalized = internal global i64 0
declare ptr @py_class_new(ptr, ptr, i32, ptr, i32)
declare void @py_class_add_method(ptr, ptr, ptr)
declare i64 @py_class_setattr(ptr, ptr, ptr)
declare ptr @py_instance_new(ptr)
declare ptr @py_func_new(ptr, ptr)
declare void @py_incref(ptr)
declare i64 @py_err_occurred()
declare i64 @py_obj_special_call_slots(ptr, ptr, ptr, ptr, ptr, ptr)
declare ptr @pcc_gc_alloc(i64, i32, i32)
declare i64 @pcc_gc_relocation_payload_slot_count_locked(ptr)
declare ptr @pcc_gc_relocation_payload_plan_prepare(i64)
declare i64 @pcc_gc_relocation_payload_raw_snapshot_locked(ptr, i64, i64, ptr)
declare i64 @pcc_gc_relocation_payload_raw_prepare(ptr)
declare i64 @pcc_gc_relocation_payload_plan_validate_locked(ptr, ptr, i64, ptr)
declare i64 @pcc_gc_relocation_payload_raw_validate_locked(ptr, ptr, i64, i64, ptr)
declare void @pcc_gc_relocation_payload_plan_finish(ptr)
declare ptr @pcc_gc_forwarding_install_plan_prepare(ptr, ptr)
declare void @pcc_gc_forwarding_install_plan_finish(ptr)
declare ptr @pcc_gc_backend4_relocate_copy_preallocated_unlocked(ptr, i64, ptr, ptr, ptr, ptr)
declare void @pcc_gc_backend4_zpage_finish_relocation_detach(ptr)
declare void @free(ptr)
declare void @pcc_thread_no_park_enter()
declare void @pcc_thread_no_park_exit()
declare i64 @pcc_thread_stop_requested_acquire()

define void @result_pin_fixture(ptr %slot) {
entry:
  %token = call i64 @pcc_gc_foreign_lease_acquire(ptr %slot)
  %ok = icmp sge i64 %token, 0
  call void @handoff_require(i1 %ok)
  %value = load ptr, ptr %slot
  %present = icmp ne ptr %value, null
  call void @handoff_require(i1 %present)
  call void @pcc_gc_pin(ptr %value)
  call void @pcc_gc_note_slot_write_barrier(ptr null, ptr %slot, ptr %value)
  %released = call i64 @pcc_gc_foreign_lease_release(ptr %slot, i64 %token)
  %balanced = icmp eq i64 %released, 0
  call void @handoff_require(i1 %balanced)
  ret void
}

define void @result_drop_fixture(ptr %slot) {
entry:
  %value = load ptr, ptr %slot
  call void @pcc_gc_unpin(ptr %value)
  call void @pcc_gc_store_root(ptr %slot, ptr null)
  ret void
}

define void @result_require_count(ptr %slot, i64 %expected) {
entry:
  call void @pcc_py_gc_minor_graph_lock()
  %value = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %slot, i64 0)
  %present = icmp ne ptr %value, null
  call void @handoff_require(i1 %present)
  %count = load i64, ptr %value
  %correct = icmp eq i64 %count, %expected
  call void @handoff_require(i1 %correct)
  call void @pcc_py_gc_minor_graph_unlock()
  ret void
}

define void @result_finalizer(ptr %self) {
entry:
  %prior = atomicrmw add ptr @result_finalized, i64 1 acq_rel
  ret void
}

define ptr @result_callback() {
entry:
  %mirror = load ptr, ptr @result_mirror_slot
  %phase = load i64, ptr @result_phase
  %final = icmp ne i64 %phase, 0
  br i1 %final, label %finalizable, label %moving
finalizable:
  ; Supporting class is held pinned for the fixture lifetime. This phase
  ; guards the returned object's address until the binder has published it.
  %class_slot = load ptr, ptr @result_final_class_slot
  %class = load ptr, ptr %class_slot
  %instance = call ptr @py_instance_new(ptr %class)
  store ptr %instance, ptr %mirror
  %token = call i64 @pcc_gc_foreign_lease_acquire(ptr %mirror)
  %leased = icmp sgt i64 %token, 0
  call void @handoff_require(i1 %leased)
  %final_value = load ptr, ptr %mirror
  call void @pcc_gc_note_slot_write_barrier(ptr null, ptr %mirror, ptr %final_value)
  call void @py_incref(ptr %final_value)
  store atomic i64 %token, ptr @result_final_lease release, align 8
  ret ptr %final_value
moving:
  %backend = load i64, ptr @result_backend
  call void @handoff_new_young(ptr %mirror, i64 %backend)
  store atomic i64 1, ptr @result_ready release, align 8
  br label %await_capture
await_capture:
  ; No saved raw object pointer exists in this phase; allocating preparation
  ; may stop the world and heal the mirror while this callback cooperates.
  call void @pcc_thread_safepoint()
  %capture_go = load atomic i64, ptr @result_capture_go acquire, align 8
  %capture = icmp ne i64 %capture_go, 0
  br i1 %capture, label %capture_owner, label %await_capture
capture_owner:
  call void @pcc_py_gc_minor_graph_lock()
  %saved = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %mirror, i64 0)
  call void @py_incref(ptr %saved)
  %bits = ptrtoint ptr %saved to i64
  store atomic i64 %bits, ptr @result_witness release, align 8
  ; Unlock can poll. The controller may not move until the later captured
  ; announcement, and verifies this exact witness against the current slot.
  call void @pcc_py_gc_minor_graph_unlock()
  store atomic i64 1, ptr @result_captured release, align 8
  br label %await_return
await_return:
  ; Deliberately no calls or polls from here through the stale owned return.
  %return_go = load atomic i64, ptr @result_return_go acquire, align 8
  %return_now = icmp ne i64 %return_go, 0
  br i1 %return_now, label %return_owner, label %await_return
return_owner:
  ret ptr %saved
}

define ptr @result_raw_method(ptr %self) {
entry:
  %value = call ptr @result_callback()
  ret ptr %value
}

define ptr @result_managed_entry(ptr %captures, ptr %args) {
entry:
  %value = call ptr @result_callback()
  ret ptr %value
}

define void @result_invoke() {
entry:
  %handled = alloca i64
  store i64 0, ptr %handled
  %receiver = load ptr, ptr @result_receiver_slot
  %output = load ptr, ptr @result_output_slot
  %status = call i64 @py_obj_special_call_slots(ptr %receiver, ptr @result_call_name, ptr null, ptr null, ptr %output, ptr %handled)
  %selected = load i64, ptr %handled
  %success = icmp eq i64 %status, 0
  %did_call = icmp eq i64 %selected, 1
  %complete = and i1 %success, %did_call
  call void @handoff_require(i1 %complete)
  %error = call i64 @py_err_occurred()
  %clean = icmp eq i64 %error, 0
  call void @handoff_require(i1 %clean)
  ret void
}

define ptr @result_worker(ptr %unused) {
entry:
  call void @result_invoke()
  store atomic i64 1, ptr @result_done release, align 8
  ret ptr null
}

define void @result_require_young_arena_locked(ptr %value, i64 %backend) {
entry:
  %present = icmp ne ptr %value, null
  call void @handoff_require(i1 %present)
  %flags_slot = getelementptr i8, ptr %value, i64 12
  %flags = load i32, ptr %flags_slot
  %young_bits = and i32 %flags, 128
  %young = icmp ne i32 %young_bits, 0
  %is3 = icmp eq i64 %backend, 3
  %mask = select i1 %is3, i32 4096, i32 65536
  %arena_bits = and i32 %flags, %mask
  %arena = icmp ne i32 %arena_bits, 0
  %young_arena = and i1 %young, %arena
  call void @handoff_require(i1 %young_arena)
  ret void
}

define void @result_require_young_locked(ptr %value, i64 %backend) {
entry:
  call void @result_require_young_arena_locked(ptr %value, i64 %backend)
  %pinned = call i64 @pcc_gc_object_is_address_pinned(ptr %value)
  %unpinned = icmp eq i64 %pinned, 0
  call void @handoff_require(i1 %unpinned)
  ret void
}

define void @result_prepare_gc4(ptr %mirror, ptr %candidate, ptr %plans) {
entry:
  ; The only peer is still polling, with no saved raw owner. All managed
  ; allocation happens here, before capture-go is published.
  %guard = call i64 @pcc_gc_foreign_lease_acquire(ptr %mirror)
  %guard_counted = icmp eq i64 %guard, 1
  call void @handoff_require(i1 %guard_counted)
  call void @pcc_py_gc_minor_graph_lock()
  %initial = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %mirror, i64 0)
  call void @result_require_young_arena_locked(ptr %initial, i64 4)
  %size = call i64 @pcc_gc_object_known_size(ptr %initial)
  %tag_slot = getelementptr i8, ptr %initial, i64 8
  %tag32 = load i32, ptr %tag_slot
  %tag = sext i32 %tag32 to i64
  %flags_slot = getelementptr i8, ptr %initial, i64 12
  %flags = load i32, ptr %flags_slot
  %base_flags = and i32 %flags, -10241
  %allocation_flags = or i32 %base_flags, 64
  %count = call i64 @pcc_gc_relocation_payload_slot_count_locked(ptr %initial)
  %count_ok = icmp sge i64 %count, 0
  call void @handoff_require(i1 %count_ok)
  call void @pcc_py_gc_minor_graph_unlock()
  %payload = call ptr @pcc_gc_relocation_payload_plan_prepare(i64 %count)
  %payload_ok = icmp ne ptr %payload, null
  call void @handoff_require(i1 %payload_ok)
  store ptr %payload, ptr %plans
  %size_slot = getelementptr i8, ptr %plans, i64 16
  %tag_save = getelementptr i8, ptr %plans, i64 24
  store i64 %size, ptr %size_slot
  store i64 %tag, ptr %tag_save
  %to = call ptr @pcc_gc_alloc(i64 %size, i32 %tag32, i32 %allocation_flags)
  store ptr %to, ptr %candidate
  %to_ok = icmp ne ptr %to, null
  call void @handoff_require(i1 %to_ok)
  ; Candidate owns the allocator's initial reference and remains PINNED until
  ; the production commit replaces its header. It is never an untraced owner.
  call void @pcc_py_gc_minor_graph_lock()
  %from = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %mirror, i64 0)
  call void @result_require_young_arena_locked(ptr %from, i64 4)
  call void @pcc_gc_note_slot_write_barrier(ptr null, ptr %candidate, ptr %to)
  %snapshot = call i64 @pcc_gc_relocation_payload_raw_snapshot_locked(ptr %from, i64 %tag, i64 %size, ptr %payload)
  %snapshot_ok = icmp ne i64 %snapshot, 0
  call void @handoff_require(i1 %snapshot_ok)
  call void @pcc_py_gc_minor_graph_unlock()
  %raw = call i64 @pcc_gc_relocation_payload_raw_prepare(ptr %payload)
  %raw_ok = icmp ne i64 %raw, 0
  call void @handoff_require(i1 %raw_ok)
  ; The remaining preparation allocates only raw plan storage. Forwarding
  ; preparation rejects a leased source: release the temporary guard inside
  ; this outer transaction, prepare/admit, then unlock. Nested helper unlocks
  ; cannot park; no managed allocation or user callback occurs in this span.
  call void @pcc_py_gc_minor_graph_lock()
  %current = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %mirror, i64 0)
  %guard_release = call i64 @pcc_gc_foreign_lease_release(ptr %mirror, i64 %guard)
  %guard_released = icmp eq i64 %guard_release, 0
  call void @handoff_require(i1 %guard_released)
  call void @result_require_young_locked(ptr %current, i64 4)
  %forwarding = call ptr @pcc_gc_forwarding_install_plan_prepare(ptr %current, ptr %to)
  %forwarding_ok = icmp ne ptr %forwarding, null
  call void @handoff_require(i1 %forwarding_ok)
  %forward_slot = getelementptr i8, ptr %plans, i64 8
  store ptr %forwarding, ptr %forward_slot
  ; Admission allocates its raw list node, so it too belongs before capture.
  %admitted = call i64 @pcc_gc_backend4_relocation_set_add(ptr %current)
  %admitted_ok = icmp eq i64 %admitted, 1
  call void @handoff_require(i1 %admitted_ok)
  call void @pcc_py_gc_minor_graph_unlock()
  ret void
}

define i64 @result_commit(ptr %mirror, ptr %candidate, ptr %plans, ptr %finish, i64 %backend) {
entry:
  call void @pcc_py_gc_minor_graph_lock()
  %source = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %mirror, i64 0)
  %witness = load atomic i64, ptr @result_witness acquire, align 8
  %source_bits = ptrtoint ptr %source to i64
  %capture_current = icmp eq i64 %source_bits, %witness
  call void @handoff_require(i1 %capture_current)
  call void @result_require_young_locked(ptr %source, i64 %backend)
  %owners = load i64, ptr %source
  %two_owners = icmp eq i64 %owners, 2
  call void @handoff_require(i1 %two_owners)
  %is3 = icmp eq i64 %backend, 3
  br i1 %is3, label %gc3, label %gc4
gc3:
  ; GC3's selected source is a string: promotion has no managed allocation
  ; or user callback. The extra raw source owner remains until adoption.
  call void @pcc_gc_generational_promote_owned_slot_mode(ptr %mirror, i64 0, i64 0)
  %promoted = load ptr, ptr %mirror
  br label %publish_go
gc4:
  %to = load ptr, ptr %candidate
  %payload = load ptr, ptr %plans
  %forward_slot = getelementptr i8, ptr %plans, i64 8
  %forwarding = load ptr, ptr %forward_slot
  %size_slot = getelementptr i8, ptr %plans, i64 16
  %size = load i64, ptr %size_slot
  %tag_slot = getelementptr i8, ptr %plans, i64 24
  %tag = load i64, ptr %tag_slot
  %valid = call i64 @pcc_gc_relocation_payload_plan_validate_locked(ptr %source, ptr %to, i64 %size, ptr %payload)
  %raw_valid = call i64 @pcc_gc_relocation_payload_raw_validate_locked(ptr %source, ptr %to, i64 %tag, i64 %size, ptr %payload)
  %valid_ok = icmp ne i64 %valid, 0
  %raw_valid_ok = icmp ne i64 %raw_valid, 0
  %plans_valid = and i1 %valid_ok, %raw_valid_ok
  call void @handoff_require(i1 %plans_valid)
  %relocated = call ptr @pcc_gc_backend4_relocate_copy_preallocated_unlocked(ptr %source, i64 %size, ptr %to, ptr %payload, ptr %forwarding, ptr %finish)
  %candidate_used = icmp eq ptr %relocated, %to
  call void @handoff_require(i1 %candidate_used)
  %healed = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %mirror, i64 0)
  %mirror_healed = icmp eq ptr %healed, %relocated
  call void @handoff_require(i1 %mirror_healed)
  br label %publish_go
publish_go:
  %moved = phi ptr [%promoted, %gc3], [%relocated, %gc4]
  %nonnull = icmp ne ptr %moved, null
  %moved_bits = ptrtoint ptr %moved to i64
  %different = icmp ne i64 %moved_bits, %witness
  %real_movement = and i1 %nonnull, %different
  call void @handoff_require(i1 %real_movement)
  ; Release the callback before any unlock/finish can park. It now returns
  ; the captured from-space owner directly into the binder's empty slot.
  store atomic i64 1, ptr @result_return_go release, align 8
  call void @pcc_py_gc_minor_graph_unlock()
  ret i64 %moved_bits
}

define void @result_finish_gc4(ptr %plans, ptr %finish) {
entry:
  %payload = load ptr, ptr %plans
  call void @pcc_gc_relocation_payload_plan_finish(ptr %payload)
  %detached = load ptr, ptr %finish
  call void @free(ptr %detached)
  %page_slot = getelementptr i8, ptr %finish, i64 8
  %page = load ptr, ptr %page_slot
  call void @free(ptr %page)
  %zpage_slot = getelementptr i8, ptr %finish, i64 16
  %zpage = load ptr, ptr %zpage_slot
  call void @pcc_gc_backend4_zpage_finish_relocation_detach(ptr %zpage)
  %forward_slot = getelementptr i8, ptr %plans, i64 8
  %forwarding = load ptr, ptr %forward_slot
  call void @pcc_gc_forwarding_install_plan_finish(ptr %forwarding)
  ; Do not decref the destination here: its allocator owner belongs to the
  ; registered candidate root and is consumed by the caller's root clear.
  ret void
}

define void @result_require_payload(ptr %slot, i64 %backend) {
entry:
  call void @pcc_py_gc_minor_graph_lock()
  %value = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %slot, i64 0)
  %present = icmp ne ptr %value, null
  call void @handoff_require(i1 %present)
  %tag_slot = getelementptr i8, ptr %value, i64 8
  %tag = load i32, ptr %tag_slot
  %length_slot = getelementptr i8, ptr %value, i64 16
  %length = load i64, ptr %length_slot
  %is3 = icmp eq i64 %backend, 3
  %expected_tag = select i1 %is3, i32 4, i32 5
  %expected_length = select i1 %is3, i64 6, i64 0
  %tag_ok = icmp eq i32 %tag, %expected_tag
  %length_ok = icmp eq i64 %length, %expected_length
  %payload_ok = and i1 %tag_ok, %length_ok
  call void @handoff_require(i1 %payload_ok)
  call void @pcc_py_gc_minor_graph_unlock()
  ret void
}

define i64 @test_special_call_result_handoff(i64 %mode) {
entry:
  %roots = alloca [7 x ptr]
  %class_slot = getelementptr [7 x ptr], ptr %roots, i64 0, i64 0
  %receiver = getelementptr [7 x ptr], ptr %roots, i64 0, i64 1
  %function = getelementptr [7 x ptr], ptr %roots, i64 0, i64 2
  %final_class = getelementptr [7 x ptr], ptr %roots, i64 0, i64 3
  %mirror = getelementptr [7 x ptr], ptr %roots, i64 0, i64 4
  %candidate = getelementptr [7 x ptr], ptr %roots, i64 0, i64 5
  %output = getelementptr [7 x ptr], ptr %roots, i64 0, i64 6
  %plans = alloca [4 x i64]
  %finish = alloca [3 x ptr]
  %handle = alloca ptr
  %thread_result = alloca ptr
  store ptr null, ptr %class_slot
  store ptr null, ptr %receiver
  store ptr null, ptr %function
  store ptr null, ptr %final_class
  store ptr null, ptr %mirror
  store ptr null, ptr %candidate
  store ptr null, ptr %output
  store ptr null, ptr %handle
  store ptr null, ptr %thread_result
  call void @pcc_gc_frame_enter_lifo(ptr @result_map, ptr %roots)
  store ptr %mirror, ptr @result_mirror_slot
  store ptr %final_class, ptr @result_final_class_slot
  store ptr %receiver, ptr @result_receiver_slot
  store ptr %output, ptr @result_output_slot
  %backend = call i64 @pcc_gc_backend()
  store i64 %backend, ptr @result_backend
  %is3 = icmp eq i64 %backend, 3
  %is4 = icmp eq i64 %backend, 4
  %moving = or i1 %is3, %is4
  call void @handoff_require(i1 %moving)
  %threads = call i64 @pcc_threads_enabled()
  %threaded = icmp ne i64 %threads, 0
  call void @handoff_require(i1 %threaded)
  %raw_mode = icmp eq i64 %mode, 0
  %managed_mode = icmp eq i64 %mode, 1
  %valid_mode = or i1 %raw_mode, %managed_mode
  call void @handoff_require(i1 %valid_mode)
  %baseline = call i64 @handoff_active_count()

  ; Ordinary runtime fixture construction is supporting setup. Pin these
  ; fixtures, never the deliberately movable callback result. Code pointers
  ; go only into native entry/method fields, never into managed root slots.
  %class = call ptr @py_class_new(ptr @result_class_name, ptr null, i32 0, ptr null, i32 0)
  store ptr %class, ptr %class_slot
  call void @result_pin_fixture(ptr %class_slot)
  %class_current = load ptr, ptr %class_slot
  %object = call ptr @py_instance_new(ptr %class_current)
  store ptr %object, ptr %receiver
  call void @result_pin_fixture(ptr %receiver)
  %final_type = call ptr @py_class_new(ptr @result_final_name, ptr null, i32 0, ptr null, i32 0)
  store ptr %final_type, ptr %final_class
  call void @result_pin_fixture(ptr %final_class)
  %final_type_current = load ptr, ptr %final_class
  call void @py_class_add_method(ptr %final_type_current, ptr @result_del_name, ptr @result_finalizer)
  br i1 %managed_mode, label %managed_method, label %native_method
native_method:
  %native_owner = load ptr, ptr %class_slot
  call void @py_class_add_method(ptr %native_owner, ptr @result_call_name, ptr @result_raw_method)
  br label %start
managed_method:
  %callable = call ptr @py_func_new(ptr @result_managed_entry, ptr null)
  store ptr %callable, ptr %function
  call void @result_pin_fixture(ptr %function)
  %managed_owner = load ptr, ptr %class_slot
  %managed_value = load ptr, ptr %function
  ; The namespace owns the PyFunc independently, so clearing the supporting
  ; root never leaves an immortal class with dangling borrowed metadata.
  %set_method = call i64 @py_class_setattr(ptr %managed_owner, ptr @result_call_name, ptr %managed_value)
  %set_method_ok = icmp eq i64 %set_method, 0
  call void @handoff_require(i1 %set_method_ok)
  br label %start
start:
  %setup_active = call i64 @handoff_active_count()
  %setup_balanced = icmp eq i64 %setup_active, %baseline
  call void @handoff_require(i1 %setup_balanced)
  store i64 0, ptr @result_phase
  store atomic i64 0, ptr @result_ready release, align 8
  store atomic i64 0, ptr @result_capture_go release, align 8
  store atomic i64 0, ptr @result_captured release, align 8
  store atomic i64 0, ptr @result_return_go release, align 8
  store atomic i64 0, ptr @result_done release, align 8
  %started = call i64 @pcc_thread_start(ptr %handle, ptr @result_worker, ptr null)
  %started_ok = icmp eq i64 %started, 0
  call void @handoff_require(i1 %started_ok)
  br label %await_ready
await_ready:
  call void @pcc_thread_safepoint()
  %ready_value = load atomic i64, ptr @result_ready acquire, align 8
  %ready = icmp ne i64 %ready_value, 0
  br i1 %ready, label %prepare, label %await_ready
prepare:
  br i1 %is4, label %prepare4, label %capture_go
prepare4:
  call void @result_prepare_gc4(ptr %mirror, ptr %candidate, ptr %plans)
  br label %capture_go
capture_go:
  ; The controller must not park after the callback stops polling. This is
  ; independent of graph locking: the callback still needs that lock to take
  ; its raw owner. Unexpected third-party STW during the transition is a hard
  ; probe failure, not a deadlock, skip or weaker-success alternative.
  call void @pcc_thread_no_park_enter()
  store atomic i64 1, ptr @result_capture_go release, align 8
  br label %await_captured
await_captured:
  %unexpected_stop = call i64 @pcc_thread_stop_requested_acquire()
  %no_stop = icmp eq i64 %unexpected_stop, 0
  call void @handoff_require(i1 %no_stop)
  %captured_value = load atomic i64, ptr @result_captured acquire, align 8
  %captured = icmp ne i64 %captured_value, 0
  br i1 %captured, label %commit, label %await_captured
commit:
  %expected = call i64 @result_commit(ptr %mirror, ptr %candidate, ptr %plans, ptr %finish, i64 %backend)
  ; result_commit has already published return-go and released graph lock.
  ; A stop may now wait until the callback's result is in the binder's root.
  call void @pcc_thread_no_park_exit()
  br label %await_done
await_done:
  call void @pcc_thread_safepoint()
  %done_value = load atomic i64, ptr @result_done acquire, align 8
  %done = icmp ne i64 %done_value, 0
  br i1 %done, label %join, label %await_done
join:
  %thread = load ptr, ptr %handle
  %joined = call i64 @pcc_thread_join(ptr %thread, ptr %thread_result)
  %joined_ok = icmp eq i64 %joined, 0
  call void @handoff_require(i1 %joined_ok)
  call void @pcc_py_gc_minor_graph_lock()
  %published = call ptr @pcc_gc_resolve_root_slot_unlocked(ptr %output, i64 0)
  %published_bits = ptrtoint ptr %published to i64
  %correct_result = icmp eq i64 %published_bits, %expected
  call void @handoff_require(i1 %correct_result)
  call void @pcc_py_gc_minor_graph_unlock()
  %invocation_active = call i64 @handoff_active_count()
  %invocation_balanced = icmp eq i64 %invocation_active, %baseline
  call void @handoff_require(i1 %invocation_balanced)
  %owner_count = select i1 %is3, i64 2, i64 3
  call void @result_require_count(ptr %output, i64 %owner_count)
  br i1 %is4, label %finish4, label %drop_setup_owners
finish4:
  call void @result_finish_gc4(ptr %plans, ptr %finish)
  br label %drop_setup_owners
drop_setup_owners:
  call void @pcc_gc_store_root(ptr %mirror, ptr null)
  call void @pcc_gc_store_root(ptr %candidate, ptr null)
  call void @result_require_count(ptr %output, i64 1)
  %output_pinned = call i64 @handoff_address_pinned(ptr %output)
  %output_unpinned = icmp eq i64 %output_pinned, 0
  call void @handoff_require(i1 %output_unpinned)
  %collected_live = call i64 @pcc_gc_collect(i32 2)
  call void @result_require_payload(ptr %output, i64 %backend)
  call void @result_require_count(ptr %output, i64 1)
  call void @pcc_gc_store_root(ptr %output, ptr null)

  ; Separate finalization assertion: after mirror cleanup, the binder output
  ; must be the sole owner. Collection must not finalize it before clearing.
  store i64 1, ptr @result_phase
  store atomic i64 0, ptr @result_finalized release, align 8
  store atomic i64 0, ptr @result_final_lease release, align 8
  call void @result_invoke()
  call void @result_require_count(ptr %output, i64 2)
  %final_token = load atomic i64, ptr @result_final_lease acquire, align 8
  %final_guard = call i64 @handoff_active_count()
  %expected_guard = add i64 %baseline, 1
  %only_guard = icmp eq i64 %final_guard, %expected_guard
  call void @handoff_require(i1 %only_guard)
  %guard_release = call i64 @pcc_gc_foreign_lease_release(ptr %mirror, i64 %final_token)
  %guard_released = icmp eq i64 %guard_release, 0
  call void @handoff_require(i1 %guard_released)
  call void @pcc_gc_store_root(ptr %mirror, ptr null)
  call void @result_require_count(ptr %output, i64 1)
  %collected_owned = call i64 @pcc_gc_collect(i32 2)
  %before_clear = load atomic i64, ptr @result_finalized acquire, align 8
  %still_live = icmp eq i64 %before_clear, 0
  call void @handoff_require(i1 %still_live)
  call void @result_require_count(ptr %output, i64 1)
  call void @pcc_gc_store_root(ptr %output, ptr null)
  %collected_dead = call i64 @pcc_gc_collect(i32 2)
  %after_clear = load atomic i64, ptr @result_finalized acquire, align 8
  %finalized_once = icmp eq i64 %after_clear, 1
  call void @handoff_require(i1 %finalized_once)
  %collected_again = call i64 @pcc_gc_collect(i32 2)
  %after_again = load atomic i64, ptr @result_finalized acquire, align 8
  %not_repeated = icmp eq i64 %after_again, 1
  call void @handoff_require(i1 %not_repeated)
  %final_active = call i64 @handoff_active_count()
  %all_balanced = icmp eq i64 %final_active, %baseline
  call void @handoff_require(i1 %all_balanced)
  call void @result_drop_fixture(ptr %function)
  call void @result_drop_fixture(ptr %receiver)
  call void @result_drop_fixture(ptr %final_class)
  call void @result_drop_fixture(ptr %class_slot)
  store ptr null, ptr @result_mirror_slot
  store ptr null, ptr @result_final_class_slot
  store ptr null, ptr @result_receiver_slot
  store ptr null, ptr @result_output_slot
  call void @pcc_gc_frame_leave_lifo(ptr %roots)
  ret i64 0
}
'''


def test_special_call_result_probe_owned_ir():
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.owned_ir_validation import verify_ir_text

    verify_ir_text('target triple = "' + host_target_triple() + '"\n' + RESULT_IR)


@pytest.mark.integration
@pytest.mark.parametrize("mode", [0, 1], ids=["raw", "managed"])
def test_special_call_result_handoff_native_gc3_gc4(tmp_path, threaded_pcc_runtime_archive, mode):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_targets import host_target_triple

    target = host_target_triple()
    helper = tmp_path / "special_result.o"
    helper.write_bytes(emit_owned_object('target triple = "' + target + '"\n' + RESULT_IR, target))
    source = tmp_path / "special_result.py"
    source.write_text('''from pcc.extern import extern, c_int64
probe = extern("test_special_call_result_handoff", (c_int64,), c_int64)
def main():
    assert probe(''' + str(mode) + ''') == 0
    print("SPECIAL_CALL_RESULT_HANDOFF_OK")
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
        assert result.returncode == 0, (backend, mode, result.returncode, result.stdout, result.stderr)
        assert result.stdout == "SPECIAL_CALL_RESULT_HANDOFF_OK\n" and result.stderr == ""
