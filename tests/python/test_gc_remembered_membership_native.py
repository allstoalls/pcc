"""Bounded native index-object proof; host pcc plus owned self assembly.

The host C compiler only assembles/links the test harness. This does not build a
runtime archive, run pcc1, qualify GC4 end to end, or measure application speed.
"""

from pathlib import Path
import shutil
import subprocess

from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.frontends.python import pipeline
from pcc.frontends.python.pipeline_targets import host_target_triple


ROOT = Path(__file__).resolve().parents[2]


def test_remembered_membership_real_self_emitted_index_object(tmp_path):
    compiler = shutil.which("cc")
    assert compiler, "native index-object test needs the host C harness linker"
    source = ROOT / "pcc/runtime/py/freestanding_gc_index_table.py"
    ir = tmp_path / "gc_index.ll"
    pipeline.compile_python(
        str(source), str(ir), emit_llvm_only=True, libpython_mode="off",
        python_library=True, target_triple=host_target_triple(),
    )
    assembly = tmp_path / "gc_index.s"
    assembly.write_text(emit_self_asm(ir.read_text(encoding="utf-8")), encoding="utf-8")
    harness = tmp_path / "remembered.c"
    harness.write_text(r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void pcc_gc_remembered_member_note(void *);
int64_t pcc_gc_remembered_member_maybe(void *);
void pcc_gc_remembered_member_require(void *);
void pcc_gc_remembered_member_forget(void *);
void pcc_gc_remembered_member_clear(void);
extern int64_t pcc_py_gc_remembered_member_cap;
extern int64_t pcc_py_gc_remembered_member_count;
extern int64_t pcc_py_gc_remembered_member_incomplete;
extern int64_t pcc_py_gc_remembered_member_busy;
static int fail_allocation, callback_site, callback_operation, callback_calls;
static int allocator_calls;
static uint64_t keys[200];
/* Deterministic native allocator doubles, not the production allocator. */
static union { max_align_t align; unsigned char bytes[1 << 20]; } arena;
static size_t arena_used, block_count;
static void *block_pointer[2048];
static size_t block_size[2048];
static int block_live[2048];
#define CHECK(expression) do { if (!(expression)) { \
    fprintf(stderr, "line %d: %s\n", __LINE__, #expression); exit(1); \
} } while (0)

static void callback(int site) {
    if (callback_site != site) return;
    callback_site = 0;
    callback_calls++;
    if (callback_operation == 6) {
        pcc_gc_remembered_member_note(&keys[199]);
        return;
    }
    CHECK(pcc_py_gc_remembered_member_busy == 1);
    CHECK(pcc_gc_remembered_member_maybe(&keys[199]) == 1);
    if (callback_operation == 1) pcc_gc_remembered_member_note(&keys[199]);
    if (callback_operation == 2) pcc_gc_remembered_member_forget(&keys[199]);
    if (callback_operation == 3) pcc_gc_remembered_member_clear();
    if (callback_operation == 4) pcc_gc_remembered_member_require(&keys[199]);
}

void *calloc(size_t count, size_t size) {
    allocator_calls++;
    callback(1);
    if (fail_allocation) return NULL;
    CHECK(size == 0 || count <= SIZE_MAX / size);
    size_t bytes = count * size;
    size_t align = _Alignof(max_align_t);
    size_t start = (arena_used + align - 1) / align * align;
    CHECK(start <= sizeof(arena.bytes) && bytes <= sizeof(arena.bytes) - start);
    CHECK(block_count < 2048);
    void *ptr = &arena.bytes[start];
    memset(ptr, 0, bytes);
    block_pointer[block_count] = ptr;
    block_size[block_count] = bytes;
    block_live[block_count++] = 1;
    arena_used = start + bytes;
    return ptr;
}
void free(void *ptr) {
    allocator_calls++;
    uintptr_t address = (uintptr_t)ptr;
    uintptr_t start = (uintptr_t)&arena.bytes[0];
    if (address >= start && address < start + sizeof(arena.bytes)) {
        size_t index = 0;
        while (index < block_count && block_pointer[index] != ptr) index++;
        CHECK(index < block_count && block_live[index]);
        memset(ptr, 0xa5, block_size[index]);
        block_live[index] = 0;
    }
    callback(2);
}
void *pcc_allocator_object_side_slot(void *ptr, int64_t create) {
    (void)ptr; (void)create; return NULL;
}
void *pcc_allocator_granule_object_slot(void *ptr) { (void)ptr; return NULL; }

int main(void) {
    CHECK(pcc_gc_remembered_member_maybe(&keys[0]) == 0);
    for (int i = 0; i < 129; i++) {
        pcc_gc_remembered_member_note(&keys[i]);
        pcc_gc_remembered_member_note(&keys[i]);
    }
    CHECK(pcc_py_gc_remembered_member_count == 129);
    CHECK(pcc_py_gc_remembered_member_cap == 512);
    for (int i = 0; i < 129; i += 2) pcc_gc_remembered_member_forget(&keys[i]);
    for (int i = 0; i < 129; i++)
        CHECK(pcc_gc_remembered_member_maybe(&keys[i]) == (i % 2));
    pcc_gc_remembered_member_clear();
    for (int i = 0; i < 10000; i++) {
        pcc_gc_remembered_member_note(&keys[i % 200]);
        int before = allocator_calls;
        pcc_gc_remembered_member_forget(&keys[i % 200]);
        CHECK(allocator_calls == before);
        CHECK(pcc_gc_remembered_member_maybe(&keys[i % 200]) == 0);
    }
    CHECK(pcc_py_gc_remembered_member_count == 0);
    CHECK(pcc_py_gc_remembered_member_cap == 256);
    pcc_gc_remembered_member_clear();

    for (int growth = 0; growth <= 1; growth++) {
        if (growth) for (int i = 0; i < 128; i++) pcc_gc_remembered_member_note(&keys[i]);
        fail_allocation = 1;
        pcc_gc_remembered_member_note(&keys[128]);
        fail_allocation = 0;
        CHECK(pcc_py_gc_remembered_member_incomplete == 1);
        pcc_gc_remembered_member_note(&keys[129]);
        CHECK(pcc_gc_remembered_member_maybe(&keys[199]) == 1);
        pcc_gc_remembered_member_clear();
        CHECK(pcc_gc_remembered_member_maybe(&keys[199]) == 0);
    }
    for (int site = 1; site <= 2; site++) {
        for (int operation = 0; operation <= 4; operation++) {
            if (site == 2) for (int i = 0; i < 128; i++) pcc_gc_remembered_member_note(&keys[i]);
            callback_site = site; callback_operation = operation;
            int before = callback_calls;
            pcc_gc_remembered_member_note(&keys[128]);
            CHECK(callback_calls == before + 1);
            CHECK(pcc_py_gc_remembered_member_busy == 0);
            CHECK(pcc_py_gc_remembered_member_incomplete == (operation != 0));
            CHECK(pcc_gc_remembered_member_maybe(&keys[128]) == 1);
            if (operation) CHECK(pcc_gc_remembered_member_maybe(&keys[199]) == 1);
            pcc_gc_remembered_member_clear();
        }
    }
    pcc_gc_remembered_member_note(&keys[0]);
    callback_site = 2; callback_operation = 6;
    pcc_gc_remembered_member_clear();
    CHECK(pcc_gc_remembered_member_maybe(&keys[199]) == 1);
    CHECK(pcc_gc_remembered_member_maybe(&keys[0]) == 0);
    CHECK(pcc_py_gc_remembered_member_incomplete == 0);
    pcc_gc_remembered_member_clear();
    pcc_gc_remembered_member_note(&keys[0]);
    callback_site = 2; callback_operation = 6; fail_allocation = 1;
    pcc_gc_remembered_member_clear();
    fail_allocation = 0;
    CHECK(pcc_py_gc_remembered_member_incomplete == 1);
    CHECK(pcc_gc_remembered_member_maybe(&keys[199]) == 1);
    pcc_gc_remembered_member_clear();
    pcc_gc_remembered_member_require(&keys[0]);
    CHECK(pcc_gc_remembered_member_maybe(&keys[0]) == 1);
    CHECK(pcc_py_gc_remembered_member_incomplete == 1);
    pcc_gc_remembered_member_clear();
    puts("remembered-membership-native-ok");
    return 0;
}
''', encoding="utf-8")
    binary = tmp_path / "remembered"
    built = subprocess.run(
        [compiler, "-std=c11", "-fno-builtin", str(assembly), str(harness),
         "-o", str(binary)],
        capture_output=True, text=True, timeout=30,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "remembered-membership-native-ok\n"
