"""Backend-4 remap plumbing: pcc_gc_visit_object_slots (C harness).

Stage-2 plumbing from docs/plans/gc4-relocation-remap-plan.md: the
slot-ADDRESS walker that relocation remap, tracing and sweeping share.
The probe builds containers through the public C API, counts the slots
the walker hands out per role (owned / borrowed / update-only), and
proves in-place rewrite through a public accessor. Coverage parity with
the trace walker is a review obligation (same per-type switch); this
gate covers the representative container types end-to-end.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
STRICT_OBJECT_SLOTS = RUNTIME_DIR / "py" / "freestanding_gc_object_slots.py"
STRICT_BACKEND0_SLOTS = (
    RUNTIME_DIR / "py" / "freestanding_gc_backend0_slots.py"
)
STRICT_BACKEND0_COLLECTOR = (
    RUNTIME_DIR / "py" / "freestanding_gc_backend0_collector.py"
)
STRICT_COMMON_MARK_CYCLE = (
    RUNTIME_DIR / "py" / "freestanding_gc_common_mark_cycle.py"
)
STRICT_GENERATIONAL_PROMOTION = (
    RUNTIME_DIR / "py" / "freestanding_gc_generational_promotion.py"
)
STRICT_RELOCATION_REMAP = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_remap.py"
)
STRICT_RELOCATION_PAYLOAD = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
)
STRICT_SWEEP_SLOTS = RUNTIME_DIR / "py" / "freestanding_gc_sweep_slots.py"
STRICT_TRACING_SWEEP_COLLECTOR = (
    RUNTIME_DIR / "py" / "freestanding_gc_tracing_sweep_collector.py"
)


def test_backend4_relocation_reuses_shared_slot_contract():
    py_source = STRICT_RELOCATION_PAYLOAD.read_text(encoding="utf-8")

    assert "pcc_gc_visit_object_slots(from_obj, _relocate_count_slot" in py_source
    assert "pcc_gc_visit_object_slots(from_obj, _relocate_from_slot" in py_source
    assert "pcc_gc_visit_object_slots(to_obj, _relocate_to_slot" in py_source
    assert "pcc_gc_backend4_remap_heal_slot" in py_source
    py_payload = py_source.split("def pcc_gc_relocate_copy_payload(", 1)[1]
    assert "py_incref(" not in py_payload
    assert "pcc_gc_backend4_remembered_set_retarget_slot(" not in py_payload


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_pcc_python_runtime()


def test_pyclass_layout_matches_pcc_python_mirror(tmp_path):
    """The C struct is the owner; the port's complete layout table is checked."""
    src = tmp_path / "pyclass_layout_probe.c"
    exe = tmp_path / "pyclass_layout_probe.out"
    fields = (
        "h",
        "name",
        "n_bases",
        "bases",
        "n_mro",
        "mro",
        "n_methods",
        "methods",
        "n_fields",
        "field_names",
        "instance_size",
        "type_tag_alloc",
        "del_method",
        "attrs",
        "metaclass",
    )
    emit_fields = "\n".join(
        f'    printf("class.{field} %zu\\n", offsetof(PyClassObject, {field}));'
        for field in fields
    )
    src.write_text(
        textwrap.dedent(
            f"""
            #include "py_internal.h"
            #include <stddef.h>
            #include <stdio.h>

            int main(void) {{
                printf("class.size %zu\\n", sizeof(PyClassObject));
            {emit_fields}
                printf("method.size %zu\\n", sizeof(PyClassMethod));
                printf("method.name %zu\\n", offsetof(PyClassMethod, name));
                printf("method.func %zu\\n", offsetof(PyClassMethod, func));
                return 0;
            }}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(src),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    c_layout = {
        name: int(offset)
        for name, offset in (line.split() for line in result.stdout.splitlines())
    }

    # Compare against the GENERATED ABI constants, not against numbers repeated
    # in py_class.py's prose.  That module's own docstring states the rule --
    # "Numeric copies do not belong in this prose: the C headers and generator
    # are the layout authority" -- so the layout table this test used to parse
    # was deliberately removed, and the test kept requiring it.  Reading the
    # generated constants checks the same invariant (port offsets equal C
    # offsets) against the artifact that is actually authoritative.
    from pcc.runtime.py import py_abi_constants as abi

    mirror_layout = {
        "class.size": abi.PYCLASSOBJECT_SIZE,
        "class.h": 0,
        "class.name": abi.PYCLASSOBJECT_NAME_OFFSET,
        "class.n_bases": abi.PYCLASSOBJECT_N_BASES_OFFSET,
        "class.bases": abi.PYCLASSOBJECT_BASES_OFFSET,
        "class.n_mro": abi.PYCLASSOBJECT_N_MRO_OFFSET,
        "class.mro": abi.PYCLASSOBJECT_MRO_OFFSET,
        "class.n_methods": abi.PYCLASSOBJECT_N_METHODS_OFFSET,
        "class.methods": abi.PYCLASSOBJECT_METHODS_OFFSET,
        "class.n_fields": abi.PYCLASSOBJECT_N_FIELDS_OFFSET,
        "class.field_names": abi.PYCLASSOBJECT_FIELD_NAMES_OFFSET,
        "class.instance_size": abi.PYCLASSOBJECT_INSTANCE_SIZE_OFFSET,
        "class.type_tag_alloc": abi.PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET,
        "class.del_method": abi.PYCLASSOBJECT_DEL_METHOD_OFFSET,
        "class.attrs": abi.PYCLASSOBJECT_ATTRS_OFFSET,
        "class.metaclass": abi.PYCLASSOBJECT_METACLASS_OFFSET,
        "method.size": abi.PYCLASSMETHOD_SIZE,
        "method.name": abi.PYCLASSMETHOD_NAME_OFFSET,
        "method.func": abi.PYCLASSMETHOD_FUNC_OFFSET,
    }

    assert set(mirror_layout) == set(c_layout)
    assert mirror_layout == c_layout

    substrate_source = (RUNTIME_DIR / "py" / "py_substrate.py").read_text(
        encoding="utf-8"
    )
    object_root = substrate_source.split("def py_subs_object_root():", 1)[1]
    object_root = object_root.split("\ndef ", 1)[0]
    # The port allocates through the named ABI constant, not a literal, so pin
    # the constant's value (already checked equal to the C offset above) and
    # assert the named form.  Requiring the literal made this fail as soon as
    # the magic number was replaced by the generated constant.
    class_size = c_layout["class.size"]
    assert abi.PYCLASSOBJECT_SIZE == class_size
    assert "r = malloc(PYCLASSOBJECT_SIZE)" in object_root
    assert "memset(r, 0, PYCLASSOBJECT_SIZE)" in object_root

    visitor_source = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    visitor = visitor_source.split("def _visit_class_slots(", 1)[1].split(
        "\ndef ", 1
    )[0]
    # The visitor reads its offsets through abi_constant("object.class.<field>")
    # rather than literals.  Assert the named form and separately pin each
    # constant's value against the C offset, which is strictly stronger than
    # matching a literal: a wrong constant now fails, and a renamed-but-correct
    # spelling no longer does.
    for field, accessor in (
        ("n_bases", "load_i32"),
        ("bases", "load_ptr"),
        ("n_mro", "load_i32"),
        ("mro", "load_ptr"),
        ("n_methods", "load_i32"),
        ("methods", "load_ptr"),
    ):
        literal = f"{accessor}(o, {c_layout[f'class.{field}']})"
        named = f'{accessor}(o, abi_constant("object.class.{field}_offset"))'
        assert literal in visitor or named in visitor, (
            f"{field}: neither {literal!r} nor {named!r} found"
        )
    for field in ("del_method", "attrs", "metaclass"):
        literal = f"{c_layout[f'class.{field}']},"
        named = f'abi_constant("object.class.{field}_offset")'
        assert literal in visitor or named in visitor, (
            f"{field}: neither literal offset nor named constant found"
        )

    internal_header = (RUNTIME_DIR / "src" / "py_internal.h").read_text(
        encoding="utf-8"
    )
    for field in fields:
        assert f"PCC_ASSERT_CLASS_OFFSET({field}," in internal_header


def test_del_method_is_update_only_in_both_runtime_sources():
    py_dunder = (RUNTIME_DIR / "py" / "py_dunder.py").read_text(encoding="utf-8")
    py_class = (RUNTIME_DIR / "py" / "py_class.py").read_text(encoding="utf-8")
    py_gc = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")

    py_dispatch = py_dunder.split("def py_user_del_dispatch(o) -> None:", 1)[1]
    py_dispatch = py_dispatch.split("\n@c_abi_export", 1)[0]
    assert 'py_class_lookup(cls, cstr("__del__"))' in py_dispatch
    assert "load_ptr(cls, 96)" not in py_dispatch
    assert "store_ptr(cls, 96" not in py_dispatch

    py_add = py_class.split("def py_class_add_method(cls, name, func) -> None:", 1)[1]
    py_add = py_add.split("\n@c_abi_export", 1)[0]
    assert "store_ptr(cls, PYCLASSOBJECT_DEL_METHOD_OFFSET, func)" in py_add
    assert "_class_note_borrowed_metadata_slot_store" in py_add

    py_gc_class = py_gc.split("def _visit_class_slots(", 1)[1]
    py_gc_class = py_gc_class.split("\ndef ", 1)[0]
    assert 'abi_constant("object.class.del_method_offset")' in py_gc_class


def test_c_finalizer_ignores_stale_update_only_del_alias(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "del_method_update_only_probe.c"
    exe = tmp_path / "del_method_update_only_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_internal.h"
            #include <stdio.h>

            static int semantic_hits = 0;
            static int stale_hits = 0;

            static PyObject *semantic_del(PyObject *captures, PyObject *args) {
                (void)captures;
                (void)args;
                semantic_hits++;
                return py_int_from_i64(1);
            }

            static PyObject *stale_del(PyObject *captures, PyObject *args) {
                (void)captures;
                (void)args;
                stale_hits++;
                return py_int_from_i64(2);
            }

            int main(void) {
                PyClassObject *cls = py_class_new("Probe", NULL, 0, NULL, 0);
                PyObject *semantic = py_func_new_named(
                    (void *)semantic_del, NULL, "semantic_del"
                );
                PyObject *stale = py_func_new_named(
                    (void *)stale_del, NULL, "stale_del"
                );
                if (cls == NULL || semantic == NULL || stale == NULL) return 10;
                py_class_add_method(cls, "__del__", semantic);

                /* The alias participates in GC pointer updating but is not the
                 * semantic lookup owner.  Poison it to make cache reads fail. */
                cls->del_method = stale;
                PyObject *inst = py_instance_new(cls);
                if (inst == NULL) return 11;
                py_user_del_dispatch(inst);

                printf("%d\\n", semantic_hits == 1);
                printf("%d\\n", stale_hits == 0);
                printf("%d\\n", cls->del_method == stale);
                return 0;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["1", "1", "1"]


_PROBE = """
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int64_t pcc_gc_visit_object_slots(
    PyObject *o,
    void (*visit)(PyObject **slot, int64_t role, void *ctx),
    void *ctx
);
void py_class_set_metaclass(PyClassObject *cls, PyClassObject *metaclass);

/* Slot roles: 1 owned (traced and updated), 2 borrowed (traced, not
 * owned), 3 update-only metadata (rewritten on relocation, never a mark
 * edge).  Index 0 counts any other role, which must never appear. */
static int64_t g_roles[4];
static PyObject *g_sentinel = NULL;
static PyObject *g_rewrite_target = NULL;

static void count_and_rewrite(PyObject **slot, int64_t role, void *ctx) {
    (void)ctx;
    if (role >= 1 && role <= 3) {
        g_roles[role]++;
    } else {
        g_roles[0]++;
    }
    if (g_rewrite_target != NULL && *slot == g_rewrite_target) {
        *slot = g_sentinel;
    }
}

static int visit_expect(
    PyObject *o, int64_t owned, int64_t borrowed, int64_t update_only
) {
    memset(g_roles, 0, sizeof g_roles);
    if (pcc_gc_visit_object_slots(o, count_and_rewrite, NULL) != 1) return 0;
    return g_roles[0] == 0
        && g_roles[1] == owned
        && g_roles[2] == borrowed
        && g_roles[3] == update_only;
}

int main(void) {
    g_sentinel = py_str_new("SENTINEL", 8);

    /* list [10, 20, 30]: 3 owned slots; rewrite the 20 in place */
    PyObject *lst = py_list_new(4);
    PyObject *twenty = py_str_new("twenty", 6);
    py_list_append(lst, py_int_from_i64(10));
    py_list_append(lst, twenty);
    py_list_append(lst, py_int_from_i64(30));
    g_rewrite_target = twenty;
    printf("%d\\n", visit_expect(lst, 3, 0, 0));
    PyObject *got = py_list_getitem(lst, 1);
    printf("%d\\n", got == g_sentinel);

    /* tuple (1, 2): 2 slots */
    PyObject *tup = py_tuple_new(2);
    py_tuple_set_item(tup, 0, py_int_from_i64(1));
    py_tuple_set_item(tup, 1, py_int_from_i64(2));
    g_rewrite_target = NULL;
    printf("%d\\n", visit_expect(tup, 2, 0, 0));

    /* dict {"a": 1, "b": 2}: 2 entries -> 4 slots */
    PyObject *d = py_dict_new();
    py_dict_set(d, py_str_new("a", 1), py_int_from_i64(1));
    py_dict_set(d, py_str_new("b", 1), py_int_from_i64(2));
    printf("%d\\n", visit_expect(d, 4, 0, 0));

    /* set {"x"}: 1 occupied key slot */
    PyObject *s = py_set_new();
    py_set_add(s, py_str_new("x", 1));
    printf("%d\\n", visit_expect(s, 1, 0, 0));

    /* non-container (str): handled with zero slots, no crash */
    printf("%d\\n", visit_expect(g_sentinel, 0, 0, 0));

    /* instance: borrowed cls + declared fields + dynamic attrs slot */
    const char *field_names[2] = {"a", "b"};
    PyClassObject *cls = py_class_new("Probe", NULL, 0, field_names, 2);
    PyObject *inst_obj = py_instance_new(cls);
    PyInstanceObject *inst = (PyInstanceObject *)inst_obj;
    PyObject *field_value = py_str_new("field", 5);
    py_instance_set_field(inst, 0, field_value);
    py_instance_setattr(inst, "dyn", py_str_new("dyn", 3));
    g_rewrite_target = field_value;
    printf("%d\\n", visit_expect(inst_obj, 3, 1, 0));
    PyObject *field_got = py_instance_get_field(inst, 0);
    printf("%d\\n", field_got == g_sentinel);

    /* class: borrowed bases/mro + update-only method and __del__
     * metadata + owned attrs and metaclass (a retaining relation since
     * f3a043008, stored through its public setter) */
    PyClassObject *base_cls = py_class_new("Base", NULL, 0, NULL, 0);
    PyClassObject *meta_cls = py_class_new("Meta", NULL, 0, NULL, 0);
    PyClassObject *klass = (PyClassObject *)pcc_gc_alloc(
        sizeof(PyClassObject), PY_TYPE_CLASS, 0
    );
    if (base_cls == NULL || meta_cls == NULL || klass == NULL) return 10;
    memset((char *)klass + sizeof(PyObjectHeader), 0,
           sizeof(PyClassObject) - sizeof(PyObjectHeader));
    klass->n_bases = 1;
    klass->bases = (PyClassObject **)calloc(1, sizeof(PyClassObject *));
    klass->n_mro = 1;
    klass->mro = (PyClassObject **)calloc(1, sizeof(PyClassObject *));
    klass->n_methods = 1;
    klass->methods = (PyClassMethod *)calloc(1, sizeof(PyClassMethod));
    if (klass->bases == NULL || klass->mro == NULL || klass->methods == NULL) {
        return 11;
    }
    klass->bases[0] = base_cls;
    klass->mro[0] = base_cls;
    klass->methods[0].func = py_str_new("method", 6);
    klass->del_method = py_str_new("del", 3);
    klass->attrs = py_dict_new();
    py_class_set_metaclass(klass, meta_cls);
    g_rewrite_target = klass->attrs;
    printf("%d\\n", visit_expect((PyObject *)klass, 2, 2, 2));
    printf("%d\\n", klass->attrs == g_sentinel);
    return 0;
}
"""


def test_object_slot_visitor_counts_roles_and_rewrites(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "object_slot_visitor_probe.c"
    exe = tmp_path / "object_slot_visitor_probe.out"
    src.write_text(textwrap.dedent(_PROBE).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "1",
        "1",
        "1",
        "1",
        "1",
        "1",
        "1",
        "1",
        "1",
        "1",
    ]


def test_object_slot_visitor_routes_capi_extension_object_slots(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "object_slot_visitor_cext_probe.c"
    exe = tmp_path / "object_slot_visitor_cext_probe.out"
    src.write_text(
        textwrap.dedent("""
        #include "Python.h"
        #include <stdio.h>
        #include <stddef.h>

        int64_t pcc_gc_visit_object_slots(
            PyObject *o,
            void (*visit)(PyObject **slot, int64_t role, void *ctx),
            void *ctx
        );

        typedef struct ProbeCextObject {
            PyObject ob_base;
            PyObject *child;
        } ProbeCextObject;

        static int probe_traverse(PyObject *self, visitproc visit, void *arg) {
            ProbeCextObject *obj = (ProbeCextObject *)self;
            Py_VISIT(obj->child);
            return 0;
        }

        static PyTypeObject ProbeType = {
            .tp_name = "pcc_probe.Cext",
            .tp_basicsize = sizeof(ProbeCextObject),
            .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
            .tp_traverse = probe_traverse,
        };

        static int64_t g_count = 0;
        static int64_t g_role = 0;
        static PyObject *g_sentinel = NULL;
        static PyObject *g_rewrite_target = NULL;

        static void count_and_rewrite(PyObject **slot, int64_t role, void *ctx) {
            (void)ctx;
            g_count++;
            g_role = role;
            if (g_rewrite_target != NULL && *slot == g_rewrite_target) {
                *slot = g_sentinel;
            }
        }

        int main(void) {
            if (PyType_Ready(&ProbeType) != 0) return 2;
            g_sentinel = py_str_new("SENTINEL", 8);
            PyObject *child = py_str_new("cext-child", 10);
            ProbeCextObject *obj = (
                ProbeCextObject *
            )PyType_GenericAlloc(&ProbeType, 0);
            if (g_sentinel == NULL || child == NULL || obj == NULL) return 3;

            pcc_gc_store_ptr((PyObject *)obj, &obj->child, child);
            g_count = 0;
            g_rewrite_target = child;
            int64_t handled = pcc_gc_visit_object_slots(
                (PyObject *)obj, count_and_rewrite, NULL
            );

            printf("%d\\n", handled == 1 && g_count == 1 && g_role == 1);
            printf("%d\\n", obj->child == g_sentinel);
            printf("%d\\n", obj->ob_base.ob_type == &ProbeType);
            return 0;
        }
        """).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{REPO_ROOT / 'utils' / 'fake_libc_include'}",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["1", "1", "1"]


def test_capi_extension_dynamic_object_finalizer_and_dealloc_dispatch(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "cext_dynamic_dealloc_probe.c"
    exe = tmp_path / "cext_dynamic_dealloc_probe.out"
    src.write_text(
        textwrap.dedent("""
        #define PY_SSIZE_T_CLEAN
        #include "Python.h"
        #include <stdint.h>
        #include <stdio.h>

        void py_user_del_dispatch(PyObject *o);

        typedef struct ProbeManagedObject {
            PyObject_HEAD
            long value;
        } ProbeManagedObject;

        static long dealloc_hits = 0;

        static void probe_managed_dealloc(PyObject *self) {
            (void)self;
            dealloc_hits += 1;
        }

        static PyTypeObject ProbeManagedType = {
            PyVarObject_HEAD_INIT(NULL, 0)
            .tp_name = "pcc_probe.Managed",
            .tp_basicsize = sizeof(ProbeManagedObject),
            .tp_flags = Py_TPFLAGS_DEFAULT | PCC_TPFLAGS_MANAGED_DEALLOC,
            .tp_dealloc = probe_managed_dealloc,
            .tp_new = PyType_GenericNew,
        };

        int main(void) {
            if (PyType_Ready(&ProbeManagedType) != 0) return 2;
            PyObject *obj = PyType_GenericNew(&ProbeManagedType, NULL, NULL);
            if (obj == NULL) return 3;

            PyObjectHeader *header = (PyObjectHeader *)obj;
            int32_t tag_before = header->type_tag;
            py_user_del_dispatch(obj);

            printf("%d\\n", header->type_tag == tag_before);
            printf("%d\\n", (header->flags & 4) == 0);  /* PY_FLAG_FINALIZED */
            printf("%d\\n", dealloc_hits == 0);

            Py_DECREF(obj);
            printf("%d\\n", dealloc_hits == 1);
            return 0;
        }
        """).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{REPO_ROOT / 'utils' / 'fake_libc_include'}",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["1", "1", "1", "1"]


def test_capi_extension_object_slot_contract_source():
    header = (RUNTIME_DIR / "src" / "py_internal.h").read_text(encoding="utf-8")
    visit_port = (RUNTIME_DIR / "py" / "py_capi_visit_runtime.py").read_text(
        encoding="utf-8"
    )

    assert "int pcc_capi_visit_cext_object_slots(" in header

    cext_visit = "pcc_capi_visit_cext_object_slots(o, visit, ctx)"

    cext_exclude = "pcc_capi_is_cext_type_tag((int64_t)tag) != 0"
    user_instance_catchall = (
        "tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START"
    )

    assert '@c_abi_typed_export("pcc_capi_visit_cext_object_slots"' in visit_port


def test_pcc_python_cext_object_slot_bridge_source():
    header = (RUNTIME_DIR / "src" / "py_internal.h").read_text(encoding="utf-8")
    visit_port = (RUNTIME_DIR / "py" / "py_capi_visit_runtime.py").read_text(
        encoding="utf-8"
    )
    strict_slots_py = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    gc_backend_py = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    obj_gc_py = (RUNTIME_DIR / "py" / "py_obj_gc.py").read_text(encoding="utf-8")
    collector_py = STRICT_BACKEND0_COLLECTOR.read_text(encoding="utf-8")

    assert "PccPyObjSlotVisitorI64" in header
    assert "int pcc_capi_visit_cext_object_slots_i64(" in header
    assert '@c_abi_typed_export("pcc_capi_visit_cext_object_slots_i64"' in visit_port
    assert "pcc_capi_visit_cext_object_slot_i64_adapter" in visit_port
    strict_i64_bridge = visit_port.split(
        'def pcc_capi_visit_cext_object_slots_i64(', 1
    )[1]
    assert 'function_addr("pcc_capi_visit_cext_object_slot_i64_adapter")' in (
        strict_i64_bridge
    )
    assert 'function_addr("pcc_capi_visit_cext_object_slot_ref")' not in (
        strict_i64_bridge
    )

    assert "pcc_capi_visit_cext_object_slots_i64 = extern(" in strict_slots_py
    strict_dispatch = strict_slots_py.split(
        # The port migrated its return annotations from `int` to explicit
        # `i64`; split on the signature without the annotation so the marker
        # survives the next such migration too.
        "def pcc_gc_visit_object_slots(o, visitor, context)", 1
    )[1]
    cext_call = "pcc_capi_visit_cext_object_slots_i64("
    assert cext_call in strict_dispatch
    assert strict_dispatch.index("pcc_gc_visit_object_slots_slice(") < (
        strict_dispatch.index(cext_call)
    )

    assert "pcc_gc_visit_object_slots = extern(" in gc_backend_py
    covered_body = gc_backend_py.split(
        "def _py_obj_visit_covered_slots(",
        1,
    )[1].split("\ndef ", 1)[0]
    assert "pcc_gc_visit_object_slots(" in covered_body
    assert "_py_obj_visit_" in covered_body
    assert "pcc_capi_visit_cext_object_slots_i64" not in gc_backend_py

    assert "pcc_gc_visit_object_slots = extern(" in obj_gc_py
    append_body = obj_gc_py.split(
        "def _append_referents_to(o, out) -> None:", 1
    )[1].split("\ndef ", 1)[0]
    assert "pcc_gc_visit_object_slots(" in append_body
    assert "_py_obj_gc_visit_append_slot" in append_body
    assert "pcc_capi_visit_cext_object_slots_i64" not in obj_gc_py

    backend0_slots = STRICT_BACKEND0_SLOTS.read_text(encoding="utf-8")
    assert "pcc_gc_visit_object_slots = extern(" in backend0_slots
    assert "pcc_capi_visit_cext_object_slots_i64" not in backend0_slots


def test_capi_extension_dynamic_tags_do_not_use_instance_layout_source():
    gc_backend_py = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    obj_gc_py = (RUNTIME_DIR / "py" / "py_obj_gc.py").read_text(encoding="utf-8")
    collector_py = STRICT_BACKEND0_COLLECTOR.read_text(encoding="utf-8")
    tracing_collector_py = STRICT_TRACING_SWEEP_COLLECTOR.read_text(
        encoding="utf-8"
    )
    obj_dealloc_py = (RUNTIME_DIR / "py" / "py_obj_dealloc.py").read_text(
        encoding="utf-8"
    )
    dunder_py = (RUNTIME_DIR / "py" / "py_dunder.py").read_text(encoding="utf-8")
    strict_slots_py = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    relocation_payload = STRICT_RELOCATION_PAYLOAD.read_text(encoding="utf-8")

    obj_maybe_guard = "pcc_capi_is_cext_type_tag((int64_t)tag) != 0"

    obj_dealloc_cext = "pcc_capi_dealloc_cext_object(o, (int64_t)h->type_tag) != 0"

    tracing_cext = "pcc_capi_dealloc_cext_object(o, (int64_t)h->type_tag) != 0"

    sweep_guard = "pcc_capi_is_cext_type_tag((int64_t)py_header(o)->type_tag) == 0"

    assert "pcc_capi_is_cext_type_tag = extern(" in gc_backend_py
    assert "pcc_capi_dealloc_cext_object = extern(" in tracing_collector_py
    py_instance_body = strict_slots_py.split(
        "def _visit_instance_slots(o, visitor, context)", 1
    )[1].split("\ndef ", 1)[0]
    assert "pcc_capi_is_cext_type_tag(tag) != 0" in py_instance_body
    assert py_instance_body.index("pcc_capi_is_cext_type_tag(tag) != 0") < (
        py_instance_body.index('tag != abi_constant("object.type.instance")')
    )
    assert 'tag != abi_constant("object.type.valuebox")' in py_instance_body
    assert 'tag < abi_constant("object.type.user_class_start")' in py_instance_body

    relocation_remap = STRICT_RELOCATION_REMAP.read_text(encoding="utf-8")
    py_colored_body = relocation_remap.split(
        "def pcc_gc_backend4_relocate_copy_supported_tag", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_remap_heal_slot")', 1)[0]
    py_cext_guard = "if pcc_capi_is_cext_type_tag(tag) != 0:"
    assert py_cext_guard in py_colored_body
    assert py_colored_body.index(py_cext_guard) < py_colored_body.index(
        'tag == abi_constant("object.type.instance")'
    )
    assert 'tag >= abi_constant("object.type.user_class_start")' in py_colored_body

    py_relocate_start = relocation_payload.index(
        "def pcc_gc_relocate_copy_payload_prepared_locked("
    )
    py_relocate_body = relocation_payload[
        py_relocate_start : relocation_payload.index(
            '@c_abi_export("pcc_gc_relocate_copy_payload")', py_relocate_start
        )
    ]
    assert py_cext_guard in py_relocate_body
    assert py_relocate_body.index(py_cext_guard) < py_relocate_body.index(
        'tag == abi_constant("object.type.instance")'
    )
    assert 'tag >= abi_constant("object.type.user_class_start")' in py_relocate_body
    assert "cls = pcc_gc_load_ptr(" in py_relocate_body
    assert "_relocate_copy_payload_finish(" in py_relocate_body
    assert (
        "child = pcc_gc_load_ptr_extern(from_obj, ptr_add(from_obj, offset))"
        not in py_relocate_body
    )
    py_wrapper = relocation_payload.split(
        "def pcc_gc_relocate_copy_payload(from_obj, to_obj, tag: i64, size: i64)",
        1,
    )[1]
    assert "_relocate_slot_pairs_prepare(from_obj, to_obj, size)" in py_wrapper
    assert "pcc_gc_relocate_copy_payload_prepared_locked(" in py_wrapper
    assert "_relocate_slot_pairs_dispose(ctx)" in py_wrapper

    py_finalize_body = tracing_collector_py[
        tracing_collector_py.index(
            "def pcc_gc_tracing_finalize_unreachable(obj) -> None:"
        ) : tracing_collector_py.index(
            '@c_abi_export("pcc_gc_tracing_recheck_reachability_after_finalizers")'
        )
    ]
    assert "pcc_capi_dealloc_cext_object(obj, tag) == 0" in py_finalize_body
    assert py_finalize_body.index(
        "pcc_capi_dealloc_cext_object(obj, tag) == 0"
    ) < py_finalize_body.index(
        'if tag >= abi_constant("object.type.user_class_start"):'
    )

    py_sweep_body = tracing_collector_py[
        tracing_collector_py.index(
            "def pcc_gc_tracing_sweep_unreachable("
        ) : tracing_collector_py.rindex(
            "    pcc_gc_tracing_recheck_reachability_after_finalizers()"
        )
    ]
    assert "pcc_capi_is_cext_type_tag(tag) == 0" in py_sweep_body
    assert py_sweep_body.index("pcc_capi_is_cext_type_tag(tag) == 0") < (
        py_sweep_body.index("py_user_del_dispatch(obj)")
    )

    assert "pcc_capi_is_cext_type_tag = extern(" in collector_py
    assert "pcc_capi_dealloc_cext_object = extern(" in collector_py
    assert "pcc_gc_visit_object_slots = extern(" in obj_gc_py
    py_obj_maybe_body = collector_py[
        collector_py.index("def _maybe_finalize_unreachable(") : collector_py.index(
            "def _dealloc_unreachable(obj)"
        )
    ]
    assert "pcc_capi_is_cext_type_tag(tag) == 0" in py_obj_maybe_body
    py_obj_dealloc_body = collector_py[
        collector_py.index("def _dealloc_unreachable(obj) -> None:") : collector_py.index(
            '@c_abi_export("py_gc_collect")'
        )
    ]
    assert "pcc_capi_dealloc_cext_object(obj, tag) != 0" in py_obj_dealloc_body
    assert py_obj_dealloc_body.index(
        "pcc_capi_dealloc_cext_object(obj, tag) != 0"
    ) < py_obj_dealloc_body.index(
        'elif tag >= abi_constant("object.type.user_class_start"):'
    )

    py_refcount_body = obj_dealloc_py[
        obj_dealloc_py.index(
            "def _dealloc_dispatch(o, tag: int) -> None:"
        ) : obj_dealloc_py.index("def _trash_enqueue(")
    ]
    assert "pcc_capi_dealloc_cext_object(o, tag) != 0" in py_refcount_body
    assert py_refcount_body.index(
        "pcc_capi_dealloc_cext_object(o, tag) != 0"
    ) < py_refcount_body.index("if tag >= PY_TYPE_USER_CLASS_START:")

    # The header offset moved from a literal 12 to PYOBJECTHEADER_FLAGS_OFFSET;
    # slice on the assignment target, which survives both spellings, and pin the
    # constant's value separately so the offset itself is still checked.
    from pcc.runtime.py.py_abi_constants import PYOBJECTHEADER_FLAGS_OFFSET

    assert PYOBJECTHEADER_FLAGS_OFFSET == 12
    py_del_body = dunder_py[
        dunder_py.index("def py_user_del_dispatch(o)") : dunder_py.index(
            "    flags: int = load_i32(o, "
        )
    ]
    assert "pcc_capi_is_cext_type_tag(tag) != 0" in py_del_body


def test_weakref_target_is_update_only_slot_contract_source():
    py_source = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    py_obj_gc_source = (RUNTIME_DIR / "py" / "py_obj_gc.py").read_text(encoding="utf-8")
    strict_source = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")

    weak_body = strict_source.split(
        "def _visit_weakref_slots(o, visitor, context)", 1
    )[1].split("\ndef ", 1)[0]
    weak_compact = "".join(weak_body.split())
    assert '!=abi_constant("object.type.weakref")' in weak_compact
    assert "_visit_slot(o,16,3,visitor,context)" in weak_compact
    assert "_visit_slot(o,24,1,visitor,context)" in weak_compact

    fixed_body = strict_source.split(
        "def _visit_fixed_owner_slots(o, visitor, context)", 1
    )[1].split("def _visit_weakref_slots(", 1)[0]
    assert 'tag == abi_constant("object.type.weakref")' not in fixed_body

    dispatch = strict_source.split(
        # The port migrated its return annotations from `int` to explicit
        # `i64`; split on the signature without the annotation so the marker
        # survives the next such migration too.
        "def pcc_gc_visit_object_slots(o, visitor, context)", 1
    )[1]
    assert "pcc_gc_visit_object_slots_slice(" in dispatch
    strict_slice = strict_source.split(
        "def pcc_gc_visit_object_slots_slice(", 1
    )[1].split('@c_abi_export("pcc_gc_visit_object_slots")', 1)[0]
    assert 'tag == abi_constant("object.type.weakref")' in strict_slice
    assert "role = 3" in strict_slice
    assert "pcc_gc_visit_object_slots = extern(" in py_source
    covered_body = py_source.split(
        "def _py_obj_visit_covered_slots(",
        1,
    )[1].split("\ndef ", 1)[0]
    assert "pcc_gc_visit_object_slots(" in covered_body

    backend0_slots = STRICT_BACKEND0_SLOTS.read_text(encoding="utf-8")
    sweep_slots = STRICT_SWEEP_SLOTS.read_text(encoding="utf-8")
    assert "pcc_gc_visit_object_slots = extern(" in backend0_slots
    for callback in (
        "pcc_gc_backend0_subtract_slot",
        "pcc_gc_backend0_mark_slot",
    ):
        assert callback in backend0_slots
    assert "pcc_gc_backend0_clear_slot" in sweep_slots
    append_body = py_obj_gc_source.split(
        "def _append_referents_to(o, out) -> None:", 1
    )[1].split("\ndef ", 1)[0]
    assert "pcc_gc_visit_object_slots(" in append_body


def test_object_slot_contract_has_named_visit_and_update_entrypoints_source():
    header = (RUNTIME_DIR / "src" / "py_internal.h").read_text(encoding="utf-8")

    assert "typedef void (*PyObjSlotVisitor)(" in header
    assert "PY_OBJ_SLOT_OWNED" in header
    assert "PY_OBJ_SLOT_BORROWED_TRACED" in header
    assert "PY_OBJ_SLOT_BORROWED_UPDATE_ONLY" in header
    assert "int64_t pcc_gc_visit_object_slots_slice(" in header
    # The named visit entrypoint hands out every slot with its role; the
    # named update entrypoint is the remap visitor that heals one slot.
    object_slots = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    assert '@c_abi_export("pcc_gc_visit_object_slots")' in object_slots
    remap = STRICT_RELOCATION_REMAP.read_text(encoding="utf-8")
    assert '@c_abi_export("pcc_gc_backend4_remap_slot")' in remap
    update_body = remap.split(
        "def pcc_gc_backend4_remap_referents(obj) -> None:", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_visit_object_slots(obj, pcc_gc_backend4_remap_slot, null())" in (
        update_body
    )


def test_trace_and_subtract_slot_visitors_read_through_load_barrier_source():
    py_source = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    mark_source = STRICT_COMMON_MARK_CYCLE.read_text(encoding="utf-8")

    trace_case = mark_source.split("def pcc_gc_trace_slot(", 1)[1].split(
        "@c_abi_export", 1
    )[0]
    assert "child = pcc_gc_load_ptr(null(), slot)" in trace_case
    assert "pcc_gc_trace_mark_gray_if_known(child)" in trace_case
    assert "load_ptr(slot, 0)" not in trace_case

    py_visit_body = py_source.split("def _py_obj_visit_slot(", 1)[1].split(
        "def _py_obj_visit_update_slot(", 1
    )[0]
    subtract_case = py_visit_body.split("if mode == 4:", 1)[1].split(
        "if mode == 6:",
        1,
    )[0]
    assert "child = pcc_gc_load_ptr_extern(" in subtract_case
    assert "null()," in subtract_case
    assert "ptr_add(slot_base, slot_offset)" in subtract_case
    assert "load_ptr(slot_base, slot_offset)" not in subtract_case


def test_no_pointer_slot_families_are_explicitly_classified_source():
    py_source = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    strict_source = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    mark_source = STRICT_COMMON_MARK_CYCLE.read_text(encoding="utf-8")

    py_helper_start = strict_source.index("def _has_no_pointer_slots(o)")
    py_helper_end = strict_source.index(
        "def pcc_gc_visit_object_slots(", py_helper_start
    )
    py_helper_body = strict_source[py_helper_start:py_helper_end]
    for token in (
        'abi_constant("object.type.none")',
        'abi_constant("object.type.bool")',
        'abi_constant("object.type.int")',
        'abi_constant("object.type.float")',
        'abi_constant("object.type.str")',
        'abi_constant("object.type.complex")',
        'abi_constant("object.type.bytes")',
        'abi_constant("object.type.bytearray")',
        'abi_constant("object.type.file")',
        'abi_constant("object.type.cpy_handle")',
        'abi_constant("object.type.thread_lock")',
        'abi_constant("object.type.thread_rlock")',
        'abi_constant("object.type.thread_event")',
        'abi_constant("object.type.thread_condition")',
        'abi_constant("object.type.thread_semaphore")',
    ):
        assert token in py_helper_body

    py_covered_body = strict_source.split(
        # The port migrated its return annotations from `int` to explicit
        # `i64`; split on the signature without the annotation so the marker
        # survives the next such migration too.
        "def pcc_gc_visit_object_slots(o, visitor, context)", 1
    )[1]
    assert "pcc_gc_visit_object_slots_slice(" in py_covered_body
    strict_slice = strict_source.split(
        'def pcc_gc_visit_object_slots_slice(', 1
    )[1].split('@c_abi_export("pcc_gc_visit_object_slots")', 1)[0]
    assert "if _has_no_pointer_slots(o) != 0:" in strict_slice

    trace_body = mark_source.split("def pcc_gc_trace_referents(obj)", 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    assert "pcc_gc_visit_object_slots(obj," in trace_body
    assert "_has_no_pointer_slots(obj)" not in trace_body

    subtract_body = py_source.split("def _subtract_referent_refs(o)", 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    assert "_py_obj_visit_covered_slots(o," in subtract_body
    assert "_has_no_pointer_slots(o)" not in subtract_body
    remap_source = STRICT_RELOCATION_REMAP.read_text(encoding="utf-8")
    remap_body = remap_source.split(
        "def pcc_gc_backend4_remap_referents(obj)", 1
    )[1]
    assert "pcc_gc_visit_object_slots(" in remap_body
    assert "_has_no_pointer_slots(obj)" not in remap_body

    promotion_source = STRICT_GENERATIONAL_PROMOTION.read_text(encoding="utf-8")
    promotion_body = promotion_source.split(
        "def pcc_gc_trace_referents_for_promotion_mode", 1
    )[1].split(
        '@c_abi_export("pcc_gc_trace_referents_for_promotion")', 1
    )[0]
    assert "pcc_gc_visit_object_slots(" in promotion_body
    assert "_has_no_pointer_slots(" not in promotion_body


def test_current_runtime_type_tags_have_a_finite_slot_classification_source():
    """A new concrete runtime tag must declare whether it owns object slots."""
    public_header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(
        encoding="utf-8"
    )
    internal_header = (RUNTIME_DIR / "src" / "py_internal.h").read_text(
        encoding="utf-8"
    )

    type_enum = public_header.split("enum {", 1)[1].split("};", 1)[0]
    current_tags = set(re.findall(r"\b(PY_TYPE_[A-Z0-9_]+)\s*=", type_enum))

    pointerless = {
        "PY_TYPE_NONE",
        # The immortal Ellipsis singleton is a bare header
        # (py_substrate.define_global_header("py_ellipsis_storage", ...)).
        "PY_TYPE_ELLIPSIS",
        # So is NotImplemented ("py_notimplemented_storage"), tagged since 1bf8df737.
        "PY_TYPE_NOTIMPLEMENTED",
        "PY_TYPE_BOOL",
        "PY_TYPE_INT",
        "PY_TYPE_FLOAT",
        "PY_TYPE_STR",
        "PY_TYPE_COMPLEX",
        "PY_TYPE_BYTES",
        "PY_TYPE_BYTEARRAY",
        "PY_TYPE_FILE",
        "PY_TYPE_THREAD_LOCK",
        "PY_TYPE_THREAD_RLOCK",
        "PY_TYPE_THREAD_EVENT",
        "PY_TYPE_THREAD_CONDITION",
        "PY_TYPE_THREAD_SEMAPHORE",
        "PY_TYPE_CPY_HANDLE",
    }
    slot_bearing = {
        "PY_TYPE_LIST",
        "PY_TYPE_DICT",
        "PY_TYPE_TUPLE",
        "PY_TYPE_SET",
        "PY_TYPE_FUNC",
        "PY_TYPE_CLASS",
        "PY_TYPE_INSTANCE",
        "PY_TYPE_EXC",
        "PY_TYPE_ITER",
        "PY_TYPE_GEN",
        "PY_TYPE_MEMORYVIEW",
        "PY_TYPE_COROUTINE",
        "PY_TYPE_WEAKREF",
        "PY_TYPE_THREAD",
        "PY_TYPE_TASK",
        "PY_TYPE_CONTINUATION",
        "PY_TYPE_VIRTUAL_THREAD",
        "PY_TYPE_PROPERTY",
        "PY_TYPE_CLASSMETHOD",
        "PY_TYPE_STATICMETHOD",
        "PY_TYPE_VALUEBOX",
        # PyVThreadChannelEndpointObject carries `PyObject *core`, so the tag is
        # slot-bearing, and the runtime does visit it (freestanding_gc_object_
        # slots.py dispatches on object.type.vthread_channel; py_obj.c lists the
        # tag in its slot-visiting switch).  The classification table simply had
        # not been extended when the tag was added -- this test is the guard
        # against a tag whose slots nobody traces, so a missing entry here is
        # exactly what it should report.
        "PY_TYPE_VTHREAD_CHANNEL",
    }
    # These names mark the reserved user-tag range and the start of the
    # C-extension registry's dynamic tags; descriptors, concrete user-class
    # tags and C-extension objects are classified separately above / by the
    # instance walker / by the extension's tp_traverse bridge.
    dynamic_boundaries = {
        "PY_TYPE_USER",
        "PY_TYPE_USER_CLASS_START",
        "PY_TYPE_CEXT_TAG_BASE",
    }
    assert current_tags == pointerless | slot_bearing | dynamic_boundaries

    for descriptor_tag in (
        "PY_TYPE_PROPERTY",
        "PY_TYPE_CLASSMETHOD",
        "PY_TYPE_STATICMETHOD",
    ):
        assert descriptor_tag in type_enum
    assert "PY_TYPE_USER_CLASS_START" in type_enum


def test_unreachable_file_uses_file_deallocator_in_c_and_python_mirror():
    py_source = STRICT_TRACING_SWEEP_COLLECTOR.read_text(encoding="utf-8")

    py_start = py_source.index(
        "def pcc_gc_tracing_finalize_unreachable(obj) -> None:"
    )
    py_end = py_source.index(
        '@c_abi_export("pcc_gc_tracing_recheck_reachability_after_finalizers")',
        py_start,
    )
    py_body = py_source[py_start:py_end]
    # The dispatch moved from literal tags to abi_constant("object.type.*").
    # Slice on the named form and pin the tag's numeric value separately, so a
    # renamed-but-correct spelling passes while a wrong tag still fails.
    from pcc.runtime.py.py_abi_constants import PY_TYPE_FILE

    assert PY_TYPE_FILE == 13
    file_case = py_body.split(
        'elif tag == abi_constant("object.type.file"):', 1
    )[1].split("elif tag ==", 1)[0]
    assert "py_dealloc_file(obj)" in file_case
    assert "if delay_zpage_freeing_note != 0:" in py_body


def test_pcc_python_gc_backend_consumers_share_slot_family_helper_source():
    py_source = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    mark_source = STRICT_COMMON_MARK_CYCLE.read_text(encoding="utf-8")
    promotion_source = STRICT_GENERATIONAL_PROMOTION.read_text(encoding="utf-8")
    sweep_source = STRICT_SWEEP_SLOTS.read_text(encoding="utf-8")
    relocation_payload = STRICT_RELOCATION_PAYLOAD.read_text(encoding="utf-8")

    def body_after(signature: str) -> str:
        return py_source.split(signature, 1)[1].split("\ndef ", 1)[0]

    helper_body = body_after(
        "def _py_obj_visit_covered_slots("
    )
    assert "pcc_gc_visit_object_slots(" in helper_body
    for callback in (
        "_py_obj_visit_update_slot",
        "_py_obj_visit_subtract_slot",
    ):
        assert callback in helper_body
    assert "pcc_gc_visit_object_slots(from_obj, _relocate_count_slot" in (
        relocation_payload
    )
    assert "pcc_gc_visit_object_slots(from_obj, _relocate_from_slot" in (
        relocation_payload
    )
    assert "pcc_gc_visit_object_slots(to_obj, _relocate_to_slot" in (
        relocation_payload
    )

    promotion_body = promotion_source.split(
        "def pcc_gc_trace_referents_for_promotion_mode", 1
    )[1].split(
        '@c_abi_export("pcc_gc_trace_referents_for_promotion")', 1
    )[0]
    assert "pcc_gc_visit_object_slots(" in promotion_body
    assert "_enqueue_promotion_owner(obj)" in promotion_body
    assert "pcc_gc_generational_promote_shallow_slot" in promotion_body
    promotion_drain = promotion_source.split(
        "def pcc_gc_backend3_drain_promotion_worklist", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_visit_object_slots_slice(" in promotion_drain
    assert "pcc_gc_generational_promote_slot" in promotion_drain

    trace_body = mark_source.split("def pcc_gc_trace_referents(obj)", 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    assert "pcc_gc_visit_object_slots(obj, pcc_gc_trace_slot, null())" in trace_body
    clear_body = sweep_source.split(
        "def pcc_gc_tracing_clear_referents(obj)", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert (
        "pcc_gc_visit_object_slots(\n        obj, pcc_gc_tracing_clear_slot, null()"
        in clear_body
    )

    subtract_body = body_after("def _subtract_referent_refs(o)")
    assert "_py_obj_visit_covered_slots(o, 4, 0)" in subtract_body
    assert "pcc_gc_visit_object_slots(" not in subtract_body
    remap_source = STRICT_RELOCATION_REMAP.read_text(encoding="utf-8")
    remap_body = remap_source.split(
        "def pcc_gc_backend4_remap_referents(obj)", 1
    )[1]
    assert "pcc_gc_visit_object_slots(" in remap_body
    assert "pcc_gc_backend4_remap_slot" in remap_body


def test_pcc_python_backend0_cycle_collector_reuses_slot_helpers_source():
    py_source = STRICT_BACKEND0_COLLECTOR.read_text(encoding="utf-8")
    strict_source = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    backend0_slots = STRICT_BACKEND0_SLOTS.read_text(encoding="utf-8")
    sweep_slots = STRICT_SWEEP_SLOTS.read_text(encoding="utf-8")

    assert "__pcc_freestanding__ = True" in backend0_slots
    assert "pcc_gc_visit_object_slots = extern(" in backend0_slots
    for action in (
        "pcc_gc_backend0_visit_subtract",
        "pcc_gc_backend0_mark_reachable",
        "pcc_gc_backend0_clear_referents",
    ):
        assert f"{action} = extern(" in py_source
        assert f'"{action}"' in py_source
    recompute_body = py_source.split(
        "def _recompute_reachability() -> None:", 1
    )[1].split("\ndef ", 1)[0]
    assert "pcc_gc_backend0_visit_subtract(" in recompute_body
    assert "pcc_gc_backend0_mark_reachable(" in recompute_body
    collect_body = py_source.split(
        '@c_abi_export("py_gc_collect")', 1
    )[1]
    assert "pcc_gc_backend0_clear_referents(obj)" in collect_body

    subtract_body = backend0_slots.split(
        "def pcc_gc_backend0_subtract_slot(", 1
    )[1].split("\ndef ", 1)[0]
    mark_body = backend0_slots.split(
        "def pcc_gc_backend0_mark_slot(", 1
    )[1].split("\ndef ", 1)[0]
    clear_body = sweep_slots.split(
        "def pcc_gc_backend0_clear_slot(", 1
    )[1].split("\ndef ", 1)[0]
    assert "role == 3" in subtract_body
    assert "pcc_gc_load_ptr(" in subtract_body
    assert "role == 3" in mark_body
    assert "pcc_gc_backend0_mark_reachable(" in mark_body
    assert "role != 1" in clear_body
    assert "py_decref(child)" in clear_body

    continuation_body = strict_source.split(
        "def _visit_continuation_slots(o, visitor, context)", 1
    )[1].split("\ndef ", 1)[0]
    assert "if ptr_is_null(slots) == 0:" in continuation_body
    assert "_visit_slot(slots, index * 8, 1, visitor, context)" in continuation_body

    clear_metadata_body = sweep_slots.split(
        "def pcc_gc_clear_container_metadata(",
        1,
    )[1].split("\n@c_abi_export", 1)[0]
    set_clear_body = clear_metadata_body.split("if tag == PY_TYPE_SET:", 1)[1]
    assert "store_ptr(entries, index * 16 + 8, null())" in set_clear_body
    assert "store_i64(entries, index * 16, 0)" in set_clear_body


def test_pcc_python_function_slot_walkers_match_current_layout_source():
    """Backend #0 and the tracing backends must see every owned function slot."""
    expected_offsets = (24, 32, 40, 64, 80, 88)
    source = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    body = source.split(
        "def _visit_fixed_owner_slots(o, visitor, context)", 1
    )[1].split('if tag == abi_constant("object.type.iter"):', 1)[0]
    func_case = body.split('if tag == abi_constant("object.type.func"):', 1)[1]
    for offset in expected_offsets:
        assert f"_visit_slot(o, {offset}, 1, visitor, context)" in func_case


def test_pcc_python_backend0_runtime_roots_reuse_root_slot_helpers_source():
    py_source = STRICT_BACKEND0_COLLECTOR.read_text(encoding="utf-8")

    for helper_name in (
        "_mapped_root_count",
        "_mark_root_slot",
        "_mark_root_slots",
        "_visit_mapped_root_slots",
        "_visit_scheduler_root_slots",
    ):
        assert f"def {helper_name}(" in py_source

    count_body = py_source.split(
        "def _mapped_root_count(frame_map)",
        1,
    )[1].split("\n@c_abi_export", 1)[0]
    assert "if root_count < 0:" in count_body
    assert "root_count = 0 - root_count" in count_body
    assert "if root_count > 100000:" in count_body

    root_slot_body = py_source.split(
        "def _mark_root_slot(",
        1,
    )[1].split("\n@c_abi_export", 1)[0]
    assert "pcc_gc_load_ptr(" in root_slot_body
    assert "ptr_add(slot_base, slot_offset)" in root_slot_body
    assert "pcc_gc_backend0_mark_reachable(child)" in root_slot_body

    mapped_body = py_source.split(
        "def _visit_mapped_root_slots(frame_map, root_slots)",
        1,
    )[1].split("\n@c_abi_export", 1)[0]
    assert "_mapped_root_count(frame_map)" in mapped_body
    assert "_mark_root_slots(root_slots, root_count)" in mapped_body

    root_slots_body = py_source.split(
        "def _mark_root_slots(",
        1,
    )[1].split("\n@c_abi_export", 1)[0]
    assert "_mark_root_slot(root_slots, i * 8)" in root_slots_body

    scheduler_body = py_source.split(
        "def _visit_scheduler_root_slots()",
        1,
    )[
        1
    ].split("\n@c_abi_export", 1)[0]
    assert 'global_load_ptr("pcc_gc_scheduler_root_head")' in scheduler_body
    assert "_mark_root_slot(slot, 0)" in scheduler_body
    assert "node = load_ptr(node, 8)" in scheduler_body

    runtime_body = py_source.split("def _mark_runtime_roots() -> None:", 1)[1]
    runtime_body = runtime_body.split("\n@c_abi_export", 1)[0]
    assert "_mark_root_slots(" in runtime_body
    assert "_visit_mapped_root_slots(frame_map, slots)" in runtime_body
    assert "_visit_scheduler_root_slots()" in runtime_body
    assert "_mark_reachable(load_ptr(slot, 0))" not in runtime_body


def test_capi_py_visit_routes_native_module_state_slots_through_load_barrier_source():
    python_h = (REPO_ROOT / "utils" / "fake_libc_include" / "Python.h").read_text(
        encoding="utf-8"
    )
    # The visit + module-state-roots helpers are owned by pcc-Python runtime
    # modules now (py_capi_visit_runtime.py / py_capi_module_state_runtime.py);
    # the C shim only carries guarded externs.
    visit_source = (RUNTIME_DIR / "py" / "py_capi_visit_runtime.py").read_text(
        encoding="utf-8"
    )
    module_state_source = (
        RUNTIME_DIR / "py" / "py_capi_module_state_runtime.py"
    ).read_text(encoding="utf-8")

    assert "int pcc_capi_visit_slot(" in python_h
    assert "PyObject **slot" in python_h
    assert "pcc_capi_visit_slot((PyObject **)&(op), visit, arg)" in python_h
    assert "visit((PyObject *)(op), arg)" not in python_h

    assert 'def pcc_capi_visit_slot(slot, visit, arg) -> int:' in visit_source
    assert "pcc_gc_load_ptr(null(), slot)" in visit_source
    assert "call_i64_ptr2(visit, obj, arg)" in visit_source

    assert (
        "def pcc_capi_visit_extension_module_state_roots(visit, ctx) -> None:"
        in module_state_source
    )
    assert "pcc_capi_visit_module_state_ref" in module_state_source
    assert "call_i64_ptr3(" in module_state_source


def test_builtin_exception_cache_uses_the_shared_runtime_root_slot_contract():
    mapped_source = (
        RUNTIME_DIR / "py" / "freestanding_gc_mapped_roots.py"
    ).read_text(encoding="utf-8")

    py_helper = mapped_source.split(
        "def pcc_gc_visit_builtin_exception_cache_slots(", 1
    )[1].split('@c_abi_export("pcc_gc_gray_mapped_roots")', 1)[0]
    assert "py_subs_exc_cache_slot(0)" in py_helper
    assert "pcc_gc_visit_mapped_root_slots(" in py_helper
    object_root_seeding = (
        RUNTIME_DIR / "py" / "freestanding_gc_object_root_seeding.py"
    ).read_text(encoding="utf-8")
    assert "pcc_gc_visit_registered_root_slots(1, 1)" in object_root_seeding
    generational_scheduler = (
        RUNTIME_DIR / "py" / "freestanding_gc_generational_scheduler.py"
    ).read_text(encoding="utf-8")
    forwarding_retirement = (
        RUNTIME_DIR / "py" / "freestanding_gc_forwarding_retirement.py"
    ).read_text(encoding="utf-8")
    assert "py_subs_exc_cache_slot(slot_index)" in generational_scheduler
    assert "pcc_gc_visit_registered_root_slots(3, 0)" in forwarding_retirement


def test_builtin_exception_cache_is_visible_as_a_runtime_root(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "builtin_exception_cache_root_probe.c"
    exe = tmp_path / "builtin_exception_cache_root_probe.out"
    src.write_text(
        textwrap.dedent(r"""
            #include "py_internal.h"

            int64_t pcc_gc_visit_builtin_exception_cache_slots(
                int64_t mode, int64_t resolve
            );

            int main(void) {
                pcc_gc_set_backend(PCC_GC_KIND_INCREMENTAL_TRICOLOR);
                PyObject *expected = (PyObject *)py_exc_builtin_class(
                    PY_EXC_STOPITERATION
                );
                if (expected == NULL) return 10;
                /* The cached class lives in the table slot the collector
                 * walks, and mode 0 (visit, no action) walks the whole
                 * PY_EXC_N_BUILTIN table plus the builtin type roots. */
                PyObject **slot = (PyObject **)py_subs_exc_cache_slot(
                    PY_EXC_STOPITERATION
                );
                if (slot == NULL || *slot != expected) return 11;
                if (
                    pcc_gc_visit_builtin_exception_cache_slots(0, 0)
                    < PY_EXC_N_BUILTIN
                ) return 12;
                (void)pcc_gc_collect(0);
                PyObject *cached = (PyObject *)py_exc_builtin_class(
                    PY_EXC_STOPITERATION
                );
                if (cached != expected) return 13;
                if (pcc_gc_object_is_known(cached) != 1) return 14;
                if (py_header(cached)->type_tag != PY_TYPE_CLASS) return 15;
                return 0;
            }
            """).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_scheduler_root_walk_visits_every_registered_root(tmp_path):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "scheduler_root_walk_probe.c"
    exe = tmp_path / "scheduler_root_walk_probe.out"
    src.write_text(
        textwrap.dedent(r"""
            #include "py_internal.h"

            int64_t pcc_gc_visit_scheduler_root_slots(
                int64_t mode, int64_t resolve
            );

            /* More roots than any fixed-size snapshot a walker might use. */
            static PyObject *roots[80];
            static void *handles[80];

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_INCREMENTAL_TRICOLOR
                    ) != 0) return 2;
                int64_t base = pcc_gc_scheduler_root_count();
                if (pcc_gc_visit_scheduler_root_slots(0, 0) != base) return 3;
                for (int i = 0; i < 80; i++) {
                    roots[i] = py_list_new(0);
                    if (roots[i] == 0) return 10;
                    handles[i] = pcc_gc_scheduler_root_register_handle(
                        &roots[i]
                    );
                    if (handles[i] == 0) return 11;
                }
                if (pcc_gc_scheduler_root_count() != base + 80) return 12;
                if (pcc_gc_visit_scheduler_root_slots(0, 0) != base + 80) {
                    return 13;
                }
                /* Fixed exit codes: 200 + i would wrap to 0 at i == 56. */
                for (int i = 0; i < 80; i++) {
                    if (pcc_gc_slot_is_runtime_root(&roots[i]) != 1) {
                        return 16;
                    }
                }
                (void)pcc_gc_collect(0);
                for (int i = 0; i < 80; i++) {
                    if (pcc_gc_object_is_known(roots[i]) != 1) return 17;
                    pcc_gc_scheduler_root_unregister_handle(handles[i]);
                    py_decref(roots[i]);
                }
                if (pcc_gc_scheduler_root_count() != base) return 14;
                if (pcc_gc_visit_scheduler_root_slots(0, 0) != base) return 15;
                return 0;
            }
            """).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
