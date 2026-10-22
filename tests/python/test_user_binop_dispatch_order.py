"""Binary and ordering operators must try user dunders in CPython's order.

``a - b`` calls ``type(a).__sub__(a, b)`` and, when that is missing or returns
NotImplemented, ``type(b).__rsub__(b, a)`` -- but only for a ``b`` of another
type, and before ``__sub__`` when ``type(b)`` is a subclass of ``type(a)``
that overrides ``__rsub__``.  ``py_user_binop_dispatch`` used to retry
``__rsub__`` for operands of the same type and never let an overriding
subclass go first.  Ordering compares reflect ``<`` to ``>`` for any two
operands, the rhs first when its class is a proper subclass; ``py_obj_lt``
and friends never asked a user dunder at all, so two instances always
compared equal.  Each method records ``method * 1000 + self.id * 10 +
other.id``, naming the method that answered and its receiver order.  Both
runtimes, all five collectors.
"""
from __future__ import annotations

from pathlib import Path

from tests.python.test_class_lookup_cache_runtime import _compile_and_run


SOURCE = r'''
    #include "py_internal.h"
    #include <stdint.h>

    enum { SUB_BASE = 1, RSUB_BASE = 2, RSUB_OTHER = 3, RSUB_SHY = 4 };
    static int64_t shy_calls = 0;

    static int64_t field_id(PyObject *inst) {
        PyObject *value = py_instance_get_field((PyInstanceObject *)inst, 0);
        int overflow = 0;
        int64_t out = py_int_to_i64(value, &overflow);
        py_decref(value);
        return out;
    }

    static PyObject *code(PyObject *args, int64_t method) {
        PyObject *self_obj = py_tuple_get(args, 0);
        PyObject *other = py_tuple_get(args, 1);
        int64_t value = method * 1000 + field_id(self_obj) * 10 + field_id(other);
        py_decref(self_obj);
        py_decref(other);
        return py_int_from_i64(value);
    }

    static PyObject *not_implemented(void) {
        py_incref(py_NotImplemented);
        return py_NotImplemented;
    }

    static PyObject *base_sub(PyObject *c, PyObject *a) { (void)c; return code(a, SUB_BASE); }
    static PyObject *base_rsub(PyObject *c, PyObject *a) { (void)c; return code(a, RSUB_BASE); }
    static PyObject *other_rsub(PyObject *c, PyObject *a) { (void)c; return code(a, RSUB_OTHER); }
    static PyObject *ptr_sub(PyObject *c, PyObject *a) { (void)c; (void)a; return not_implemented(); }
    static PyObject *shy_rsub(PyObject *c, PyObject *a) {
        (void)c; (void)a;
        shy_calls++;
        return not_implemented();
    }

    static const char *FIELDS[1] = {"id"};

    static PyClassObject *make_class(const char *name, PyClassObject *base) {
        PyClassObject *bases[1] = {base};
        return py_class_new(name, base ? bases : NULL, base ? 1 : 0, FIELDS, 1);
    }

    static void add(PyClassObject *cls, const char *name, void *entry) {
        py_class_add_method(cls, name, py_func_new(entry, py_tuple_new(0)));
    }

    static PyObject *make(PyClassObject *cls, int64_t id) {
        PyObject *inst = py_instance_new(cls);
        py_instance_set_field((PyInstanceObject *)inst, 0, py_int_from_i64(id));
        return inst;
    }

    static int64_t sub(PyObject *a, PyObject *b) {
        PyObject *out = py_user_binop_dispatch(a, b, "__sub__", "__rsub__", "bad -");
        if (out == NULL) {
            int raised = py_err_occurred() != 0;
            py_clear_exception();
            return raised ? -1 : -2;
        }
        int overflow = 0;
        int64_t value = py_int_to_i64(out, &overflow);
        py_decref(out);
        return value;
    }

    int main(void) {
        if (pcc_gc_set_backend(DISPATCH_BACKEND) != 0) return 1;
        PyClassObject *base = make_class("Base", NULL);
        add(base, "__sub__", (void *)base_sub);
        add(base, "__rsub__", (void *)base_rsub);
        PyClassObject *ptr = make_class("Ptr", base);
        add(ptr, "__sub__", (void *)ptr_sub);
        PyClassObject *other = make_class("Other", base);
        add(other, "__rsub__", (void *)other_rsub);
        PyClassObject *leaf = make_class("Leaf", base);
        PyClassObject *shy = make_class("Shy", base);
        add(shy, "__rsub__", (void *)shy_rsub);
        if (!base || !ptr || !other || !leaf || !shy) return 2;

        for (int round = 0; round < 3; round++) {
            /* same type: __sub__ is NotImplemented and __rsub__ is not tried */
            if (sub(make(ptr, 1), make(ptr, 2)) != -1) return 10;
            /* Other is not a Ptr subclass: Ptr.__sub__, then Other.__rsub__ */
            if (sub(make(ptr, 1), make(other, 2)) != RSUB_OTHER * 1000 + 21) return 11;
            /* Other subclasses Base and overrides __rsub__: it goes first */
            if (sub(make(base, 1), make(other, 2)) != RSUB_OTHER * 1000 + 21) return 12;
            /* Leaf inherits Base.__rsub__ unchanged: Base.__sub__ answers */
            if (sub(make(base, 1), make(leaf, 2)) != SUB_BASE * 1000 + 12) return 13;
            if (sub(make(base, 1), make(base, 2)) != SUB_BASE * 1000 + 12) return 14;
            /* the reflected-first attempt is not repeated after __sub__ */
            shy_calls = 0;
            if (sub(make(base, 1), make(shy, 2)) != SUB_BASE * 1000 + 12) return 15;
            if (shy_calls != 1) return 16;
            /* Base is no subclass of Other: Base.__sub__ first */
            if (sub(make(other, 1), make(base, 2)) != SUB_BASE * 1000 + 12) return 17;
        }
        return 0;
    }
'''


ORDER_SOURCE = r'''
    #include "py_internal.h"
    #include <stdint.h>

    enum { LT_BASE = 1, LT_PTR = 2, GT_OTHER = 3, LT_SHY = 4 };
    static int64_t last = 0;

    static int64_t field_id(PyObject *inst) {
        if (PY_IS_TAGGED_INT(inst)) return 0;
        PyObject *value = py_instance_get_field((PyInstanceObject *)inst, 0);
        int overflow = 0;
        int64_t out = py_int_to_i64(value, &overflow);
        py_decref(value);
        return out;
    }

    static void record(PyObject *args, int64_t method) {
        PyObject *self_obj = py_tuple_get(args, 0);
        PyObject *other = py_tuple_get(args, 1);
        last = method * 1000 + field_id(self_obj) * 10 + field_id(other);
        py_decref(self_obj);
        py_decref(other);
    }

    static PyObject *answer(PyObject *args, int64_t method, PyObject *value) {
        record(args, method);
        py_incref(value);
        return value;
    }

    static PyObject *base_lt(PyObject *c, PyObject *a) { (void)c; return answer(a, LT_BASE, py_True); }
    static PyObject *ptr_lt(PyObject *c, PyObject *a) { (void)c; return answer(a, LT_PTR, py_False); }
    static PyObject *other_gt(PyObject *c, PyObject *a) { (void)c; return answer(a, GT_OTHER, py_True); }
    static PyObject *shy_lt(PyObject *c, PyObject *a) {
        (void)c;
        return answer(a, LT_SHY, py_NotImplemented);
    }

    static const char *FIELDS[1] = {"id"};

    static PyClassObject *make_class(const char *name, PyClassObject *base) {
        PyClassObject *bases[1] = {base};
        return py_class_new(name, base ? bases : NULL, base ? 1 : 0, FIELDS, 1);
    }

    static void add(PyClassObject *cls, const char *name, void *entry) {
        py_class_add_method(cls, name, py_func_new(entry, py_tuple_new(0)));
    }

    static PyObject *make(PyClassObject *cls, int64_t id) {
        PyObject *inst = py_instance_new(cls);
        py_instance_set_field((PyInstanceObject *)inst, 0, py_int_from_i64(id));
        return inst;
    }

    int main(void) {
        if (pcc_gc_set_backend(DISPATCH_BACKEND) != 0) return 1;
        PyClassObject *base = make_class("Base", NULL);
        add(base, "__lt__", (void *)base_lt);
        PyClassObject *ptr = make_class("Ptr", base);
        add(ptr, "__lt__", (void *)ptr_lt);
        PyClassObject *other = make_class("Other", base);
        add(other, "__gt__", (void *)other_gt);
        PyClassObject *shy = make_class("Shy", NULL);
        add(shy, "__lt__", (void *)shy_lt);
        PyClassObject *plain = make_class("Plain", NULL);
        if (!base || !ptr || !other || !shy || !plain) return 2;

        for (int round = 0; round < 3; round++) {
            /* the receiver's own override answers */
            last = 0;
            if (py_obj_lt(make(ptr, 1), make(ptr, 2)) != 0) return 10;
            if (last != LT_PTR * 1000 + 12) return 11;
            /* Other subclasses Base: its reflected __gt__ goes first */
            last = 0;
            if (py_obj_lt(make(base, 1), make(other, 2)) != 1) return 12;
            if (last != GT_OTHER * 1000 + 21) return 13;
            /* Base defines no __gt__: the reflected __lt__ answers, even for
             * operands of one type */
            last = 0;
            if (py_obj_gt(make(base, 1), make(base, 2)) != 1) return 14;
            if (last != LT_BASE * 1000 + 21) return 15;
            /* NotImplemented from every method, or no ordering method at all:
             * as in CPython, the comparison raises TypeError */
            last = 0;
            (void)py_obj_lt(make(shy, 1), make(shy, 2));
            if (last != LT_SHY * 1000 + 12) return 17;
            if (py_err_occurred() == 0) return 18;
            if (!py_exc_matches(py_current_exception(),
                                (PyObject *)py_exc_builtin_class(PY_EXC_TYPEERROR))) return 19;
            py_clear_exception();
            (void)py_obj_le(make(plain, 1), make(plain, 2));
            if (py_err_occurred() == 0) return 20;
            if (!py_exc_matches(py_current_exception(),
                                (PyObject *)py_exc_builtin_class(PY_EXC_TYPEERROR))) return 23;
            py_clear_exception();
            /* an int on the left asks the instance's reflected method */
            last = 0;
            if (py_obj_lt(py_int_from_i64(5), make(other, 3)) != 1) return 21;
            if (last != GT_OTHER * 1000 + 30 + 0) return 22;
        }
        return 0;
    }
'''


def test_binop_dispatch_follows_cpython_reflected_order(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    for backend in range(5):
        backend_source = SOURCE.replace("DISPATCH_BACKEND", str(backend))
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"binop_order_backend{backend}_{runtime_name}",
                backend_source,
                archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: rc={result.returncode} "
                + result.stdout
                + result.stderr
            )


def test_ordering_compares_ask_user_dunders_in_cpython_order(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    for backend in range(5):
        backend_source = ORDER_SOURCE.replace("DISPATCH_BACKEND", str(backend))
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"order_backend{backend}_{runtime_name}",
                backend_source,
                archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: rc={result.returncode} "
                + result.stdout
                + result.stderr
            )
