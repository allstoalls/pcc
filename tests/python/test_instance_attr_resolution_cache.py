"""Cached instance attribute outcomes must follow every later mutation.

`py_instance_getattr` records one proven outcome per (class, name): a field
index, a plain class function, a data descriptor, or the absence of the name
from the class; the absence of `__getattribute__` is recorded per class, and
`py_instance_setattr` stores straight into a field whose outcome is cached.
`py_obj_load_method` reuses a cached function to call it with the receiver
first instead of allocating a bound method.  Each check below repeats the
operation so the second and later calls run from the cache, then mutates the
class or the instance and checks that the next call sees the change -- in the
C runtime and in the pcc-Python runtime, under all five collectors.
"""
from __future__ import annotations

from pathlib import Path

from tests.python.test_class_lookup_cache_runtime import _compile_and_run


SOURCE = r'''
    #include "py_internal.h"
    #include <stdint.h>

    /* Each entry reports how many positional arguments it received, so a
     * receiver that was (or was not) prepended is visible in the result. */
    static PyObject *first_method(PyObject *captures, PyObject *args) {
        (void)captures;
        return py_int_from_i64(100 + py_tuple_len(args));
    }
    static PyObject *second_method(PyObject *captures, PyObject *args) {
        (void)captures;
        return py_int_from_i64(200 + py_tuple_len(args));
    }
    static PyObject *instance_callable(PyObject *captures, PyObject *args) {
        (void)captures;
        return py_int_from_i64(300 + py_tuple_len(args));
    }

    static int64_t call_m(PyObject *inst, int *unbound) {
        PyObject *self_obj = NULL;
        PyObject *method = py_obj_load_method(inst, "m", &self_obj);
        if (method == NULL) return -1;
        *unbound = self_obj != NULL;
        PyObject *args = py_tuple_new(1);
        py_tuple_set_item(args, 0, py_int_from_i64(5));
        PyObject *out = py_obj_call_method(method, self_obj, args);
        py_decref(args);
        py_decref(method);
        if (out == NULL) return -2;
        int overflow = 0;
        int64_t value = py_int_to_i64(out, &overflow);
        py_decref(out);
        return overflow ? -3 : value;
    }

    static int64_t field_x(PyObject *inst) {
        PyObject *value = py_instance_getattr((PyInstanceObject *)inst, "x");
        if (value == NULL) return -1;
        int overflow = 0;
        int64_t out = py_int_to_i64(value, &overflow);
        py_decref(value);
        return overflow ? -3 : out;
    }

    int main(void) {
        if (pcc_gc_set_backend(CACHE_BACKEND) != 0) return 1;
        const char *fields[1] = {"x"};
        PyClassObject *cls = py_class_new("Obj", NULL, 0, fields, 1);
        if (cls == NULL) return 2;
        PyObject *first = py_func_new((void *)first_method, py_tuple_new(0));
        if (first == NULL || py_class_setattr(cls, "m", first) != 0) return 3;
        PyObject *inst = py_instance_new(cls);
        if (inst == NULL) return 4;
        py_instance_set_field((PyInstanceObject *)inst, 0, py_int_from_i64(7));

        for (int i = 0; i < 3; i++) {
            if (field_x(inst) != 7) return 10;
        }
        py_instance_set_field((PyInstanceObject *)inst, 0, py_int_from_i64(8));
        if (field_x(inst) != 8) return 11;

        /* receiver + one argument, whether bound or called directly */
        int unbound = 0;
        for (int i = 0; i < 3; i++) {
            if (call_m(inst, &unbound) != 102) return 20;
        }
        /* After the first call the cached function is called directly on the
         * non-moving collectors; the forwarding ones keep the bound path. */
        if (unbound != (CACHE_BACKEND < 3)) return 21;

        PyObject *second = py_func_new((void *)second_method, py_tuple_new(0));
        if (second == NULL || py_class_setattr(cls, "m", second) != 0) return 30;
        for (int i = 0; i < 3; i++) {
            if (call_m(inst, &unbound) != 202) return 31;
        }

        /* An instance attribute shadows the class function and is called
         * with the argument alone. */
        PyObject *own = py_func_new((void *)instance_callable, py_tuple_new(0));
        if (own == NULL
            || py_instance_setattr((PyInstanceObject *)inst, "m", own) != 0) return 40;
        for (int i = 0; i < 3; i++) {
            if (call_m(inst, &unbound) != 301) return 41;
            if (unbound) return 42;
        }

        /* A fresh instance without the shadow still uses the class function. */
        PyObject *other = py_instance_new(cls);
        if (other == NULL) return 50;
        py_instance_set_field((PyInstanceObject *)other, 0, py_int_from_i64(9));
        for (int i = 0; i < 3; i++) {
            if (call_m(other, &unbound) != 202) return 51;
            if (field_x(other) != 9) return 52;
        }
        return 0;
    }
'''


def test_cached_attribute_outcomes_follow_mutation(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    for backend in range(5):
        backend_source = SOURCE.replace("CACHE_BACKEND", str(backend))
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"attr_resolution_backend{backend}_{runtime_name}",
                backend_source,
                archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: rc={result.returncode} "
                + result.stdout
                + result.stderr
            )


ABSENT_AND_DESCRIPTOR_SOURCE = r'''
    #include "py_internal.h"
    #include <stdint.h>

    static int64_t setter_calls = 0;

    static PyObject *getattr_hook(PyObject *captures, PyObject *args) {
        (void)captures;
        return py_int_from_i64(400 + py_tuple_len(args));
    }
    static PyObject *prop_get(PyObject *captures, PyObject *args) {
        (void)captures;
        return py_int_from_i64(500 + py_tuple_len(args));
    }
    static PyObject *prop_set(PyObject *captures, PyObject *args) {
        (void)captures;
        setter_calls += py_tuple_len(args);
        py_incref(py_None);
        return py_None;
    }

    /* The attribute as an int, -1 when absent (clearing any error). */
    static int64_t int_attr(PyObject *inst, const char *name) {
        PyObject *value = py_instance_getattr((PyInstanceObject *)inst, name);
        if (value == NULL) {
            if (py_err_occurred()) py_clear_exception();
            return -1;
        }
        int overflow = 0;
        int64_t out = py_int_to_i64(value, &overflow);
        py_decref(value);
        return overflow ? -3 : out;
    }

    static int64_t raw_field0(PyObject *inst) {
        PyObject *value = py_instance_get_field((PyInstanceObject *)inst, 0);
        if (value == NULL) return -1;
        int overflow = 0;
        int64_t out = py_int_to_i64(value, &overflow);
        py_decref(value);
        return overflow ? -3 : out;
    }

    int main(void) {
        if (pcc_gc_set_backend(CACHE_BACKEND) != 0) return 1;
        const char *fields[1] = {"x"};
        PyClassObject *cls = py_class_new("Node", NULL, 0, fields, 1);
        if (cls == NULL) return 2;
        PyObject *inst = py_instance_new(cls);
        PyObject *other = py_instance_new(cls);
        if (inst == NULL || other == NULL) return 3;
        py_instance_set_field((PyInstanceObject *)inst, 0, py_int_from_i64(7));
        py_instance_set_field((PyInstanceObject *)other, 0, py_int_from_i64(8));

        /* An absent name, answered from the cache after the first miss, must
         * see a later class attribute. */
        for (int i = 0; i < 3; i++) {
            if (int_attr(inst, "missing") != -1) return 10;
        }
        if (py_class_setattr(cls, "missing", py_int_from_i64(5)) != 0) return 11;
        if (int_attr(inst, "missing") != 5) return 12;

        /* ...and a later instance attribute, on that instance only. */
        for (int i = 0; i < 3; i++) {
            if (int_attr(inst, "gone") != -1) return 20;
        }
        if (py_instance_setattr((PyInstanceObject *)inst, "gone", py_int_from_i64(6)) != 0) return 21;
        for (int i = 0; i < 3; i++) {
            if (int_attr(inst, "gone") != 6) return 22;
            if (int_attr(other, "gone") != -1) return 23;
        }

        /* ...and a later __getattr__. */
        for (int i = 0; i < 3; i++) {
            if (int_attr(inst, "nothing") != -1) return 30;
        }
        PyObject *hook = py_func_new((void *)getattr_hook, py_tuple_new(0));
        if (hook == NULL) return 31;
        py_class_add_method(cls, "__getattr__", hook);
        if (int_attr(inst, "nothing") != 402) return 32;

        /* A cached property answers until the class attribute changes. */
        PyObject *getter = py_func_new((void *)prop_get, py_tuple_new(0));
        PyObject *prop = py_property_new(getter, NULL, NULL);
        if (prop == NULL || py_class_setattr(cls, "p", prop) != 0) return 40;
        for (int i = 0; i < 3; i++) {
            if (int_attr(inst, "p") != 501) return 41;
        }
        if (py_class_setattr(cls, "p", py_int_from_i64(9)) != 0) return 42;
        if (int_attr(inst, "p") != 9) return 43;

        /* A store to a field whose read outcome is cached goes to the slot,
         * until a data descriptor with a setter takes the name. */
        if (int_attr(inst, "x") != 7) return 50;
        for (int i = 0; i < 3; i++) {
            if (py_instance_setattr((PyInstanceObject *)inst, "x", py_int_from_i64(9 + i)) != 0) return 51;
            if (raw_field0(inst) != 9 + i || int_attr(inst, "x") != 9 + i) return 52;
        }
        PyObject *setter = py_func_new((void *)prop_set, py_tuple_new(0));
        PyObject *x_prop = py_property_new(getter, setter, NULL);
        if (x_prop == NULL || py_class_setattr(cls, "x", x_prop) != 0) return 53;
        if (py_instance_setattr((PyInstanceObject *)inst, "x", py_int_from_i64(12)) != 0) return 54;
        if (setter_calls != 2) return 55;
        if (raw_field0(inst) != 11) return 56;
        if (int_attr(inst, "x") != 501) return 57;
        return 0;
    }
'''


def test_absent_descriptor_and_field_store_outcomes_follow_mutation(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    for backend in range(5):
        backend_source = ABSENT_AND_DESCRIPTOR_SOURCE.replace(
            "CACHE_BACKEND", str(backend)
        )
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"attr_absent_backend{backend}_{runtime_name}",
                backend_source,
                archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: rc={result.returncode} "
                + result.stdout
                + result.stderr
            )
