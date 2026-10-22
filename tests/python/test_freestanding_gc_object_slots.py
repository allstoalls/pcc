from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_RUNTIME_GLOBALS
from pcc.runtime.py import py_abi_constants


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_object_slots.py"
BACKEND_SOURCE = RUNTIME_DIR / "py" / "py_gc_backend.py"
OBJ_GC_SOURCE = RUNTIME_DIR / "py" / "py_obj_gc.py"
MAKEFILE = RUNTIME_DIR / "Makefile"

OWNED_SYMBOLS = {
    "pcc_gc_memoryview_initialize_owned_buffer",
    "pcc_gc_memoryview_refresh_owned_buffer",
    "pcc_gc_visit_object_slots",
    "pcc_gc_visit_object_slots_slice",
    "pcc_gc_object_slots_visit_slot",
    "pcc_gc_object_slots_visit_core_container_slots",
    "pcc_gc_object_slots_visit_fixed_owner_slots",
    "pcc_gc_object_slots_visit_weakref_slots",
    "pcc_gc_object_slots_visit_continuation_slots",
    "pcc_gc_object_slots_visit_class_slots",
    "pcc_gc_object_slots_visit_instance_slots",
    "pcc_gc_object_slots_has_no_pointer_slots",
}
RAW_FUNCTION_IMPORTS = {
    "memset",
    "pcc_capi_is_cext_type_tag",
    "pcc_capi_visit_cext_object_slots_i64",
    "pcc_gc_load_ptr",
}
RAW_GLOBAL_IMPORTS = {"py_set_dummy"}


def test_object_slot_freestanding_abi_is_generated_from_header_layout():
    expected = {
        "object.dict_entry.key_offset": "DICTENTRY_KEY_OFFSET",
        "object.dict_entry.size": "DICTENTRY_SIZE",
        "object.dict_entry.value_offset": "DICTENTRY_VALUE_OFFSET",
        "object.bytes.byte_len_offset": "PYBYTESOBJECT_BYTE_LEN_OFFSET",
        "object.bytes.data_offset": "PYBYTESOBJECT_DATA_OFFSET",
        "object.class.attrs_offset": "PYCLASSOBJECT_ATTRS_OFFSET",
        "object.class.del_method_offset": "PYCLASSOBJECT_DEL_METHOD_OFFSET",
        "object.class.metaclass_offset": "PYCLASSOBJECT_METACLASS_OFFSET",
        "object.dict.entries_offset": "PYDICTOBJECT_ENTRIES_OFFSET",
        "object.dict.entries_used_offset": "PYDICTOBJECT_ENTRIES_USED_OFFSET",
        "object.list.items_offset": "PYLISTOBJECT_ITEMS_OFFSET",
        "object.list.length_offset": "PYLISTOBJECT_LENGTH_OFFSET",
        "object.memoryview.base_offset": "PYMEMORYVIEWOBJECT_BASE_OFFSET",
        "object.header.type_tag_offset": "PYOBJECTHEADER_TYPE_TAG_OFFSET",
        "object.tuple.items_offset": "PYTUPLEOBJECT_ITEMS_OFFSET",
        "object.tuple.length_offset": "PYTUPLEOBJECT_LEN_OFFSET",
        "object.type.bytearray": "PY_TYPE_BYTEARRAY",
        "object.type.bytes": "PY_TYPE_BYTES",
        "object.type.class": "PY_TYPE_CLASS",
        "object.type.dict": "PY_TYPE_DICT",
        "object.type.instance": "PY_TYPE_INSTANCE",
        "object.type.list": "PY_TYPE_LIST",
        "object.type.memoryview": "PY_TYPE_MEMORYVIEW",
        "object.type.set": "PY_TYPE_SET",
        "object.type.tuple": "PY_TYPE_TUPLE",
        "object.type.valuebox": "PY_TYPE_VALUEBOX",
        "object.type.weakref": "PY_TYPE_WEAKREF",
    }
    for abi_name, generated_name in expected.items():
        assert ABI_CONSTANTS[abi_name] == getattr(
            py_abi_constants, generated_name
        )
    source = STRICT_SOURCE.read_text(encoding="utf-8")
    assert "pcc.runtime.py.py_abi_constants" not in source
    for abi_name in expected:
        assert f'abi_constant("{abi_name}")' in source


def _compile_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = tmp_path / ("freestanding_gc_object_slots_" + emitter + ".ll")
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

        source = tmp_path / "freestanding_gc_object_slots.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    obj = tmp_path / ("freestanding_gc_object_slots_" + emitter + ".o")
    result = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return obj


def _exported_symbols(source: str) -> set[str]:
    return set(re.findall(r'@c_abi_export\("([^"]+)"\)', source))


def test_object_slot_contract_has_one_production_graph_owner(
    pcc_runtime_archive: Path,
):
    strict = STRICT_SOURCE.read_text(encoding="utf-8")
    backend = BACKEND_SOURCE.read_text(encoding="utf-8")
    obj_gc = OBJ_GC_SOURCE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert _exported_symbols(strict) == OWNED_SYMBOLS
    assert "freestanding_gc_object_slots" in makefile
    for source in (backend, obj_gc):
        assert "pcc_gc_visit_object_slots = extern(" in source
        assert '"pcc_gc_visit_object_slots", (c_ptr, c_ptr, c_ptr), c_int64' in source
        assert "def _py_obj_visit_core_container_owner_slots" not in source
        assert "def _py_obj_gc_visit_core_container_owner_slots" not in source
        assert "def _py_obj_visit_class_slots" not in source
        assert "def _py_obj_gc_visit_class_slots" not in source

    symbols = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    for symbol in OWNED_SYMBOLS:
        owners = [
            line
            for line in symbols.stdout.splitlines()
            if line.strip()
            and line.split()[-1].lstrip("_") == symbol
            and " U " not in line
        ]
        assert len(owners) == 1, (symbol, owners)
        assert ":freestanding_gc_object_slots.o:" in owners[0]


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_object_slot_contract_has_exact_raw_object_closure(
    tmp_path: Path, emitter: str
):
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


def test_object_slot_contract_enumerates_list_owned_slots(tmp_path: Path):
    obj = _compile_object(tmp_path, "llvm")
    harness = tmp_path / "object_slots_list.c"
    executable = tmp_path / "object_slots_list"
    harness.write_text(
        r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void *expected_base;
static int calls;
void *py_set_dummy;

void *pcc_gc_load_ptr(void *base, void *slot) {
    (void)base;
    return *(void **)slot;
}

int64_t pcc_capi_is_cext_type_tag(int64_t tag) {
    (void)tag;
    return 0;
}

int32_t pcc_capi_visit_cext_object_slots_i64(
    void *object, void *visitor, void *context
) {
    (void)object; (void)visitor; (void)context;
    return 0;
}

static void slot_visitor(
    void *slot,
    int64_t role,
    void *context
) {
    int *count = (int *)context;
    printf("slot:%lld,%lld\n",
           (long long)((unsigned char *)slot - (unsigned char *)expected_base),
           (long long)role);
    *count += 1;
}

extern int64_t pcc_gc_visit_object_slots(
    void *object,
    void *visitor,
    void *context
);

int main(void) {
    unsigned char object[64];
    void *items[2] = {(void *)1, (void *)2};
    memset(object, 0, sizeof(object));
    *(int32_t *)(object + 8) = 5;   /* PY_TYPE_LIST */
    *(int64_t *)(object + 16) = 2;
    *(void ***)(object + 32) = items;
    expected_base = items;
    int64_t handled = pcc_gc_visit_object_slots(object, slot_visitor, &calls);
    printf("handled:%lld,calls:%d\n", (long long)handled, calls);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == (
        "slot:0,1\n"
        "slot:8,1\n"
        "handled:1,calls:2\n"
    )


def test_object_slot_contract_preserves_weakref_roles(tmp_path: Path):
    obj = _compile_object(tmp_path, "llvm")
    harness = tmp_path / "object_slots_weakref.c"
    executable = tmp_path / "object_slots_weakref"
    harness.write_text(
        r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>

void *py_set_dummy;
static void *object_base;

void *pcc_gc_load_ptr(void *base, void *slot) {
    (void)base;
    return *(void **)slot;
}

int64_t pcc_capi_is_cext_type_tag(int64_t tag) {
    (void)tag;
    return 0;
}

int32_t pcc_capi_visit_cext_object_slots_i64(
    void *object, void *visitor, void *context
) {
    (void)object; (void)visitor; (void)context;
    return 0;
}

static void slot_visitor(
    void *slot,
    int64_t role,
    void *context
) {
    (void)context;
    printf("slot:%lld,%lld\n",
           (long long)((unsigned char *)slot - (unsigned char *)object_base),
           (long long)role);
}

extern int64_t pcc_gc_visit_object_slots(
    void *object,
    void *visitor,
    void *context
);

int main(void) {
    unsigned char object[48];
    memset(object, 0, sizeof(object));
    *(int32_t *)(object + 8) = 21;  /* PY_TYPE_WEAKREF */
    *(void **)(object + 16) = (void *)1;
    *(void **)(object + 24) = (void *)2;
    object_base = object;
    printf("handled:%lld\n",
           (long long)pcc_gc_visit_object_slots(object, slot_visitor, NULL));
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "slot:16,3\nslot:24,1\nhandled:1\n"


def test_object_slot_contract_covers_runtime_layout_families(tmp_path: Path):
    obj = _compile_object(tmp_path, "llvm")
    harness = tmp_path / "object_slots_families.c"
    executable = tmp_path / "object_slots_families"
    harness.write_text(
        r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>

void *py_set_dummy = (void *)0xd00d;
static const char *label;
static void *object_base;

void *pcc_gc_load_ptr(void *base, void *slot) {
    (void)base;
    return *(void **)slot;
}

static void slot_visitor(
    void *slot,
    int64_t role,
    void *context
) {
    (void)context;
    unsigned char *raw = (unsigned char *)slot;
    unsigned char *object = (unsigned char *)object_base;
    if (raw >= object && raw < object + 160) {
        printf("%s:o%lldr%lld\n", label,
               (long long)(raw - object), (long long)role);
    } else {
        printf("%s:xr%lld\n", label, (long long)role);
    }
}

int64_t pcc_capi_is_cext_type_tag(int64_t tag) {
    (void)tag;
    return 0;
}

int32_t pcc_capi_visit_cext_object_slots_i64(
    void *object, void *visitor, void *context
) {
    (void)object; (void)visitor; (void)context;
    return 0;
}

extern int64_t pcc_gc_visit_object_slots(
    void *object,
    void *visitor,
    void *context
);

static void visit(const char *name, unsigned char *object) {
    label = name;
    object_base = object;
    printf("%s:h%lld\n", name,
           (long long)pcc_gc_visit_object_slots(object, slot_visitor, NULL));
}

int main(void) {
    unsigned char object[160];
    unsigned char aux[160];

    memset(object, 0, sizeof(object));
    *(int32_t *)(object + 8) = 7;  /* tuple */
    *(int64_t *)(object + 16) = 2;
    visit("tuple", object);

    memset(object, 0, sizeof(object));
    memset(aux, 0, sizeof(aux));
    *(int32_t *)(object + 8) = 6;  /* dict */
    *(void **)(object + 40) = aux;
    *(int64_t *)(object + 48) = 2;
    *(void **)(aux + 8) = (void *)1;
    *(void **)(aux + 16) = (void *)2;
    visit("dict", object);

    memset(object, 0, sizeof(object));
    memset(aux, 0, sizeof(aux));
    *(int32_t *)(object + 8) = 8;  /* set */
    *(int64_t *)(object + 24) = 3;
    *(void **)(object + 40) = aux;
    *(void **)(aux + 8) = (void *)1;
    *(void **)(aux + 24) = py_set_dummy;
    visit("set", object);

    memset(object, 0, sizeof(object));
    *(int32_t *)(object + 8) = 9;  /* function */
    visit("func", object);

    memset(object, 0, sizeof(object));
    memset(aux, 0, sizeof(aux));
    void *continuation_slots[2] = {(void *)1, (void *)2};
    *(int32_t *)(object + 8) = 29;  /* continuation */
    *(void **)(object + 24) = aux;
    *(int64_t *)(aux + 8) = 2;
    *(void ***)(aux + 16) = continuation_slots;
    visit("continuation", object);

    memset(object, 0, sizeof(object));
    void *bases[1] = {(void *)1};
    void *mro[1] = {(void *)2};
    unsigned char methods[16];
    memset(methods, 0, sizeof(methods));
    *(int32_t *)(object + 8) = 10;  /* class */
    *(int32_t *)(object + 24) = 1;
    *(void ***)(object + 32) = bases;
    *(int32_t *)(object + 40) = 1;
    *(void ***)(object + 48) = mro;
    *(int32_t *)(object + 56) = 1;
    *(void **)(object + 64) = methods;
    visit("class", object);

    memset(object, 0, sizeof(object));
    memset(aux, 0, sizeof(aux));
    *(int32_t *)(object + 8) = 11;  /* instance */
    *(void **)(object + 16) = aux;
    *(int32_t *)(aux + 8) = 10;  /* live class */
    *(int32_t *)(aux + 72) = 2;
    visit("instance", object);

    memset(object, 0, sizeof(object));
    *(int32_t *)(object + 8) = 2;  /* pointer-free int */
    visit("int", object);

    memset(object, 0, sizeof(object));
    *(int32_t *)(object + 8) = 31;  /* vthread channel, unknown kind */
    visit("unknown", object);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == (
        "tuple:o24r1\n"
        "tuple:o32r1\n"
        "tuple:h1\n"
        "dict:xr1\n"
        "dict:xr1\n"
        "dict:h1\n"
        "set:xr1\n"
        "set:h1\n"
        "func:o24r1\n"
        "func:o32r1\n"
        "func:o40r1\n"
        "func:o64r1\n"
        "func:o80r1\n"
        "func:o88r1\n"
        "func:h1\n"
        "continuation:xr1\n"
        "continuation:xr1\n"
        "continuation:h1\n"
        "class:xr2\n"
        "class:xr2\n"
        "class:xr3\n"
        "class:o96r3\n"
        "class:o104r1\n"
        # f3a043008 made the class own its metaclass (role 1, as attrs).
        "class:o112r1\n"
        "class:h1\n"
        "instance:o16r2\n"
        "instance:o24r1\n"
        "instance:o32r1\n"
        "instance:o40r1\n"
        "instance:h1\n"
        "int:h1\n"
        "unknown:h1\n"
    )


def test_object_slot_contract_delegates_cext_slots_with_same_callback(
    tmp_path: Path,
):
    obj = _compile_object(tmp_path, "llvm")
    harness = tmp_path / "object_slots_cext.c"
    executable = tmp_path / "object_slots_cext"
    harness.write_text(
        r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>

void *py_set_dummy;
static void *cext_slots[2];

void *pcc_gc_load_ptr(void *base, void *slot) {
    (void)base;
    return *(void **)slot;
}

int64_t pcc_capi_is_cext_type_tag(int64_t tag) {
    return tag == 500;
}

int32_t pcc_capi_visit_cext_object_slots_i64(
    void *object,
    void (*visitor)(void *, int64_t, void *),
    void *context
) {
    (void)object;
    visitor(&cext_slots[0], 2, context);
    visitor(&cext_slots[1], 3, context);
    return 1;
}

static void slot_visitor(void *slot, int64_t role, void *context) {
    int *calls = (int *)context;
    printf("slot:%lld,%lld\n",
           (long long)((void **)slot - cext_slots),
           (long long)role);
    *calls += 1;
}

extern int64_t pcc_gc_visit_object_slots(
    void *object,
    void *visitor,
    void *context
);

int main(void) {
    unsigned char object[32];
    int calls = 0;
    memset(object, 0, sizeof(object));
    *(int32_t *)(object + 8) = 500;
    printf("handled:%lld\n",
           (long long)pcc_gc_visit_object_slots(
               object, slot_visitor, &calls));
    printf("calls:%d\n", calls);
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "slot:0,2\nslot:1,3\nhandled:1\ncalls:2\n"
