"""The primary tracked-object index keeps object cells in slab side arrays.

Tracking and untracking every container used to insert into and remove from
one global pointer hash.  An exact object-family allocator cell now keeps its
tracked node in a per-slab side word instead, and only keys the object
allocator does not own (static storage, large or raw allocations) still use
the hash table.  This drives the real allocator and tracking objects of the
production archive through both provenances.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"

HARNESS = r'''
    #define _GNU_SOURCE
    #include "py_internal.h"
    #include <sys/mman.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <stdlib.h>

    extern void *pcc_allocator_object_side_slot(void *ptr, int64_t create);
    extern int64_t pcc_py_gc_primary_count;

    typedef union {
        uint64_t align[2];
        unsigned char bytes[16];
    } ObjectStorage;

    static ObjectStorage foreign;

    static void *side_word(void *object) {
        void **slot = pcc_allocator_object_side_slot(object, 0);
        return slot == NULL ? (void *)-1 : *slot;
    }

    int main(void) {
        if (pcc_gc_set_backend(atoi(getenv("PCC_GC_BACKEND"))) != 0) return 10;
        py_gc_init();
        void *base = mmap(NULL, 65536, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANON, -1, 0);
        if (base == MAP_FAILED) return 1;
        if (pcc_gc_granule_register_slab(base, 1, 112) != 1) return 2;
        PyObject *first = (PyObject *)((char *)base + 48);
        PyObject *middle = (PyObject *)((char *)base + 48 + 112);
        PyObject *last = (PyObject *)((char *)base + 48 + 584 * 112);
        void *interior = (char *)first + 16;

        /* A slab has no side array until one of its cells is tracked. */
        if (pcc_allocator_object_side_slot(first, 0) != NULL) return 3;
        int64_t hashed = pcc_py_gc_primary_count;
        int64_t tracked = py_gc_get_count(0);

        py_gc_track(first);
        py_gc_track(last);
        py_gc_track(first);
        PyGcNode *first_node = py_gc_index_find(first);
        PyGcNode *last_node = py_gc_index_find(last);
        if (first_node == NULL || last_node == NULL) return 4;
        if (*(void **)first_node != first || *(void **)last_node != last) return 5;
        if (side_word(first) != first_node || side_word(last) != last_node) {
            return 6;
        }
        if (side_word(middle) != NULL || py_gc_index_find(middle) != NULL) {
            return 7;
        }
        if (pcc_py_gc_primary_count != hashed) return 8;
        if (py_gc_get_count(0) != tracked + 2) return 9;

        /* Interior pointers never alias a cell's side word. */
        if (pcc_allocator_object_side_slot(interior, 1) != NULL) return 11;
        if (py_gc_index_find(interior) != NULL) return 12;

        /* Storage the object allocator does not own stays in the hash. */
        py_gc_track((PyObject *)foreign.bytes);
        if (pcc_allocator_object_side_slot(foreign.bytes, 1) != NULL) return 13;
        if (pcc_py_gc_primary_count != hashed + 1) return 14;
        if (py_gc_index_find((PyObject *)foreign.bytes) == NULL) return 15;
        py_gc_untrack((PyObject *)foreign.bytes);
        if (pcc_py_gc_primary_count != hashed) return 16;

        py_gc_untrack(first);
        if (py_gc_index_find(first) != NULL || side_word(first) != NULL) return 17;
        if (py_gc_is_tracked(first) != 0) return 18;
        if (py_gc_index_find(last) != last_node) return 19;
        py_gc_untrack(last);
        if (py_gc_get_count(0) != tracked) return 20;
        printf("side-slots-ok\n");
        return 0;
    }
'''


def test_object_cells_use_side_words_and_foreign_keys_use_the_hash(
    tmp_path: Path, pcc_runtime_archive: Path,
) -> None:
    source = tmp_path / "side_slots.c"
    output = tmp_path / "side_slots"
    source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
    built = subprocess.run(
        [os.environ.get("CC", "cc"), "-std=c11", "-pthread",
         f"-I{RUNTIME_DIR / 'include'}", f"-I{RUNTIME_DIR / 'src'}",
         str(source), str(pcc_runtime_archive), "-lm", "-o", str(output)],
        capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    for backend in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=10,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == "side-slots-ok\n"
