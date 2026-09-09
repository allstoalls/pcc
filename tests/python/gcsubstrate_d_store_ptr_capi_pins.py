"""store_ptr prepare/commit/finish transaction, CAPI pin and lease lifetimes, sorted/min-max/enumerate root balance.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403

import re




@pytest.mark.parametrize("kind", ["c", "pcc_python"])
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
            + ("1\n" if kind == "pcc_python" else "0\n")
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
        REPO_ROOT / "pcc" / "py_runtime" / "src" / "py_internal.h"
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

    c_src = PY_OBJ_C.read_text(encoding="utf-8")
    c_store = c_src.split("void pcc_gc_store_ptr(", 1)[1].split(
        "void pcc_gc_store_ptr_fresh_native_instance", 1
    )[0]
    c_plan = c_store.index("pcc_gc_store_ptr_plan_init(&plan, owner, backend)")
    c_lock = c_store.index("pcc_gc_root_slot_lock()", c_plan)
    c_commit = c_store.index("pcc_gc_store_ptr_plan_commit_locked(", c_lock)
    c_unlock = c_store.index("pcc_gc_root_slot_unlock()", c_commit)
    c_finish = c_store.index("pcc_gc_store_ptr_plan_finish(&plan)", c_unlock)
    assert c_plan < c_lock < c_commit < c_unlock < c_finish
    c_plan_init = c_src.split("void pcc_gc_store_ptr_plan_init(", 1)[1].split(
        "static int64_t pcc_gc_store_plan_commit_locked_impl(", 1
    )[0]
    assert c_plan_init.index("pcc_gc_store_root_plan_init(plan, backend)") < (
        c_plan_init.index("pcc_obj_runtime_log_event_code(")
    )
    c_commit_impl = c_src.split(
        "static int64_t pcc_gc_store_plan_commit_locked_impl(", 1
    )[1].split("int64_t pcc_gc_store_root_plan_commit_locked", 1)[0]
    assert c_commit_impl.index("pcc_incref_prepare(") < c_commit_impl.index(
        "pcc_gc_note_slot_write_barrier("
    ) < c_commit_impl.index("PyObject *old = *slot;") < c_commit_impl.index(
        "*slot = impl->new_prepared.obj;"
    ) < c_commit_impl.index("pcc_decref_prepare(")
    for forbidden in ("py_decref(", "pcc_decref_finish(", "runtime_log_event"):
        assert forbidden not in c_commit_impl

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
        py_plan_init.index("pcc_runtime_log_event_code(")
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
    assert "pcc_gc_publish_initialized(PyObject *obj)" in (
        REPO_ROOT / "pcc" / "py_runtime" / "src" / "py_internal.h"
    ).read_text(encoding="utf-8")

    c_obj = PY_OBJ_C.read_text(encoding="utf-8")
    c_alloc = c_obj.split("PyObject *pcc_gc_alloc(", 1)[1].split(
        "void pcc_gc_publish_initialized", 1
    )[0]
    for tag in ("PY_TYPE_LIST", "PY_TYPE_TUPLE", "PY_TYPE_DICT", "PY_TYPE_SET"):
        assert tag in c_alloc
    assert "stored_flags |= PY_FLAG_GC_FRESH_ALLOC" in c_alloc
    c_publish = c_obj.split("void pcc_gc_publish_initialized(", 1)[1].split(
        "PyObject *pcc_gc_retain", 1
    )[0]
    assert c_publish.index("pcc_gc_root_slot_lock()") < c_publish.index(
        "~PY_FLAG_GC_FRESH_ALLOC"
    ) < c_publish.index("pcc_gc_root_slot_unlock()")

    py_obj = PY_OBJ_PORT.read_text(encoding="utf-8")
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

    for c_name in ("py_list.c", "py_dict.c", "py_set.c"):
        source = (RUNTIME_DIR / "src" / c_name).read_text(encoding="utf-8")
        assert "pcc_gc_publish_initialized(" in source
    c_tuple = (RUNTIME_DIR / "src" / "py_tuple.c").read_text(encoding="utf-8")
    assert "if (complete) pcc_gc_publish_initialized(tuple)" in c_tuple
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


def _backend4_fresh_admission_sources() -> tuple[str, str]:
    """The admission condition in both mirrors of pcc_gc_alloc."""

    c_obj = PY_OBJ_C.read_text(encoding="utf-8")
    c_alloc = c_obj.split("PyObject *pcc_gc_alloc(", 1)[1].split(
        "void pcc_gc_publish_initialized", 1
    )[0]
    py_obj = PY_OBJ_PORT.read_text(encoding="utf-8")
    py_alloc = py_obj.split("def pcc_gc_alloc(", 1)[1].split(
        '@c_abi_export("pcc_gc_publish_initialized")', 1
    )[0]
    return c_alloc, py_alloc


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

    c_alloc, py_alloc = _backend4_fresh_admission_sources()
    backend_c = PY_GC_BACKEND_C.read_text(encoding="utf-8")
    supported = backend_c.split(
        "static int pcc_gc_colored_relocate_copy_supported_tag(", 1
    )[1].split("\n}", 1)[0]
    supported_tags = set(re.findall(r"PY_TYPE_[A-Z_]+", supported))
    assert supported_tags, supported[:200]
    for source in (c_alloc, py_alloc):
        admitted = set(re.findall(r"PY_TYPE_[A-Z_]+", source))
        assert admitted, source[:200]
        assert admitted <= supported_tags, sorted(admitted - supported_tags)


def test_backend4_container_and_wrapper_tags_are_admitted_and_publish():
    c_alloc, py_alloc = _backend4_fresh_admission_sources()
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
        assert tag in c_alloc, tag
        assert tag in py_alloc, tag
    _publishes(
        "src/py_list.c",
        "src/py_dict.c",
        "src/py_set.c",
        "src/py_class_attrs.c",
        "src/py_weakref.c",
        "src/py_bytes.c",
        "py/py_list.py",
        "py/py_dict.py",
        "py/py_set.py",
        "py/py_class.py",
        "py/py_weakref.py",
        "py/py_obj_stubs.py",
    )
    c_tuple = (RUNTIME_DIR / "src" / "py_tuple.c").read_text(encoding="utf-8")
    assert "if (complete) pcc_gc_publish_initialized(tuple)" in c_tuple
    py_tuple = (RUNTIME_DIR / "py" / "py_tuple.py").read_text(encoding="utf-8")
    assert (
        "if complete != 0:\n        pcc_gc_publish_initialized(tuple_ptr)"
        in py_tuple
    )


def test_backend4_staticmethod_admission_is_inert_for_lack_of_a_constructor():
    """STATICMETHOD is admitted, and that admission can strand nothing.

    The tag is relocate-copy supported, so leaving it out would be the only
    inconsistency in the list; but static methods lower directly to their
    wrapped callable, so no constructor allocates one and no object can be
    left stuck fresh by admitting it.  If a constructor is ever added it must
    publish, and this test is where that shows up.
    """

    c_alloc, py_alloc = _backend4_fresh_admission_sources()
    assert "PY_TYPE_STATICMETHOD" in c_alloc
    assert "PY_TYPE_STATICMETHOD" in py_alloc

    allocations = []
    for directory, suffix in (("src", ".c"), ("py", ".py")):
        for path in sorted((RUNTIME_DIR / directory).glob("*" + suffix)):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "pcc_gc_alloc(" in line and "PY_TYPE_STATICMETHOD" in line:
                    allocations.append(path.name + ": " + line.strip())
    assert allocations == [], allocations

    internal = (
        RUNTIME_DIR / "src" / "py_internal.h"
    ).read_text(encoding="utf-8")
    assert "PY_TYPE_STATICMETHOD remains part of the runtime layout/GC contract" in (
        internal
    )
    assert "has\n * no public constructor" in internal


def test_backend4_function_and_iterator_tags_are_admitted_and_publish():
    c_alloc, py_alloc = _backend4_fresh_admission_sources()
    for tag in ("PY_TYPE_FUNC", "PY_TYPE_ITER"):
        assert tag in c_alloc, tag
        assert tag in py_alloc, tag
    _publishes(
        "src/py_func.c",
        "src/py_iter.c",
        "py/py_func.py",
        "py/py_iter.py",
    )


def test_backend4_suspended_execution_tags_are_admitted_and_publish():
    c_alloc, py_alloc = _backend4_fresh_admission_sources()
    for tag in (
        "PY_TYPE_GEN",
        "PY_TYPE_COROUTINE",
        "PY_TYPE_CONTINUATION",
        "PY_TYPE_TASK",
        "PY_TYPE_EXC",
        "PY_TYPE_CLASS",
    ):
        assert tag in c_alloc, tag
        assert tag in py_alloc, tag
    _publishes(
        "src/py_gen.c",
        "src/py_coroutine.c",
        "src/py_exc_objects.c",
        "src/py_class.c",
        "py/py_gen.py",
        "py/py_coroutine.py",
        "py/py_exc_objects.py",
        "py/py_class.py",
    )


def test_capi_borrowed_container_items_pin_but_getitemref_stays_owned():
    for name in ("py_capi_shim.c", "py_capi_shim_oracle.c"):
        source = (RUNTIME_DIR / "src" / name).read_text(encoding="utf-8")
        for fn, next_fn in (
            ("PyTuple_GetItem", "PyTuple_New"),
            ("PyList_GetItem", "PyList_GetItemRef"),
            ("PyDict_GetItem", "PyDict_GetItemString"),
            ("PyDict_GetItemWithError", "PyDict_GetItemRef"),
        ):
            body = source.rsplit(f"PyObject *{fn}(", 1)[1].split(next_fn, 1)[0]
            assert body.index("pcc_gc_pin(item)") < body.index("py_decref(item)")
        item_ref = source.rsplit("PyObject *PyList_GetItemRef(", 1)[1].split(
            "Py_ssize_t PyList_Size", 1
        )[0]
        assert "py_list_get(obj" in item_ref
        assert "PyList_GetItem(obj" not in item_ref
        assert "pcc_gc_pin" not in item_ref

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
    for name in ("py_capi_shim.c", "py_capi_shim_oracle.c"):
        source = (RUNTIME_DIR / "src" / name).read_text(encoding="utf-8")
        body = source.rsplit("PyObject **PySequence_Fast_ITEMS(", 1)[1].split(
            "PyObject *PySequence_List", 1
        )[0]
        assert body.index("pcc_gc_pin(obj)") < body.index("PyTuple_Check(obj)")
    strict = (
        RUNTIME_DIR / "py" / "py_capi_sequence_runtime.py"
    ).read_text(encoding="utf-8")
    body = strict.split("def PySequence_Fast_ITEMS(", 1)[1].split(
        '@c_abi_typed_export("PySequence_List"', 1
    )[0]
    assert body.index("pcc_gc_pin(obj)") < body.index("PyList_Check(obj)")


def test_capi_unicode_bytes_raw_accessors_pin_without_polluting_internal_utf8():
    c_accessor = (
        RUNTIME_DIR / "src" / "py_str_accessors.c"
    ).read_text(encoding="utf-8")
    internal = c_accessor.split("const char *py_str_utf8(", 1)[1].split(
        "const char *pcc_capi_str_utf8_pinned", 1
    )[0]
    pinned = c_accessor.split("const char *pcc_capi_str_utf8_pinned", 1)[1].split(
        "int64_t py_str_len", 1
    )[0]
    assert "pcc_gc_pin" not in internal
    assert pinned.index("pcc_gc_pin(s)") < pinned.index("py_str_utf8(s)")
    header = (
        REPO_ROOT / "utils" / "fake_libc_include" / "Python.h"
    ).read_text(encoding="utf-8")
    for macro in (
        "PyUnicode_1BYTE_DATA", "PyUnicode_2BYTE_DATA",
        "PyUnicode_4BYTE_DATA", "PyUnicode_DATA",
    ):
        line = next(line for line in header.splitlines() if line.startswith(f"#define {macro}"))
        assert "pcc_capi_str_utf8_pinned" in line

    for name in ("py_capi_shim.c", "py_capi_shim_oracle.c"):
        source = (RUNTIME_DIR / "src" / name).read_text(encoding="utf-8")
        for fn, next_fn in (
            ("PyUnicode_AsUTF8", "const char *PyUnicode_AsUTF8AndSize"),
            ("PyUnicode_AsUTF8AndSize", "PyObject *PyUnicode_AsUTF8String"),
        ):
            body = source.rsplit(f"const char *{fn}(", 1)[1].split(next_fn, 1)[0]
            assert "pcc_capi_str_utf8_pinned(obj)" in body
        for fn, next_fn in (
            ("PyBytes_AsString", "int PyBytes_AsStringAndSize"),
            ("PyBytes_AsStringAndSize", "Py_ssize_t PyBytes_Size"),
        ):
            body = source.rsplit(f"{fn}(", 1)[1].split(next_fn, 1)[0]
            assert "pcc_gc_pin(obj)" in body

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


@pytest.mark.parametrize("kind", ["c", "pcc_python"])
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
    for name in ("py_capi_shim.c", "py_capi_shim_oracle.c"):
        source = (RUNTIME_DIR / "src" / name).read_text(encoding="utf-8")
        struct = source.split("typedef struct PccBufferMeta", 1)[1].split(
            "} PccBufferMeta", 1
        )[0]
        for field in ("lease_owner", "view_owner", "next"):
            assert field in struct
        get_buffer = source.rsplit("int PyObject_GetBuffer(", 1)[1].split(
            "void PyBuffer_Release", 1
        )[0]
        assert "pcc_capi_buffer_lease_owner(obj)" in get_buffer
        assert "pcc_buffer_leases = meta" in get_buffer
        assert "pcc_gc_pin(lease_owner)" in get_buffer
        assert "pcc_gc_pin(obj)" in get_buffer
        release = source.rsplit("void PyBuffer_Release(", 1)[1].split(
            "PyMemoryView_Check", 1
        )[0]
        assert release.index("*cursor = meta->next") < release.index(
            "pcc_gc_unpin(meta->lease_owner)"
        ) < release.index("py_decref(view->obj)")

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


@pytest.mark.parametrize("kind", ["c", "pcc_python"])
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
    c_source = (
        RUNTIME_DIR / "src" / "py_obj_ops_compare.c"
    ).read_text(encoding="utf-8")
    c_body = c_source.split("PyObject *py_obj_sorted(", 1)[1].split(
        "int64_t py_obj_contains", 1
    )[0]
    assert (
        c_body.index("pcc_gc_scheduler_root_register_handle(&x_root)")
        < c_body.index("py_obj_len(x)")
    )
    assert c_body.index("pcc_gc_pin(out)") < c_body.index("py_obj_iter(x)")
    assert (
        c_body.index("pcc_gc_scheduler_root_register_handle(&it_root)")
        < c_body.index("py_obj_next(it)")
    )
    assert c_body.index("pcc_gc_pin(scratch)") < c_body.index("src_list = out")
    assert c_body.count("pcc_gc_unpin(out)") == 3
    assert "pcc_gc_pin(x)" not in c_body
    assert "pcc_gc_unpin(x)" not in c_body
    assert "pcc_gc_pin(it)" not in c_body
    assert "pcc_gc_unpin(it)" not in c_body

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
    assert py_body.count("pcc_gc_unpin(out)") == 3
    assert "pcc_gc_pin(x)" not in py_body
    assert "pcc_gc_unpin(x)" not in py_body
    assert "pcc_gc_pin(it)" not in py_body
    assert "pcc_gc_unpin(it)" not in py_body


@pytest.mark.parametrize("kind", ["c", "pcc_python"])
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
    c_source = (RUNTIME_DIR / "src" / "py_obj_min_max.c").read_text(
        encoding="utf-8"
    )
    c_body = c_source.split("PyObject *py_obj_min_max(", 1)[1]
    assert c_body.count("min_max_prepare_root(") == 3
    compact_c_body = (
        " ".join(c_body.split()).replace("( ", "(").replace(" )", ")")
    )
    assert (
        "py_obj_next(min_max_reload_root(&it_storage, it_handle))"
        in compact_c_body
    )
    compare_at = c_body.index("int replace = want_max")
    assert c_body.index(
        "min_max_reload_root(best_slot, best_handle);", compare_at
    ) > compare_at
    assert c_body.index(
        "min_max_reload_root(element_slot, element_handle);", compare_at
    ) > compare_at
    assert "pcc_gc_pin(" not in c_body

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


@pytest.mark.parametrize("kind", ["c", "pcc_python"])
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
            (RUNTIME_DIR / "src" / "py_obj_min_max.c",)
            if kind == "c"
            else ()
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
    c_source = (RUNTIME_DIR / "src" / "py_enumerate.c").read_text(
        encoding="utf-8"
    )
    c_body = c_source.split("PyObject *py_enumerate_list(", 1)[1]
    assert c_body.count("enumerate_prepare_root(") == 3
    assert "py_obj_next( enumerate_reload_root(&it_storage, it_handle) )" in (
        " ".join(c_body.split())
    )
    assert c_body.index("pcc_gc_pin(out)") < c_body.index("py_obj_next(")
    assert c_body.index("pcc_gc_pin(tup)") < c_body.index("py_int_from_i64")
    assert c_body.count("pcc_gc_unpin(out)") == 7

    py_source = (RUNTIME_DIR / "py" / "py_iter.py").read_text(encoding="utf-8")
    py_body = py_source.split("def py_enumerate_list(iterable, start: int):", 1)[
        1
    ]
    assert py_body.count("pcc_gc_scheduler_root_register_handle(") == 3
    assert py_body.index("pcc_gc_pin(out)") < py_body.index("py_obj_next(it)")
    assert py_body.index("pcc_gc_pin(pair)") < py_body.index("py_int_from_i64")
    assert py_body.count("pcc_gc_unpin(out)") == 7


@pytest.mark.parametrize("kind", ["c", "pcc_python"])
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
            (RUNTIME_DIR / "src" / "py_enumerate.c",)
            if kind == "c"
            else ()
        ),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} py_enumerate_list root-balance returned {run.returncode}: "
        + run.stdout + run.stderr
    )
