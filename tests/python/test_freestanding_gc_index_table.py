from __future__ import annotations

import subprocess
from pathlib import Path

from pcc.frontends.python import pipeline
from pcc.frontends.python.pipeline_targets import host_target_triple


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
INDEX_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_index_table.py"

PUBLIC_SYMBOLS = {
    "pcc_gc_index_slot_size",
    "py_gc_index_find",
    "py_gc_index_insert",
    "py_gc_index_remove",
    "pcc_gc_object_index_find",
    "pcc_gc_object_index_insert",
    "pcc_gc_object_index_plan_capacity",
    "pcc_gc_object_index_plan_commit",
    "pcc_gc_object_index_insert_preallocated",
    "pcc_gc_object_index_remove",
    "pcc_gc_object_index_clear",
    "pcc_gc_ptr_index_tls_pool_drain",
    "pcc_gc_managed_pointer_find_slot",
    "pcc_gc_managed_pointer_rehash",
    "pcc_gc_managed_pointer_index_contains",
    "pcc_gc_managed_pointer_index_insert",
    "pcc_gc_managed_pointer_index_remove",
    "pcc_gc_forwarding_index_find",
    "pcc_gc_forwarding_index_insert",
    "pcc_gc_forwarding_index_remove",
    "pcc_gc_forwarding_index_clear",
    "pcc_gc_forwarding_target_index_find",
    "pcc_gc_forwarding_target_index_insert",
    "pcc_gc_forwarding_target_index_upsert",
    "pcc_gc_forwarding_target_index_remove",
    "pcc_gc_forwarding_target_index_clear",
    "pcc_gc_identity_index_find",
    "pcc_gc_identity_index_insert",
    "pcc_gc_identity_index_remove",
    "pcc_gc_identity_index_clear",
    "pcc_gc_forwarding_plan_index_capacity",
    "pcc_gc_forwarding_plan_index_commit",
    "pcc_gc_forwarding_plan_index_insert",
    "pcc_gc_frame_index_find",
    "pcc_gc_frame_index_insert",
    "pcc_gc_frame_index_plan_capacity",
    "pcc_gc_frame_index_plan_commit",
    "pcc_gc_frame_index_replace",
    "pcc_gc_frame_index_replace_preallocated",
    "pcc_gc_frame_index_remove",
    "pcc_gc_frame_index_clear",
    "pcc_gc_zpage_owner_index_find",
    "pcc_gc_zpage_owner_index_insert",
    "pcc_gc_zpage_owner_index_upsert",
    "pcc_gc_zpage_owner_index_plan_capacity",
    "pcc_gc_zpage_owner_index_plan_commit",
    "pcc_gc_zpage_owner_index_upsert_preallocated",
    "pcc_gc_zpage_owner_index_remove",
    "pcc_gc_zpage_owner_index_clear",
    "pcc_gc_zpage_page_index_find",
    "pcc_gc_zpage_page_index_insert",
    "pcc_gc_zpage_page_index_upsert",
    "pcc_gc_zpage_page_index_remove",
    "pcc_gc_zpage_page_index_clear",
}


def _compile_ir(tmp_path: Path) -> Path:
    llvm_ir = tmp_path / "freestanding_gc_index_table.ll"
    pipeline.compile_python(
        str(INDEX_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
        target_triple=host_target_triple(),
    )
    return llvm_ir


def _build_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = _compile_ir(tmp_path)
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_index_table.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_index_table_" + emitter + ".o")
    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _harness_source() -> str:
    return r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int64_t pcc_gc_index_slot_size(void);
void *py_gc_index_find(void *obj);
int64_t py_gc_index_insert(void *obj, void *node);
void *py_gc_index_remove(void *obj);
void *pcc_gc_object_index_find(void *obj);
int64_t pcc_gc_object_index_insert(void *obj, void *node);
int64_t pcc_gc_object_index_plan_capacity(int64_t extra);
int64_t pcc_gc_object_index_plan_commit(
    void **prepared_slots, int64_t prepared_cap, int64_t extra
);
int64_t pcc_gc_object_index_insert_preallocated(void *obj, void *node);
void *pcc_gc_object_index_remove(void *obj);
void pcc_gc_object_index_clear(void);
void pcc_gc_ptr_index_tls_pool_drain(void);
int64_t pcc_gc_managed_pointer_index_contains(void *obj);
int64_t pcc_gc_managed_pointer_index_insert(void *obj);
int64_t pcc_gc_managed_pointer_index_remove(void *obj);

#define DECL_INDEX(prefix) \
    void *prefix##_find(void *key); \
    int64_t prefix##_insert(void *key, void *node); \
    void *prefix##_remove(void *key); \
    void prefix##_clear(void)

DECL_INDEX(pcc_gc_forwarding_index);
DECL_INDEX(pcc_gc_identity_index);
DECL_INDEX(pcc_gc_zpage_owner_index);
DECL_INDEX(pcc_gc_zpage_page_index);
DECL_INDEX(pcc_gc_frame_index);
void *pcc_gc_frame_index_replace(void *key, void *node);
int64_t pcc_gc_frame_index_plan_capacity(int64_t extra);
int64_t pcc_gc_frame_index_plan_commit(
    void **prepared_slots, int64_t prepared_cap, int64_t extra
);
void *pcc_gc_frame_index_replace_preallocated(void *key, void *node);
int64_t pcc_gc_forwarding_target_index_insert(void *key, void *node);
int64_t pcc_gc_forwarding_target_index_upsert(void *key, void *node);
void *pcc_gc_forwarding_target_index_find(void *key);
void *pcc_gc_forwarding_target_index_remove(void *key);
void pcc_gc_forwarding_target_index_clear(void);
int64_t pcc_gc_zpage_owner_index_upsert(void *key, void *node);
int64_t pcc_gc_zpage_owner_index_plan_capacity(int64_t extra);
int64_t pcc_gc_zpage_owner_index_plan_commit(
    void **prepared_slots, int64_t prepared_cap, int64_t extra
);
int64_t pcc_gc_zpage_owner_index_upsert_preallocated(
    void *key, void *node
);
int64_t pcc_gc_zpage_page_index_upsert(void *key, void *node);
int64_t pcc_gc_forwarding_plan_index_capacity(int64_t kind, int64_t extra);
int64_t pcc_gc_forwarding_plan_index_commit(
    int64_t kind, void **prepared_slots, int64_t prepared_cap, int64_t extra
);
int64_t pcc_gc_forwarding_plan_index_insert(
    int64_t kind, void *key, void *node
);

/* The primary index asks the allocator whether a key is an object cell.
 * This harness allocates none, so every key takes the hash table; linked
 * against the production archive, the allocator's definitions win. */
__attribute__((weak)) void *pcc_allocator_object_side_slot(
    void *ptr, int64_t create
) {
    (void)ptr;
    (void)create;
    return NULL;
}

__attribute__((weak)) void *pcc_allocator_granule_object_slot(void *ptr) {
    (void)ptr;
    return NULL;
}

static uint64_t hash_ptr(const void *ptr) {
    uint64_t value = (uint64_t)(uintptr_t)ptr >> 3;
    value ^= value >> 17;
    value ^= value >> 33;
    return value;
}

static void *key_for_bucket(
    uint64_t mask,
    uint64_t bucket,
    uint64_t *cursor
) {
    for (;;) {
        uintptr_t raw = (((uintptr_t)(*cursor)) << 4) + 0x10;
        (*cursor)++;
        if ((hash_ptr((void *)raw) & mask) == bucket) return (void *)raw;
    }
}

int main(void) {
    if (pcc_gc_index_slot_size() != 16) return 150;
    uintptr_t nodes[640];
    void *keys[520];
    uint64_t cursor = 1;
    int i = 0;

    if (py_gc_index_insert(0, &nodes[0]) != -1) return 1;
    if (py_gc_index_insert((void *)(uintptr_t)3, &nodes[0]) != -1) return 2;
    if (py_gc_index_find((void *)(uintptr_t)3) != 0) return 3;
    if (pcc_gc_object_index_insert((void *)(uintptr_t)0x1000, 0) != -1) return 4;

    for (i = 0; i < 8; i++) {
        keys[i] = key_for_bucket(255, 250, &cursor);
        nodes[i] = 0x1000u + (uintptr_t)i;
        if (py_gc_index_insert(keys[i], &nodes[i]) != 1) return 10 + i;
    }
    if (py_gc_index_insert(keys[0], &nodes[8]) != 0) return 20;
    if (py_gc_index_remove(keys[0]) != &nodes[0]) return 21;
    if (py_gc_index_remove(keys[3]) != &nodes[3]) return 22;
    for (i = 1; i < 8; i++) {
        if (i == 3) continue;
        if (py_gc_index_find(keys[i]) != &nodes[i]) return 30 + i;
    }

    for (i = 0; i < 500; i++) {
        keys[i] = (void *)(uintptr_t)(0x1000000u + (uintptr_t)i * 16u);
        nodes[16 + i] = 0x2000u + (uintptr_t)i;
        if (py_gc_index_insert(keys[i], &nodes[16 + i]) != 1) return 40;
    }
    for (i = 0; i < 500; i += 3) {
        if (py_gc_index_remove(keys[i]) != &nodes[16 + i]) return 41;
    }
    for (i = 0; i < 500; i++) {
        void *expected = i % 3 == 0 ? 0 : (void *)&nodes[16 + i];
        if (py_gc_index_find(keys[i]) != expected) return 42;
    }

    {
        void *null_node_key = (void *)(uintptr_t)0x7000000;
        if (py_gc_index_insert(null_node_key, 0) != 1) return 43;
        if (py_gc_index_insert(null_node_key, &nodes[0]) != 0) return 44;
        if (py_gc_index_remove(null_node_key) != 0) return 45;
        if (py_gc_index_insert(null_node_key, &nodes[0]) != 1) return 46;
        if (py_gc_index_remove(null_node_key) != &nodes[0]) return 47;
    }

    cursor = 100000;
    {
        int64_t required = pcc_gc_object_index_plan_capacity(1);
        if (required != 16384) return 48;
        void *plan = calloc((size_t)required, (size_t)pcc_gc_index_slot_size());
        if (plan == 0) return 49;
        void *original_plan = plan;
        if (pcc_gc_object_index_plan_commit(&plan, required / 2, 1) != -1) {
            return 50;
        }
        if (plan != original_plan) return 51;
        if (pcc_gc_object_index_plan_commit(&plan, required, 1) != 1) {
            return 52;
        }
        if (plan != 0) return 53;
    }
    for (i = 0; i < 6; i++) {
        keys[i] = key_for_bucket(16383, 16380, &cursor);
        if (pcc_gc_object_index_insert_preallocated(keys[i], &nodes[i]) != 1) {
            return 54 + i;
        }
    }
    if (pcc_gc_object_index_insert_preallocated(keys[1], &nodes[7]) != 0) {
        return 60;
    }
    if (pcc_gc_object_index_find(keys[1]) != &nodes[1]) return 61;
    if (pcc_gc_object_index_remove(keys[0]) != &nodes[0]) return 62;
    if (pcc_gc_object_index_remove(keys[2]) != &nodes[2]) return 63;
    if (pcc_gc_object_index_find(keys[5]) != &nodes[5]) return 64;
    pcc_gc_object_index_clear();
    if (pcc_gc_object_index_find(keys[5]) != 0) return 65;
    if (pcc_gc_object_index_insert(keys[5], &nodes[5]) != 1) return 66;
    if (pcc_gc_object_index_remove(keys[5]) != &nodes[5]) return 67;
    pcc_gc_object_index_clear();

    {
        void *key = (void *)(uintptr_t)0x8000;
        if (pcc_gc_forwarding_index_insert(key, &nodes[0]) != 1) return 70;
        if (pcc_gc_forwarding_index_insert(key, &nodes[1]) != 0) return 71;
        if (pcc_gc_forwarding_index_find(key) != &nodes[0]) return 72;
        if (pcc_gc_forwarding_index_remove(key) != &nodes[0]) return 73;
        pcc_gc_forwarding_index_clear();

        if (pcc_gc_identity_index_insert(key, &nodes[2]) != 1) return 74;
        if (pcc_gc_identity_index_find(key) != &nodes[2]) return 75;
        pcc_gc_identity_index_clear();

        if (pcc_gc_forwarding_target_index_upsert(key, &nodes[3]) != 1) return 76;
        if (pcc_gc_forwarding_target_index_upsert(key, &nodes[4]) != 0) return 77;
        if (pcc_gc_forwarding_target_index_find(key) != &nodes[4]) return 78;
        if (pcc_gc_forwarding_target_index_remove(key) != &nodes[4]) return 79;
        pcc_gc_forwarding_target_index_clear();
    }

    {
        int64_t cap = pcc_gc_forwarding_plan_index_capacity(0, 1);
        if (cap != 256) return 110;
        void *prepared = calloc((size_t)cap, (size_t)pcc_gc_index_slot_size());
        if (prepared == 0) return 111;
        if (
            pcc_gc_forwarding_plan_index_commit(0, &prepared, cap, 1) != 1
        ) return 112;
        if (prepared != 0) return 113;
        for (i = 0; i < 128; i++) {
            void *key = (void *)(uintptr_t)(0x9000000u + (uintptr_t)i * 16u);
            if (
                pcc_gc_forwarding_plan_index_insert(0, key, &nodes[i]) != 1
            ) return 114;
        }
        {
            void *blocked_key = (void *)(uintptr_t)0x9800000u;
            if (
                pcc_gc_forwarding_plan_index_insert(
                    0, blocked_key, &nodes[128]
                )
                != -1
            ) return 115;
            if (pcc_gc_forwarding_index_find(blocked_key) != 0) return 116;
        }
        cap = pcc_gc_forwarding_plan_index_capacity(0, 1);
        if (cap <= 256) return 117;
        prepared = calloc((size_t)(cap / 2), (size_t)pcc_gc_index_slot_size());
        if (prepared == 0) return 118;
        {
            void *undersized = prepared;
            if (
                pcc_gc_forwarding_plan_index_commit(
                    0, &prepared, cap / 2, 1
                )
                != -1
            ) return 119;
            if (prepared != undersized) return 120;
            free(prepared);
        }
        prepared = calloc((size_t)cap, (size_t)pcc_gc_index_slot_size());
        if (prepared == 0) return 121;
        if (
            pcc_gc_forwarding_plan_index_commit(0, &prepared, cap, 1) != 1
        ) return 122;
        if (prepared == 0) return 123;
        free(prepared);
        {
            void *key = (void *)(uintptr_t)0x9900000u;
            if (
                pcc_gc_forwarding_plan_index_insert(0, key, &nodes[128]) != 1
            ) return 124;
            if (pcc_gc_forwarding_index_find(key) != &nodes[128]) return 125;
        }
        for (i = 0; i < 128; i++) {
            void *key = (void *)(uintptr_t)(0x9000000u + (uintptr_t)i * 16u);
            if (pcc_gc_forwarding_index_find(key) != &nodes[i]) return 126;
        }
        pcc_gc_forwarding_index_clear();

        for (i = 1; i <= 2; i++) {
            cap = pcc_gc_forwarding_plan_index_capacity(i, 1);
            if (cap != 256) return 127;
            prepared = calloc((size_t)cap, (size_t)pcc_gc_index_slot_size());
            if (prepared == 0) return 128;
            if (
                pcc_gc_forwarding_plan_index_commit(
                    i, &prepared, cap, 1
                )
                != 1
            ) return 129;
            if (prepared != 0) return 130;
            if (
                pcc_gc_forwarding_plan_index_insert(
                    i, (void *)(uintptr_t)(0xa000000u + (uintptr_t)i * 16u),
                    &nodes[130 + i]
                )
                != 1
            ) return 131;
        }
        pcc_gc_forwarding_target_index_clear();
        pcc_gc_identity_index_clear();
    }

    {
        char storage[32];
        void *odd = (void *)(storage + 1);
        if (pcc_gc_frame_index_insert(odd, &nodes[0]) != 1) return 80;
        if (pcc_gc_frame_index_insert(odd, &nodes[1]) != 0) return 81;
        if (pcc_gc_frame_index_replace(odd, &nodes[2]) != &nodes[0]) return 82;
        if (pcc_gc_frame_index_find(odd) != &nodes[2]) return 83;
        if (pcc_gc_frame_index_remove(odd) != &nodes[2]) return 84;
        if (pcc_gc_frame_index_replace(odd, &nodes[3]) != 0) return 85;
        if (pcc_gc_frame_index_remove(odd) != &nodes[3]) return 86;
        pcc_gc_frame_index_clear();
    }

    {
        int64_t cap = pcc_gc_frame_index_plan_capacity(1);
        if (cap != 256) return 132;
        void *prepared = calloc((size_t)cap, (size_t)pcc_gc_index_slot_size());
        if (prepared == 0) return 133;
        if (pcc_gc_frame_index_plan_commit(&prepared, cap, 1) != 1) return 134;
        if (prepared != 0) return 135;
        for (i = 0; i < 128; i++) {
            void *key = (void *)(uintptr_t)(0xb000000u + (uintptr_t)i * 16u);
            if (
                pcc_gc_frame_index_replace_preallocated(key, &nodes[i]) != 0
            ) return 136;
        }
        cap = pcc_gc_frame_index_plan_capacity(1);
        if (cap <= 256) return 137;
        prepared = calloc((size_t)(cap / 2), (size_t)pcc_gc_index_slot_size());
        if (prepared == 0) return 138;
        {
            void *undersized = prepared;
            if (
                pcc_gc_frame_index_plan_commit(
                    &prepared, cap / 2, 1
                ) != -1
            ) return 139;
            if (prepared != undersized) return 140;
            free(prepared);
        }
        prepared = calloc((size_t)cap, (size_t)pcc_gc_index_slot_size());
        if (prepared == 0) return 141;
        if (pcc_gc_frame_index_plan_commit(&prepared, cap, 1) != 1) return 142;
        if (prepared == 0) return 143;
        free(prepared);
        {
            void *key = (void *)(uintptr_t)0xb800000u;
            if (
                pcc_gc_frame_index_replace_preallocated(key, &nodes[128]) != 0
            ) return 144;
            if (pcc_gc_frame_index_find(key) != &nodes[128]) return 145;
        }
        for (i = 0; i < 128; i++) {
            void *key = (void *)(uintptr_t)(0xb000000u + (uintptr_t)i * 16u);
            if (pcc_gc_frame_index_find(key) != &nodes[i]) return 146;
        }
        pcc_gc_frame_index_clear();
    }

    {
        void *owner = (void *)(uintptr_t)0xa000;
        void *raw_page = (void *)(uintptr_t)3;
        if (pcc_gc_zpage_owner_index_upsert(owner, &nodes[0]) != 1) return 90;
        if (pcc_gc_zpage_owner_index_upsert(owner, &nodes[1]) != 0) return 91;
        if (pcc_gc_zpage_owner_index_find(owner) != &nodes[1]) return 92;
        if (pcc_gc_zpage_owner_index_remove(owner) != &nodes[1]) return 93;
        pcc_gc_zpage_owner_index_clear();

        {
            int64_t cap = pcc_gc_zpage_owner_index_plan_capacity(1);
            if (cap != 256) return 147;
            void *prepared = calloc((size_t)cap, (size_t)pcc_gc_index_slot_size());
            if (prepared == 0) return 148;
            if (
                pcc_gc_zpage_owner_index_plan_commit(
                    &prepared, cap, 1
                ) != 1
            ) return 149;
            if (prepared != 0) return 150;
            if (
                pcc_gc_zpage_owner_index_upsert_preallocated(
                    owner, &nodes[4]
                ) != 1
            ) return 151;
            if (
                pcc_gc_zpage_owner_index_upsert_preallocated(
                    owner, &nodes[5]
                ) != 0
            ) return 152;
            if (pcc_gc_zpage_owner_index_find(owner) != &nodes[5]) return 153;
            if (pcc_gc_zpage_owner_index_remove(owner) != &nodes[5]) return 154;
            pcc_gc_zpage_owner_index_clear();
        }

        if (pcc_gc_zpage_page_index_insert(raw_page, &nodes[2]) != 1) return 94;
        if (pcc_gc_zpage_page_index_upsert(raw_page, &nodes[3]) != 0) return 95;
        if (pcc_gc_zpage_page_index_find(raw_page) != &nodes[3]) return 96;
        if (pcc_gc_zpage_page_index_remove(raw_page) != &nodes[3]) return 97;
        pcc_gc_zpage_page_index_clear();
    }

    {
        void *managed_keys[520];
        cursor = 200000;
        if (pcc_gc_managed_pointer_index_insert(0) != -1) return 100;
        if (pcc_gc_managed_pointer_index_insert((void *)(uintptr_t)3) != -1) {
            return 101;
        }
        for (i = 0; i < 8; i++) {
            managed_keys[i] = key_for_bucket(255, 252, &cursor);
            if (pcc_gc_managed_pointer_index_insert(managed_keys[i]) != 1) {
                return 102;
            }
        }
        if (pcc_gc_managed_pointer_index_insert(managed_keys[0]) != 0) {
            return 103;
        }
        if (pcc_gc_managed_pointer_index_remove(managed_keys[3]) != 1) {
            return 104;
        }
        for (i = 0; i < 8; i++) {
            int64_t expected = i == 3 ? 0 : 1;
            if (pcc_gc_managed_pointer_index_contains(managed_keys[i]) != expected) {
                return 105;
            }
        }
        for (i = 8; i < 508; i++) {
            managed_keys[i] = (void *)(uintptr_t)(
                0x9000000u + (uintptr_t)i * 16u
            );
            if (pcc_gc_managed_pointer_index_insert(managed_keys[i]) != 1) {
                return 106;
            }
        }
        for (i = 8; i < 508; i += 3) {
            if (pcc_gc_managed_pointer_index_remove(managed_keys[i]) != 1) {
                return 107;
            }
        }
        for (i = 8; i < 508; i++) {
            int64_t expected = (i - 8) % 3 == 0 ? 0 : 1;
            if (pcc_gc_managed_pointer_index_contains(managed_keys[i]) != expected) {
                return 108;
            }
        }
    }

    pcc_gc_ptr_index_tls_pool_drain();
    puts("gc-index-ok");
    return 0;
}
"""


def _build_and_run_harness(
    tmp_path: Path,
    name: str,
    implementation: list[str],
) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(_harness_source(), encoding="utf-8")
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            "-O0",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(harness),
            *implementation,
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_freestanding_gc_index_table_ir_is_raw_and_exports_complete_abi(tmp_path):
    obj = _build_object(tmp_path, "llvm")
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    undefined_names = {
        line.split()[-1].lstrip("_")
        for line in undefined.stdout.splitlines()
        if line.strip()
    }
    # The primary index asks the allocator whether a key is an object cell
    # with a slab side word; every other dependency is raw memory.
    assert undefined_names == {
        "calloc",
        "free",
        "pcc_allocator_granule_object_slot",
        "pcc_allocator_object_side_slot",
    }

    symbols = subprocess.run(
        ["nm", "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    defined_names = {
        line.split()[-1].lstrip("_")
        for line in symbols.stdout.splitlines()
        if " T " in line or " t " in line
    }
    assert PUBLIC_SYMBOLS <= defined_names


def test_freestanding_llvm_gc_index_harness(tmp_path):
    obj = _build_object(tmp_path, "llvm")
    port = _build_and_run_harness(
        tmp_path,
        "gc_index_freestanding_llvm",
        [str(obj)],
    )
    assert port.returncode == 0, port.stdout + port.stderr
    assert port.stdout == "gc-index-ok\n"


def test_freestanding_self_backend_gc_index_harness(tmp_path):
    obj = _build_object(tmp_path, "self")
    port = _build_and_run_harness(
        tmp_path,
        "gc_index_freestanding_self",
        [str(obj)],
    )
    assert port.returncode == 0, port.stdout + port.stderr
    assert port.stdout == "gc-index-ok\n"


def test_production_archive_plan_owns_gc_indexes_in_freestanding_python():
    makefile = (RUNTIME_DIR / "Makefile").read_text(encoding="utf-8")
    # 27b290cb widened PY_REPLACED_C_MODULES well beyond the original
    # two-entry form; assert the semantic requirement (the gc index table's
    # C module is replaced by the pcc-Python port) instead of the exact
    # historical line.
    assert "FREESTANDING_PY_MODULES =" in makefile
    assert "freestanding_gc_index_table" in makefile.split(
        "FREESTANDING_PY_MODULES =", 1
    )[1].splitlines()[0]
    assert "$(OBJDIR_PY)/py_gc_index_table.o:" not in makefile

    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=RUNTIME_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    archive_lines = [
        line
        for line in plan.stdout.splitlines()
        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line
    ]
    assert len(archive_lines) == 1
    assert "build_py/freestanding_gc_index_table.o" in archive_lines[0]
    assert "build_py/py_gc_index_table.o" not in archive_lines[0]


def test_built_production_archive_attributes_and_runs_gc_index_python_object(
    tmp_path,
    pcc_runtime_archive,
):
    members = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members.returncode == 0, members.stdout + members.stderr
    member_names = members.stdout.splitlines()
    assert "freestanding_gc_index_table.o" in member_names
    assert "py_gc_index_table.o" not in member_names

    symbols = subprocess.run(
        ["nm", "-A", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    owners = [
        line
        for line in symbols.stdout.splitlines()
        if " T " in line
        and (
            line.rstrip().endswith(" pcc_gc_object_index_insert")
            or line.rstrip().endswith(" _pcc_gc_object_index_insert")
        )
    ]
    assert len(owners) == 1
    assert ":freestanding_gc_index_table.o:" in owners[0]

    result = _build_and_run_harness(
        tmp_path,
        "gc_index_production_archive",
        [str(pcc_runtime_archive)],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "gc-index-ok\n"
