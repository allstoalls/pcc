"""Actual tuple setters retain ownership while avoiding no-op completion scans."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime"
FRESH_ALLOC = 16384


class _PortMemory:
    """Unsafe/extern oracle; tuple construction and cycle checks stay real."""

    def __init__(self, module, backend, initialized):
        self.module = module
        self.backend = backend
        self.initialized = initialized
        self.memory = {}
        self.next_address = 4096
        self.events = []
        self.publications = []
        self.reads = []

    def allocate(self, size, tag, flags):
        address = self.next_address
        self.next_address += size + 64
        self.memory[address] = 1
        self.memory[address + 8] = tag
        # Backend1/2 tracking also uses FRESH_ALLOC as allocation grace;
        # only GC4's bit is cleared by constructor publication.
        self.memory[address + 12] = flags | (FRESH_ALLOC if self.backend in (1, 2, 4) else 0)
        return address

    def read_i32(self, address, offset):
        if address == -1:
            return int(self.initialized)
        if address == -2:
            return self.backend
        return self.memory.get(address + offset, 0)

    def get_backend(self):
        self.initialized = True
        return self.backend

    def incref(self, item):
        self.events.append(("incref", item))
        if item:
            self.memory[item] += 1

    def store(self, owner, slot, item):
        self.events.append(("barrier", owner, slot, item))
        self.get_backend()
        self.incref(item)
        self.memory[slot] = item

    def load(self, owner, slot):
        self.reads.append((owner, slot))
        return self.memory.get(slot, 0)

    def track(self, owner):
        self.events.append(("track", owner))
        self.memory[owner + 12] |= self.module.PY_FLAG_GC_TRACKED

    def publish(self, owner):
        length = self.memory[owner + self.module.PYTUPLEOBJECT_LEN_OFFSET]
        snapshot = tuple(
            self.memory.get(owner + self.module.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8, 0)
            for index in range(length)
        )
        self.publications.append((owner, snapshot))
        if self.backend == 4:
            self.memory[owner + 12] &= ~FRESH_ALLOC

    def memset(self, address, value, size):
        assert value == 0 and size % 8 == 0
        for offset in range(0, size, 8):
            self.memory[address + offset] = 0

    def install(self, monkeypatch):
        primitives = {
            "ptr_is_null": lambda value: value == 0,
            "null": lambda: 0,
            "ptr_add": lambda value, offset: value + offset,
            "is_tagged_int": lambda value: False,
            "global_addr": lambda name: {
                "pcc_gc_config_initialized": -1, "pcc_gc_backend_selected": -2,
            }[name],
            "load_i32": self.read_i32,
            "load_i64": lambda address, offset: self.memory.get(address + offset, 0),
            "store_i64": lambda address, offset, value: self.memory.__setitem__(address + offset, value),
            "store_ptr": lambda address, offset, value: self.memory.__setitem__(address + offset, value),
            "memset": self.memset,
            "py_incref": self.incref,
            "py_gc_track": self.track,
            "pcc_gc_alloc": self.allocate,
            "pcc_gc_backend": self.get_backend,
            "pcc_gc_store_ptr": self.store,
            "pcc_gc_load_ptr": self.load,
            "pcc_gc_pointer_is_managed": lambda address: address in self.memory,
            "pcc_gc_publish_initialized": self.publish,
        }
        for name, value in primitives.items():
            monkeypatch.setattr(self.module, name, value)


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("initialized", [True, False])
def test_python_port_completion_reads_and_publication_order(monkeypatch, backend, initialized):
    from pcc.runtime.py import py_tuple

    memory = _PortMemory(py_tuple, backend, initialized)
    memory.install(monkeypatch)
    owner = py_tuple.py_tuple_new(4)
    child = memory.allocate(24, py_tuple.PY_TYPE_LIST, 0)
    initial_fresh = bool(memory.memory[owner + 12] & FRESH_ALLOC)
    read_counts = []
    for index, value in ((2, child), (0, child), (1, 0), (3, child), (1, child)):
        before = len(memory.reads)
        py_tuple.py_tuple_set_item(owner, index, value)
        read_counts.append(len(memory.reads) - before)
        filled = all(memory.memory[owner + 24 + index * 8] for index in range(4))
        expected_fresh = not filled if backend == 4 else initial_fresh
        assert bool(memory.memory[owner + 12] & FRESH_ALLOC) == expected_fresh
        if not filled:
            assert memory.publications == []
    assert memory.memory[child] == 5
    assert memory.memory[owner + 12] & py_tuple.PY_FLAG_GC_TRACKED
    assert [event for event in memory.events if event[0] == "track"] == [("track", owner)]
    barriers = [event for event in memory.events if event[0] == "barrier"]
    assert len(barriers) == (5 if backend != 0 else int(not initialized))
    # A list child exercises real cycle tracking without nested-tuple reads.
    # GC4 probes the last slot first and scans the prefix only once it is
    # filled: stores 0-2 stop at the empty slot 3, store 3 then scans 0..1
    # (slot 1 is still NULL), and the completing store scans all four.
    assert read_counts == ([1, 1, 1, 3, 5] if backend == 4 else [0] * 5)
    assert memory.publications == ([(owner, (child,) * 4)] if backend == 4 else [])


@pytest.mark.parametrize("backend", range(5))
def test_python_port_empty_null_and_invalid_stores_keep_publication_guards(monkeypatch, backend):
    from pcc.runtime.py import py_tuple

    memory = _PortMemory(py_tuple, backend, True)
    memory.install(monkeypatch)
    empty = py_tuple.py_tuple_new(0)
    assert memory.publications == [(empty, ())]
    assert bool(memory.memory[empty + 12] & FRESH_ALLOC) == (backend in (1, 2))
    owner = py_tuple.py_tuple_new(1)
    before = (list(memory.events), list(memory.publications), dict(memory.memory))
    py_tuple.py_tuple_set_item(0, 0, 0)
    py_tuple.py_tuple_set_item(empty, 0, 0)
    py_tuple.py_tuple_set_item(owner, -1, 0)
    py_tuple.py_tuple_set_item(owner, 1, 0)
    assert (memory.events, memory.publications, memory.memory) == before
    assert memory.reads == []
    py_tuple.py_tuple_set_item(owner, 0, 0)
    assert memory.publications == [(empty, ())]
    assert len(memory.reads) == (1 if backend == 4 else 0)


def test_python_port_publication_backend_is_selected_per_call(monkeypatch):
    from pcc.runtime.py import py_tuple

    memory = _PortMemory(py_tuple, 0, True)
    memory.install(monkeypatch)
    child = memory.allocate(24, py_tuple.PY_TYPE_LIST, 0)
    first = py_tuple.py_tuple_new(1)
    py_tuple.py_tuple_set_item(first, 0, child)
    assert memory.reads == [] and memory.publications == []
    memory.backend = 4
    partial = py_tuple.py_tuple_new(2)
    py_tuple.py_tuple_set_item(partial, 0, child)
    assert len(memory.reads) == 1 and memory.publications == []
    memory.backend = 3
    other = py_tuple.py_tuple_new(1)
    py_tuple.py_tuple_set_item(other, 0, child)
    assert len(memory.reads) == 1 and memory.publications == []
    memory.backend = 4
    py_tuple.py_tuple_set_item(partial, 1, child)
    assert len(memory.reads) == 4
    assert memory.publications == [(partial, (child, child))]


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        # Sequential fill: the last slot stays empty until the final store.
        (list(range(8)), [1] * 7 + [9]),
        # Reverse fill: the last slot is filled first, so each prefix scan
        # stops at slot 0 until the final store completes the tuple.
        (list(range(7, -1, -1)), [2] * 7 + [9]),
    ],
)
def test_python_port_gc4_fill_orders_scan_in_linear_total(monkeypatch, order, expected):
    """Scanning every slot on every store made an n-item tuple O(n^2) load
    barriers under GC4; both common fill orders now stay O(1) per store."""
    from pcc.runtime.py import py_tuple

    memory = _PortMemory(py_tuple, 4, True)
    memory.install(monkeypatch)
    owner = py_tuple.py_tuple_new(8)
    child = memory.allocate(24, py_tuple.PY_TYPE_LIST, 0)
    counts = []
    for index in order:
        before = len(memory.reads)
        py_tuple.py_tuple_set_item(owner, index, child)
        counts.append(len(memory.reads) - before)
    assert counts == expected
    assert memory.publications == [(owner, (child,) * 8)]


def test_python_port_none_object_fills_a_slot_but_null_pointer_does_not(monkeypatch):
    from pcc.runtime.py import py_tuple
    from pcc.runtime.py.py_abi_constants import PY_TYPE_NONE

    memory = _PortMemory(py_tuple, 4, True)
    memory.install(monkeypatch)
    owner = py_tuple.py_tuple_new(1)
    none_object = memory.allocate(16, PY_TYPE_NONE, 0)
    py_tuple.py_tuple_set_item(owner, 0, 0)
    assert memory.publications == []
    assert memory.memory[owner + 12] & FRESH_ALLOC
    py_tuple.py_tuple_set_item(owner, 0, none_object)
    assert memory.publications == [(owner, (none_object,))]
    assert not memory.memory[owner + 12] & FRESH_ALLOC


_C_DRIVER = r"""
#include "py_internal.h"
#include <stdio.h>
#include <stdlib.h>

static int fresh(PyObject *owner) {
    return (py_header(owner)->flags & PY_FLAG_GC_FRESH_ALLOC) != 0;
}

static int selectable(PyObject *owner) {
    pcc_gc_reset_relocation_set();
    int result = (int)pcc_gc_backend4_relocation_set_add(owner);
    pcc_gc_reset_relocation_set();
    return result;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int backend = atoi(argv[1]);
    if (pcc_gc_set_backend(backend) != 0) return 3;
    PyObject *empty = py_tuple_new(0);
    PyObject *watched = py_tuple_new(4);
    PyObject *child = py_list_new(0);
    if (!empty || !watched || !child) return 4;
    if (backend == 4 && fresh(empty)) return 5;
    if (backend == 4 && selectable(empty) != 1) return 6;
    int initial_fresh = fresh(watched);
    int64_t refcount_before = py_header(child)->refcount;
    /* Invalid stores keep ownership and publication state unchanged. */
    py_tuple_set_item(NULL, 0, child);
    py_tuple_set_item(empty, 0, child);
    py_tuple_set_item(watched, -1, child);
    py_tuple_set_item(watched, 4, child);
    if (py_header(child)->refcount != refcount_before) return 7;
    int indexes[5] = {2, 0, 1, 3, 1};
    for (int step = 0; step < 5; step++) {
        py_tuple_set_item(watched, indexes[step], step == 2 ? NULL : child);
        /* GC1/2 keep their allocation-grace bit until root seeding. Only
         * GC4 clears it, once the last NULL slot is filled. */
        int expected_fresh = backend == 4 ? step < 4 : initial_fresh;
        if (fresh(watched) != expected_fresh) return 10 + step;
        if (!(py_header(watched)->flags & PY_FLAG_GC_TRACKED)) return 20 + step;
        if (backend == 4 && selectable(watched) != (step == 4)) return 30 + step;
    }
    if (py_header(child)->refcount != refcount_before + 4) return 40;
    for (int index = 0; index < 4; index++) {
        if (((PyTupleObject *)watched)->items[index] != child) return 41;
    }
    py_decref(watched);
    py_decref(child);
    py_decref(empty);
    puts("ownership-publication-ok");
    return 0;
}
"""


@pytest.fixture(scope="module")
def tuple_publication_probe(tmp_path_factory, pcc_runtime_archive):
    tmp_path = tmp_path_factory.mktemp("tuple_publication_probe")
    driver = tmp_path / "driver.c"
    driver.write_text(_C_DRIVER, encoding="utf-8")
    executable = tmp_path / "tuple_publication_probe"
    cc = os.environ.get("CC", "cc")
    linked = subprocess.run(
        [
            cc,
            "-std=c11",
            "-O0",
            f"-I{RUNTIME / 'include'}",
            f"-I{RUNTIME / 'src'}",
            str(driver),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert linked.returncode == 0, linked.stdout + linked.stderr
    return executable


@pytest.mark.parametrize("backend", range(5))
def test_tuple_completion_publication_and_relocation_eligibility(
    tuple_publication_probe, backend
):
    """The linked pcc-Python tuple setters, driven through the C ABI.

    The per-store completion-scan counts are pinned at host level by
    test_python_port_completion_reads_and_publication_order; this proves the
    compiled archive keeps the same publication, tracking, relocation and
    ownership outcomes on every collector.
    """
    result = subprocess.run(
        [str(tuple_publication_probe), str(backend)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "ownership-publication-ok\n"
