"""Execute production lease bodies against deterministic lock/object metadata."""
from __future__ import annotations
import ast
from pathlib import Path
import pytest

from pcc.runtime.py import py_abi_constants as abi

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime/py"


def _functions(path, names, namespace):
    tree = ast.parse(path.read_text())
    chosen = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            chosen.append(node)
    assert len(chosen) == len(names)
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + chosen, type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), namespace)


class Model:
    def __init__(self, backend=3):
        self.memory = {("pcc_gc_backend_selected", 0): backend}
        self.nodes = {}
        self.proven = set()
        self.depth = 0
        self.on_lock = None
        self.on_unlock = None
        self.events = []
        self.ns = dict(i64=int, PYOBJECTHEADER_FLAGS_OFFSET=12, PY_FLAG_GC_PINNED=64,
            ptr_is_null=lambda p: int(p == 0), is_tagged_int=lambda p: int(isinstance(p, int) and p & 1),
            null=lambda: 0, ptr_eq=lambda a,b: int(a == b), global_addr=lambda p:p,
            load_i32=self.load, load_i64=self.load, load_ptr=self.load,
            store_i32=self.store, store_i64=self.store, store_ptr=self.store,
            global_load_ptr=lambda p:self.load(p,0), global_store_ptr=lambda p,v:self.store(p,0,v),
            pcc_gc_config_ensure=lambda:self.load("pcc_gc_backend_selected",0),
            pcc_gc_resolve_root_slot_unlocked=self.resolve,
            pcc_gc_object_index_find=lambda p:self.nodes.get(p,0),
            pcc_gc_granule_is_object_start=lambda p:int(p in self.proven),
            pcc_gc_managed_pointer_index_contains=lambda p:0,
            pcc_py_gc_minor_graph_lock=self.lock, pcc_py_gc_minor_graph_unlock=self.unlock)
        self.ns.update({name: value for name, value in vars(abi).items()
                        if name.startswith("PY_TYPE_")})
        _functions(RUNTIME / "freestanding_gc_root_operations.py", {
            "pcc_gc_foreign_lease_acquire", "pcc_gc_foreign_lease_release", "pcc_gc_object_is_address_pinned"}, self.ns)

    def load(self, base, offset=0):
        return self.memory.get((base,offset),0)

    def store(self, base, offset, value):
        self.memory[base,offset] = value

    def resolve(self, slot, offset):
        assert self.depth > 0
        self.events.append(("reload",slot))
        return self.load(slot,offset)

    def lock(self):
        if self.depth == 0 and self.on_lock:
            hook,self.on_lock = self.on_lock,None
            hook()
        self.depth += 1
        self.events.append(("lock",self.depth))

    def unlock(self):
        self.depth -= 1
        assert self.depth >= 0
        self.events.append(("unlock",self.depth))
        if self.depth == 0 and self.on_unlock:
            hook,self.on_unlock = self.on_unlock,None
            hook()

    def object(self, obj=1000, *, node=True, flags=0, tag=4, proven=True):
        self.store(obj,12,flags); self.store(obj,8,tag)
        if node:
            self.nodes[obj] = obj+10000
            self.store(obj+10000,80,0)
        if proven:
            self.proven.add(obj)
        slot = obj+20000
        self.store(slot,0,obj)
        return slot

    def acquire(self, slot):
        return self.ns["pcc_gc_foreign_lease_acquire"](slot)

    def release(self, slot, token):
        return self.ns["pcc_gc_foreign_lease_release"](slot,token)

    def count(self, obj=1000):
        return self.load(self.nodes[obj],80)

    def active(self):
        return self.load("pcc_gc_foreign_lease_active",0)

    def pinned(self, obj=1000):
        return self.ns["pcc_gc_object_is_address_pinned"](obj)


@pytest.mark.parametrize("order", [(0,1),(1,0)])
def test_overlapping_leases_survive_legacy_callback_unpin(order):
    m=Model(); slot=m.object()
    tokens=[m.acquire(slot),m.acquire(slot)]
    assert tokens == [1,1] and m.count() == 2 and m.active() == 2
    m.store(1000,12,64)  # callback container pins its alias
    m.store(1000,12,0)   # callback container unpins it, then collects
    assert m.pinned() == 1
    assert m.release(slot,tokens[order[0]]) == 0
    assert m.count() == 1 and m.pinned() == 1
    assert m.release(slot,tokens[order[1]]) == 0
    assert m.count() == 0 and m.active() == 0 and m.pinned() == 0


def test_acquire_reloads_after_contended_lock_relocation():
    m=Model(); slot=m.object(); m.object(2000)
    m.on_lock=lambda:m.store(slot,0,2000)
    assert m.acquire(slot) == 1
    assert m.count(1000) == 0 and m.count(2000) == 1
    assert m.release(slot,1) == 0


@pytest.mark.parametrize("backend",range(5))
def test_valid_no_node_nonmoving_owner_has_mode_guard(backend):
    m=Model(backend); slot=m.object(node=False,flags=262144)
    assert m.acquire(slot) == 2
    assert m.active() == 1
    assert m.release(slot,2) == 0 and m.active() == 0


@pytest.mark.parametrize("backend",range(5))
@pytest.mark.parametrize("tag",[abi.PY_TYPE_FILE,abi.PY_TYPE_CLASS])
def test_no_node_heap_immortal_nonleaf_has_mode_guard(backend,tag):
    # The malloc'd standard streams and object class root: immortal, outside
    # the object graph. GC3/4 rejected them, so passing sys.stdout as an
    # argument raised "slot-call owned-result lease failed".
    m=Model(backend); slot=m.object(node=False,flags=1|262144,tag=tag)
    assert m.acquire(slot) == 2
    assert m.active() == 1
    assert m.release(slot,2) == 0 and m.active() == 0


@pytest.mark.parametrize("backend",(3,4))
@pytest.mark.parametrize("flags",[1|4096,1|65536,1|524288])
def test_no_node_immortal_in_a_moving_or_dying_state_fails_closed(backend,flags):
    m=Model(backend); slot=m.object(node=False,flags=flags,tag=abi.PY_TYPE_FILE)
    assert m.acquire(slot) == -1 and m.active() == 0


@pytest.mark.parametrize("flags,tag,proven",[(0,4,False),(4096,4,True),(65536,4,True),(0,5,True),(524288,4,True)])
def test_unknown_or_potentially_moving_no_node_fails_closed(flags,tag,proven):
    m=Model(); slot=m.object(node=False,flags=flags,tag=tag,proven=proven)
    assert m.acquire(slot) == -1 and m.active() == 0


def test_noop_token_never_decrements_a_later_node():
    m=Model(); slot=m.object(node=False,flags=1,proven=False)
    assert m.acquire(slot) == 0
    m.object(flags=0); assert m.acquire(slot) == 1
    assert m.release(slot,0) == 0 and m.count() == 1 and m.active() == 1
    assert m.release(slot,1) == 0


@pytest.mark.parametrize("counter",["node","active"])
def test_overflow_is_atomic_and_nonwrapping(counter):
    m=Model(); slot=m.object()
    if counter == "node": m.store(m.nodes[1000],80,2**63-1)
    else: m.store("pcc_gc_foreign_lease_active",0,2**63-1)
    before=dict(m.memory)
    assert m.acquire(slot) == -2
    assert m.memory == before


def test_underflow_and_bad_tokens_do_not_mutate_or_touch_pending_exception():
    m=Model(); slot=m.object(); m.ns["pending_exception"]=object()
    error=m.ns["pending_exception"]; before=dict(m.memory)
    assert m.release(slot,1) == -3
    assert m.release(slot,-1) == -1
    assert m.memory == before and m.ns["pending_exception"] is error


def test_legacy_pin_is_independent_of_count():
    m=Model(); slot=m.object(flags=64)
    assert m.acquire(slot) == 1
    assert m.release(slot,1) == 0
    assert m.pinned() == 1 and m.load(1000,12) == 64


def _mode_setter(model):
    ns=model.ns
    # One live mutator: switching into GC3/GC4 is refused only when others run.
    ns.update(_init_config=lambda:model.load("pcc_gc_backend_selected",0),
        pcc_threads_enabled=lambda:0, pcc_thread_live_count=lambda:1,
        _object_graph_lock=model.lock, _object_graph_unlock=model.unlock,
        _forwarding_head=lambda:0, _object_head=lambda:0,
        _tracing_cycle_epoch_advance_unlocked=lambda:1,
        _set_gray_count=lambda v:None, atomic_store_i32=model.store,
        _clear_object_list=lambda:model.events.append(("clear_nodes",model.active())),
        _backend_uses_forwarding=lambda:int(model.load("pcc_gc_backend_selected",0) in (3,4)),
        _forwarding_clear_all=lambda:None, _identity_clear_all=lambda:None,
        pcc_gc_reset_relocation_set=lambda:None, _backend4_store_buffer_clear=lambda:None,
        _stop_cms_worker=lambda:0, _maybe_start_cms_worker=lambda:None,
        # No collector thread here: the pause is idle and needs no join.
        _release_cms_worker_pause=lambda:None,
        _release_cms_worker_pause_unless_kept=lambda keeps:None,
        _is_cms_worker=lambda:0)
    # Atomic stores carry one memory-order argument beyond ordinary stores.
    ns["atomic_store_i32"]=lambda base,offset,value,order:model.store(base,offset,value)
    _functions(RUNTIME / "py_gc_backend.py", {"pcc_gc_set_backend"},ns)
    return ns["pcc_gc_set_backend"]


@pytest.mark.parametrize("before",range(5))
@pytest.mark.parametrize("after",range(5))
def test_mode_change_and_same_mode_reset_refuse_live_leases(before,after):
    m=Model(before); slot=m.object(node=before != 0,flags=262144)
    token=m.acquire(slot)
    assert token > 0
    setter=_mode_setter(m)
    m.ns["pending_exception"]=object(); error=m.ns["pending_exception"]
    assert setter(after) == -1
    assert m.load("pcc_gc_backend_selected",0) == before
    assert m.ns["pending_exception"] is error
    assert m.active() == 1
    assert m.release(slot,token) == 0
    assert setter(after) == 0
    assert m.load("pcc_gc_backend_selected",0) == after


def test_backend_teardown_gap_rejects_new_acquisition():
    m=Model(4); slot=m.object(node=False,flags=262144)
    setter=_mode_setter(m); observed=[]
    m.on_unlock=lambda:observed.append(m.acquire(slot))
    assert setter(0) == 0
    assert observed == [-1]
    assert ("clear_nodes",-1) in m.events
    assert m.active() == 0


def _node_functions(model):
    next_pointer=[100000]
    def allocate(size):
        assert size == 88
        result=next_pointer[0]; next_pointer[0]+=100
        model.store(result,80,777)  # allocation is dirty until initialized
        return result
    def abort():
        raise RuntimeError("leased node retirement")
    model.ns.update(malloc=allocate,free=lambda p:model.events.append(("free",p)),pcc_platform_abort=abort)
    _functions(RUNTIME / "freestanding_gc_object_nodes.py", {
        "_clear_promotion_state", "pcc_gc_object_node_alloc", "pcc_gc_object_node_prepare",
        "pcc_gc_object_node_take_prepared", "pcc_gc_object_node_release", "pcc_gc_object_node_finish_detached"},model.ns)
    return model.ns


def test_node_fresh_prepare_reuse_and_live_promotion_preserve_count_contract():
    m=Model(); ns=_node_functions(m)
    fresh=ns["pcc_gc_object_node_alloc"](); assert m.load(fresh,80) == 0
    prepared=ns["pcc_gc_object_node_prepare"](); assert m.load(prepared,80) == 0
    m.store("prepared_slot",0,prepared)
    assert ns["pcc_gc_object_node_take_prepared"]("prepared_slot") == prepared
    m.store(fresh,80,2)
    ns["_clear_promotion_state"](fresh)
    assert m.load(fresh,80) == 2
    with pytest.raises(RuntimeError): ns["pcc_gc_object_node_release"](fresh)
    with pytest.raises(RuntimeError): ns["pcc_gc_object_node_finish_detached"](fresh)
    m.store(fresh,80,0)
    ns["pcc_gc_object_node_release"](fresh)
    assert ns["pcc_gc_object_node_alloc"]() == fresh
    assert m.load(fresh,80) == 0


def test_lease_count_is_not_copied_or_reset_during_forwarding():
    # Execute the exact final forwarding rejection before any graph mutation.
    m=Model(); source=m.object(); m.object(2000)
    assert m.acquire(source) == 1
    ns=m.ns
    ns.update(pcc_gc_object_is_known_no_lock=lambda p:int(p in m.nodes))
    _functions(RUNTIME / "freestanding_gc_forwarding_identity.py", {"pcc_gc_install_forwarding_unlocked"},ns)
    assert ns["pcc_gc_install_forwarding_unlocked"](1000,2000) == -2
    assert m.count(1000) == 1 and m.count(2000) == 0
    assert m.release(source,1) == 0


def test_tracked_immortal_requires_count_and_forwarded_shell_resolves_first():
    m=Model(4); slot=m.object(flags=1)
    assert m.acquire(slot) == 1
    assert m.release(slot,1) == 0
    m.object(2000,flags=0)
    m.ns["pcc_gc_resolve_root_slot_unlocked"]=lambda s,o:2000
    assert m.acquire(slot) == 1
    assert m.count(1000) == 0 and m.count(2000) == 1


def test_final_copy_rejects_a_lease_acquired_after_plan_preparation():
    m=Model(4); slot=m.object(); m.object(2000)
    m.store("pcc_gc_relocation_set_head",0,3000); m.store(3000,0,1000)
    m.ns.update(pcc_gc_forwarding_find=lambda p:0)
    _functions(RUNTIME / "freestanding_gc_relocation_copy.py", {"pcc_gc_backend4_relocate_copy_preallocated_unlocked"},m.ns)
    assert m.acquire(slot) == 1
    # No payload-plan access is allowed: the final lock-reacquired gate must
    # reject before moving/copying any field or consulting prepared payload.
    assert m.ns["pcc_gc_backend4_relocate_copy_preallocated_unlocked"](1000,64,2000,4000,5000,6000) == 0
    assert m.count(1000) == 1 and m.count(2000) == 0


def test_gc3_oldification_rejects_live_count_without_touching_payload():
    m=Model(3); slot=m.object(flags=128|4096)
    m.ns.update(pcc_gc_object_is_known_no_lock=lambda p:int(p in m.nodes),pcc_gc_forwarding_find=lambda p:0)
    _functions(RUNTIME / "freestanding_gc_generational_oldification.py", {"pcc_gc_generational_oldify_copy"},m.ns)
    assert m.acquire(slot) == 1
    assert m.ns["pcc_gc_generational_oldify_copy"](1000) == 0
    assert m.count() == 1


@pytest.mark.parametrize("name,file",[
    ("pcc_gc_tracing_clear_unreachable","freestanding_gc_sweep_slots.py"),
    ("pcc_gc_tracing_finalize_unreachable","freestanding_gc_tracing_sweep_collector.py"),
])
def test_sweep_destructive_entries_refuse_leased_owner(name,file):
    m=Model(4); slot=m.object(flags=1024)
    _functions(RUNTIME/file,{name},m.ns)
    assert m.acquire(slot) == 1
    # Destructors/weakref invalidation are intentionally not supplied. A call
    # past the new guard would fail rather than silently pass this model.
    m.ns[name](1000)
    assert m.load(1000,12) == 1024 and m.count() == 1


# This owned-IR callee performs raw address checks that Python string equality
# cannot prove (a stale from-space buffer may still compare equal).
MOVEMENT_IR = r'''
@lease_map = internal constant i32 1
@lease_text = internal constant [7 x i8] c"leased\00"
@lease_phase = internal global i64 0
@lease_worker_status = internal global i64 0
declare ptr @py_str_new(ptr, i64)
declare ptr @py_list_new(i64)
declare void @py_decref(ptr)
declare void @pcc_gc_store_root(ptr, ptr)
declare ptr @pcc_gc_load_ptr(ptr, ptr)
declare void @pcc_gc_frame_enter_lifo(ptr, ptr)
declare void @pcc_gc_frame_leave_lifo(ptr)
declare i64 @pcc_gc_backend()
declare i64 @pcc_gc_collect(i32)
declare i64 @pcc_gc_foreign_lease_acquire(ptr)
declare i64 @pcc_gc_foreign_lease_release(ptr, i64)
declare i64 @pcc_gc_object_is_address_pinned(ptr)
declare void @pcc_gc_pin(ptr)
declare void @pcc_gc_unpin(ptr)
declare ptr @pcc_gc_generational_oldify_copy(ptr)
declare i64 @pcc_gc_backend4_relocation_set_add(ptr)
declare i64 @pcc_gc_object_known_size(ptr)
declare ptr @pcc_gc_relocate_copy(ptr, i64)
declare void @pcc_py_gc_minor_graph_lock()
declare void @pcc_py_gc_minor_graph_unlock()
declare i64 @pcc_threads_enabled()
declare i64 @pcc_thread_start(ptr, ptr, ptr)
declare i64 @pcc_thread_join(ptr, ptr)
declare void @pcc_thread_safepoint()
declare i64 @pcc_stop_the_world()
declare void @pcc_resume_world()

define ptr @lease_try_move(ptr %slot, i64 %backend) {
entry:
  %is3 = icmp eq i64 %backend, 3
  br i1 %is3, label %gc3, label %gc4
gc3:
  call void @pcc_py_gc_minor_graph_lock()
  %a = call ptr @pcc_gc_load_ptr(ptr null, ptr %slot)
  %m3 = call ptr @pcc_gc_generational_oldify_copy(ptr %a)
  call void @pcc_py_gc_minor_graph_unlock()
  ret ptr %m3
gc4:
  call void @pcc_py_gc_minor_graph_lock()
  %b = call ptr @pcc_gc_load_ptr(ptr null, ptr %slot)
  %admitted = call i64 @pcc_gc_backend4_relocation_set_add(ptr %b)
  %size = call i64 @pcc_gc_object_known_size(ptr %b)
  call void @pcc_py_gc_minor_graph_unlock()
  %m4 = call ptr @pcc_gc_relocate_copy(ptr %b, i64 %size)
  ret ptr %m4
}

define ptr @lease_thread(ptr %slot) {
entry:
  store atomic i64 1, ptr @lease_phase release, align 8
  br label %await_go
await_go:
  call void @pcc_thread_safepoint()
  %phase = load atomic i64, ptr @lease_phase acquire, align 8
  %go = icmp eq i64 %phase, 2
  br i1 %go, label %acquire, label %await_go
acquire:
  store atomic i64 3, ptr @lease_phase release, align 8
  %token = call i64 @pcc_gc_foreign_lease_acquire(ptr %slot)
  store atomic i64 %token, ptr @lease_worker_status release, align 8
  store atomic i64 4, ptr @lease_phase release, align 8
  br label %await_release
await_release:
  call void @pcc_thread_safepoint()
  %phase2 = load atomic i64, ptr @lease_phase acquire, align 8
  %done = icmp eq i64 %phase2, 5
  br i1 %done, label %release, label %await_release
release:
  %status = call i64 @pcc_gc_foreign_lease_release(ptr %slot, i64 %token)
  store atomic i64 %status, ptr @lease_worker_status release, align 8
  ret ptr null
}

define i64 @test_foreign_lease_movement(ptr %unused, i64 %threaded) {
entry:
  %slot = alloca ptr
  %handle = alloca ptr
  %thread_result = alloca ptr
  store ptr null, ptr %slot
  store ptr null, ptr %handle
  store ptr null, ptr %thread_result
  %backend = call i64 @pcc_gc_backend()
  %is3 = icmp eq i64 %backend, 3
  br i1 %is3, label %new_string, label %new_list
new_string:
  %text = call ptr @py_str_new(ptr @lease_text, i64 6)
  br label %root
new_list:
  %list = call ptr @py_list_new(i64 0)
  br label %root
root:
  %obj = phi ptr [%text, %new_string], [%list, %new_list]
  call void @pcc_gc_store_root(ptr %slot, ptr %obj)
  call void @pcc_gc_frame_enter_lifo(ptr @lease_map, ptr %slot)
  call void @py_decref(ptr %obj)
  %token = call i64 @pcc_gc_foreign_lease_acquire(ptr %slot)
  %counted = icmp eq i64 %token, 1
  br i1 %counted, label %nested, label %bad_count
nested:
  %is_threaded = icmp ne i64 %threaded, 0
  br i1 %is_threaded, label %thread_start, label %same_thread
same_thread:
  %second = call i64 @pcc_gc_foreign_lease_acquire(ptr %slot)
  %twice = icmp eq i64 %second, 1
  br i1 %twice, label %check_pin, label %bad_count
thread_start:
  %enabled = call i64 @pcc_threads_enabled()
  %can_thread = icmp ne i64 %enabled, 0
  br i1 %can_thread, label %start, label %bad_thread
start:
  %started = call i64 @pcc_thread_start(ptr %handle, ptr @lease_thread, ptr %slot)
  %start_ok = icmp eq i64 %started, 0
  br i1 %start_ok, label %await_started, label %bad_thread
await_started:
  call void @pcc_thread_safepoint()
  %p1 = load atomic i64, ptr @lease_phase acquire, align 8
  %worker_ready = icmp eq i64 %p1, 1
  br i1 %worker_ready, label %hold_lock, label %await_started
hold_lock:
  call void @pcc_py_gc_minor_graph_lock()
  store atomic i64 2, ptr @lease_phase release, align 8
  br label %await_contender
await_contender:
  %p2 = load atomic i64, ptr @lease_phase acquire, align 8
  %contending = icmp eq i64 %p2, 3
  br i1 %contending, label %drop_lock, label %await_contender
drop_lock:
  call void @pcc_py_gc_minor_graph_unlock()
  br label %await_acquired
await_acquired:
  call void @pcc_thread_safepoint()
  %p3 = load atomic i64, ptr @lease_phase acquire, align 8
  %acquired = icmp eq i64 %p3, 4
  br i1 %acquired, label %check_thread_token, label %await_acquired
check_thread_token:
  %worker_token = load atomic i64, ptr @lease_worker_status acquire, align 8
  %worker_counted = icmp eq i64 %worker_token, 1
  br i1 %worker_counted, label %check_pin, label %bad_thread
check_pin:
  %current = call ptr @pcc_gc_load_ptr(ptr null, ptr %slot)
  call void @pcc_gc_pin(ptr %current)
  call void @pcc_gc_unpin(ptr %current)
  %attempt1 = call ptr @lease_try_move(ptr %slot, i64 %backend)
  %refused1 = icmp eq ptr %attempt1, null
  br i1 %refused1, label %release_first, label %bad_move
release_first:
  %released1 = call i64 @pcc_gc_foreign_lease_release(ptr %slot, i64 %token)
  %ok1 = icmp eq i64 %released1, 0
  br i1 %ok1, label %collect, label %bad_release
collect:
  ; Exercise a real STW handshake without promoting a pinned GC3 nursery
  ; object in place; the separate callback regression performs collection.
  %stopped = call i64 @pcc_stop_the_world()
  call void @pcc_resume_world()
  %attempt2 = call ptr @lease_try_move(ptr %slot, i64 %backend)
  %refused2 = icmp eq ptr %attempt2, null
  br i1 %refused2, label %release_last, label %bad_move
release_last:
  br i1 %is_threaded, label %join, label %release_second
release_second:
  %released2 = call i64 @pcc_gc_foreign_lease_release(ptr %slot, i64 1)
  %ok2 = icmp eq i64 %released2, 0
  br i1 %ok2, label %move, label %bad_release
join:
  store atomic i64 5, ptr @lease_phase release, align 8
  %thread = load ptr, ptr %handle
  %joined = call i64 @pcc_thread_join(ptr %thread, ptr %thread_result)
  %worker_release = load atomic i64, ptr @lease_worker_status acquire, align 8
  %jok = icmp eq i64 %joined, 0
  %wok = icmp eq i64 %worker_release, 0
  %both = and i1 %jok, %wok
  br i1 %both, label %move, label %bad_thread
move:
  %before = call ptr @pcc_gc_load_ptr(ptr null, ptr %slot)
  %moved = call ptr @lease_try_move(ptr %slot, i64 %backend)
  %nonnull = icmp ne ptr %moved, null
  %different = icmp ne ptr %moved, %before
  %changed = and i1 %nonnull, %different
  br i1 %changed, label %resolve, label %bad_after
resolve:
  %after = call ptr @pcc_gc_load_ptr(ptr null, ptr %slot)
  %resolved = icmp eq ptr %after, %moved
  br i1 %resolved, label %finish, label %bad_after
finish:
  call void @pcc_gc_store_root(ptr %slot, ptr null)
  call void @pcc_gc_frame_leave_lifo(ptr %slot)
  ret i64 0
bad_count:
  ret i64 -1
bad_move:
  ret i64 -2
bad_release:
  ret i64 -3
bad_after:
  ret i64 -4
bad_thread:
  ret i64 -5
}
'''


def test_movement_and_threaded_probe_emit_verified_owned_object():
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from tests.owned_ir_validation import verify_ir_text
    target=host_target_triple(); text='target triple = "'+target+'"\n'+MOVEMENT_IR
    verify_ir_text(text)
    assert emit_owned_object(text,target)


@pytest.mark.integration
@pytest.mark.parametrize("threaded", [False,True])
def test_native_gc3_gc4_foreign_address_movement_and_overlap(tmp_path, request, monkeypatch, threaded):
    import os,subprocess
    # The contending lease runs on a second thread only in the threaded
    # runtime; the default archive reports pcc_threads_enabled() == 0.
    if threaded:
        monkeypatch.setenv("PCC_WITH_THREADS", "1")
    pcc_runtime_archive=request.getfixturevalue("threaded_pcc_runtime_archive" if threaded else "pcc_runtime_archive")
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    from pcc.frontends.python.pipeline_targets import host_target_triple
    target=host_target_triple(); helper=tmp_path/'lease_movement.o'
    helper.write_bytes(emit_owned_object('target triple = "'+target+'"\n'+MOVEMENT_IR,target))
    source=tmp_path/'lease_movement.py'; output=tmp_path/'lease_movement'
    source.write_text('''from pcc.extern import extern, c_ptr, c_int64
from pcc.unsafe import null
probe = extern("test_foreign_lease_movement", (c_ptr, c_int64), c_int64)
def check():
    print(probe(null(), '''+str(int(threaded))+'''))
check()
''')
    compile_python(str(source),str(output),backend='self',libpython_mode='off',ir_scaffold_mode='on',runtime_archive=str(pcc_runtime_archive),object_inputs=(str(helper),))
    for backend in (3,4):
        result=subprocess.run([str(output)],capture_output=True,text=True,timeout=60,
            env=dict(os.environ,PCC_GC_BACKEND=str(backend),PCC_GC_MINOR_ALLOC_MAX='4096',PATH='',PCC_HOST_PYTHON='/nonexistent/host-python'))
        (tmp_path/('gc'+str(backend)+'.stdout')).write_text(result.stdout)
        (tmp_path/('gc'+str(backend)+'.stderr')).write_text(result.stderr)
        assert result.returncode == 0 and result.stderr == '' and result.stdout == '0\n', (backend,threaded,result.returncode,result.stdout,result.stderr)


def test_underflow_cannot_consume_another_owners_active_guard():
    m=Model(); first=m.object(); second=m.object(2000)
    assert m.acquire(second) == 1
    before=dict(m.memory)
    assert m.release(first,1) == -3
    assert m.memory == before
    assert m.release(second,1) == 0


def test_no_node_guard_release_never_decrements_a_newly_registered_node():
    m=Model(0); slot=m.object(node=False)
    assert m.acquire(slot) == 2
    m.nodes[1000]=11000; m.store(11000,80,5)
    assert m.release(slot,2) == 0
    assert m.count() == 5 and m.active() == 0


def test_direct_free_and_note_freeing_leave_lease_metadata_untouched():
    m=Model(4); slot=m.object(); m.acquire(slot)
    m.ns.update(_init_config=lambda:4,pcc_gc_pointer_is_managed=lambda p:1,
        _object_graph_lock=m.lock,_object_graph_unlock=m.unlock,
        stack_alloc=lambda n:90000)
    m.store("pcc_gc_config_initialized",0,1)
    _functions(RUNTIME / "py_gc_backend.py", {"pcc_gc_free_object_memory","pcc_gc_note_object_freeing"},m.ns)
    m.ns["pcc_gc_note_object_freeing"](1000)
    m.ns["pcc_gc_free_object_memory"](1000)
    assert m.count() == 1 and m.nodes[1000] == 11000
    assert m.load(11000,32) == 0 and m.load(1000,12) == 0


def test_node_pool_never_reuses_a_corrupt_live_lease():
    m=Model(); ns=_node_functions(m)
    m.store("pcc_gc_object_node_free_head",0,100000)
    m.store("pcc_gc_object_node_free_count",0,1)
    m.store(100000,80,1)
    with pytest.raises(RuntimeError): ns["pcc_gc_object_node_alloc"]()
    assert m.load(100000,80) == 1


def test_allocated_immortal_no_node_needs_mode_guard():
    m=Model(0); slot=m.object(node=False,flags=1,tag=5,proven=True)
    assert m.acquire(slot) == 2
    assert m.active() == 1
    assert _mode_setter(m)(4) == -1
    assert m.release(slot,2) == 0
