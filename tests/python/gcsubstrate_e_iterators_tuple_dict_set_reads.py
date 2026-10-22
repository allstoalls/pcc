"""Iterator, tuple, dict and set rooted reads with callback-relocation owner reloads.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403


def test_py_obj_next_roots_internal_iterator_state_across_call_and_equality():

    py_source = (RUNTIME_DIR / "py" / "py_iter.py").read_text(encoding="utf-8")
    py_body = py_source.split("def py_obj_next(it_obj):", 1)[1].split(
        '@c_abi_export("py_enumerate_list")', 1
    )[0]
    assert py_body.count("_iter_prepare_moving_root(") == 6
    eq_at = py_body.index("is_stop: int = py_obj_eq(")
    assert py_body.index("_iter_finish_owned_root(result_slot, result_handle, it_handle)", eq_at) > eq_at
    assert py_body.index("_iter_release_owned_root(sentinel_slot, sentinel_handle)", eq_at) > eq_at
    done_at = py_body.index("store_i64(it_obj, 24, -2)", eq_at)
    assert py_body.rindex(
        "_iter_reload_moving_root(it_slot, it_handle)", eq_at, done_at
    ) < done_at


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_callable_iterator_reloads_state_after_cext_equality_relocation(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_callable_iterator_callback_roots",
        source_text=r'''
            #include "Python.h"
            #include <stdint.h>
            #include <stdio.h>

            typedef struct ProbeEqObject {
                PyObject_HEAD
            } ProbeEqObject;

            static PyObject *iterator_root;
            static PyObject *sentinel_root;
            static int64_t call_count;
            static int64_t equality_count;

            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);
            static int relocate_once(PyObject *value, int64_t size) {
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(value) != 1) return 0;
                PyObject *target = pcc_gc_relocate_copy(value, size);
                if (target == NULL || target == value) return 0;
                py_decref(target);
                return 1;
            }

            static PyObject *probe_eq(
                PyObject *self,
                PyObject *other,
                int op
            ) {
                (void)self;
                (void)other;
                if (op != Py_EQ) {
                    Py_INCREF(Py_NotImplemented);
                    return Py_NotImplemented;
                }
                if (equality_count == 0) {
                    PyObject *iterator = pcc_gc_load_ptr(
                        NULL, &iterator_root
                    );
                    if (!relocate_once(
                            iterator, 32
                        )) return NULL;
                }
                equality_count++;
                return py_bool_from_bit(0);
            }

            static PyTypeObject ProbeEqType = {
                PyVarObject_HEAD_INIT(NULL, 0)
                .tp_name = "pcc_probe.CallableIteratorEq",
                .tp_basicsize = sizeof(ProbeEqObject),
                .tp_flags = Py_TPFLAGS_DEFAULT,
                .tp_richcompare = probe_eq,
            };

            static PyObject *probe_callable(
                PyObject *captures,
                PyObject *args
            ) {
                (void)captures;
                (void)args;
                call_count++;
                if (call_count == 1) {
                    return (PyObject *)PyObject_New(
                        ProbeEqObject, &ProbeEqType
                    );
                }
                PyObject *sentinel = pcc_gc_load_ptr(NULL, &sentinel_root);
                py_incref(sentinel);
                return sentinel;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeEqType) != 0) return 3;
                PyObject *captures = py_tuple_new(0);
                PyObject *callable = py_func_new(
                    (void *)(uintptr_t)probe_callable, captures
                );
                py_decref(captures);
                sentinel_root = (PyObject *)PyObject_New(
                    ProbeEqObject, &ProbeEqType
                );
                if (callable == NULL || sentinel_root == NULL) return 4;
                void *sentinel_handle = pcc_gc_scheduler_root_register_handle(
                    &sentinel_root
                );
                if (sentinel_handle == NULL) return 5;
                iterator_root = py_iter_callable_new(callable, sentinel_root);
                py_decref(callable);
                if (iterator_root == NULL) return 6;
                void *iterator_handle = pcc_gc_scheduler_root_register_handle(
                    &iterator_root
                );
                if (iterator_handle == NULL) return 7;

                PyObject *first = py_obj_next(iterator_root);
                if (first == NULL || equality_count != 1) return 8;
                py_decref(first);
                iterator_root = pcc_gc_load_ptr(NULL, &iterator_root);
                PyObject *second = py_obj_next(iterator_root);
                if (second != NULL || !py_err_occurred()) return 9;
                py_clear_exception();
                iterator_root = pcc_gc_load_ptr(NULL, &iterator_root);
                PyObject *third = py_obj_next(iterator_root);
                if (third != NULL || !py_err_occurred()) return 10;
                py_clear_exception();
                if (call_count != 2 || equality_count != 1) return 11;
                if (pcc_gc_scheduler_root_count() != 2) return 12;

                iterator_root = pcc_gc_load_ptr(NULL, &iterator_root);
                sentinel_root = pcc_gc_load_ptr(NULL, &sentinel_root);
                pcc_gc_scheduler_root_unregister_handle(iterator_handle);
                pcc_gc_scheduler_root_unregister_handle(sentinel_handle);
                py_decref(iterator_root);
                Py_DECREF(sentinel_root);
                return 0;
            }
        ''',
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} callable iterator C-extension callback roots returned "
        f"{run.returncode}: "
        + run.stdout + run.stderr
    )


def test_tuple_method_scans_share_rooted_callback_and_owned_element_cleanup():

    py_source = (RUNTIME_DIR / "py" / "py_tuple.py").read_text(encoding="utf-8")
    py_scan = py_source.split("def _tuple_method_scan(", 1)[1].split(
        '@c_abi_export("py_tuple_count")', 1
    )[0]
    assert py_scan.count("_tuple_method_prepare_root(") == 3
    eq_at = py_scan.index("equal: int = py_obj_eq(")
    assert py_scan.index("_tuple_method_reload_root(tuple_slot", eq_at) > eq_at
    assert py_scan.index("_tuple_method_reload_root(query_slot", eq_at) > eq_at
    assert py_scan.index("_tuple_method_reload_root(element_slot", eq_at) > eq_at
    assert py_scan.index("py_decref(element)", eq_at) > eq_at


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_tuple_method_equality_callback_reloads_relocated_tuple(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_tuple_method_equality_roots",
        source_text=r'''
            #include "Python.h"
            #include <stdint.h>

            typedef struct ProbeEqObject {
                PyObject_HEAD
            } ProbeEqObject;

            static PyObject *tuple_root;
            static int64_t equality_count;

            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);
            extern int64_t py_tuple_index_range(
                PyObject *tuple,
                PyObject *item,
                PyObject *start,
                PyObject *stop
            );

            static PyObject *probe_eq(
                PyObject *self,
                PyObject *other,
                int op
            ) {
                (void)self;
                (void)other;
                if (op != Py_EQ) {
                    Py_INCREF(Py_NotImplemented);
                    return Py_NotImplemented;
                }
                if (equality_count == 0) {
                    PyObject *tuple = pcc_gc_load_ptr(NULL, &tuple_root);
                    pcc_gc_reset_relocation_set();
                    if (pcc_gc_backend4_relocation_set_add(tuple) != 1) {
                        return NULL;
                    }
                    PyObject *target = pcc_gc_relocate_copy(tuple, 40);
                    if (target == NULL || target == tuple) return NULL;
                    py_decref(target);
                }
                equality_count++;
                Py_INCREF(Py_True);
                return Py_True;
            }

            static PyTypeObject ProbeEqType = {
                PyVarObject_HEAD_INIT(NULL, 0)
                .tp_name = "pcc_probe.TupleEq",
                .tp_basicsize = sizeof(ProbeEqObject),
                .tp_flags = Py_TPFLAGS_DEFAULT,
                .tp_richcompare = probe_eq,
            };

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeEqType) != 0) return 3;
                PyObject *left = (PyObject *)PyObject_New(
                    ProbeEqObject, &ProbeEqType
                );
                PyObject *right = (PyObject *)PyObject_New(
                    ProbeEqObject, &ProbeEqType
                );
                PyObject *query = (PyObject *)PyObject_New(
                    ProbeEqObject, &ProbeEqType
                );
                tuple_root = py_tuple_new(2);
                if (left == NULL || right == NULL || query == NULL
                    || tuple_root == NULL) return 4;
                py_tuple_set_item(tuple_root, 0, left);
                py_tuple_set_item(tuple_root, 1, right);
                Py_DECREF(left);
                Py_DECREF(right);
                void *tuple_handle = pcc_gc_scheduler_root_register_handle(
                    &tuple_root
                );
                if (tuple_handle == NULL) return 5;

                if (py_tuple_count(tuple_root, query) != 2) return 6;
                tuple_root = pcc_gc_load_ptr(NULL, &tuple_root);
                if (py_tuple_index(tuple_root, query) != 0) return 7;
                PyObject *start = py_int_from_i64(1);
                tuple_root = pcc_gc_load_ptr(NULL, &tuple_root);
                if (py_tuple_index_range(
                        tuple_root, query, start, NULL
                    ) != 1) return 8;
                if (equality_count != 4) return 9;
                if (pcc_gc_scheduler_root_count() != 1) return 10;
                py_decref(start);
                Py_DECREF(query);
                tuple_root = pcc_gc_load_ptr(NULL, &tuple_root);
                pcc_gc_scheduler_root_unregister_handle(tuple_handle);
                py_decref(tuple_root);
                return 0;
            }
        ''',
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
        extra_sources=(
            ()
        ),
        extra_compile_args=(
            ()
        ),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} tuple method callback roots returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_dict_get_contains_use_rooted_restartable_hash_equality_lookup():
    # dict, key, value and the equality candidate are all rooted.

    py_source = (RUNTIME_DIR / "py" / "py_dict.py").read_text(encoding="utf-8")
    py_read = py_source.split("def _dict_rooted_op(", 1)[1].split(
        "def _rehash_find_empty_slot", 1
    )[0]
    assert py_read.count("_dict_read_prepare_root(") == 4
    hash_at = py_read.index("hash_val: int = py_obj_hash(key)")
    assert py_read.index("_dict_read_reload_root(dict_slot", hash_at) > hash_at
    eq_at = py_read.index("equal = py_obj_eq(entry_key, key)")
    assert py_read.index("_dict_read_reload_root(dict_slot", eq_at) > eq_at
    assert "if stable == 0:" in py_read


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_dict_read_hash_and_equality_callbacks_reload_relocated_owner(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_dict_read_callback_roots",
        source_text=r'''
            #include "Python.h"
            #include <stdint.h>
            #include <stdio.h>

            typedef struct ProbeKeyObject {
                PyObject_HEAD
            } ProbeKeyObject;

            static PyObject *hash_dict_root;
            static PyObject *eq_dict_root;
            static struct PyClassObject *hash_class;
            static int64_t mode;
            static int64_t hash_relocations;
            static int64_t equality_relocations;

            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);
            extern struct PyClassObject *py_class_new(
                const char *, struct PyClassObject **, int32_t,
                const char **, int32_t
            );
            extern void py_class_add_method(
                struct PyClassObject *, const char *, PyObject *
            );
            extern PyObject *py_instance_new(struct PyClassObject *);

            static int relocate_dict(PyObject **slot) {
                PyObject *dict = pcc_gc_load_ptr(NULL, slot);
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(dict) != 1) return 0;
                PyObject *target = pcc_gc_relocate_copy(dict, 56);
                if (target == NULL || target == dict) return 0;
                py_decref(target);
                return 1;
            }

            static PyObject *probe_hash(PyObject *self) {
                (void)self;
                if (mode == 1 && hash_relocations == 0) {
                    if (!relocate_dict(&hash_dict_root)) return NULL;
                    hash_relocations++;
                }
                return py_int_from_i64(7);
            }

            static PyObject *probe_eq(
                PyObject *self,
                PyObject *other,
                int op
            ) {
                (void)self;
                (void)other;
                if (op != Py_EQ) {
                    Py_INCREF(Py_NotImplemented);
                    return Py_NotImplemented;
                }
                if (mode == 2 && equality_relocations == 0) {
                    if (!relocate_dict(&eq_dict_root)) return NULL;
                    equality_relocations++;
                }
                Py_INCREF(Py_True);
                return Py_True;
            }

            static PyTypeObject ProbeKeyType = {
                PyVarObject_HEAD_INIT(NULL, 0)
                .tp_name = "pcc_probe.DictReadKey",
                .tp_basicsize = sizeof(ProbeKeyObject),
                .tp_flags = Py_TPFLAGS_DEFAULT,
                .tp_richcompare = probe_eq,
            };

            static PyObject *new_key(void) {
                return (PyObject *)PyObject_New(ProbeKeyObject, &ProbeKeyType);
            }

            static int64_t owned_int_value(PyObject *value) {
                if (value == NULL) return -999;
                int overflow = 0;
                int64_t out = py_int_to_i64(value, &overflow);
                py_decref(value);
                return overflow ? -999 : out;
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeKeyType) != 0) return 3;
                hash_class = py_class_new(
                    "HashMovingKey", NULL, 0, NULL, 0
                );
                if (hash_class == NULL) return 4;
                pcc_gc_pin((PyObject *)hash_class);
                py_class_add_method(
                    hash_class,
                    "__hash__",
                    (PyObject *)(uintptr_t)probe_hash
                );
                hash_dict_root = py_dict_new();
                eq_dict_root = py_dict_new();
                PyObject *hash_key = py_instance_new(hash_class);
                PyObject *eq_key = new_key();
                PyObject *eq_query = new_key();
                if (hash_dict_root == NULL || eq_dict_root == NULL
                    || hash_key == NULL || eq_key == NULL
                    || eq_query == NULL) return 5;
                py_dict_set(hash_dict_root, hash_key, py_int_from_i64(11));
                py_dict_set(eq_dict_root, eq_key, py_int_from_i64(22));
                void *hash_handle = pcc_gc_scheduler_root_register_handle(
                    &hash_dict_root
                );
                void *eq_handle = pcc_gc_scheduler_root_register_handle(
                    &eq_dict_root
                );
                if (hash_handle == NULL || eq_handle == NULL) return 6;

                mode = 1;
                if (owned_int_value(
                        py_dict_get(hash_dict_root, hash_key)
                    ) != 11) return 7;
                hash_dict_root = pcc_gc_load_ptr(NULL, &hash_dict_root);
                mode = 2;
                if (owned_int_value(
                        py_dict_get(eq_dict_root, eq_query)
                    ) != 22) return 8;
                eq_dict_root = pcc_gc_load_ptr(NULL, &eq_dict_root);
                mode = 0;
                if (!py_dict_contains(hash_dict_root, hash_key)) return 9;
                if (!py_dict_contains(eq_dict_root, eq_query)) return 10;
                if (hash_relocations != 1) return 11;
                if (equality_relocations != 1) return 12;
                if (pcc_gc_scheduler_root_count() != 2) return 13;

                py_decref(hash_key);
                Py_DECREF(eq_key);
                Py_DECREF(eq_query);
                hash_dict_root = pcc_gc_load_ptr(NULL, &hash_dict_root);
                eq_dict_root = pcc_gc_load_ptr(NULL, &eq_dict_root);
                pcc_gc_scheduler_root_unregister_handle(hash_handle);
                pcc_gc_scheduler_root_unregister_handle(eq_handle);
                py_decref(hash_dict_root);
                py_decref(eq_dict_root);
                pcc_gc_unpin((PyObject *)hash_class);
                py_decref((PyObject *)hash_class);
                return 0;
            }
        ''',
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} dict read callback roots returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_set_contains_uses_rooted_restartable_hash_equality_lookup():

    py_source = (RUNTIME_DIR / "py" / "py_set.py").read_text(encoding="utf-8")
    py_read = py_source.split("def _set_lookup_rooted(s, item, mode: int) -> int:", 1)[
        1
    ].split("def _rehash_find_empty_slot", 1)[0]
    assert py_read.count("_set_read_prepare_root(") == 3
    hash_at = py_read.index("hash_val: int = py_obj_hash(item)")
    assert py_read.index("_set_read_reload_root(set_slot", hash_at) > hash_at
    eq_at = py_read.index("equal: int = py_obj_eq(entry_key, item)")
    assert py_read.index("_set_read_reload_root(set_slot", eq_at) > eq_at
    assert "if stable == 0:" in py_read


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_set_contains_hash_and_equality_callbacks_reload_relocated_owner(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_set_contains_callback_roots",
        source_text=r'''
            #include "Python.h"
            #include <stdint.h>

            typedef struct ProbeKeyObject {
                PyObject_HEAD
            } ProbeKeyObject;

            static PyObject *hash_set_root;
            static PyObject *eq_set_root;
            static struct PyClassObject *hash_class;
            static int64_t mode;
            static int64_t hash_relocations;
            static int64_t equality_relocations;

            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);
            extern struct PyClassObject *py_class_new(
                const char *, struct PyClassObject **, int32_t,
                const char **, int32_t
            );
            extern void py_class_add_method(
                struct PyClassObject *, const char *, PyObject *
            );
            extern PyObject *py_instance_new(struct PyClassObject *);

            static int relocate_set(PyObject **slot) {
                PyObject *set = pcc_gc_load_ptr(NULL, slot);
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(set) != 1) return 0;
                PyObject *target = pcc_gc_relocate_copy(set, 48);
                if (target == NULL || target == set) return 0;
                py_decref(target);
                return 1;
            }

            static PyObject *probe_hash(PyObject *self) {
                (void)self;
                if (mode == 1 && hash_relocations == 0) {
                    if (!relocate_set(&hash_set_root)) return NULL;
                    hash_relocations++;
                }
                return py_int_from_i64(7);
            }

            static PyObject *probe_eq(
                PyObject *self,
                PyObject *other,
                int op
            ) {
                (void)self;
                (void)other;
                if (op != Py_EQ) {
                    Py_INCREF(Py_NotImplemented);
                    return Py_NotImplemented;
                }
                if (mode == 2 && equality_relocations == 0) {
                    if (!relocate_set(&eq_set_root)) return NULL;
                    equality_relocations++;
                }
                Py_INCREF(Py_True);
                return Py_True;
            }

            static PyTypeObject ProbeKeyType = {
                PyVarObject_HEAD_INIT(NULL, 0)
                .tp_name = "pcc_probe.SetContainsKey",
                .tp_basicsize = sizeof(ProbeKeyObject),
                .tp_flags = Py_TPFLAGS_DEFAULT,
                .tp_richcompare = probe_eq,
            };

            static PyObject *new_key(void) {
                return (PyObject *)PyObject_New(ProbeKeyObject, &ProbeKeyType);
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeKeyType) != 0) return 3;
                hash_class = py_class_new(
                    "SetHashMovingKey", NULL, 0, NULL, 0
                );
                if (hash_class == NULL) return 4;
                pcc_gc_pin((PyObject *)hash_class);
                py_class_add_method(
                    hash_class,
                    "__hash__",
                    (PyObject *)(uintptr_t)probe_hash
                );
                hash_set_root = py_set_new();
                eq_set_root = py_set_new();
                PyObject *hash_key = py_instance_new(hash_class);
                PyObject *eq_key = new_key();
                PyObject *eq_query = new_key();
                if (hash_set_root == NULL || eq_set_root == NULL
                    || hash_key == NULL || eq_key == NULL
                    || eq_query == NULL) return 5;
                py_set_add(hash_set_root, hash_key);
                py_set_add(eq_set_root, eq_key);
                void *hash_handle = pcc_gc_scheduler_root_register_handle(
                    &hash_set_root
                );
                void *eq_handle = pcc_gc_scheduler_root_register_handle(
                    &eq_set_root
                );
                if (hash_handle == NULL || eq_handle == NULL) return 6;

                mode = 1;
                if (!py_set_contains(hash_set_root, hash_key)) return 7;
                hash_set_root = pcc_gc_load_ptr(NULL, &hash_set_root);
                mode = 2;
                if (!py_set_contains(eq_set_root, eq_query)) return 8;
                eq_set_root = pcc_gc_load_ptr(NULL, &eq_set_root);
                mode = 0;
                if (!py_set_contains(hash_set_root, hash_key)) return 9;
                if (!py_set_contains(eq_set_root, eq_query)) return 10;
                if (hash_relocations != 1 || equality_relocations != 1) return 11;
                if (pcc_gc_scheduler_root_count() != 2) return 12;

                py_decref(hash_key);
                Py_DECREF(eq_key);
                Py_DECREF(eq_query);
                hash_set_root = pcc_gc_load_ptr(NULL, &hash_set_root);
                eq_set_root = pcc_gc_load_ptr(NULL, &eq_set_root);
                pcc_gc_scheduler_root_unregister_handle(hash_handle);
                pcc_gc_scheduler_root_unregister_handle(eq_handle);
                py_decref(hash_set_root);
                py_decref(eq_set_root);
                pcc_gc_unpin((PyObject *)hash_class);
                py_decref((PyObject *)hash_class);
                return 0;
            }
        ''',
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} set contains callback roots returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_set_remove_commits_tombstone_and_size_before_decref_finish():

    py_source = (RUNTIME_DIR / "py" / "py_set.py").read_text(encoding="utf-8")
    py_remove = py_source.split("def _set_remove_rooted_slot(", 1)[1].split(
        "def _set_lookup_rooted", 1
    )[0]
    plan = py_remove.index("pcc_gc_store_ptr_plan_init")
    lock = py_remove.index("pcc_py_gc_minor_graph_lock()", plan)
    commit = py_remove.index("pcc_gc_store_ptr_plan_commit_locked", lock)
    size = py_remove.index("store_i64(s, 16, size - 1)", commit)
    unlock = py_remove.index("pcc_py_gc_minor_graph_unlock()", size)
    finish = py_remove.index("pcc_gc_store_ptr_plan_finish", unlock)
    assert plan < lock < commit < size < unlock < finish


def test_set_add_commits_key_hash_and_counters_under_graph_lock():

    py_source = (RUNTIME_DIR / "py" / "py_set.py").read_text(encoding="utf-8")
    py_add = py_source.split("def _set_add_rooted_slot(", 1)[1].split(
        "def _set_lookup_rooted", 1
    )[0]
    plan = py_add.index("pcc_gc_store_ptr_plan_init")
    lock = py_add.index("pcc_py_gc_minor_graph_lock()", plan)
    commit = py_add.index("pcc_gc_store_ptr_plan_commit_locked", lock)
    entry_hash = py_add.index("store_i64(entries, slot_off, hash_val)", commit)
    size = py_add.index("store_i64(s, 16, size + 1)", entry_hash)
    unlock = py_add.index("pcc_py_gc_minor_graph_unlock()", size)
    finish = py_add.index("pcc_gc_store_ptr_plan_finish", unlock)
    assert plan < lock < commit < entry_hash < size < unlock < finish


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_set_add_and_update_survive_callback_relocation_and_source_mutation(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_set_add_update_callback_commit",
        source_text=r'''
            #include "Python.h"
            #include <stdint.h>
            #include <stdio.h>

            typedef struct ProbeKeyObject {
                PyObject_HEAD
            } ProbeKeyObject;

            static PyObject *hash_set_root;
            static PyObject *eq_set_root;
            static PyObject *update_dst_root;
            static PyObject *update_src_root;
            static struct PyClassObject *hash_class;
            static int64_t mode;
            static int64_t hash_relocations;
            static int64_t equality_relocations;
            static int64_t update_mutations;
            static int64_t update_len_before = -1;
            static int64_t update_len_after = -1;
            static int64_t update_error_after = -1;

            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);

            static int relocate_set(PyObject **slot) {
                PyObject *set = pcc_gc_load_ptr(NULL, slot);
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(set) != 1) return 0;
                PyObject *target = pcc_gc_relocate_copy(set, 48);
                if (target == NULL || target == set) return 0;
                py_decref(target);
                return 1;
            }

            extern struct PyClassObject *py_class_new(
                const char *, struct PyClassObject **, int32_t,
                const char **, int32_t
            );
            extern void py_class_add_method(
                struct PyClassObject *, const char *, PyObject *
            );
            extern PyObject *py_instance_new(struct PyClassObject *);

            static PyObject *probe_hash(PyObject *self) {
                (void)self;
                if (mode == 1 && hash_relocations == 0) {
                    if (!relocate_set(&hash_set_root)) return NULL;
                    hash_relocations++;
                }
                return py_int_from_i64(7);
            }

            static void mutate_update_source(void) {
                if (update_mutations == 0) {
                    if (!relocate_set(&update_dst_root)) return;
                    update_src_root = pcc_gc_load_ptr(
                        NULL, &update_src_root
                    );
                    update_len_before = py_set_len(update_src_root);
                    py_set_add(update_src_root, py_int_from_i64(99));
                    update_src_root = pcc_gc_load_ptr(
                        NULL, &update_src_root
                    );
                    update_len_after = py_set_len(update_src_root);
                    update_error_after = py_err_occurred();
                    update_mutations++;
                }
            }

            static PyObject *probe_eq(
                PyObject *self,
                PyObject *other,
                int op
            ) {
                (void)self;
                (void)other;
                if (op != Py_EQ) {
                    Py_INCREF(Py_NotImplemented);
                    return Py_NotImplemented;
                }
                if (mode == 2 && equality_relocations == 0) {
                    if (!relocate_set(&eq_set_root)) return NULL;
                    equality_relocations++;
                } else if (mode == 3 && update_mutations == 0) {
                    mutate_update_source();
                }
                Py_INCREF(Py_True);
                return Py_True;
            }

            static PyTypeObject ProbeKeyType = {
                PyVarObject_HEAD_INIT(NULL, 0)
                .tp_name = "pcc_probe.SetAddUpdateKey",
                .tp_basicsize = sizeof(ProbeKeyObject),
                .tp_flags = Py_TPFLAGS_DEFAULT,
                .tp_richcompare = probe_eq,
            };

            static PyObject *new_key(void) {
                return (PyObject *)PyObject_New(ProbeKeyObject, &ProbeKeyType);
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeKeyType) != 0) return 3;
                hash_class = py_class_new(
                    "SetAddHashMovingKey", NULL, 0, NULL, 0
                );
                if (hash_class == NULL) return 20;
                pcc_gc_pin((PyObject *)hash_class);
                py_class_add_method(
                    hash_class,
                    "__hash__",
                    (PyObject *)(uintptr_t)probe_hash
                );

                hash_set_root = py_set_new();
                PyObject *hash_key = py_instance_new(hash_class);
                if (hash_set_root == NULL || hash_key == NULL) return 4;
                void *hash_handle = pcc_gc_scheduler_root_register_handle(
                    &hash_set_root
                );
                if (hash_handle == NULL) return 5;
                mode = 1;
                py_set_add(hash_set_root, hash_key);
                hash_set_root = pcc_gc_load_ptr(NULL, &hash_set_root);
                mode = 0;
                if (py_set_len(hash_set_root) != 1) return 6;
                if (!py_set_contains(hash_set_root, hash_key)) return 7;

                eq_set_root = py_set_new();
                PyObject *eq_stored = new_key();
                PyObject *eq_query = new_key();
                if (eq_set_root == NULL || eq_stored == NULL
                    || eq_query == NULL) return 8;
                py_set_add(eq_set_root, eq_stored);
                void *eq_handle = pcc_gc_scheduler_root_register_handle(
                    &eq_set_root
                );
                if (eq_handle == NULL) return 9;
                mode = 2;
                py_set_add(eq_set_root, eq_query);
                eq_set_root = pcc_gc_load_ptr(NULL, &eq_set_root);
                mode = 0;
                if (py_set_len(eq_set_root) != 1) return 10;
                if (!py_set_contains(eq_set_root, eq_query)) return 11;

                update_dst_root = py_set_new();
                update_src_root = py_set_new();
                PyObject *update_stored = new_key();
                PyObject *update_key = new_key();
                if (update_dst_root == NULL || update_src_root == NULL
                    || update_stored == NULL || update_key == NULL) return 12;
                py_set_add(update_dst_root, update_stored);
                py_set_add(update_src_root, update_key);
                void *dst_handle = pcc_gc_scheduler_root_register_handle(
                    &update_dst_root
                );
                void *src_handle = pcc_gc_scheduler_root_register_handle(
                    &update_src_root
                );
                if (dst_handle == NULL || src_handle == NULL) return 13;
                mode = 3;
                py_set_update(update_dst_root, update_src_root);
                update_dst_root = pcc_gc_load_ptr(NULL, &update_dst_root);
                update_src_root = pcc_gc_load_ptr(NULL, &update_src_root);
                mode = 0;
                if (py_set_len(update_src_root) != 2) {
                    printf(
                        "update before=%lld after=%lld error=%lld count=%lld "
                        "src=%lld dst=%lld outer_error=%lld\n",
                        (long long)update_len_before,
                        (long long)update_len_after,
                        (long long)update_error_after,
                        (long long)update_mutations,
                        (long long)py_set_len(update_src_root),
                        (long long)py_set_len(update_dst_root),
                        (long long)py_err_occurred()
                    );
                    return 14;
                }
                if (py_set_len(update_dst_root) != 1) return 15;
                if (!py_set_contains(update_dst_root, update_key)) return 16;
                if (py_set_contains(
                        update_dst_root, py_int_from_i64(99)
                    )) return 17;
                if (hash_relocations != 1
                    || equality_relocations != 1
                    || update_mutations != 1) return 18;
                if (pcc_gc_scheduler_root_count() != 4) return 19;

                py_decref(hash_key);
                Py_DECREF(eq_stored);
                Py_DECREF(eq_query);
                Py_DECREF(update_stored);
                Py_DECREF(update_key);
                hash_set_root = pcc_gc_load_ptr(NULL, &hash_set_root);
                eq_set_root = pcc_gc_load_ptr(NULL, &eq_set_root);
                update_dst_root = pcc_gc_load_ptr(NULL, &update_dst_root);
                update_src_root = pcc_gc_load_ptr(NULL, &update_src_root);
                pcc_gc_scheduler_root_unregister_handle(hash_handle);
                pcc_gc_scheduler_root_unregister_handle(eq_handle);
                pcc_gc_scheduler_root_unregister_handle(dst_handle);
                pcc_gc_scheduler_root_unregister_handle(src_handle);
                py_decref(hash_set_root);
                py_decref(eq_set_root);
                py_decref(update_dst_root);
                py_decref(update_src_root);
                pcc_gc_unpin((PyObject *)hash_class);
                py_decref((PyObject *)hash_class);
                return 0;
            }
        ''',
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} set add/update callback commit returned {run.returncode}: "
        + run.stdout + run.stderr
    )


def test_strict_cext_managed_dealloc_flag_matches_public_bit62_abi():
    sources = [
        RUNTIME_DIR / "py" / "py_capi_cext_runtime.py",
        RUNTIME_DIR / "py" / "py_capi_contextvar_runtime.py",
        RUNTIME_DIR / "py" / "py_capi_seqiter_runtime.py",
        RUNTIME_DIR / "py" / "py_capi_slice_runtime.py",
    ]
    for path in sources:
        source = path.read_text(encoding="utf-8")
        assert "4611686018427387904" in source
        assert "0x1000000" not in source
    fake_python = (
        REPO_ROOT / "utils" / "fake_libc_include" / "Python.h"
    ).read_text(encoding="utf-8")
    assert "#define PCC_TPFLAGS_MANAGED_DEALLOC (1UL << 62)" in fake_python


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_set_remove_relocates_then_finalizer_observes_committed_absence(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_set_remove_split_commit",
        source_text=(
            "#define PCC_PROBE_STRICT "
            + ("1\n")
            + r'''
            #include "Python.h"
            #include <stdint.h>

            typedef struct ProbeKeyObject {
                PyObject_HEAD
            } ProbeKeyObject;

            static PyObject *set_root;
            static PyObject *query_key;
            static int64_t equality_count;
            static int64_t dealloc_count;
            static int64_t observe_dealloc;
            static int64_t observed_len = -1;
            static int64_t observed_contains = -1;

            extern int64_t pcc_gc_backend4_relocation_set_add(PyObject *obj);
            extern int64_t pcc_gc_backend4_remap_and_retire_stopped_world(void);
            extern int64_t pcc_capi_dealloc_cext_object(
                PyObject *obj, int64_t type_tag
            );

            static Py_hash_t probe_hash(PyObject *self) {
                (void)self;
                return 0;
            }

            static PyObject *probe_eq(
                PyObject *self,
                PyObject *other,
                int op
            ) {
                (void)self;
                (void)other;
                if (op != Py_EQ) {
                    Py_INCREF(Py_NotImplemented);
                    return Py_NotImplemented;
                }
                if (equality_count == 0) {
                    PyObject *set = pcc_gc_load_ptr(NULL, &set_root);
                    pcc_gc_reset_relocation_set();
                    if (pcc_gc_backend4_relocation_set_add(set) != 1) {
                        return NULL;
                    }
                    PyObject *target = pcc_gc_relocate_copy(set, 48);
                    if (target == NULL || target == set) return NULL;
                    py_decref(target);
                }
                equality_count++;
                Py_INCREF(Py_True);
                return Py_True;
            }

            static void probe_dealloc(PyObject *self) {
                if (observe_dealloc != 0) {
                    PyObject *set = pcc_gc_load_ptr(NULL, &set_root);
                    observed_len = py_set_len(set);
                    observed_contains = py_set_contains(set, query_key);
                }
                dealloc_count++;
                (void)self;
            }

            static PyTypeObject ProbeKeyType = {
                PyVarObject_HEAD_INIT(NULL, 0)
                .tp_name = "pcc_probe.SetRemoveKey",
                .tp_basicsize = sizeof(ProbeKeyObject),
                .tp_flags = Py_TPFLAGS_DEFAULT | PCC_TPFLAGS_MANAGED_DEALLOC,
                .tp_hash = probe_hash,
                .tp_richcompare = probe_eq,
                .tp_dealloc = probe_dealloc,
            };

            static PyObject *new_key(void) {
                return (PyObject *)PyObject_New(ProbeKeyObject, &ProbeKeyType);
            }

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                if (PyType_Ready(&ProbeKeyType) != 0) return 3;
                PyObject *spare = new_key();
                if (spare == NULL) return 15;
                if (pcc_capi_dealloc_cext_object(
                        spare, (int64_t)((PyObjectHeader *)spare)->type_tag
                    ) != 1) return 16;
                if (dealloc_count != 1) return 17;
                dealloc_count = 0;
                set_root = py_set_new();
                PyObject *stored = new_key();
                query_key = new_key();
                if (set_root == NULL || stored == NULL || query_key == NULL) {
                    return 4;
                }
                py_set_add(set_root, stored);
                Py_DECREF(stored);
                void *set_handle = pcc_gc_scheduler_root_register_handle(
                    &set_root
                );
                if (set_handle == NULL) return 5;
                observe_dealloc = 1;
                if (py_set_remove(set_root, query_key) != 0) return 6;
                set_root = pcc_gc_load_ptr(NULL, &set_root);
                if (py_set_len(set_root) != 0) return 7;
                if (py_set_contains(set_root, query_key) != 0) return 8;
                if (equality_count != 2) return 9;
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_remap_and_retire_stopped_world() <= 0) {
                    return 13;
                }
                if (pcc_gc_backend4_remap_and_retire_stopped_world() <= 0) {
                    return 14;
                }
#if PCC_PROBE_STRICT
                if (dealloc_count != 0) return 10;
                if (observed_len != -1 || observed_contains != -1) return 11;
#else
                if (dealloc_count != 1) return 10;
                if (observed_len != 0 || observed_contains != 0) return 11;
#endif
                if (pcc_gc_scheduler_root_count() != 1) return 12;
                Py_DECREF(query_key);
                set_root = pcc_gc_load_ptr(NULL, &set_root);
                pcc_gc_scheduler_root_unregister_handle(set_handle);
                py_decref(set_root);
                return 0;
            }
        '''),
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} set remove split commit returned {run.returncode}: "
        + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_list_growth_reaches_large_capacity_without_span_failure(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_list_large_capacity_growth",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *list = py_list_new(0);
                if (list == NULL) return 3;
                for (int64_t i = 0; i < 500; i++) {
                    py_list_append(list, py_int_from_i64(i));
                    if (py_err_occurred()) {
                        printf("failed_index=%lld\n", (long long)i);
                        return 4;
                    }
                }
                if (py_list_len(list) != 500) return 5;
                PyObject *last = py_list_get(list, 499);
                int overflow = 0;
                int64_t value = py_int_to_i64(last, &overflow);
                py_decref(last);
                if (overflow || value != 499) return 6;
                py_decref(list);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} Backend4 large list growth returned {run.returncode}: "
        + run.stdout + run.stderr
    )


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_backend4_capi_sequence_fast_items_pins_list_and_tuple_owners(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="backend4_capi_sequence_fast_items_pin",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            extern PyObject *PySequence_Fast(PyObject *, const char *);
            extern PyObject **PySequence_Fast_ITEMS(PyObject *);

            int main(void) {
                if (pcc_gc_set_backend(
                        PCC_GC_KIND_COLORED_RELOCATING
                    ) != 0) return 2;
                PyObject *list = py_list_new(0);
                PyObject *tuple = py_tuple_new(1);
                if (list == NULL || tuple == NULL) return 3;
                py_list_append(list, py_int_from_i64(11));
                py_tuple_set_item(tuple, 0, py_int_from_i64(12));
                PyObject *fast_list = PySequence_Fast(list, "expected list");
                PyObject *fast_tuple = PySequence_Fast(tuple, "expected tuple");
                if (fast_list != list || fast_tuple != tuple) return 4;
                PyObject **list_items = PySequence_Fast_ITEMS(fast_list);
                PyObject **tuple_items = PySequence_Fast_ITEMS(fast_tuple);
                if (list_items != ((PyListObject *)list)->items) return 5;
                if (tuple_items != ((PyTupleObject *)tuple)->items) return 6;
                if ((py_header(list)->flags & PY_FLAG_GC_PINNED) == 0) return 7;
                if ((py_header(tuple)->flags & PY_FLAG_GC_PINNED) == 0) return 8;
                pcc_gc_reset_relocation_set();
                if (pcc_gc_backend4_relocation_set_add(list) != 0) return 9;
                if (pcc_gc_backend4_relocation_set_add(tuple) != 0) return 10;
                if (list_items[0] != py_int_from_i64(11)) return 11;
                if (tuple_items[0] != py_int_from_i64(12)) return 12;
                py_decref(fast_tuple);
                py_decref(fast_list);
                py_decref(tuple);
                py_decref(list);
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} PySequence_Fast_ITEMS pin probe returned {run.returncode}: "
        + run.stdout + run.stderr
    )
