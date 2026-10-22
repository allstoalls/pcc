"""store_ptr prepare/commit/finish transaction, CAPI pin and lease lifetimes, sorted/min-max/enumerate root balance.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403

import re


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_graph_lock_recursive_depth_maps_to_one_no_park_lease(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="graph_lock_no_park_lease",
        source_text=(
            "#define PCC_PROBE_STRICT "
            + ("1\n")
            + r'''
            #include "py_internal.h"
            #include <stdint.h>

            extern int64_t pcc_thread_no_park_depth(void);
#if PCC_PROBE_STRICT
            extern void pcc_py_gc_minor_graph_lock(void);
            extern void pcc_py_gc_minor_graph_unlock(void);
#define PROBE_LOCK() pcc_py_gc_minor_graph_lock()
#define PROBE_UNLOCK() pcc_py_gc_minor_graph_unlock()
#else
#define PROBE_LOCK() pcc_gc_root_slot_lock()
#define PROBE_UNLOCK() pcc_gc_root_slot_unlock()
#endif

            int main(void) {
                if (pcc_threads_enabled() != 1) return 2;
                if (pcc_current_thread_id() <= 0) return 3;
                if (pcc_thread_no_park_depth() != 0) return 4;
                PROBE_LOCK();
                if (pcc_thread_no_park_depth() != 1) return 5;
                PROBE_LOCK();
                if (pcc_thread_no_park_depth() != 1) return 6;
                PROBE_UNLOCK();
                if (pcc_thread_no_park_depth() != 1) return 7;
                PROBE_UNLOCK();
                if (pcc_thread_no_park_depth() != 0) return 8;
                return 0;
            }
        '''),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} graph-lock/no-park probe returned {run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_store_ptr_uses_owner_aware_prepare_commit_finish_transaction():
    internal_header = (
        REPO_ROOT / "pcc" / "runtime" / "src" / "py_internal.h"
    ).read_text(encoding="utf-8")
    expected_cross_signatures = {
        "pcc_gc_store_ptr_plan_init": (
            ("c_ptr", "c_ptr", "c_int64"),
            "c_void",
        ),
        "pcc_gc_store_ptr_plan_commit_locked": (
            ("c_ptr", "c_ptr", "c_ptr", "c_ptr"),
            "c_int64",
        ),
        "pcc_gc_store_ptr_plan_finish": (("c_ptr",), "c_void"),
    }
    for symbol, signature in expected_cross_signatures.items():
        assert symbol in internal_header
        assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[symbol] == signature
        assert symbol not in RUNTIME_SIGNATURES

    py_src = PY_OBJ_PORT.read_text(encoding="utf-8")
    for symbol in expected_cross_signatures:
        assert f'@c_abi_export("{symbol}")' in py_src
    py_store = py_src.split("def pcc_gc_store_ptr(owner, slot, value) -> None:", 1)[
        1
    ].split('@c_abi_export("pcc_gc_store_ptr_fresh_native_instance")', 1)[0]
    py_plan = py_store.index("pcc_gc_store_ptr_plan_init(plan, owner, backend)")
    py_lock = py_store.index("pcc_py_gc_minor_graph_lock()", py_plan)
    py_commit = py_store.index("pcc_gc_store_ptr_plan_commit_locked(", py_lock)
    py_unlock = py_store.index("pcc_py_gc_minor_graph_unlock()", py_commit)
    py_finish = py_store.index("pcc_gc_store_ptr_plan_finish(plan)", py_unlock)
    assert py_plan < py_lock < py_commit < py_unlock < py_finish
    py_plan_init = py_src.split(
        '@c_abi_export("pcc_gc_store_ptr_plan_init")', 1
    )[1].split("def _pcc_gc_store_plan_commit_locked", 1)[0]
    assert py_plan_init.index("pcc_gc_store_root_plan_init(plan, backend)") < (
        py_plan_init.index("pcc_diagnostics_runtime_log_event_code(")
    )
    py_commit_impl = py_src.split(
        "def _pcc_gc_store_plan_commit_locked(", 1
    )[1].split('@c_abi_export("pcc_gc_store_root_plan_commit_locked")', 1)[0]
    assert py_commit_impl.index("_py_incref_prepare(") < py_commit_impl.index(
        "pcc_gc_note_slot_write_barrier("
    ) < py_commit_impl.index("old = load_ptr(slot, 0)") < py_commit_impl.index(
        "store_ptr(slot, 0, load_ptr(plan, 0))"
    ) < py_commit_impl.index("_py_decref_prepare(")
    for forbidden in ("py_decref(", "_py_decref_finish(", "runtime_log_event"):
        assert forbidden not in py_commit_impl


def test_backend4_container_constructors_use_fresh_then_publish_contract():
    expected_signature = (("c_ptr",), "c_void")
    assert FREESTANDING_GC_CROSS_OBJECT_SIGNATURES[
        "pcc_gc_publish_initialized"
    ] == expected_signature
    py_obj = PY_OBJ_PORT.read_text(encoding="utf-8")
    # The C runtime is retired (its header declaration left in 23dec4e97);
    # the port's export is the publication owner.
    assert (
        '@c_abi_export("pcc_gc_publish_initialized")\n'
        "def pcc_gc_publish_initialized(obj) -> None:"
    ) in py_obj
    py_alloc = py_obj.split("def pcc_gc_alloc(", 1)[1].split(
        '@c_abi_export("pcc_gc_publish_initialized")', 1
    )[0]
    for tag in ("PY_TYPE_LIST", "PY_TYPE_TUPLE", "PY_TYPE_DICT", "PY_TYPE_SET"):
        assert tag in py_alloc
    assert "stored_flags = stored_flags | 16384" in py_alloc
    py_publish = py_obj.split(
        '@c_abi_export("pcc_gc_publish_initialized")', 1
    )[1].split('@c_abi_export("pcc_gc_retain")', 1)[0]
    assert py_publish.index("pcc_py_gc_minor_graph_lock()") < py_publish.index(
        "flags & ~16384"
    ) < py_publish.index("pcc_py_gc_minor_graph_unlock()")

    for py_name in ("py_list.py", "py_dict.py", "py_set.py"):
        source = (RUNTIME_DIR / "py" / py_name).read_text(encoding="utf-8")
        assert "pcc_gc_publish_initialized(" in source
    py_tuple = (RUNTIME_DIR / "py" / "py_tuple.py").read_text(encoding="utf-8")
    assert "if complete != 0:\n        pcc_gc_publish_initialized(tuple_ptr)" in py_tuple


# ---------------------------------------------------------------------------
# Backend-4 fresh-allocation admission.
#
# PY_FLAG_GC_FRESH_ALLOC is the only thing that keeps a half-built object out
# of the relocation set: pcc_gc_relocation_set_add refuses it, and the zpage
# candidate snapshot refuses a fresh owner outright.  So any tag that
# pcc_gc_colored_relocate_copy_supported_tag accepts must also be admitted at
# pcc_gc_alloc, or a mid-construction object of that tag can be copied with
# uninitialized pointer slots.  Admission alone is not enough either: a tag
# admitted whose constructor never publishes would stay fresh forever and
# could never relocate, which is why each test below pairs the two halves.
#
# These three tests previously asserted the OPPOSITE -- that FUNC/ITER,
# GEN/COROUTINE/CONTINUATION/TASK and STATICMETHOD stay out of the admission
# set.  That was the static contract for two [DENIED] verdicts in
# docs/investigations/gc-backend4-relocation-mutator-quiescence.md, whose
# reason was an implementation blocker ("the first ordinary allocation fail[s]
# through the same recursive MemoryError path"), not an argument against
# admission.  The blocker is gone: see that investigation's 2026-09-08 update
# for the strict-GC4 execution evidence that overturned both verdicts.
# ---------------------------------------------------------------------------


def _backend4_fresh_admission_sources() -> str:
    """The admission condition of pcc_gc_alloc."""

    py_obj = PY_OBJ_PORT.read_text(encoding="utf-8")
    return py_obj.split("def pcc_gc_alloc(", 1)[1].split(
        '@c_abi_export("pcc_gc_publish_initialized")', 1
    )[0]


def _publishes(*relative_paths: str) -> None:
    for relative in relative_paths:
        source = (RUNTIME_DIR / relative).read_text(encoding="utf-8")
        assert "pcc_gc_publish_initialized(" in source, relative


def test_backend4_admission_never_exceeds_the_relocatable_tag_set():
    """The mechanical half, so neither list can drift alone.

    A tag admitted but not relocatable would be pinned fresh for nothing; the
    reverse gap is the dangerous one and is named in the investigation as an
    open boundary (THREAD / VIRTUAL_THREAD / VTHREAD_CHANNEL are relocatable
    but not admitted, and rely on their own liveness guards instead).
    """

    py_alloc = _backend4_fresh_admission_sources()
    remap = (
        RUNTIME_DIR / "py" / "freestanding_gc_relocation_remap.py"
    ).read_text(encoding="utf-8")
    supported = remap.split(
        "def pcc_gc_backend4_relocate_copy_supported_tag(tag: i64) -> i64:", 1
    )[1].split("\n@c_abi_export", 1)[0]
    supported_tags = set(
        "PY_TYPE_" + name.upper()
        for name in re.findall(r'abi_constant\("object\.type\.([a-z_]+)"\)', supported)
    )
    assert supported_tags, supported[:200]
    admitted = set(re.findall(r"PY_TYPE_[A-Z_]+", py_alloc))
    assert admitted, py_alloc[:200]
    assert admitted <= supported_tags, sorted(admitted - supported_tags)


def test_backend4_container_and_wrapper_tags_are_admitted_and_publish():
    py_alloc = _backend4_fresh_admission_sources()
    for tag in (
        "PY_TYPE_LIST",
        "PY_TYPE_TUPLE",
        "PY_TYPE_DICT",
        "PY_TYPE_SET",
        "PY_TYPE_PROPERTY",
        "PY_TYPE_CLASSMETHOD",
        "PY_TYPE_WEAKREF",
        "PY_TYPE_MEMORYVIEW",
    ):
        assert tag in py_alloc, tag
    _publishes(
        "py/py_list.py",
        "py/py_dict.py",
        "py/py_set.py",
        "py/py_class.py",
        "py/py_weakref.py",
        "py/py_obj_stubs.py",
    )
    py_tuple = (RUNTIME_DIR / "py" / "py_tuple.py").read_text(encoding="utf-8")
    assert (
        "if complete != 0:\n        pcc_gc_publish_initialized(tuple_ptr)"
        in py_tuple
    )


def test_backend4_staticmethod_constructor_publishes_its_owned_slot():
    """Admitted staticmethod descriptors must leave the fresh allocation set."""

    py_alloc = _backend4_fresh_admission_sources()
    assert "PY_TYPE_STATICMETHOD" in py_alloc

    text = (RUNTIME_DIR / "py" / "py_class.py").read_text(encoding="utf-8")
    constructor = text.split("py_staticmethod_new(", 1)[1].split("py_property_new(", 1)[0]
    assert "PY_TYPE_STATICMETHOD" in constructor
    assert constructor.index("pcc_gc_store_ptr(") < constructor.index("pcc_gc_publish_initialized(")

    internal = (
        RUNTIME_DIR / "src" / "py_internal.h"
    ).read_text(encoding="utf-8")
    assert "PyObject *py_staticmethod_new(PyObject *func);" in internal


def test_backend4_function_and_iterator_tags_are_admitted_and_publish():
    py_alloc = _backend4_fresh_admission_sources()
    for tag in ("PY_TYPE_FUNC", "PY_TYPE_ITER"):
        assert tag in py_alloc, tag
    _publishes(
        "py/py_func.py",
        "py/py_iter.py",
    )


def test_backend4_suspended_execution_tags_are_admitted_and_publish():
    py_alloc = _backend4_fresh_admission_sources()
    for tag in (
        "PY_TYPE_GEN",
        "PY_TYPE_COROUTINE",
        "PY_TYPE_CONTINUATION",
        "PY_TYPE_TASK",
        "PY_TYPE_EXC",
        "PY_TYPE_CLASS",
    ):
        assert tag in py_alloc, tag
    _publishes(
        "py/py_gen.py",
        "py/py_coroutine.py",
        "py/py_exc_objects.py",
        "py/py_class.py",
    )


def test_capi_borrowed_container_items_pin_but_getitemref_stays_owned():

    collections = (
        RUNTIME_DIR / "py" / "py_capi_collections_runtime.py"
    ).read_text(encoding="utf-8")
    dictionary = (
        RUNTIME_DIR / "py" / "py_capi_dict_runtime.py"
    ).read_text(encoding="utf-8")
    assert collections.count("pcc_gc_pin(item)") >= 2
    assert dictionary.count("pcc_gc_pin(item)") >= 2
    strict_ref = collections.split("def PyList_GetItemRef(", 1)[1].split(
        '@c_abi_typed_export("PyList_Size"', 1
    )[0]
    assert "py_list_get(obj" in strict_ref
    assert "PyList_GetItem(obj" not in strict_ref
    assert "pcc_gc_pin" not in strict_ref


def test_capi_sequence_fast_items_lifetime_pins_owner_storage():
    strict = (
        RUNTIME_DIR / "py" / "py_capi_sequence_runtime.py"
    ).read_text(encoding="utf-8")
    body = strict.split("def PySequence_Fast_ITEMS(", 1)[1].split(
        '@c_abi_typed_export("PySequence_List"', 1
    )[0]
    assert body.index("pcc_gc_pin(obj)") < body.index("PyList_Check(obj)")


def test_capi_unicode_bytes_raw_accessors_pin_without_polluting_internal_utf8():
    header = (
        REPO_ROOT / "utils" / "fake_libc_include" / "Python.h"
    ).read_text(encoding="utf-8")
    for macro in (
        "PyUnicode_1BYTE_DATA", "PyUnicode_2BYTE_DATA",
        "PyUnicode_4BYTE_DATA", "PyUnicode_DATA",
    ):
        line = next(line for line in header.splitlines() if line.startswith(f"#define {macro}"))
        assert "pcc_capi_str_utf8_pinned" in line


    strict_utf8 = (
        RUNTIME_DIR / "py" / "py_str_accessors.py"
    ).read_text(encoding="utf-8")
    strict_internal = strict_utf8.split("def py_str_utf8(s):", 1)[1].split(
        '@c_abi_export("pcc_capi_str_utf8_pinned")', 1
    )[0]
    assert "pcc_gc_pin" not in strict_internal
    strict_unicode = (
        RUNTIME_DIR / "py" / "py_capi_unicode_runtime.py"
    ).read_text(encoding="utf-8")
    assert strict_unicode.count("return pcc_capi_str_utf8_pinned(obj)") == 2


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_capi_unicode_bytes_raw_pointers_pin_only_capi_owners(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_capi_unicode_bytes_pin",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <string.h>

            extern const char *PyUnicode_AsUTF8(PyObject *);
            extern const char *PyUnicode_AsUTF8AndSize(PyObject *, int64_t *);
            extern char *PyBytes_AsString(PyObject *);
            extern int PyBytes_AsStringAndSize(PyObject *, char **, int64_t *);

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *internal = py_str_new("hot", 3);
                PyObject *unicode = py_str_new("hello", 5);
                PyObject *bytes = py_bytes_new("world", 5);
                if (internal == NULL || unicode == NULL || bytes == NULL) return 3;
                if (strcmp(py_str_utf8(internal), "hot") != 0) return 4;
                if ((py_header(internal)->flags & PY_FLAG_GC_PINNED) != 0) return 5;
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(internal) != 1) return 6;
                pcc_gc_reset_relocation_set();

                int64_t usize = 0;
                const char *udata = PyUnicode_AsUTF8(unicode);
                const char *udata2 = PyUnicode_AsUTF8AndSize(unicode, &usize);
                char *bdata = PyBytes_AsString(bytes);
                char *bdata2 = NULL;
                int64_t bsize = 0;
                if (PyBytes_AsStringAndSize(bytes, &bdata2, &bsize) != 0) return 7;
                if (strcmp(udata, "hello") != 0 || udata2 != udata || usize != 5) return 8;
                if (memcmp(bdata, "world", 5) != 0 || bdata2 != bdata || bsize != 5) return 9;
                if ((py_header(unicode)->flags & PY_FLAG_GC_PINNED) == 0) return 10;
                if ((py_header(bytes)->flags & PY_FLAG_GC_PINNED) == 0) return 11;
                if (pcc_gc_backend4_relocation_set_add(unicode) != 0) return 12;
                if (pcc_gc_backend4_relocation_set_add(bytes) != 0) return 13;
                py_decref(bytes);
                py_decref(unicode);
                py_decref(internal);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} unicode/bytes C-API pin probe returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_capi_buffer_leases_count_final_exporter_and_view_owner():

    strict_get = (
        RUNTIME_DIR / "py" / "py_capi_buffer_runtime.py"
    ).read_text(encoding="utf-8")
    body = strict_get.split("def PyObject_GetBuffer(", 1)[1].split(
        '@c_abi_typed_export("PyMemoryView_Check"', 1
    )[0]
    assert "meta = PyMem_Malloc(40)" in body
    assert "store_ptr(meta, 16, lease_owner)" in body
    assert "store_ptr(meta, 24, obj)" in body
    assert "store_ptr(meta, 32," in body
    strict_release = (
        RUNTIME_DIR / "py" / "py_capi_misc_runtime.py"
    ).read_text(encoding="utf-8").split("def PyBuffer_Release(", 1)[1].split(
        "define_global_ptr_null", 1
    )[0]
    assert strict_release.index("store_ptr(previous, 32, nxt)") < (
        strict_release.index("pcc_gc_unpin(lease_owner)")
    ) < strict_release.index("py_decref(obj)")


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_nested_buffer_leases_pin_until_final_release(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_nested_buffer_leases",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <string.h>

            typedef struct ProbeBuffer {
                void *buf;
                PyObject *obj;
                int64_t len;
                int64_t itemsize;
                int32_t readonly;
                int32_t ndim;
                char *format;
                int64_t *shape;
                int64_t *strides;
                int64_t *suboffsets;
                void *internal;
            } ProbeBuffer;

            extern int PyObject_GetBuffer(PyObject *, ProbeBuffer *, int);
            extern void PyBuffer_Release(ProbeBuffer *);

            static int relocation_rejected(PyObject *obj) {
                pcc_gc_reset_relocation_set();
                return pcc_gc_backend4_relocation_set_add(obj) == 0;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *base = py_bytes_new("lease", 5);
                PyObject *view = py_memoryview_new(base);
                if (base == NULL || view == NULL) return 3;
                ProbeBuffer first;
                ProbeBuffer second;
                if (PyObject_GetBuffer(view, &first, 0x001c) != 0) return 4;
                if (PyObject_GetBuffer(view, &second, 0) != 0) return 5;
                if (first.obj != view || second.obj != view) return 6;
                if (first.buf != second.buf || memcmp(first.buf, "lease", 5) != 0) return 7;
                if ((py_header(base)->flags & PY_FLAG_GC_PINNED) == 0) return 8;
                if ((py_header(view)->flags & PY_FLAG_GC_PINNED) == 0) return 9;
                if (!relocation_rejected(base) || !relocation_rejected(view)) return 10;

                PyBuffer_Release(&first);
                if ((py_header(base)->flags & PY_FLAG_GC_PINNED) == 0) return 11;
                if ((py_header(view)->flags & PY_FLAG_GC_PINNED) == 0) return 12;
                if (!relocation_rejected(base) || !relocation_rejected(view)) return 13;

                PyBuffer_Release(&second);
                if ((py_header(base)->flags & PY_FLAG_GC_PINNED) != 0) return 14;
                if ((py_header(view)->flags & PY_FLAG_GC_PINNED) != 0) return 15;
                if (pcc_gc_backend4_relocation_set_add(base) != 1) return 16;
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(view) != 1) return 17;
                pcc_gc_reset_relocation_set();
                py_decref(view);
                py_decref(base);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} nested Py_buffer lease probe returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_py_obj_sorted_roots_shared_inputs_and_pins_private_working_lists():

    py_source = (
        RUNTIME_DIR / "py" / "py_obj_ops_compare.py"
    ).read_text(encoding="utf-8")
    py_body = py_source.split("def py_obj_sorted(x):", 1)[1].split(
        '@c_abi_export("py_obj_contains")', 1
    )[0]
    assert (
        py_body.index("pcc_gc_scheduler_root_register_handle(x_slot)")
        < py_body.index("py_obj_len(x)")
    )
    assert py_body.index("pcc_gc_pin(out)") < py_body.index("py_obj_iter(x)")
    assert (
        py_body.index("pcc_gc_scheduler_root_register_handle(it_slot)")
        < py_body.index("py_obj_next(it)")
    )
    assert py_body.index("pcc_gc_pin(scratch)") < py_body.index("src_list = out")
    # Four exits after the pin unpin it themselves (early failure, merge
    # result, insertion-sort comparison failure, insertion result); the
    # merge comparison failure unpins through _sorted_release_list.
    assert py_body.count("pcc_gc_unpin(out)") == 4
    assert "_sorted_release_list(out)" in py_body
    helper = py_source.split("def _sorted_release_list(items) -> None:", 1)[1].split("\n\n\n", 1)[0]
    assert "pcc_gc_unpin(items)" in helper
    assert "pcc_gc_pin(x)" not in py_body
    assert "pcc_gc_unpin(x)" not in py_body
    assert "pcc_gc_pin(it)" not in py_body
    assert "pcc_gc_unpin(it)" not in py_body


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_py_obj_sorted_releases_all_constant_cost_pins(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_py_obj_sorted_pin_balance",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *input = py_list_new(0);
                if (input == NULL) return 3;
                py_list_append(input, py_int_from_i64(3));
                py_list_append(input, py_int_from_i64(1));
                py_list_append(input, py_int_from_i64(2));
                pcc_gc_pin(input);
                PyObject *out = py_obj_sorted(input);
                if (out == NULL || py_list_len(out) != 3) return 4;
                if ((py_header(input)->flags & PY_FLAG_GC_PINNED) == 0) return 5;
                pcc_gc_unpin(input);
                if ((py_header(input)->flags & PY_FLAG_GC_PINNED) != 0) return 6;
                if ((py_header(out)->flags & PY_FLAG_GC_PINNED) != 0) return 7;
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(input) != 1) return 8;
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(out) != 1) return 9;
                pcc_gc_reset_relocation_set();
                py_decref(out);
                py_decref(input);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} py_obj_sorted pin-balance probe returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_py_obj_min_max_roots_every_callback_retained_managed_local():

    py_source = (
        RUNTIME_DIR / "py" / "py_obj_ops_compare.py"
    ).read_text(encoding="utf-8")
    py_body = py_source.split("def py_obj_min_max(iterable, want_max: int):", 1)[
        1
    ].split('# ---- sorted', 1)[0]
    assert py_body.count("pcc_gc_scheduler_root_register_handle(") == 3
    assert py_body.index("it = pcc_gc_load_ptr(null(), it_slot)") < (
        py_body.index("element = py_obj_next(it)")
    )
    compare_at = py_body.index("replace = py_obj_lt")
    assert py_body.index(
        "best = pcc_gc_load_ptr(null(), best_slot)", compare_at
    ) > compare_at
    assert py_body.index(
        "element = pcc_gc_load_ptr(null(), element_slot)", compare_at
    ) > compare_at
    assert "pcc_gc_pin(" not in py_body


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_py_obj_min_max_balances_iterator_best_and_element_roots(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_py_obj_min_max_root_balance",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (pcc_gc_scheduler_root_count() != 0) return 3;
                PyObject *values = py_list_new(0);
                if (values == NULL) return 4;
                PyObject *c = py_str_new("c", 1);
                PyObject *a = py_str_new("a", 1);
                PyObject *b = py_str_new("b", 1);
                if (c == NULL || a == NULL || b == NULL) return 5;
                py_list_append(values, c);
                py_list_append(values, a);
                py_list_append(values, b);
                py_decref(c);
                py_decref(a);
                py_decref(b);
                PyObject *minimum = py_obj_min_max(values, 0);
                PyObject *maximum = py_obj_min_max(values, 1);
                if (minimum == NULL || maximum == NULL) return 6;
                PyObject *expected_min = py_str_new("a", 1);
                PyObject *expected_max = py_str_new("c", 1);
                if (expected_min == NULL || expected_max == NULL) return 7;
                int min_ok = py_str_eq(minimum, expected_min);
                int max_ok = py_str_eq(maximum, expected_max);
                py_decref(expected_min);
                py_decref(expected_max);
                if (min_ok != 1 || max_ok != 1) return 8;
                if (pcc_gc_scheduler_root_count() != 0) return 9;
                py_decref(minimum);
                py_decref(maximum);
                py_decref(values);
                return 0;
            }
        ''',
        extra_sources=(
            ()
        ),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} py_obj_min_max root-balance probe returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_py_enumerate_list_roots_callback_retained_values_and_pins_private_output():

    py_source = (RUNTIME_DIR / "py" / "py_iter.py").read_text(encoding="utf-8")
    py_body = py_source.split("def py_enumerate_list(iterable, start: int):", 1)[
        1
    ]
    assert py_body.count("pcc_gc_scheduler_root_register_handle(") == 3
    assert py_body.index("pcc_gc_pin(out)") < py_body.index("py_obj_next(it)")
    assert py_body.index("pcc_gc_pin(pair)") < py_body.index("py_int_from_i64")
    assert py_body.count("pcc_gc_unpin(out)") == 7


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_py_enumerate_list_balances_heap_item_roots(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_py_enumerate_list_root_balance",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (pcc_gc_scheduler_root_count() != 0) return 3;
                PyObject *values = py_list_new(0);
                PyObject *x = py_str_new("x", 1);
                PyObject *y = py_str_new("y", 1);
                if (values == NULL || x == NULL || y == NULL) return 4;
                py_list_append(values, x);
                py_list_append(values, y);
                py_decref(x);
                py_decref(y);
                PyObject *out = py_enumerate_list(values, 5);
                if (out == NULL || py_list_len(out) != 2) return 5;
                PyObject *first = py_list_get(out, 0);
                PyObject *second = py_list_get(out, 1);
                if (first == NULL || second == NULL) return 6;
                PyObject *i0 = py_tuple_get(first, 0);
                PyObject *v0 = py_tuple_get(first, 1);
                PyObject *i1 = py_tuple_get(second, 0);
                PyObject *v1 = py_tuple_get(second, 1);
                int overflow0 = 0;
                int overflow1 = 0;
                int64_t index0 = py_int_to_i64(i0, &overflow0);
                int64_t index1 = py_int_to_i64(i1, &overflow1);
                PyObject *expected_x = py_str_new("x", 1);
                PyObject *expected_y = py_str_new("y", 1);
                int values_ok = py_str_eq(v0, expected_x)
                    && py_str_eq(v1, expected_y);
                py_decref(expected_x);
                py_decref(expected_y);
                py_decref(i0);
                py_decref(v0);
                py_decref(i1);
                py_decref(v1);
                py_decref(first);
                py_decref(second);
                if (overflow0 || overflow1 || index0 != 5 || index1 != 6) {
                    return 7;
                }
                if (!values_ok) return 8;
                if ((py_header(out)->flags & PY_FLAG_GC_PINNED) != 0) return 9;
                if (pcc_gc_scheduler_root_count() != 0) return 10;
                py_decref(out);
                py_decref(values);
                return 0;
            }
        ''',
        extra_sources=(
            ()
        ),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} py_enumerate_list root-balance returned {run.returncode}: "
        + run.stdout + run.stderr
    )
