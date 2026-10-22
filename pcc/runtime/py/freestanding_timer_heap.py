"""Freestanding pcc-Python ABI implementation of the vthread timer heap."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import (
    calloc,
    free,
    load_i8,
    load_i64,
    load_ptr,
    logical_shift_right_i64,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    realloc,
    store_i8,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True

@c_abi_export("pcc_timer_heap_node_addr")
def _node_addr(nodes, index: i64):
    return ptr_add(nodes, index * 24)


@c_abi_export("pcc_timer_heap_slot_addr")
def _slot_addr(live, index: i64):
    return ptr_add(live, index * 32)


@c_abi_export("pcc_timer_heap_live_probe")
def _live_probe(heap, timer_id: i64) -> i64:
    cap: i64 = load_i64(heap, 48)
    mask: i64 = cap - 1
    index: i64 = timer_id & mask
    first_free: i64 = -1
    live = load_ptr(heap, 24)
    done: i64 = 0
    found: i64 = 0
    while done == 0:
        slot = _slot_addr(live, index)
        state: i64 = load_i8(slot, 24)
        if state == 0:
            if first_free >= 0:
                index = first_free
            done: i64 = 1
        elif state == 2:
            if first_free < 0:
                first_free = index
        elif load_i64(slot, 0) == timer_id:
            found: i64 = 1
            done: i64 = 1
        if done == 0:
            index = (index + 1) & mask
    return index * 2 + found


@c_abi_export("pcc_timer_heap_live_rehash")
def _live_rehash(heap, new_cap: i64) -> i64:
    old = load_ptr(heap, 24)
    old_cap: i64 = load_i64(heap, 48)
    fresh = calloc(new_cap, 32)
    if ptr_is_null(fresh):
        return -1
    store_ptr(heap, 24, fresh)
    store_i64(heap, 48, new_cap)
    store_i64(heap, 40, 0)
    store_i64(heap, 32, 0)
    if not ptr_is_null(old):
        i: i64 = 0
        while i < old_cap:
            old_slot = _slot_addr(old, i)
            if load_i8(old_slot, 24) == 1:
                timer_id: i64 = load_i64(old_slot, 0)
                packed: i64 = _live_probe(heap, timer_id)
                slot = _slot_addr(fresh, logical_shift_right_i64(packed, 1))
                store_i64(slot, 0, timer_id)
                store_i64(slot, 8, load_i64(old_slot, 8))
                store_i64(slot, 16, load_i64(old_slot, 16))
                store_i8(slot, 24, 1)
                store_i64(heap, 40, load_i64(heap, 40) + 1)
                store_i64(heap, 32, load_i64(heap, 32) + 1)
            i = i + 1
        free(old)
    return 0


@c_abi_export("pcc_timer_heap_live_reserve")
def _live_reserve(heap) -> i64:
    used: i64 = load_i64(heap, 40)
    cap: i64 = load_i64(heap, 48)
    if (used + 1) * 10 <= cap * 7:
        return 0
    new_cap: i64 = cap
    count: i64 = load_i64(heap, 32)
    if (count + 1) * 10 > cap * 5:
        new_cap = cap * 2
    return _live_rehash(heap, new_cap)


@c_abi_export("pcc_timer_heap_reserve")
def _heap_reserve(heap) -> i64:
    length: i64 = load_i64(heap, 8)
    cap: i64 = load_i64(heap, 16)
    if length < cap:
        return 0
    new_cap: i64 = 8
    if cap > 0:
        new_cap = cap * 2
    grown = realloc(load_ptr(heap, 0), new_cap * 24)
    if ptr_is_null(grown):
        return -1
    store_ptr(heap, 0, grown)
    store_i64(heap, 16, new_cap)
    return 0


@c_abi_export("pcc_timer_heap_less")
def _less(deadline_a: i64, seq_a: i64, deadline_b: i64, seq_b: i64) -> bool:
    if deadline_a < deadline_b:
        return True
    if deadline_a > deadline_b:
        return False
    return seq_a < seq_b


@c_abi_export("pcc_timer_heap_sift_up")
def _sift_up(heap, index: i64) -> None:
    nodes = load_ptr(heap, 0)
    item = _node_addr(nodes, index)
    deadline: i64 = load_i64(item, 0)
    seq: i64 = load_i64(item, 8)
    timer_id: i64 = load_i64(item, 16)
    done: i64 = 0
    while index > 0 and done == 0:
        parent_index: i64 = logical_shift_right_i64(index - 1, 1)
        parent = _node_addr(nodes, parent_index)
        if _less(deadline, seq, load_i64(parent, 0), load_i64(parent, 8)):
            target = _node_addr(nodes, index)
            store_i64(target, 0, load_i64(parent, 0))
            store_i64(target, 8, load_i64(parent, 8))
            store_i64(target, 16, load_i64(parent, 16))
            index = parent_index
        else:
            done: i64 = 1
    target = _node_addr(nodes, index)
    store_i64(target, 0, deadline)
    store_i64(target, 8, seq)
    store_i64(target, 16, timer_id)


@c_abi_export("pcc_timer_heap_sift_down")
def _sift_down(heap, index: i64) -> None:
    nodes = load_ptr(heap, 0)
    length: i64 = load_i64(heap, 8)
    item = _node_addr(nodes, index)
    deadline: i64 = load_i64(item, 0)
    seq: i64 = load_i64(item, 8)
    timer_id: i64 = load_i64(item, 16)
    done: i64 = 0
    while done == 0:
        left: i64 = index * 2 + 1
        right: i64 = left + 1
        smallest: i64 = index
        best_deadline: i64 = deadline
        best_seq: i64 = seq
        if left < length:
            left_node = _node_addr(nodes, left)
            if _less(
                load_i64(left_node, 0),
                load_i64(left_node, 8),
                best_deadline,
                best_seq,
            ):
                smallest = left
                best_deadline = load_i64(left_node, 0)
                best_seq = load_i64(left_node, 8)
        if right < length:
            right_node = _node_addr(nodes, right)
            if _less(
                load_i64(right_node, 0),
                load_i64(right_node, 8),
                best_deadline,
                best_seq,
            ):
                smallest = right
        if smallest == index:
            done: i64 = 1
        else:
            source = _node_addr(nodes, smallest)
            target = _node_addr(nodes, index)
            store_i64(target, 0, load_i64(source, 0))
            store_i64(target, 8, load_i64(source, 8))
            store_i64(target, 16, load_i64(source, 16))
            index = smallest
    target = _node_addr(nodes, index)
    store_i64(target, 0, deadline)
    store_i64(target, 8, seq)
    store_i64(target, 16, timer_id)


@c_abi_export("pcc_timer_heap_pop_root")
def _pop_root(heap) -> None:
    length: i64 = load_i64(heap, 8) - 1
    store_i64(heap, 8, length)
    if length > 0:
        nodes = load_ptr(heap, 0)
        last = _node_addr(nodes, length)
        root = _node_addr(nodes, 0)
        store_i64(root, 0, load_i64(last, 0))
        store_i64(root, 8, load_i64(last, 8))
        store_i64(root, 16, load_i64(last, 16))
        _sift_down(heap, 0)


@c_abi_export("pcc_timer_heap_root_is_stale")
def _root_is_stale(heap) -> bool:
    root = load_ptr(heap, 0)
    timer_id: i64 = load_i64(root, 16)
    packed: i64 = _live_probe(heap, timer_id)
    if packed & 1 == 0:
        return True
    slot = _slot_addr(load_ptr(heap, 24), logical_shift_right_i64(packed, 1))
    return load_i64(slot, 8) != load_i64(root, 0) or load_i64(
        slot, 16
    ) != load_i64(root, 8)


@c_abi_export("pcc_timer_heap_init")
def pcc_timer_heap_init(heap) -> i64:
    if ptr_is_null(heap):
        return -1
    memset(heap, 0, 64)
    live = calloc(8, 32)
    if ptr_is_null(live):
        return -1
    store_ptr(heap, 24, live)
    store_i64(heap, 48, 8)
    return 0


@c_abi_export("pcc_timer_heap_dispose")
def pcc_timer_heap_dispose(heap) -> None:
    if ptr_is_null(heap):
        return
    free(load_ptr(heap, 0))
    free(load_ptr(heap, 24))
    memset(heap, 0, 64)


@c_abi_export("pcc_timer_heap_insert")
def pcc_timer_heap_insert(heap, deadline: i64, timer_id: i64) -> i64:
    if ptr_is_null(heap):
        return -1
    if _live_reserve(heap) != 0 or _heap_reserve(heap) != 0:
        return -1
    seq: i64 = load_i64(heap, 56) + 1
    store_i64(heap, 56, seq)
    packed: i64 = _live_probe(heap, timer_id)
    index: i64 = logical_shift_right_i64(packed, 1)
    found: i64 = packed & 1
    slot = _slot_addr(load_ptr(heap, 24), index)
    if found != 0:
        store_i64(slot, 8, deadline)
        store_i64(slot, 16, seq)
    else:
        store_i64(slot, 0, timer_id)
        store_i64(slot, 8, deadline)
        store_i64(slot, 16, seq)
        store_i8(slot, 24, 1)
        store_i64(heap, 40, load_i64(heap, 40) + 1)
        store_i64(heap, 32, load_i64(heap, 32) + 1)
    heap_index: i64 = load_i64(heap, 8)
    node = _node_addr(load_ptr(heap, 0), heap_index)
    store_i64(node, 0, deadline)
    store_i64(node, 8, seq)
    store_i64(node, 16, timer_id)
    store_i64(heap, 8, heap_index + 1)
    _sift_up(heap, heap_index)
    return 0


@c_abi_export("pcc_timer_heap_cancel")
def pcc_timer_heap_cancel(heap, timer_id: i64) -> i64:
    if ptr_is_null(heap) or load_i64(heap, 32) == 0:
        return 0
    packed: i64 = _live_probe(heap, timer_id)
    if packed & 1 == 0:
        return 0
    slot = _slot_addr(load_ptr(heap, 24), logical_shift_right_i64(packed, 1))
    store_i8(slot, 24, 2)
    store_i64(heap, 32, load_i64(heap, 32) - 1)
    return 1


@c_abi_export("pcc_timer_heap_is_registered")
def pcc_timer_heap_is_registered(heap, timer_id: i64) -> i64:
    if ptr_is_null(heap) or load_i64(heap, 32) == 0:
        return 0
    return _live_probe(heap, timer_id) & 1


@c_abi_export("pcc_timer_heap_size")
def pcc_timer_heap_size(heap) -> i64:
    if ptr_is_null(heap):
        return 0
    return load_i64(heap, 32)


@c_abi_export("pcc_timer_heap_peek")
def pcc_timer_heap_peek(heap, out_deadline) -> i64:
    if ptr_is_null(heap):
        return 0
    result: i64 = 0
    done: i64 = 0
    while load_i64(heap, 8) > 0 and done == 0:
        if _root_is_stale(heap):
            _pop_root(heap)
        else:
            if not ptr_is_null(out_deadline):
                store_i64(out_deadline, 0, load_i64(load_ptr(heap, 0), 0))
            result: i64 = 1
            done: i64 = 1
    return result


@c_abi_export("pcc_timer_heap_pop_expired")
def pcc_timer_heap_pop_expired(
    heap, now: i64, out_ids, out_cap: i64
) -> i64:
    if ptr_is_null(heap):
        return 0
    drained: i64 = 0
    done: i64 = 0
    while load_i64(heap, 8) > 0 and done == 0:
        if _root_is_stale(heap):
            _pop_root(heap)
        else:
            root = load_ptr(heap, 0)
            if load_i64(root, 0) > now:
                done: i64 = 1
            elif not ptr_is_null(out_ids) and drained >= out_cap:
                done: i64 = 1
            else:
                timer_id: i64 = load_i64(root, 16)
                packed: i64 = _live_probe(heap, timer_id)
                if packed & 1 != 0:
                    slot = _slot_addr(
                        load_ptr(heap, 24), logical_shift_right_i64(packed, 1)
                    )
                    store_i8(slot, 24, 2)
                    store_i64(
                        heap, 32, load_i64(heap, 32) - 1
                    )
                _pop_root(heap)
                if not ptr_is_null(out_ids):
                    store_i64(out_ids, drained * 8, timer_id)
                drained = drained + 1
    return drained
