from __future__ import annotations

import ast
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_CROSS_OBJECT_SIGNATURES,
    FREESTANDING_GC_RUNTIME_GLOBALS,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_zpage_allocation.py"
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_backend4_try_zpage_alloc",
    "pcc_gc_backend4_zpage_track_alloc",
    "pcc_gc_backend4_zpage_track_alloc_preallocated",
    "pcc_gc_backend4_zpage_track_page_prepare",
}
RAW_FUNCTION_IMPORTS = {
    "free",
    "malloc",
    "memset",
    "pcc_gc_backend4_evacuation_page_find",
    "pcc_gc_backend4_zpage_active_page",
    "pcc_gc_backend4_zpage_clear_active_page",
    "pcc_gc_backend4_zpage_find_page_for_addr",
    "pcc_gc_backend4_zpage_find_reusable_page",
    "pcc_gc_backend4_zpage_find_reusable_page_for_gen",
    "pcc_gc_backend4_zpage_link_node",
    "pcc_gc_backend4_zpage_link_node_preallocated",
    "pcc_gc_backend4_zpage_node_alloc",
    "pcc_gc_backend4_zpage_node_release",
    "pcc_gc_backend4_zpage_node_take_prepared",
    "pcc_gc_backend4_zpage_pop_free_page",
    "pcc_gc_backend4_zpage_reset",
    "pcc_gc_backend4_zpage_set_active_page",
    "pcc_gc_config_ensure",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_backend4_evacuation_page_head",
    "pcc_gc_backend4_free_page_head",
    "pcc_gc_backend4_page_head",
    "pcc_gc_backend_selected",
    "pcc_gc_config_initialized",
}


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _literal_global_imports() -> set[str]:
    globals_: set[str] = set()
    tree = ast.parse(STRICT_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"global_addr", "global_load_ptr", "global_store_ptr"}:
            continue
        if not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            globals_.add(value.value)
    return globals_


def test_zpage_allocation_has_one_strict_source_owner() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert _exported_symbols(managed).isdisjoint(OWNED_SYMBOLS)
    assert "freestanding_gc_zpage_allocation" in makefile
    assert "def pcc_gc_backend4_try_zpage_alloc(" not in managed
    assert "def _backend4_zpage_track_alloc(" not in managed
    assert 'pcc_gc_backend4_try_zpage_alloc = extern(' in managed
    assert '_backend4_zpage_track_alloc = extern(' in managed


def _assert_calls_follow_latest_unlock(
    body: str, call: str, lock: str, unlock: str
) -> None:
    starts = [match.start() for match in re.finditer(re.escape(call), body)]
    assert starts, call
    for start in starts:
        prefix = body[:start]
        assert prefix.rfind(unlock) > prefix.rfind(lock), (call, body[start - 80 : start + 80])


def test_zpage_allocation_prepares_and_clears_storage_outside_graph_lock() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    strict_alloc = strict.split("def pcc_gc_backend4_try_zpage_alloc", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_track_alloc")', 1
    )[0]

    assert "prepared_page" in strict_alloc
    _assert_calls_follow_latest_unlock(
        strict_alloc,
        "pcc_gc_backend4_zpage_reset(",
        "pcc_py_gc_minor_graph_lock()",
        "pcc_py_gc_minor_graph_unlock()",
    )
    _assert_calls_follow_latest_unlock(
        strict_alloc,
        "malloc(",
        "pcc_py_gc_minor_graph_lock()",
        "pcc_py_gc_minor_graph_unlock()",
    )
    _assert_calls_follow_latest_unlock(
        strict_alloc,
        "memset(obj",
        "pcc_py_gc_minor_graph_lock()",
        "pcc_py_gc_minor_graph_unlock()",
    )


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_zpage_allocation_has_exact_strict_object_closure(
    tmp_path: Path, emitter: str
) -> None:
    llvm_ir = tmp_path / ("zpage_allocation_" + emitter + ".ll")
    pipeline.compile_python(
        str(STRICT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "zpage_allocation.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("zpage_allocation_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _literal_global_imports() == RAW_GLOBAL_IMPORTS
    assert RAW_GLOBAL_IMPORTS <= FREESTANDING_GC_RUNTIME_GLOBALS

    undefined_result = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined_result.returncode == 0, (
        undefined_result.stdout + undefined_result.stderr
    )
    undefined = {
        line.split()[-1].lstrip("_")
        for line in undefined_result.stdout.splitlines()
        if line.strip()
    }
    assert undefined == RAW_FUNCTION_IMPORTS | RAW_GLOBAL_IMPORTS

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == OWNED_SYMBOLS


def test_zpage_allocation_preserves_page_and_pending_handoff_contract() -> None:
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    allocation = strict.split("def pcc_gc_backend4_try_zpage_alloc", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_track_alloc")', 1
    )[0]
    tracking = strict.split("def pcc_gc_backend4_zpage_track_alloc", 1)[1]

    assert "size < 16" in allocation
    assert "(size + 7) & -8" in allocation
    assert "capacity - allocated >= alloc_size" in allocation
    assert "pcc_gc_backend4_evacuation_page_find(active)" in allocation
    assert "store_i64(page, 88, load_i64(page, 88) + 1)" in allocation
    assert "pcc_gc_backend4_zpage_find_page_for_addr(owner, size)" in tracking
    assert "pending - 1" in tracking
    assert "store_i64(node, 24, existing_offset)" in tracking
    assert "pcc_gc_backend4_zpage_link_node(node)" in tracking


def test_object_registration_prepares_zpage_tracking_before_graph_lock() -> None:
    managed = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    strict_registration = managed.split(
        "def pcc_gc_note_object_allocated_sized", 1
    )[1].split('@c_abi_export("pcc_gc_note_object_allocated")', 1)[0]

    for registration, prepare, lock, unlock, preallocated in ((
            strict_registration,
            "_backend4_zpage_node_prepare()",
            "_object_graph_lock()",
            "_object_graph_unlock()",
            "_backend4_zpage_track_alloc_preallocated(",
        ),):
        prepare_at = registration.index(prepare)
        assert registration[:prepare_at].rfind(unlock) > (
            registration[:prepare_at].rfind(lock)
        )
        assert "pcc_gc_zpage_owner_index_plan_capacity" in registration
        assert "pcc_gc_zpage_owner_index_plan_commit" in registration
        assert "zpage_track_page_prepare" in registration
        assert preallocated in registration
        assert "zpage_track_alloc_unlocked(\n                o, n->size\n" not in registration
        assert "_backend4_zpage_track_alloc(o, size)" not in registration
        for allocating_call in (
            prepare,
            "zpage_track_page_prepare(",
            "calloc(" if "pcc_gc_graph_lock();" in registration else "malloc(",
        ):
            _assert_calls_follow_latest_unlock(
                registration, allocating_call, lock, unlock
            )

    expected_signatures = {
        "pcc_gc_backend4_zpage_node_prepare": ((), "c_ptr"),
        "pcc_gc_backend4_zpage_node_plan_requires_prepare": (
            (),
            "c_int64",
        ),
        "pcc_gc_backend4_zpage_node_take_prepared": (
            ("c_ptr",),
            "c_ptr",
        ),
        "pcc_gc_zpage_owner_index_plan_capacity": (
            ("c_int64",),
            "c_int64",
        ),
        "pcc_gc_zpage_owner_index_plan_commit": (
            ("c_ptr", "c_int64", "c_int64"),
            "c_int64",
        ),
        "pcc_gc_zpage_owner_index_upsert_preallocated": (
            ("c_ptr", "c_ptr"),
            "c_int64",
        ),
        "pcc_gc_backend4_zpage_track_page_prepare": (
            ("c_ptr", "c_ptr", "c_int64"),
            "c_ptr",
        ),
        "pcc_gc_backend4_zpage_track_alloc_preallocated": (
            ("c_ptr", "c_int64", "c_ptr", "c_ptr", "c_int64"),
            "c_ptr",
        ),
    }
    for symbol, signature in expected_signatures.items():
        assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[symbol] == signature


def test_production_archive_has_one_zpage_allocation_owner(
    pcc_runtime_archive: Path,
) -> None:
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    for symbol in OWNED_SYMBOLS:
        owners = [
            line
            for line in symbols_result.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_zpage_allocation.o:" in owners[0]
        assert ":py_gc_backend.o:" not in owners[0]


def _link_zpage_allocation_probe(
    tmp_path: Path, name: str, archive: Path, size: int
) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(
        textwrap.dedent(
            r'''
            #include "py_runtime.h"
            #include <stdio.h>

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                    return 2;
                }
                long long count0 = pcc_gc_backend4_zpage_count();
                long long capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
                long long used0 = pcc_gc_backend4_zpage_used_bytes();
                PyObject *a = pcc_gc_alloc(SIZE_VALUE, PY_TYPE_LIST, 0);
                PyObject *b = pcc_gc_alloc(SIZE_VALUE, PY_TYPE_LIST, 0);
                if (a == 0 || b == 0) return 3;
                printf("%lld,%lld,%lld,%lld,%lld\n",
                       (long long)pcc_gc_backend4_zpage_count() - count0,
                       (long long)pcc_gc_backend4_zpage_capacity_bytes() - capacity0,
                       (long long)pcc_gc_backend4_zpage_used_bytes() - used0,
                       (long long)pcc_gc_backend4_zpage_owner_offset_bytes(a),
                       (long long)pcc_gc_backend4_zpage_owner_offset_bytes(b));
                pcc_gc_release(b);
                pcc_gc_release(a);
                printf("%lld,%lld,%lld\n",
                       (long long)pcc_gc_backend4_zpage_count() - count0,
                       (long long)pcc_gc_backend4_zpage_capacity_bytes() - capacity0,
                       (long long)pcc_gc_backend4_zpage_used_bytes() - used0);
                return 0;
            }
            '''
        ).replace("SIZE_VALUE", str(size)).lstrip(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(source),
            str(archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (128, "1,4096,256,0,128\n0,0,0\n"),
        (8192, "1,65536,16384,0,8192\n0,0,0\n"),
        (70000, "2,262144,140000,0,0\n0,0,0\n"),
    ],
)
def test_zpage_allocation_across_page_classes(
    tmp_path: Path,
    pcc_runtime_archive: Path,
    size: int,
    expected: str,
) -> None:
    # ``expected`` is what the retired C runtime oracle printed.
    implementation = _link_zpage_allocation_probe(
        tmp_path,
        "zpage_alloc_pcc_python_" + str(size),
        pcc_runtime_archive,
        size,
    )
    result = subprocess.run(
        [str(implementation)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected


def _link_zpage_prepare_probe(
    tmp_path: Path, name: str, archive: Path, source_text: str
) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(textwrap.dedent(source_text).lstrip(), encoding="utf-8")
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            str(source),
            str(archive),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_zpage_allocation_failure_does_not_publish_partial_page(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        extern void *pcc_gc_backend4_try_zpage_alloc(int64_t, int32_t);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            void *p = pcc_gc_backend4_try_zpage_alloc(INT64_C(1) << 62, 0);
            printf("%d,%lld,%lld,%lld\n",
                   p == NULL,
                   (long long)(pcc_gc_backend4_zpage_count() - count0),
                   (long long)(pcc_gc_backend4_zpage_capacity_bytes() - capacity0),
                   (long long)(pcc_gc_backend4_zpage_free_pages() - free0));
            return p == NULL ? 0 : 3;
        }
    '''
    executable = _link_zpage_prepare_probe(
        tmp_path, "zpage_prepare_failure", pcc_runtime_archive, source
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "1,0,0,0\n"


def test_zpage_tracking_fallback_admission_and_prepare_failure(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct {
            int64_t refcount;
            int32_t type_tag;
            int32_t flags;
        } ProbeHeader;

        extern void pcc_gc_note_object_allocated_sized(PyObject *, int64_t);
        extern void pcc_gc_note_object_freeing(PyObject *);
        extern void *pcc_gc_object_index_find(PyObject *);
        extern void *pcc_gc_zpage_owner_index_find(PyObject *);
        extern void *pcc_gc_backend4_zpage_track_page_prepare(
            void *, PyObject *, int64_t
        );

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            PyObject *owner = (PyObject *)calloc(1, 128);
            if (owner == NULL) return 3;
            ProbeHeader *header = (ProbeHeader *)owner;
            header->refcount = 1;
            header->type_tag = PY_TYPE_LIST;
            header->flags = 262144;
            pcc_gc_note_object_allocated_sized(owner, 128);
            int tracked = pcc_gc_zpage_owner_index_find(owner) != NULL;
            int offset_ok = pcc_gc_backend4_zpage_owner_offset_bytes(owner) >= 0;
            pcc_gc_note_object_freeing(owner);
            int removed = pcc_gc_zpage_owner_index_find(owner) == NULL;
            void *huge = pcc_gc_backend4_zpage_track_page_prepare(
                NULL, owner, INT64_C(1) << 62
            );
            int failed_closed = huge == NULL;
            PyObject *failed_owner = (PyObject *)calloc(1, 128);
            if (failed_owner == NULL) return 5;
            ProbeHeader *failed_header = (ProbeHeader *)failed_owner;
            failed_header->refcount = 1;
            failed_header->type_tag = PY_TYPE_LIST;
            failed_header->flags = 262144;
            pcc_gc_note_object_allocated_sized(
                failed_owner, INT64_C(1) << 62
            );
            int registration_rolled_back =
                pcc_gc_object_index_find(failed_owner) == NULL
                && pcc_gc_zpage_owner_index_find(failed_owner) == NULL;
            printf(
                "%d,%d,%d,%d,%d\n",
                tracked,
                offset_ok,
                removed,
                failed_closed,
                registration_rolled_back
            );
            free(owner);
            free(failed_owner);
            return tracked && offset_ok && removed && failed_closed
                && registration_rolled_back ? 0 : 4;
        }
    '''
    executable = _link_zpage_prepare_probe(
        tmp_path, "zpage_track_fallback", pcc_runtime_archive, source
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "1,1,1,1,1\n"


def test_zpage_first_page_race_publishes_one_page(
    tmp_path: Path,
    threaded_pcc_runtime_archive: Path,
) -> None:
    source = r'''
        #include "py_runtime.h"
        #include <pthread.h>
        #include <sched.h>
        #include <stdatomic.h>
        #include <stdint.h>
        #include <stdio.h>

        #define THREADS 16
        static _Atomic int ready = 0;
        static _Atomic int go = 0;
        static PyObject *objects[THREADS];

        static void *worker(void *opaque) {
            intptr_t index = (intptr_t)opaque;
            atomic_fetch_add_explicit(&ready, 1, memory_order_release);
            while (atomic_load_explicit(&go, memory_order_acquire) == 0) {
                sched_yield();
            }
            objects[index] = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            pcc_thread_unregister_current();
            return NULL;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            pthread_t threads[THREADS];
            for (intptr_t i = 0; i < THREADS; i++) {
                if (pthread_create(&threads[i], NULL, worker, (void *)i) != 0) {
                    return 3;
                }
            }
            while (atomic_load_explicit(&ready, memory_order_acquire) != THREADS) {
                sched_yield();
            }
            atomic_store_explicit(&go, 1, memory_order_release);
            for (int i = 0; i < THREADS; i++) {
                if (pthread_join(threads[i], NULL) != 0) return 4;
            }
            int errors = 0;
            for (int i = 0; i < THREADS; i++) {
                if (objects[i] == NULL) {
                    errors++;
                    continue;
                }
                int64_t offset = pcc_gc_backend4_zpage_owner_offset_bytes(objects[i]);
                if (offset < 0 || offset >= 4096 || (offset % 128) != 0) errors++;
                for (int j = 0; j < i; j++) {
                    if (objects[i] == objects[j]) errors++;
                }
            }
            printf("%lld,%lld,%lld,%lld,%d\n",
                   (long long)(pcc_gc_backend4_zpage_count() - count0),
                   (long long)(pcc_gc_backend4_zpage_capacity_bytes() - capacity0),
                   (long long)(pcc_gc_backend4_zpage_used_bytes() - used0),
                   (long long)(pcc_gc_backend4_zpage_free_pages() - free0),
                   errors);
            for (int i = 0; i < THREADS; i++) pcc_gc_release(objects[i]);
            return errors == 0 ? 0 : 5;
        }
    '''
    executable = _link_zpage_prepare_probe(
        tmp_path, "zpage_prepare_race", threaded_pcc_runtime_archive, source
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "1,4096,2048,0,0\n"
