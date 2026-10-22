from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_RUNTIME_GLOBALS,
    RUNTIME_SIGNATURES,
)
from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
from pcc.backend import precise_stackmap
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64,
    FunctionStackMap,
    LOCATION_MANAGED,
    LOCATION_OWNED,
    LOCATION_STACK_INDIRECT,
    NO_BASE,
    PreciseStackMap,
    SAFEPOINT_CALL,
    SafepointRecord,
    StackMapLocation,
    encode_stack_map,
    function_id,
    safepoint_id,
)
from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
MAPPED_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_mapped_roots.py"
ROOT_OPS_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_root_operations.py"
PROMOTION_SOURCE = (
    RUNTIME_DIR / "py" / "freestanding_gc_generational_promotion.py"
)
MANAGED_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
SUBSTRATE_SOURCE = RUNTIME_DIR / "py" / "py_substrate.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

PUBLIC_SYMBOLS = {
    "pcc_gc_trace_continuation_roots",
    "pcc_gc_rewrite_continuation_roots",
    "pcc_gc_consume_precise_stackmap",
    "pcc_gc_register_precise_stackmap_table",
    "pcc_gc_precise_stack_diff",
    "pcc_gc_precise_stack_diff_if_requested",
    "pcc_gc_visit_precise_frame",
}
INTERNAL_SYMBOLS = {
    "pcc_gc_visit_mapped_root_slot",
    "pcc_gc_visit_mapped_root_slots",
    "pcc_gc_visit_registered_root_slots",
    "pcc_gc_visit_scheduler_root_slots",
    "pcc_gc_visit_builtin_exception_cache_slots",
    "pcc_gc_gray_mapped_roots",
    "pcc_gc_rewrite_mapped_roots",
}
STACKMAP_HELPER_SYMBOLS = {
    "pcc_gc_stackmap_range_fits",
    "pcc_gc_stackmap_u32",
    "pcc_gc_stackmap_u64_strictly_after",
    "pcc_gc_stackmap_validate_location",
    "pcc_gc_registered_roots_hold",
    "pcc_gc_stack_diff_note_mapped",
    "pcc_gc_stack_diff_mapped_holds",
    "pcc_gc_stack_diff_unmapped_registered",
    "pcc_gc_stack_diff_registered_in_range",
    "pcc_gc_stackmap_index_swap",
    "pcc_gc_stackmap_index_sift",
    "pcc_gc_stackmap_index_ready",
    "pcc_gc_stackmap_find_function",
    "pcc_gc_stackmap_roots_mode",
    "pcc_gc_stackmap_visit_stack_roots",
    "pcc_gc_stackmap_payload_size",
    "pcc_gc_stackmap_runtime_table_valid",
    "pcc_gc_stackmap_visit_runtime_record",
    "pcc_gc_stack_diff_write_text",
    "pcc_gc_stack_diff_write_decimal",
}
ROOT_OPS_PROVIDER_SYMBOLS = {
    "pcc_gc_mark_root_gray_if_known",
    "pcc_gc_resolve_root_slot_unlocked",
}
PROMOTION_PROVIDER_SYMBOLS = {"pcc_gc_promote_cached_frame_slot"}
PROVIDER_SYMBOLS = ROOT_OPS_PROVIDER_SYMBOLS | PROMOTION_PROVIDER_SYMBOLS
# py_subs_exc_cache_slot is not raw-only: codegen reads the builtin exception
# cache slots through it (name, iterator, numeric and generator lowering).
RAW_ONLY_CROSS_OBJECT_SYMBOLS = PROVIDER_SYMBOLS | INTERNAL_SYMBOLS | {
    "pcc_gc_root_map_is_borrowed",
    "pcc_gc_root_slot_count_from_map",
}
RAW_FUNCTION_IMPORTS = {
    "pcc_gc_backend",
    "malloc",
    "pcc_gc_mark_root_gray_if_known",
    "pcc_gc_pointer_is_managed",
    "pcc_gc_promote_cached_frame_slot",
    "pcc_gc_resolve_root_slot_unlocked",
    "pcc_gc_root_map_is_borrowed",
    "pcc_gc_root_slot_count_from_map",
    "pcc_platform_getenv",
    "pcc_py_gc_minor_graph_lock",
    "pcc_py_gc_minor_graph_unlock",
    "py_subs_exc_cache_slot",
    "realloc",
    "write",
}
RAW_GLOBAL_IMPORTS = {
    "pcc_gc_continuation_root_head",
    "pcc_gc_frame_head",
    "pcc_gc_precise_stackmap_table",
    "pcc_gc_scheduler_root_head",
    "pcc_gc_stack_diff_capacity",
    "pcc_gc_stack_diff_count",
    "pcc_gc_stack_diff_values",
    "pcc_gc_stackmap_index",
    "pcc_gc_stackmap_index_count",
    "pcc_gc_stackmap_location_base",
    "pcc_gc_stackmap_roots_setting",
}
# Not GC state: the managed runtime's builtin type root table, which the
# visitor walks for the builtin class statics it keeps alive.
MANAGED_ROOT_TABLE_IMPORTS = {"pcc_builtin_type_root_slots"}


def test_mapped_roots_stackmap_constants_match_producer_abi():
    assert ABI_CONSTANTS["stackmap.magic_i64"] == precise_stackmap.MAGIC_I64
    assert ABI_CONSTANTS["stackmap.header_size"] == precise_stackmap.HEADER_SIZE
    assert (
        ABI_CONSTANTS["stackmap.function_size"]
        == precise_stackmap.FUNCTION_SIZE
    )
    assert ABI_CONSTANTS["stackmap.record_size"] == precise_stackmap.RECORD_SIZE
    assert (
        ABI_CONSTANTS["stackmap.location_size"]
        == precise_stackmap.LOCATION_SIZE
    )
    assert ABI_CONSTANTS["stackmap.no_offset"] == precise_stackmap.NO_OFFSET
    assert (
        ABI_CONSTANTS["stackmap.location.stack_indirect"]
        == precise_stackmap.LOCATION_STACK_INDIRECT
    )
    assert (
        ABI_CONSTANTS["stackmap.location.managed"]
        == precise_stackmap.LOCATION_MANAGED
    )
    assert (
        ABI_CONSTANTS["stackmap.location.owned"]
        == precise_stackmap.LOCATION_OWNED
    )
    for part in ("header", "function", "record", "live_set", "location", "rare"):
        assert (
            ABI_CONSTANTS["stackmap.runtime." + part + "_size"]
            == getattr(precise_stackmap, "RUNTIME_" + part.upper() + "_SIZE")
        )
    source = MAPPED_SOURCE.read_text(encoding="utf-8")
    assert "_PCC_STACKMAP_" not in source
    for name in (
        "stackmap.magic_i64",
        "stackmap.header_size",
        "stackmap.function_size",
        "stackmap.record_size",
        "stackmap.location_size",
        "stackmap.no_offset",
        "stackmap.location.stack_indirect",
        "stackmap.location.managed",
        "stackmap.location.owned",
        "stackmap.runtime.header_size",
        "stackmap.runtime.function_size",
        "stackmap.runtime.record_size",
        "stackmap.runtime.live_set_size",
        "stackmap.runtime.location_size",
        "stackmap.runtime.rare_size",
    ):
        assert f'abi_constant("{name}")' in source


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_mapped_roots_" + emitter + ".ll")
    pipeline.compile_python(
        str(MAPPED_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_mapped_roots.s"
        source.write_text(emit_self_asm(ir_text), encoding="utf-8")
    obj = tmp_path / ("freestanding_gc_mapped_roots_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def test_mapped_roots_have_one_strict_visitor_and_split_providers():
    strict = MAPPED_SOURCE.read_text(encoding="utf-8")
    root_ops = ROOT_OPS_SOURCE.read_text(encoding="utf-8")
    promotion = PROMOTION_SOURCE.read_text(encoding="utf-8")
    managed = MANAGED_SOURCE.read_text(encoding="utf-8")
    substrate = SUBSTRATE_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in strict
    assert _exported_symbols(strict) == (
        PUBLIC_SYMBOLS | INTERNAL_SYMBOLS | STACKMAP_HELPER_SYMBOLS
    )
    assert _exported_symbols(managed).isdisjoint(PUBLIC_SYMBOLS | INTERNAL_SYMBOLS)
    assert ROOT_OPS_PROVIDER_SYMBOLS <= _exported_symbols(root_ops)
    assert PROMOTION_PROVIDER_SYMBOLS <= _exported_symbols(promotion)
    assert _exported_symbols(managed).isdisjoint(PROVIDER_SYMBOLS)
    assert RAW_ONLY_CROSS_OBJECT_SYMBOLS.isdisjoint(RUNTIME_SIGNATURES)
    assert "freestanding_gc_mapped_roots" in makefile
    for provider in PROVIDER_SYMBOLS:
        assert f"{provider} = extern(" in strict
        assert f'"{provider}"' in strict
    assert '"py_subs_exc_cache_slot"' in strict
    assert '@c_abi_export("py_subs_exc_cache_slot")' in substrate
    assert '"pcc_gc_visit_registered_root_slots"' in managed


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_mapped_root_object_has_exact_raw_closure(tmp_path: Path, emitter: str):
    obj = _compile_object(tmp_path, emitter)
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
    assert undefined == (
        RAW_FUNCTION_IMPORTS | RAW_GLOBAL_IMPORTS | MANAGED_ROOT_TABLE_IMPORTS
    )

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == PUBLIC_SYMBOLS | INTERNAL_SYMBOLS | STACKMAP_HELPER_SYMBOLS


def _mapped_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>

int main(void) {
    int32_t owned_map[1] = {2};
    int32_t borrowed_map[1] = {-1};
    PyObject *owned[2] = {NULL, NULL};
    PyObject *borrowed[1] = {NULL};
    pcc_gc_register_continuation_root(owned_map, owned);
    pcc_gc_register_continuation_root(borrowed_map, borrowed);
    printf("mapped:%lld,%lld\n",
           (long long)pcc_gc_trace_continuation_roots(),
           (long long)pcc_gc_rewrite_continuation_roots());
    pcc_gc_unregister_continuation_root(owned);
    pcc_gc_unregister_continuation_root(borrowed);
    printf("empty:%lld\n", (long long)pcc_gc_trace_continuation_roots());
    return 0;
}
'''


def _relocation_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>

typedef struct {
    PyObjectHeader h;
    int64_t length;
    int64_t capacity;
    PyObject **items;
} ProbeListObject;

int main(void) {
    if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
    int32_t frame_map[1] = {1};
    PyObject *slots[1] = {NULL};
    ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(64, PY_TYPE_LIST, 0);
    if (obj == NULL) return 3;
    obj->length = 0;
    obj->capacity = 0;
    obj->items = NULL;
    /* Relocation skips an owner until its initialization is published. */
    pcc_gc_publish_initialized((PyObject *)obj);
    int64_t stable = pcc_gc_object_id((PyObject *)obj);
    pcc_gc_store_root(&slots[0], (PyObject *)obj);
    pcc_gc_register_continuation_root(frame_map, slots);
    int64_t traced = pcc_gc_trace_continuation_roots();
    if (pcc_gc_select_relocation_set(1) != 1) return 4;
    PyObject *moved = pcc_gc_relocate_copy((PyObject *)obj, 64);
    if (moved == NULL) return 5;
    int64_t rewritten = pcc_gc_rewrite_continuation_roots();
    printf("relocate:%lld,%lld,%lld,%lld\n",
           (long long)traced,
           (long long)rewritten,
           (long long)(slots[0] != (PyObject *)obj),
           (long long)(pcc_gc_object_id(slots[0]) == stable));
    pcc_gc_release((PyObject *)obj);
    pcc_gc_release(moved);
    pcc_gc_store_root(&slots[0], NULL);
    pcc_gc_unregister_continuation_root(slots);
    return 0;
}
'''


def _registered_scan_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>

extern int64_t pcc_gc_visit_registered_root_slots(int64_t mode, int64_t resolve);
extern int64_t pcc_gc_visit_builtin_exception_cache_slots(int64_t mode, int64_t resolve);

int main(void) {
    if (pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR) != 0) return 2;
    int32_t frame_map[1] = {2};
    int32_t continuation_map[1] = {-1};
    PyObject *frame_slots[2] = {NULL, NULL};
    PyObject *continuation_slots[1] = {NULL};
    PyObject *scheduler_root = NULL;
    pcc_gc_frame_enter(frame_map, frame_slots);
    pcc_gc_register_continuation_root(continuation_map, continuation_slots);
    pcc_gc_scheduler_root_register(&scheduler_root);
    /* The builtin exception and type caches are visited too; their size
       follows the runtime's builtin tables, so report them separately. */
    int64_t builtin = pcc_gc_visit_builtin_exception_cache_slots(1, 0);
    printf("registered:%lld builtin:%lld\n",
           (long long)(pcc_gc_visit_registered_root_slots(1, 0) - builtin),
           (long long)builtin);
    pcc_gc_scheduler_root_unregister(&scheduler_root);
    pcc_gc_unregister_continuation_root(continuation_slots);
    pcc_gc_frame_leave(frame_slots);
    return 0;
}
'''


def _thread_harness_source() -> str:
    return r'''
#include "py_runtime.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>

enum { THREADS = 4, ROUNDS = 512 };
static PyObject *slots[THREADS][2];
static int32_t maps[THREADS][1] = {{-2}, {-2}, {-2}, {-2}};

static void *mutate(void *raw) {
    intptr_t index = (intptr_t)raw;
    for (int i = 0; i < ROUNDS; i++) {
        pcc_gc_register_continuation_root(maps[index], slots[index]);
        pcc_gc_unregister_continuation_root(slots[index]);
    }
    return NULL;
}

static void *observe(void *raw) {
    (void)raw;
    for (int i = 0; i < THREADS * ROUNDS; i++) {
        int64_t traced = pcc_gc_trace_continuation_roots();
        int64_t rewritten = pcc_gc_rewrite_continuation_roots();
        if (traced < 0 || traced > THREADS * 2) return (void *)1;
        if (rewritten != 0) return (void *)1;
    }
    return NULL;
}

int main(void) {
    pthread_t workers[THREADS];
    pthread_t observer;
    for (intptr_t i = 0; i < THREADS; i++) {
        if (pthread_create(&workers[i], NULL, mutate, (void *)i) != 0) return 2;
    }
    if (pthread_create(&observer, NULL, observe, NULL) != 0) return 3;
    for (int i = 0; i < THREADS; i++) pthread_join(workers[i], NULL);
    void *result = NULL;
    pthread_join(observer, &result);
    if (result != NULL) return 4;
    printf("final:%lld,%lld\n",
           (long long)pcc_gc_trace_continuation_roots(),
           (long long)pcc_gc_rewrite_continuation_roots());
    return 0;
}
'''


def _precise_stackmap_harness_source() -> str:
    symbol = "consumer_probe"
    payload = encode_stack_map(
        PreciseStackMap(
            arch=ARCH_AARCH64,
            functions=(FunctionStackMap(
                function_id=function_id(symbol),
                function_address=0x1000,
                code_size=16,
                frame_size=32,
                records=(SafepointRecord(
                    safepoint_id=safepoint_id(symbol, 0, SAFEPOINT_CALL),
                    instruction_offset=4,
                    kind=SAFEPOINT_CALL,
                    locations=(StackMapLocation(
                        kind=LOCATION_STACK_INDIRECT,
                        flags=LOCATION_MANAGED | LOCATION_OWNED,
                        register=29,
                        base_index=NO_BASE,
                        offset=-8,
                    ),),
                ),),
            ),),
        ),
        final_image=True,
    )
    byte_text = ", ".join(str(value) for value in payload)
    # Derive the byte to corrupt instead of hard-coding it.  Since the format
    # interned locations into one shared table at the end of the payload, a
    # fixed offset no longer lands on a location at all, so the fail-closed
    # assertion below silently stopped testing anything.  Locations are the
    # last `table_count` 16-byte entries; byte 1 of one holds its flags, and
    # clearing them drops the `managed` bit that validation requires.
    table_count = int.from_bytes(payload[16:20], "little")
    flags_offset = len(payload) - table_count * 16 + 1
    # Raw: the C body below contains printf escapes such as `\n` that must
    # reach clang as two characters, not as a real newline inside a string
    # literal (which is a hard C syntax error).
    return rf'''
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef struct {{
    PyObjectHeader h;
    int64_t length;
    int64_t capacity;
    PyObject **items;
}} ProbeListObject;

static const unsigned char STACKMAP[] = {{{byte_text}}};

int main(void) {{
    if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
    int32_t frame_map[1] = {{1}};
    PyObject *frame[4] = {{NULL, NULL, NULL, NULL}};
    ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(64, PY_TYPE_LIST, 0);
    if (obj == NULL) return 3;
    obj->length = 0;
    obj->capacity = 0;
    obj->items = NULL;
    /* Relocation skips an owner until its initialization is published. */
    pcc_gc_publish_initialized((PyObject *)obj);
    int64_t stable = pcc_gc_object_id((PyObject *)obj);
    pcc_gc_store_root(&frame[3], (PyObject *)obj);
    pcc_gc_register_continuation_root(frame_map, &frame[3]);
    if (pcc_gc_select_relocation_set(1) != 1) return 4;
    PyObject *moved = pcc_gc_relocate_copy((PyObject *)obj, 64);
    if (moved == NULL) return 5;
    int64_t rewritten = pcc_gc_consume_precise_stackmap(
        STACKMAP, sizeof(STACKMAP), 0x1004, &frame[4], 1, 3
    );
    printf("precise:%lld,%lld,%lld\n",
           (long long)rewritten,
           (long long)(frame[3] != (PyObject *)obj),
           (long long)(pcc_gc_object_id(frame[3]) == stable));

    unsigned char malformed[sizeof(STACKMAP)];
    memcpy(malformed, STACKMAP, sizeof(STACKMAP));
    malformed[{flags_offset}] = 0; /* location flags: raw pointer must fail closed */
    int64_t rejected = pcc_gc_consume_precise_stackmap(
        malformed, sizeof(malformed), 0x1004, &frame[4], 1, 3
    );
    printf("raw:%lld\n", (long long)rejected);
    pcc_gc_release((PyObject *)obj);
    pcc_gc_release(moved);
    pcc_gc_store_root(&frame[3], NULL);
    pcc_gc_unregister_continuation_root(&frame[3]);
    return 0;
}}
'''


def _link_harness(tmp_path: Path, name: str, source_text: str, archive: Path) -> Path:
    source = tmp_path / (name + ".c")
    executable = tmp_path / name
    source.write_text(source_text, encoding="utf-8")
    result = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(source),
            str(archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def _run_ok(implementation: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(implementation)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_archive_owns_mapped_visitor_gc0_to_gc4(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    owners: dict[str, list[str]] = {
        symbol: [] for symbol in PUBLIC_SYMBOLS | INTERNAL_SYMBOLS
    }
    for line in symbols_result.stdout.splitlines():
        symbol = line.split()[-1].lstrip("_") if line.strip() else ""
        if symbol in owners and " U " not in line:
            owners[symbol].append(line)
    assert all(len(lines) == 1 for lines in owners.values())
    assert all(
        ":freestanding_gc_mapped_roots.o:" in lines[0]
        for lines in owners.values()
    )

    implementation = _link_harness(
        tmp_path,
        "mapped_pcc_python",
        _mapped_harness_source(),
        pcc_runtime_archive,
    )
    for backend in range(5):
        output = _run_ok(implementation, {**os.environ, "PCC_GC_BACKEND": str(backend)})
        assert output == "mapped:3,0\nempty:0\n"


def test_backend4_precise_stackmap_consumer_rewrites_exact_frame_location(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    implementation = _link_harness(
        tmp_path,
        "precise_stackmap_pcc_python",
        _precise_stackmap_harness_source(),
        pcc_runtime_archive,
    )
    result = subprocess.run(
        [str(implementation)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "precise:1,1,1\nraw:-10\n"

    relocation_implementation = _link_harness(
        tmp_path,
        "mapped_relocation_pcc_python",
        _relocation_harness_source(),
        pcc_runtime_archive,
    )
    assert _run_ok(relocation_implementation, dict(os.environ)) == "relocate:1,1,1,1\n"

    registered_scan = _link_harness(
        tmp_path,
        "mapped_registered_scan_pcc_python",
        _registered_scan_harness_source(),
        pcc_runtime_archive,
    )
    registered_result = subprocess.run(
        [str(registered_scan)], capture_output=True, text=True, timeout=30
    )
    assert registered_result.returncode == 0, (
        registered_result.stdout + registered_result.stderr
    )
    # Frame (2 slots), continuation (1) and scheduler (1) roots, each once,
    # plus at least every builtin exception cache slot.
    registered, builtin = (
        int(field.split(":")[1]) for field in registered_result.stdout.split()
    )
    assert registered == 4, registered_result.stdout
    from pcc.runtime.py.py_abi_constants import PY_EXC_N_BUILTIN
    assert builtin >= PY_EXC_N_BUILTIN, registered_result.stdout


def test_mapped_root_visitor_survives_threaded_registry_mutation(
    tmp_path: Path,
):
    threaded_pcc_python_archive = (
        cached_threaded_pcc_python_runtime() / "libpy_runtime_pcc_py.a"
    )
    implementation = _link_harness(
        tmp_path,
        "mapped_threads_pcc_python",
        _thread_harness_source(),
        threaded_pcc_python_archive,
    )
    for backend in range(5):
        output = _run_ok(implementation, {**os.environ, "PCC_GC_BACKEND": str(backend)})
        assert output == "final:0,0\n"
