/* pcc/py_runtime/src/py_obj_ops_dispatch.c
 *
 * Type-tag dispatch for the simpler generic ops: truthy / len /
 * subscript (getitem / setitem / delitem / slice) / attribute
 * (getattr / setattr) / call / isinstance.
 *
 * Split out of py_obj_ops.c so this half can be replaced by
 * py_obj_ops_dispatch.py while the compare/hash/sorted half
 * (py_obj_ops_compare.c) stays C.
 */

#include "py_internal.h"
#include <stdlib.h>
#include <string.h>

static int dispatch_ptr_can_have_header(void *ptr) {
    return pcc_gc_pointer_is_managed((PyObject *)ptr) != 0;
}

static int dispatch_is_heap_obj(PyObject *o) {
    return o != NULL && !PY_IS_TAGGED_INT(o) && dispatch_ptr_can_have_header(o);
}

static PyObject *dispatch_call_method_with_args(
    PyObject *method,
    PyObject *self,
    PyObject *args,
    PyObject *kwargs
) {
    if (method == NULL) {
        return py_runtime_error_if_unset(
            "dispatch_call_method_with_args",
            "dispatch_call_method_with_args received NULL method"
        );
    }
    int64_t n = args == NULL ? 0 : py_tuple_len(args);
    if (
        dispatch_is_heap_obj(method)
        && py_type_of(method) == PY_TYPE_FUNC
    ) {
        PyObject *full_args = py_tuple_new(n + 1);
        if (full_args == NULL) {
            return py_runtime_error_if_unset(
                "py_tuple_new",
                "bound method call could not allocate its argument tuple"
            );
        }
        py_tuple_set_item(full_args, 0, self);
        for (int64_t i = 0; i < n; i++) {
            PyObject *item = py_tuple_get(args, i);
            py_tuple_set_item(full_args, i + 1, item);
            py_decref(item);
        }
        PyObject *out = py_func_call_kwargs(method, full_args, kwargs);
        if (out == NULL) {
            py_runtime_error_if_unset(
                "py_func_call_kwargs",
                "bound function call returned NULL without setting an exception"
            );
        }
        py_decref(full_args);
        return out;
    }
    if (n == 0) {
        typedef PyObject *(*M0)(PyObject *);
        PyObject *out = ((M0)(uintptr_t)method)(self);
        if (out == NULL) {
            py_runtime_error_if_unset(
                "bound native method",
                "bound native method returned NULL without setting an exception"
            );
        }
        return out;
    }
    if (n == 1) {
        PyObject *a0 = py_tuple_get(args, 0);
        typedef PyObject *(*M1)(PyObject *, PyObject *);
        PyObject *out = ((M1)(uintptr_t)method)(self, a0);
        if (out == NULL) {
            py_runtime_error_if_unset(
                "bound native method",
                "bound native method returned NULL without setting an exception"
            );
        }
        py_decref(a0);
        return out;
    }
    if (n == 2) {
        PyObject *a0 = py_tuple_get(args, 0);
        PyObject *a1 = py_tuple_get(args, 1);
        typedef PyObject *(*M2)(PyObject *, PyObject *, PyObject *);
        PyObject *out = ((M2)(uintptr_t)method)(self, a0, a1);
        if (out == NULL) {
            py_runtime_error_if_unset(
                "bound native method",
                "bound native method returned NULL without setting an exception"
            );
        }
        py_decref(a0);
        py_decref(a1);
        return out;
    }
    if (n == 3) {
        PyObject *a0 = py_tuple_get(args, 0);
        PyObject *a1 = py_tuple_get(args, 1);
        PyObject *a2 = py_tuple_get(args, 2);
        typedef PyObject *(*M3)(PyObject *, PyObject *, PyObject *, PyObject *);
        PyObject *out = ((M3)(uintptr_t)method)(self, a0, a1, a2);
        if (out == NULL) {
            py_runtime_error_if_unset(
                "bound native method",
                "bound native method returned NULL without setting an exception"
            );
        }
        py_decref(a0);
        py_decref(a1);
        py_decref(a2);
        return out;
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "too many native method args"));
    return NULL;
}

int64_t py_obj_truthy(PyObject *o) {
    if (o == NULL) return 0;
    if (o == py_None || o == py_False) return 0;
    if (o == py_True) return 1;
    if (PY_IS_TAGGED_INT(o)) return py_untag_int(o) != 0;
    int32_t tag = py_header(o)->type_tag;
    if (pcc_capi_is_cext_type_tag(tag)) {
        int64_t truth = pcc_capi_cext_truthy(o);
        return truth > 0 ? 1 : 0;
    }
    switch (tag) {
        case PY_TYPE_INT:
            return py_int_value_i64(o) != 0;
        case PY_TYPE_FLOAT: return ((PyFloatObject *)o)->value != 0.0;
        case PY_TYPE_LIST:  return ((PyListObject *)o)->length != 0;
        case PY_TYPE_TUPLE: return ((PyTupleObject *)o)->len != 0;
        case PY_TYPE_STR:   return ((PyStrObject *)o)->byte_len != 0;
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_MEMORYVIEW:
            return py_bytes_len(o) != 0;
        case PY_TYPE_DICT:  return ((PyDictObject *)o)->size != 0;
        case PY_TYPE_SET:   return ((PySetObject *)o)->size != 0;
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                int64_t handled = 0;
                int64_t user_bool = py_user_bool_dispatch(o, &handled);
                if (handled) return user_bool ? 1 : 0;
                int64_t user_len = py_user_len_dispatch(o, &handled);
                if (handled) return user_len != 0 ? 1 : 0;
            }
            return 1;
    }
}

int64_t py_obj_type_tag(PyObject *o) {
    if (o == NULL) return -1;
    return py_type_of(o);
}

PyObject *py_obj_add(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for +"));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    /* A C-extension operand owns numeric dispatch even when the other side is
     * a native float/complex/int.  Checking the builtin fast paths first made
     * ``ndarray + 0.0`` call py_float_add instead of ndarray.nb_add, returning
     * NULL without ever invoking the extension slot. */
    if (pcc_capi_is_cext_type_tag(at) || pcc_capi_is_cext_type_tag(bt)) {
        return pcc_capi_cext_binary_number(a, b, 0);
    }
    if (
        (at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)
    ) {
        return py_int_add(a, b);
    }
    if (at == PY_TYPE_COMPLEX || bt == PY_TYPE_COMPLEX) {
        return py_complex_add(a, b);
    }
    if (at == PY_TYPE_FLOAT || bt == PY_TYPE_FLOAT) {
        return py_float_add(a, b);
    }
    if (at == PY_TYPE_STR && bt == PY_TYPE_STR) {
        return py_str_concat(a, b);
    }
    if (
        (at == PY_TYPE_BYTES || at == PY_TYPE_BYTEARRAY)
        && (bt == PY_TYPE_BYTES || bt == PY_TYPE_BYTEARRAY)
    ) {
        return py_bytes_concat(a, b);
    }
    if (at == PY_TYPE_LIST && bt == PY_TYPE_LIST) {
        return py_list_concat(a, b);
    }
    if (at == PY_TYPE_TUPLE && bt == PY_TYPE_TUPLE) {
        return py_tuple_concat(a, b);
    }
    if (at == PY_TYPE_INSTANCE || at >= PY_TYPE_USER_CLASS_START
        || bt == PY_TYPE_INSTANCE || bt >= PY_TYPE_USER_CLASS_START) {
        return py_user_binop_dispatch(
            a, b, "__add__", "__radd__",
            "unsupported operand type(s) for +");
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for +"));
    return NULL;
}

PyObject *py_obj_sub(PyObject *a, PyObject *b) {
    /* Generic a - b (mirrors py_obj_add). int/bool -> py_int_sub; any float ->
     * py_float_sub. Subtraction is numeric-only in Python. */
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for -"));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    if ((at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)) {
        return py_int_sub(a, b);
    }
    if ((at == PY_TYPE_FLOAT || at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_FLOAT || bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)) {
        return py_float_sub(a, b);
    }
    if (pcc_capi_is_cext_type_tag(at) || pcc_capi_is_cext_type_tag(bt)) {
        return pcc_capi_cext_subtract(a, b);
    }
    if (at == PY_TYPE_INSTANCE || at >= PY_TYPE_USER_CLASS_START
        || bt == PY_TYPE_INSTANCE || bt >= PY_TYPE_USER_CLASS_START) {
        return py_user_binop_dispatch(
            a, b, "__sub__", "__rsub__",
            "unsupported operand type(s) for -");
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for -"));
    return NULL;
}

PyObject *py_obj_mul(PyObject *a, PyObject *b) {
    /* Generic a * b (mirrors py_obj_add). int/bool -> py_int_mul; any-float
     * numeric -> py_float_mul; sequence * int -> repetition. */
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for *"));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    if ((at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)) {
        return py_int_mul(a, b);
    }
    if ((at == PY_TYPE_FLOAT || at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_FLOAT || bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)) {
        return py_float_mul(a, b);
    }
    if (at == PY_TYPE_STR && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)) {
        return py_str_repeat(a, b);
    }
    if (bt == PY_TYPE_STR && (at == PY_TYPE_INT || at == PY_TYPE_BOOL)) {
        return py_str_repeat(b, a);
    }
    if (at == PY_TYPE_LIST && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)) {
        return py_list_repeat(a, py_int_value_i64(b));
    }
    if (bt == PY_TYPE_LIST && (at == PY_TYPE_INT || at == PY_TYPE_BOOL)) {
        return py_list_repeat(b, py_int_value_i64(a));
    }
    if (at == PY_TYPE_TUPLE && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)) {
        return py_tuple_repeat(a, py_int_value_i64(b));
    }
    if (bt == PY_TYPE_TUPLE && (at == PY_TYPE_INT || at == PY_TYPE_BOOL)) {
        return py_tuple_repeat(b, py_int_value_i64(a));
    }
    if (pcc_capi_is_cext_type_tag(at) || pcc_capi_is_cext_type_tag(bt)) {
        return pcc_capi_cext_binary_number(a, b, 2);
    }
    if (at == PY_TYPE_INSTANCE || at >= PY_TYPE_USER_CLASS_START
        || bt == PY_TYPE_INSTANCE || bt >= PY_TYPE_USER_CLASS_START) {
        return py_user_binop_dispatch(
            a, b, "__mul__", "__rmul__",
            "unsupported operand type(s) for *");
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for *"));
    return NULL;
}

static PyObject *py_obj_bitwise_dispatch(
    PyObject *a,
    PyObject *b,
    int64_t op
) {
    const char *name = op == 0 ? "__and__" : (op == 1 ? "__or__" : "__xor__");
    const char *rname = op == 0 ? "__rand__" : (op == 1 ? "__ror__" : "__rxor__");
    const char *message = op == 0
        ? "unsupported operand type(s) for &"
        : (op == 1
            ? "unsupported operand type(s) for |"
            : "unsupported operand type(s) for ^");
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, message));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    if (pcc_capi_is_cext_type_tag(at) || pcc_capi_is_cext_type_tag(bt)) {
        int64_t cext_op = op == 0 ? 8 : (op == 1 ? 10 : 9);
        return pcc_capi_cext_binary_number(a, b, cext_op);
    }
    if (
        (at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)
    ) {
        if (op == 0) return py_int_and(a, b);
        if (op == 1) return py_int_or(a, b);
        return py_int_xor(a, b);
    }
    if (at == PY_TYPE_SET && bt == PY_TYPE_SET) {
        if (op == 0) return py_set_intersection(a, b);
        if (op == 2) return py_set_symmetric_difference(a, b);
        PyObject *out = py_set_new();
        if (out == NULL) return NULL;
        py_set_update(out, a);
        if (py_err_occurred()) {
            py_decref(out);
            return NULL;
        }
        py_set_update(out, b);
        if (py_err_occurred()) {
            py_decref(out);
            return NULL;
        }
        return out;
    }
    if (op == 1 && at == PY_TYPE_DICT && bt == PY_TYPE_DICT) {
        PyObject *out = py_dict_new();
        if (out == NULL) return NULL;
        py_dict_update(out, a);
        if (py_err_occurred()) {
            py_decref(out);
            return NULL;
        }
        py_dict_update(out, b);
        if (py_err_occurred()) {
            py_decref(out);
            return NULL;
        }
        return out;
    }
    if (at == PY_TYPE_INSTANCE || at >= PY_TYPE_USER_CLASS_START
        || bt == PY_TYPE_INSTANCE || bt >= PY_TYPE_USER_CLASS_START) {
        return py_user_binop_dispatch(a, b, name, rname, message);
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, message));
    return NULL;
}

PyObject *py_obj_and(PyObject *a, PyObject *b) {
    return py_obj_bitwise_dispatch(a, b, 0);
}

PyObject *py_obj_or(PyObject *a, PyObject *b) {
    return py_obj_bitwise_dispatch(a, b, 1);
}

PyObject *py_obj_xor(PyObject *a, PyObject *b) {
    return py_obj_bitwise_dispatch(a, b, 2);
}

static PyObject *py_obj_shift_dispatch(
    PyObject *a,
    PyObject *b,
    int64_t op
) {
    const char *name = op == 0 ? "__lshift__" : "__rshift__";
    const char *rname = op == 0 ? "__rlshift__" : "__rrshift__";
    const char *message = op == 0
        ? "unsupported operand type(s) for <<"
        : "unsupported operand type(s) for >>";
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, message));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    if (pcc_capi_is_cext_type_tag(at) || pcc_capi_is_cext_type_tag(bt)) {
        return pcc_capi_cext_binary_number(a, b, op == 0 ? 6 : 7);
    }
    if (
        (at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)
    ) {
        return op == 0 ? py_int_shl(a, b) : py_int_shr(a, b);
    }
    if (at == PY_TYPE_INSTANCE || at >= PY_TYPE_USER_CLASS_START
        || bt == PY_TYPE_INSTANCE || bt >= PY_TYPE_USER_CLASS_START) {
        return py_user_binop_dispatch(a, b, name, rname, message);
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, message));
    return NULL;
}

PyObject *py_obj_lshift(PyObject *a, PyObject *b) {
    return py_obj_shift_dispatch(a, b, 0);
}

PyObject *py_obj_rshift(PyObject *a, PyObject *b) {
    return py_obj_shift_dispatch(a, b, 1);
}

PyObject *py_obj_mod(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for %"));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    if (at == PY_TYPE_STR) {
        return py_str_mod(a, b);
    }
    if (at == PY_TYPE_BYTES || at == PY_TYPE_BYTEARRAY) {
        return py_bytes_mod(a, b);
    }
    if (
        (at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)
    ) {
        return py_int_mod(a, b);
    }
    if (at == PY_TYPE_INSTANCE || at >= PY_TYPE_USER_CLASS_START
        || bt == PY_TYPE_INSTANCE || bt >= PY_TYPE_USER_CLASS_START) {
        return py_user_binop_dispatch(
            a, b, "__mod__", "__rmod__",
            "unsupported operand type(s) for %");
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for %"));
    return NULL;
}

/* Python true division (``a / b``) for dynamically-typed operands.
 *
 * CPython's ``/`` always yields a float for numeric operands, so a DynType
 * operand (e.g. ``obj.attr / n`` where ``obj.attr`` was inferred as a boxed
 * object) cannot use the static int/float fast path. The frontend used to
 * route this to the ``__truediv__`` dunder, but a tagged int has no
 * ``__truediv__`` attribute (py_obj_getattr returns missing for tagged ints),
 * so ``obj.attr / 3`` raised ``AttributeError: __truediv__``. This generic
 * dispatcher mirrors py_obj_add/py_obj_mod: numeric operands divide as
 * doubles; everything else defers to the ``__truediv__`` dunder (real user
 * classes that define it). */
PyObject *py_obj_truediv(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for /"));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    int a_num = (at == PY_TYPE_INT || at == PY_TYPE_BOOL || at == PY_TYPE_FLOAT);
    int b_num = (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL || bt == PY_TYPE_FLOAT);
    if (a_num && b_num) {
        double bd = py_float_to_f64(b);
        if (bd == 0.0) {
            py_raise_owned(py_exc_new(PY_EXC_ZERODIVISIONERROR, "division by zero"));
            return NULL;
        }
        return py_float_from_f64(py_float_to_f64(a) / bd);
    }
    if (pcc_capi_is_cext_type_tag(at) || pcc_capi_is_cext_type_tag(bt)) {
        return pcc_capi_cext_binary_number(a, b, 3);
    }
    /* Non-numeric: full dunder protocol (__truediv__, NotImplemented,
     * reflected __rtruediv__) — the old call_method1 defer only tried
     * the LHS, so ``1 / R()`` raised AttributeError instead of using
     * R.__rtruediv__. */
    return py_user_binop_dispatch(
        a, b, "__truediv__", "__rtruediv__",
        "unsupported operand type(s) for /");
}

PyObject *py_obj_type_name(PyObject *o) {
    if (o == NULL) return py_str_new("NoneType", 8);
    int32_t tag = py_type_of(o);
    const char *name = NULL;
    switch (tag) {
        case PY_TYPE_NONE: name = "NoneType"; break;
        case PY_TYPE_BOOL: name = "bool"; break;
        case PY_TYPE_INT: name = "int"; break;
        case PY_TYPE_FLOAT: name = "float"; break;
        case PY_TYPE_STR: name = "str"; break;
        case PY_TYPE_LIST: name = "list"; break;
        case PY_TYPE_DICT: name = "dict"; break;
        case PY_TYPE_TUPLE: name = "tuple"; break;
        case PY_TYPE_SET: name = "set"; break;
        case PY_TYPE_CLASS: name = "type"; break;
        case PY_TYPE_COMPLEX: name = "complex"; break;
        case PY_TYPE_BYTES: name = "bytes"; break;
        case PY_TYPE_BYTEARRAY: name = "bytearray"; break;
        case PY_TYPE_MEMORYVIEW: name = "memoryview"; break;
        case PY_TYPE_COROUTINE: name = "coroutine"; break;
        case PY_TYPE_CONTINUATION: name = "continuation"; break;
        case PY_TYPE_VIRTUAL_THREAD: name = "virtual_thread"; break;
        case PY_TYPE_VTHREAD_CHANNEL: name = "vthread_channel"; break;
        case PY_TYPE_EXC: {
            PyExceptionObject *exc = (PyExceptionObject *)o;
            PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
                o,
                (PyObject **)&exc->exc_class
            );
            if (cls && cls->name) {
                name = cls->name;
            }
            break;
        }
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                PyInstanceObject *inst = (PyInstanceObject *)o;
                PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
                    o,
                    (PyObject **)&inst->cls
                );
                if (cls && cls->name) name = cls->name;
            }
            break;
    }
    if (name == NULL) name = "object";
    return py_str_new(name, (int64_t)strlen(name));
}

static PyClassObject *pcc_type_cls_none = NULL;
static PyClassObject *pcc_type_cls_bool = NULL;
static PyClassObject *pcc_type_cls_int = NULL;
static PyClassObject *pcc_type_cls_float = NULL;
static PyClassObject *pcc_type_cls_str = NULL;
static PyClassObject *pcc_type_cls_list = NULL;
static PyClassObject *pcc_type_cls_dict = NULL;
static PyClassObject *pcc_type_cls_tuple = NULL;
static PyClassObject *pcc_type_cls_set = NULL;
static PyClassObject *pcc_type_cls_type = NULL;
static PyClassObject *pcc_type_cls_complex = NULL;
static PyClassObject *pcc_type_cls_bytes = NULL;
static PyClassObject *pcc_type_cls_bytearray = NULL;
static PyClassObject *pcc_type_cls_memoryview = NULL;
static PyClassObject *pcc_type_cls_coroutine = NULL;
static PyClassObject *pcc_type_cls_object = NULL;
static PyClassObject *pcc_type_cls_super = NULL;
static PyClassObject *pcc_slice_cls = NULL;

void **pcc_builtin_type_root_slots[] = {
    (void **)&pcc_type_cls_none,
    (void **)&pcc_type_cls_bool,
    (void **)&pcc_type_cls_int,
    (void **)&pcc_type_cls_float,
    (void **)&pcc_type_cls_str,
    (void **)&pcc_type_cls_list,
    (void **)&pcc_type_cls_dict,
    (void **)&pcc_type_cls_tuple,
    (void **)&pcc_type_cls_set,
    (void **)&pcc_type_cls_type,
    (void **)&pcc_type_cls_complex,
    (void **)&pcc_type_cls_bytes,
    (void **)&pcc_type_cls_bytearray,
    (void **)&pcc_type_cls_memoryview,
    (void **)&pcc_type_cls_coroutine,
    (void **)&pcc_type_cls_object,
    (void **)&pcc_type_cls_super,
    (void **)&pcc_slice_cls,
    NULL,
};

static PyObject *pcc_builtin_type_class(
    const char *name,
    PyClassObject **slot
) {
    if (slot == NULL) return NULL;
    if (*slot == NULL) {
        *slot = py_class_new(name, NULL, 0, NULL, 0);
    }
    if (*slot == NULL) return NULL;
    py_incref((PyObject *)*slot);
    return (PyObject *)*slot;
}

PyObject *py_slice_new(PyObject *start, PyObject *stop, PyObject *step) {
    if (start == NULL) start = py_None;
    if (stop == NULL) stop = py_None;
    if (step == NULL) step = py_None;
    if (pcc_slice_cls == NULL) {
        pcc_slice_cls = py_class_new("slice", NULL, 0, NULL, 0);
        if (pcc_slice_cls == NULL) return NULL;
    }
    PyObject *inst = py_instance_new(pcc_slice_cls);
    if (inst == NULL) return NULL;
    py_instance_setattr((PyInstanceObject *)inst, "start", start);
    py_instance_setattr((PyInstanceObject *)inst, "stop", stop);
    py_instance_setattr((PyInstanceObject *)inst, "step", step);
    return inst;
}

/* isinstance(x, slice): a slice is an instance of the lazily-created
 * pcc_slice_cls. Returns 0 when no slice has been created yet. */
int64_t py_obj_is_slice(PyObject *o) {
    if (o == NULL || pcc_slice_cls == NULL) return 0;
    /* py_isinstance handles the tagged-int/header/instance-tag checks and the
     * MRO walk; an instance may carry a per-class tag at or above
     * PY_TYPE_USER_CLASS_START, so do
     * NOT pre-filter on PY_TYPE_INSTANCE here. */
    return py_isinstance(o, pcc_slice_cls) ? 1 : 0;
}

static PyObject *pcc_builtin_type_class_for_tag(int32_t tag) {
    switch (tag) {
        /* Synthetic tag for the first-class ``super`` type object.  It is
         * deliberately outside PyTypeTag: this token describes a built-in
         * class value, not an object-header representation. */
        case -3:
            return pcc_builtin_type_class("super", &pcc_type_cls_super);
        case PY_TYPE_NONE:
            return pcc_builtin_type_class("NoneType", &pcc_type_cls_none);
        case PY_TYPE_BOOL:
            return pcc_builtin_type_class("bool", &pcc_type_cls_bool);
        case PY_TYPE_INT:
            return pcc_builtin_type_class("int", &pcc_type_cls_int);
        case PY_TYPE_FLOAT:
            return pcc_builtin_type_class("float", &pcc_type_cls_float);
        case PY_TYPE_STR:
            return pcc_builtin_type_class("str", &pcc_type_cls_str);
        case PY_TYPE_LIST:
            return pcc_builtin_type_class("list", &pcc_type_cls_list);
        case PY_TYPE_DICT:
            return pcc_builtin_type_class("dict", &pcc_type_cls_dict);
        case PY_TYPE_TUPLE:
            return pcc_builtin_type_class("tuple", &pcc_type_cls_tuple);
        case PY_TYPE_SET:
            return pcc_builtin_type_class("set", &pcc_type_cls_set);
        case PY_TYPE_CLASS:
            return pcc_builtin_type_class("type", &pcc_type_cls_type);
        case PY_TYPE_COMPLEX:
            return pcc_builtin_type_class("complex", &pcc_type_cls_complex);
        case PY_TYPE_BYTES:
            return pcc_builtin_type_class("bytes", &pcc_type_cls_bytes);
        case PY_TYPE_BYTEARRAY:
            return pcc_builtin_type_class("bytearray", &pcc_type_cls_bytearray);
        case PY_TYPE_MEMORYVIEW:
            return pcc_builtin_type_class("memoryview", &pcc_type_cls_memoryview);
        case PY_TYPE_COROUTINE:
            return pcc_builtin_type_class("coroutine", &pcc_type_cls_coroutine);
        default:
            return pcc_builtin_type_class("object", &pcc_type_cls_object);
    }
}

PyObject *py_builtin_type_for_tag(int64_t tag) {
    return pcc_builtin_type_class_for_tag((int32_t)tag);
}

int64_t py_builtin_type_class_tag(PyObject *value) {
    if (value == NULL || PY_IS_TAGGED_INT(value)) return -2;
    if (value == (PyObject *)pcc_type_cls_super) return -3;
    if (value == (PyObject *)pcc_type_cls_none) return PY_TYPE_NONE;
    if (value == (PyObject *)pcc_type_cls_bool) return PY_TYPE_BOOL;
    if (value == (PyObject *)pcc_type_cls_int) return PY_TYPE_INT;
    if (value == (PyObject *)pcc_type_cls_float) return PY_TYPE_FLOAT;
    if (value == (PyObject *)pcc_type_cls_str) return PY_TYPE_STR;
    if (value == (PyObject *)pcc_type_cls_list) return PY_TYPE_LIST;
    if (value == (PyObject *)pcc_type_cls_dict) return PY_TYPE_DICT;
    if (value == (PyObject *)pcc_type_cls_tuple) return PY_TYPE_TUPLE;
    if (value == (PyObject *)pcc_type_cls_set) return PY_TYPE_SET;
    if (value == (PyObject *)pcc_type_cls_type) return PY_TYPE_CLASS;
    if (value == (PyObject *)pcc_type_cls_complex) return PY_TYPE_COMPLEX;
    if (value == (PyObject *)pcc_type_cls_bytes) return PY_TYPE_BYTES;
    if (value == (PyObject *)pcc_type_cls_bytearray) return PY_TYPE_BYTEARRAY;
    if (value == (PyObject *)pcc_type_cls_memoryview) return PY_TYPE_MEMORYVIEW;
    if (value == (PyObject *)pcc_type_cls_object) return -1;
    return -2;
}

PyObject *py_type_builtin(PyObject *o) {
    if (o == NULL) {
        return pcc_builtin_type_class_for_tag(PY_TYPE_NONE);
    }
    if (PY_IS_TAGGED_INT(o)) {
        return pcc_builtin_type_class_for_tag(PY_TYPE_INT);
    }

    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_EXC) {
        PyExceptionObject *exc = (PyExceptionObject *)o;
        PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
            o,
            (PyObject **)&exc->exc_class
        );
        if (cls != NULL) {
            py_incref((PyObject *)cls);
            return (PyObject *)cls;
        }
    }
    if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
        PyInstanceObject *inst = (PyInstanceObject *)o;
        PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
            o,
            (PyObject **)&inst->cls
        );
        if (cls != NULL) {
            py_incref((PyObject *)cls);
            return (PyObject *)cls;
        }
    }
    return pcc_builtin_type_class_for_tag(tag);
}

int64_t py_obj_len(PyObject *o) {
    if (o == NULL) return 0;
    int32_t tag = py_type_of(o);
    switch (tag) {
        case PY_TYPE_LIST:  return py_list_len(o);
        case PY_TYPE_TUPLE: return py_tuple_len(o);
        case PY_TYPE_STR:   return py_str_len(o);
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_MEMORYVIEW:
            return py_bytes_len(o);
        case PY_TYPE_DICT:  return py_dict_len(o);
        case PY_TYPE_SET:   return py_set_len(o);
        default:
            /* Symmetric with py_obj_getitem below: a C-extension object's
             * length lives in its mp_length/sq_length slot, not in a Python
             * __len__. Without this, len(np.array(...)) returned 0. */
            if (pcc_capi_is_cext_type_tag(tag)) {
                int64_t cext_len = pcc_capi_cext_object_length(o);
                if (cext_len >= 0) return cext_len;
            }
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                int64_t handled = 0;
                int64_t user_len = py_user_len_dispatch(o, &handled);
                if (handled) return user_len;
            }
            return 0;
    }
}

PyObject *py_obj_getitem(PyObject *o, PyObject *k) {
    if (o == NULL || k == NULL) return NULL;
    int32_t tag = py_type_of(o);
    pcc_runtime_log_event_code(7, 1, tag, py_type_of(k), o);
    if (pcc_capi_is_cext_type_tag(tag)) {
        return pcc_capi_cext_object_getitem(o, k);
    }
    switch (tag) {
        case PY_TYPE_LIST: {
            int64_t idx = py_obj_index_i64(k);
            if (py_err_occurred()) return NULL;
            return py_list_get(o, idx);
        }
        case PY_TYPE_TUPLE: {
            int64_t idx = py_obj_index_i64(k);
            if (py_err_occurred()) return NULL;
            return py_tuple_get(o, idx);
        }
        case PY_TYPE_DICT:
            return py_dict_get(o, k);
        case PY_TYPE_STR:
            return py_str_index(o, k);
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_MEMORYVIEW:
            return py_bytes_getitem(o, k);
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                return py_user_getitem_dispatch(o, k);
            }
            return NULL;
    }
}

PyObject *py_obj_getitem_i64(PyObject *o, int64_t idx) {
    if (o == NULL) return NULL;
    int32_t tag = py_type_of(o);
    pcc_runtime_log_event_code(7, 1, tag, PY_TYPE_INT, o);
    if (pcc_capi_is_cext_type_tag(tag)) {
        PyObject *key = py_int_from_i64(idx);
        PyObject *out = pcc_capi_cext_object_getitem(o, key);
        py_decref(key);
        return out;
    }
    switch (tag) {
        case PY_TYPE_LIST:
            return py_list_get(o, idx);
        case PY_TYPE_TUPLE:
            return py_tuple_get(o, idx);
        case PY_TYPE_DICT: {
            PyObject *key = py_int_from_i64(idx);
            PyObject *out = py_dict_get(o, key);
            py_decref(key);
            return out;
        }
        case PY_TYPE_STR: {
            PyObject *key = py_int_from_i64(idx);
            PyObject *out = py_str_index(o, key);
            py_decref(key);
            return out;
        }
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_MEMORYVIEW: {
            PyObject *key = py_int_from_i64(idx);
            PyObject *out = py_bytes_getitem(o, key);
            py_decref(key);
            return out;
        }
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                PyObject *key = py_int_from_i64(idx);
                PyObject *out = py_user_getitem_dispatch(o, key);
                py_decref(key);
                return out;
            }
            return NULL;
    }
}

/* User-level ``o[k]`` and ``o[i]``.
 *
 * py_obj_getitem / py_obj_getitem_i64 keep their silent-NULL contract: tuple
 * unpacking, argument splatting and the C-API shims rely on NULL-without-error
 * for an absent key or index.  A frontend subscript expression must raise what
 * CPython raises instead, so ``try`` sees KeyError/IndexError/TypeError and an
 * uncaught failure carries a traceback rather than a NULL that later trips
 * "unsupported operand type(s)".  Mirrored by py_obj_ops_dispatch.py. */
static void py_obj_subscript_raise_missing(PyObject *o, PyObject *key) {
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_DICT) {
        py_raise_owned(py_exc_new_with_value(PY_EXC_KEYERROR, key));
        return;
    }
    if (tag == PY_TYPE_LIST) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "list index out of range"));
        return;
    }
    if (tag == PY_TYPE_TUPLE) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "tuple index out of range"));
        return;
    }
    if (tag == PY_TYPE_STR) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "string index out of range"));
        return;
    }
    if (tag == PY_TYPE_BYTES || tag == PY_TYPE_BYTEARRAY || tag == PY_TYPE_MEMORYVIEW) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "index out of range"));
        return;
    }
    PyObject *name = py_obj_type_name(o);
    PyObject *quote = py_str_new("'", 1);
    PyObject *head = py_str_concat(quote, name);
    PyObject *tail = py_str_new("' object is not subscriptable", 29);
    PyObject *message = py_str_concat(head, tail);
    py_decref(quote);
    py_decref(name);
    py_decref(head);
    py_decref(tail);
    py_raise_owned(py_exc_new_with_value(PY_EXC_TYPEERROR, message));
    py_decref(message);
}

PyObject *py_obj_subscript(PyObject *o, PyObject *k) {
    PyObject *out = py_obj_getitem(o, k);
    if (out != NULL || o == NULL || k == NULL) return out;
    if (py_err_occurred()) return NULL;
    py_obj_subscript_raise_missing(o, k);
    return NULL;
}

PyObject *py_obj_subscript_i64(PyObject *o, int64_t idx) {
    PyObject *out = py_obj_getitem_i64(o, idx);
    if (out != NULL || o == NULL) return out;
    if (py_err_occurred()) return NULL;
    PyObject *key = py_int_from_i64(idx);
    py_obj_subscript_raise_missing(o, key);
    py_decref(key);
    return NULL;
}

PyObject *py_obj_slice(PyObject *o, PyObject *lo, PyObject *hi, PyObject *step) {
    if (o == NULL) return NULL;
    int32_t tag = py_type_of(o);
    pcc_runtime_log_event_code(7, 2, tag, 0, o);
    switch (tag) {
        case PY_TYPE_LIST:
            return py_list_slice(o, lo, hi, step);
        case PY_TYPE_TUPLE:
            return py_tuple_slice(o, lo, hi, step);
        case PY_TYPE_STR:
            return py_str_slice(o, lo, hi, step);
        case PY_TYPE_BYTES:
        case PY_TYPE_BYTEARRAY:
        case PY_TYPE_MEMORYVIEW:
            return py_bytes_slice(o, lo, hi, step);
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                /* obj[lo:hi:step] on a user class dispatches
                 * __getitem__(slice(lo, hi, step)), like CPython. */
                PyObject *sl = py_slice_new(lo, hi, step);
                if (sl == NULL) return NULL;
                PyObject *r = py_obj_getitem(o, sl);
                py_decref(sl);
                return r;
            }
            return NULL;
    }
}

int64_t py_obj_set_slice(PyObject *o, PyObject *lo, PyObject *hi,
                         PyObject *step, PyObject *replacement) {
    if (o == NULL) return -1;
    int32_t tag = py_type_of(o);
    if (pcc_capi_is_cext_type_tag(tag)) {
        PyObject *slice = py_slice_new(lo, hi, step);
        if (slice == NULL) return -1;
        int64_t rc = pcc_capi_cext_object_setitem(o, slice, replacement);
        py_decref(slice);
        return rc;
    }
    switch (tag) {
        case PY_TYPE_LIST:
            return py_list_set_slice(o, lo, hi, step, replacement);
        default:
            return -1;
    }
}

int64_t py_obj_del_slice(PyObject *o, PyObject *lo, PyObject *hi,
                         PyObject *step) {
    if (o == NULL) return -1;
    int32_t tag = py_type_of(o);
    switch (tag) {
        case PY_TYPE_LIST:
            return py_list_del_slice(o, lo, hi, step);
        case PY_TYPE_BYTEARRAY:
            return py_bytearray_del_slice(o, lo, hi, step);
        default:
            return -1;
    }
}

int64_t py_obj_setitem(PyObject *o, PyObject *k, PyObject *v) {
    if (o == NULL || k == NULL) return -1;
    int32_t tag = py_type_of(o);
    pcc_runtime_log_event_code(7, 3, tag, py_type_of(k), o);
    if (pcc_capi_is_cext_type_tag(tag)) {
        return pcc_capi_cext_object_setitem(o, k, v);
    }
    switch (tag) {
        case PY_TYPE_LIST: {
            int64_t idx = py_obj_index_i64(k);
            if (py_err_occurred()) return -1;
            /* User-visible store: out-of-range raises catchable IndexError
             * (py_list_set stays the internal non-raising setter). */
            return py_list_setitem(o, idx, v);
        }
        case PY_TYPE_DICT:
            py_dict_set(o, k, v);
            return 0;
        case PY_TYPE_BYTEARRAY:
            return py_bytearray_setitem(o, k, v);
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                int64_t handled = 0;
                int64_t rc = py_user_setitem_dispatch(o, k, v, &handled);
                if (handled) return rc;
            }
            return -1;
    }
}

int64_t py_obj_setitem_i64(PyObject *o, int64_t idx, PyObject *v) {
    if (o == NULL) return -1;
    int32_t tag = py_type_of(o);
    pcc_runtime_log_event_code(7, 3, tag, PY_TYPE_INT, o);
    if (pcc_capi_is_cext_type_tag(tag)) {
        PyObject *key = py_int_from_i64(idx);
        if (key == NULL) return -1;
        int64_t rc = pcc_capi_cext_object_setitem(o, key, v);
        py_decref(key);
        return rc;
    }
    switch (tag) {
        case PY_TYPE_LIST:
            /* User-visible store: out-of-range raises catchable IndexError. */
            return py_list_setitem(o, idx, v);
        case PY_TYPE_DICT: {
            PyObject *key = py_int_from_i64(idx);
            py_dict_set(o, key, v);
            py_decref(key);
            return 0;
        }
        case PY_TYPE_BYTEARRAY: {
            PyObject *key = py_int_from_i64(idx);
            int64_t rc = py_bytearray_setitem(o, key, v);
            py_decref(key);
            return rc;
        }
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                int64_t handled = 0;
                PyObject *key = py_int_from_i64(idx);
                int64_t rc = py_user_setitem_dispatch(o, key, v, &handled);
                py_decref(key);
                return handled ? rc : -1;
            }
            return -1;
    }
}

int64_t py_obj_delitem(PyObject *o, PyObject *k) {
    if (o == NULL || k == NULL) return -1;
    int32_t tag = py_type_of(o);
    pcc_runtime_log_event_code(7, 4, tag, py_type_of(k), o);
    switch (tag) {
        case PY_TYPE_LIST: {
            int64_t idx = py_obj_index_i64(k);
            if (py_err_occurred()) return -1;
            PyObject *popped = py_list_pop(o, idx);
            if (popped != NULL) py_decref(popped);
            return 0;
        }
        case PY_TYPE_DICT:
            return py_dict_del(o, k);
        default:
            if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
                int64_t handled = 0;
                int64_t rc = py_user_delitem_dispatch(o, k, &handled);
                if (handled) return rc;
            }
            return -1;
    }
}

static int is_instance_tag_d(int32_t tag) {
    return tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START;
}

int64_t py_obj_assign_subscript(PyObject *o, PyObject *key, PyObject *value) {
    int64_t status = py_obj_setitem(o, key, value);
    if (status < 0 && !py_err_occurred()) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
            "object does not support item assignment"));
    }
    return status;
}

int64_t py_obj_delete_subscript(PyObject *o, PyObject *key) {
    int64_t status = py_obj_delitem(o, key);
    if (status < 0 && !py_err_occurred()) {
        if (py_type_of(o) == PY_TYPE_DICT) {
            py_raise_owned(py_exc_new_with_value(PY_EXC_KEYERROR, key));
        } else {
            py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                "object does not support item deletion"));
        }
    }
    return status;
}

static PyObject *py_obj_missing_attr(const char *name) {
    if (!py_err_occurred()) {
        py_raise_owned(py_exc_new(
            PY_EXC_ATTRIBUTEERROR,
            name != NULL ? name : ""
        ));
    }
    return NULL;
}

static int64_t py_obj_attr_status_failed(const char *name) {
    if (!py_err_occurred()) {
        py_raise_owned(py_exc_new(
            PY_EXC_ATTRIBUTEERROR,
            name != NULL ? name : ""
        ));
    }
    return -1;
}

static PyObject *py_coroutine_send_bound_entry(PyObject *captures, PyObject *args) {
    PyObject *coro = py_tuple_get(captures, 0);
    if (coro == NULL) return NULL;
    PyObject *value = NULL;
    if (args != NULL && py_tuple_len(args) > 0) {
        value = py_tuple_get(args, 0);
        if (value == NULL) {
            py_decref(coro);
            return NULL;
        }
    } else {
        value = py_None;
        py_incref(value);
    }
    PyObject *out = py_gen_send(coro, value);
    py_decref(value);
    py_decref(coro);
    return out;
}

static PyObject *py_coroutine_bound_send(PyObject *coro) {
    PyObject *captures = py_tuple_new(1);
    if (captures == NULL) return NULL;
    py_tuple_set_item(captures, 0, coro);
    PyObject *fn = py_func_new_bound(
        (void *)py_coroutine_send_bound_entry,
        captures,
        "send",
        coro
    );
    py_decref(captures);
    return fn;
}

static PyObject *py_list_pop_bound_entry(PyObject *captures, PyObject *args) {
    PyObject *lst = py_tuple_get(captures, 0);
    if (lst == NULL) return NULL;
    int64_t nargs = (args != NULL && !PY_IS_TAGGED_INT(args)
                     && py_type_of(args) == PY_TYPE_TUPLE)
                        ? py_tuple_len(args) : 0;
    if (nargs > 1) {
        py_decref(lst);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "list.pop expected at most 1 argument"));
        return NULL;
    }
    int64_t idx = -1;
    if (nargs == 1) {
        PyObject *idx_obj = py_tuple_get(args, 0);
        if (idx_obj == NULL) {
            py_decref(lst);
            return NULL;
        }
        idx = py_int_value_i64(idx_obj);
        py_decref(idx_obj);
        if (py_err_occurred()) {
            py_decref(lst);
            return NULL;
        }
    }
    PyObject *out = py_list_pop(lst, idx);
    py_decref(lst);
    return out;
}

static PyObject *py_dict_pop_bound_entry(PyObject *captures, PyObject *args) {
    PyObject *dict = py_tuple_get(captures, 0);
    if (dict == NULL) return NULL;
    int64_t nargs = (args != NULL && !PY_IS_TAGGED_INT(args)
                     && py_type_of(args) == PY_TYPE_TUPLE)
                        ? py_tuple_len(args) : 0;
    if (nargs < 1) {
        py_decref(dict);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "dict.pop expected at least 1 argument"));
        return NULL;
    }
    if (nargs > 2) {
        py_decref(dict);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "dict.pop expected at most 2 arguments"));
        return NULL;
    }
    PyObject *key = py_tuple_get(args, 0);
    if (key == NULL) {
        py_decref(dict);
        return NULL;
    }
    PyObject *out = py_dict_get(dict, key);
    if (out != NULL) {
        (void)py_dict_del(dict, key);
    } else if (nargs == 2) {
        out = py_tuple_get(args, 1);
    } else {
        py_raise_owned(py_exc_new_with_value(PY_EXC_KEYERROR, key));
    }
    py_decref(key);
    py_decref(dict);
    return out;
}

static PyObject *py_set_pop_bound_entry(PyObject *captures, PyObject *args) {
    PyObject *set = py_tuple_get(captures, 0);
    if (set == NULL) return NULL;
    int64_t nargs = (args != NULL && !PY_IS_TAGGED_INT(args)
                     && py_type_of(args) == PY_TYPE_TUPLE)
                        ? py_tuple_len(args) : 0;
    if (nargs > 0) {
        py_decref(set);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "set.pop expected no arguments"));
        return NULL;
    }
    PyObject *out = py_set_pop(set);
    py_decref(set);
    return out;
}

static PyObject *py_builtin_pop_bound(PyObject *o, void *entry) {
    PyObject *captures = py_tuple_new(1);
    if (captures == NULL) return NULL;
    py_tuple_set_item(captures, 0, o);
    PyObject *fn = py_func_new_bound(entry, captures, "pop", o);
    py_decref(captures);
    return fn;
}

static PyObject *py_str_count_bound_entry(PyObject *captures, PyObject *args) {
    PyObject *s = py_tuple_get(captures, 0);
    if (s == NULL) return NULL;
    int64_t nargs = (args != NULL && !PY_IS_TAGGED_INT(args)
                     && py_type_of(args) == PY_TYPE_TUPLE)
                        ? py_tuple_len(args) : 0;
    if (nargs < 1 || nargs > 3) {
        py_decref(s);
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "str.count expected 1 to 3 arguments"
        ));
        return NULL;
    }

    PyObject *sub = py_tuple_get(args, 0);
    if (sub == NULL) {
        py_decref(s);
        return NULL;
    }
    if (PY_IS_TAGGED_INT(sub) || py_type_of(sub) != PY_TYPE_STR) {
        py_decref(sub);
        py_decref(s);
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "str.count argument must be str"
        ));
        return NULL;
    }

    int64_t count = 0;
    if (nargs >= 2) {
        PyObject *start = py_tuple_get(args, 1);
        PyObject *end = nargs == 3 ? py_tuple_get(args, 2) : NULL;
        if (start == NULL || (nargs == 3 && end == NULL)) {
            if (start != NULL) py_decref(start);
            if (end != NULL) py_decref(end);
            py_decref(sub);
            py_decref(s);
            return NULL;
        }
        count = py_str_count_range(s, sub, start, end);
        py_decref(start);
        if (end != NULL) py_decref(end);
    } else {
        count = py_str_count(s, sub);
    }

    PyObject *out = py_int_from_i64(count);
    py_decref(s);
    py_decref(sub);
    return out;
}

static PyObject *py_str_count_bound(PyObject *o) {
    PyObject *captures = py_tuple_new(1);
    if (captures == NULL) return NULL;
    py_tuple_set_item(captures, 0, o);
    PyObject *fn = py_func_new_bound(
        (void *)py_str_count_bound_entry,
        captures,
        "count",
        o
    );
    py_decref(captures);
    return fn;
}

PyObject *py_obj_getattr(PyObject *o, const char *name) {
    if (!o || !name) return py_obj_missing_attr(name);

    if (strcmp(name, "__class__") == 0) {
        return py_type_builtin(o);
    }

    if (PY_IS_TAGGED_INT(o)) return py_obj_missing_attr(name);
    int32_t tag = py_header(o)->type_tag;
    pcc_runtime_log_event_code(7, 5, tag, 0, o);

    if (tag == PY_TYPE_STATICMETHOD
        && (strcmp(name, "__func__") == 0 || strcmp(name, "__wrapped__") == 0)) {
        PyObject *func = pcc_gc_load_ptr(o, &((PyStaticMethodObject *)o)->func);
        py_incref(func);
        return func;
    }

    PyObject *type_attr = pcc_capi_type_object_getattr(o, name);
    if (type_attr != NULL || py_err_occurred()) return type_attr;

    PyObject *builtin_attr = pcc_capi_builtin_object_getattr(o, name);
    if (builtin_attr != NULL || py_err_occurred()) return builtin_attr;

    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) {
        PyObject *result = pcc_capi_cext_object_getattr(o, name);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }

    if (strcmp(name, "pop") == 0) {
        if (tag == PY_TYPE_LIST) {
            return py_builtin_pop_bound(o, (void *)py_list_pop_bound_entry);
        }
        if (tag == PY_TYPE_DICT) {
            return py_builtin_pop_bound(o, (void *)py_dict_pop_bound_entry);
        }
        if (tag == PY_TYPE_SET) {
            return py_builtin_pop_bound(o, (void *)py_set_pop_bound_entry);
        }
    }

    if (tag == PY_TYPE_STR && strcmp(name, "count") == 0) {
        return py_str_count_bound(o);
    }

    if (is_instance_tag_d(tag)) {
        PyObject *result = py_instance_getattr((PyInstanceObject *)o, name);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }
    if (tag == PY_TYPE_CLASS) {
        PyObject *result = py_class_getattr((PyClassObject *)o, name);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }
    if (tag == PY_TYPE_FUNC) {
        PyFuncObject *f = (PyFuncObject *)o;
        PyObject *attrs = pcc_gc_load_ptr(o, &f->attrs);
        if (strcmp(name, "__dict__") == 0) {
            if (attrs == NULL) {
                attrs = py_dict_new();
                if (attrs == NULL) return NULL;
                pcc_gc_store_ptr(o, &f->attrs, attrs);
                return attrs;
            }
            py_incref(attrs);
            return attrs;
        }
        if (attrs != NULL) {
            PyObject *key = py_str_new(name, (int64_t)strlen(name));
            if (key == NULL) return NULL;
            PyObject *value = py_dict_get(attrs, key);
            py_decref(key);
            if (value != NULL) return value;
        }
        if (
            (strcmp(name, "__name__") == 0
             || strcmp(name, "__qualname__") == 0)
            && f->name != NULL
        ) {
            return py_str_new(f->name, (int64_t)strlen(f->name));
        }
        if (strcmp(name, "__code__") == 0) {
            PyObject *code = py_func_get_code_metadata(o);
            if (code != NULL || py_err_occurred()) return code;
        }
        if (strcmp(name, "__defaults__") == 0) {
            PyObject *defaults = py_func_get_defaults_metadata(o);
            if (defaults != NULL || py_err_occurred()) return defaults;
        }
        if (strcmp(name, "__doc__") == 0) {
            py_incref(py_None);
            return py_None;
        }
        if (strcmp(name, "__self__") == 0) {
            PyObject *self_obj = f->capi_method != NULL
                ? pcc_gc_load_ptr(o, &f->capi_self)
                : pcc_gc_load_ptr(o, &f->self_obj);
            if (self_obj != NULL) {
                py_incref(self_obj);
                return self_obj;
            }
        }
        return py_obj_missing_attr(name);
    }
    if (tag == PY_TYPE_WEAKREF) {
        PyObject *target = py_weakref_call(o);
        if (target == NULL || target == py_None) {
            if (target != NULL) py_decref(target);
            py_raise_owned(py_exc_new(
                PY_EXC_REFERENCEERROR,
                "weakly-referenced object no longer exists"
            ));
            return NULL;
        }
        PyObject *result = py_obj_getattr(target, name);
        py_decref(target);
        return result;
    }
    if (tag == PY_TYPE_COROUTINE) {
        PyObject *result = NULL;
        if (strcmp(name, "__class__") == 0) {
            result = py_coroutine_class();
        } else if (strcmp(name, "send") == 0) {
            return py_coroutine_bound_send(o);
        }
        if (result) py_incref(result);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }
    if (tag == PY_TYPE_CONTINUATION) {
        PyObject *result = NULL;
        if (strcmp(name, "__class__") == 0) {
            result = py_continuation_class();
        }
        if (result) py_incref(result);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }
    if (tag == PY_TYPE_COMPLEX) {
        if (strcmp(name, "real") == 0) return py_complex_real(o);
        if (strcmp(name, "imag") == 0) return py_complex_imag(o);
        return py_obj_missing_attr(name);
    }
    if (tag == PY_TYPE_EXC) {
        PyExceptionObject *e = (PyExceptionObject *)o;
        PyObject *result = NULL;
        if (strcmp(name, "__class__") == 0) {
            result = pcc_gc_load_ptr(o, (PyObject **)&e->exc_class);
        } else if (strcmp(name, "__cause__") == 0) {
            result = pcc_gc_load_ptr(o, &e->cause);
            if (result == NULL) result = py_None;
        } else if (strcmp(name, "__context__") == 0) {
            result = pcc_gc_load_ptr(o, &e->context);
            if (result == NULL) result = py_None;
        } else if (strcmp(name, "value") == 0) {
            result = pcc_gc_load_ptr(o, &e->message);
            if (result == NULL) result = py_None;
        } else if (strcmp(name, "msg") == 0) {
            /* CPython exposes `.msg` on ImportError/ModuleNotFoundError (it is
             * args[0]); other builtin exceptions have no `.msg`. numpy's
             * `_core` re-init recovery branches on `exc.msg == "..."`, so
             * faithful support is required — but stay scoped so a bare
             * RuntimeError does not grow a `.msg` it lacks in CPython. */
            PyClassObject *imp = py_exc_builtin_class(PY_EXC_IMPORTERROR);
            if (imp != NULL && py_isinstance(o, imp)) {
                result = pcc_gc_load_ptr(o, &e->message);
                if (result == NULL) result = py_None;
            } else {
                return py_obj_missing_attr(name);
            }
        } else if (strcmp(name, "args") == 0) {
            /* args tuple. The object stores only args[0] as `message`
             * (capturing args[1:] needs a dedicated args-tuple field — a
             * documented follow-up shared with multi-arg str(exc)), so return
             * () when there is no message else (message,). py_tuple_set_item
             * increfs, so msg's borrowed ref stays independent. */
            PyObject *msg = pcc_gc_load_ptr(o, &e->message);
            /* A no-arg exception (RuntimeError()) stores "" as its message, so
             * an empty-string message reads back as args == () like CPython
             * (message-only storage can't tell Error() from Error("") — the
             * no-arg form is the common one). */
            int empty = (msg == NULL || msg == py_None);
            if (!empty && py_type_of(msg) == PY_TYPE_STR
                && ((PyStrObject *)msg)->byte_len == 0) {
                empty = 1;
            }
            PyObject *t;
            if (empty) {
                t = py_tuple_new(0);
            } else {
                t = py_tuple_new(1);
                if (t != NULL) py_tuple_set_item(t, 0, msg);
            }
            return t;  /* already a new owned ref; skip the shared incref */
        }
        if (result) py_incref(result);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }
    return py_obj_missing_attr(name);
}

/* No-raise attribute probe for ``hasattr`` and 3-arg ``getattr``: identical
 * probe order to py_obj_getattr, but a plain not-found terminal returns NULL
 * WITHOUT constructing the AttributeError those callers immediately clear.
 * User __getattr__ still runs and its real exceptions still surface (NULL
 * with the error set).  Tags outside the fast terminals fall back to the
 * raising py_obj_getattr, whose exception the callers clear exactly as
 * before.  Motivation: one full exception lifecycle per miss measured at the
 * top of pcc1 worker profiles (docs/investigations/
 * pcc1-worker-object-protocol-tax.md, candidate 1). */
PyObject *py_obj_getattr_maybe(PyObject *o, const char *name) {
    if (!o || !name) return NULL;

    if (strcmp(name, "__class__") == 0) {
        return py_type_builtin(o);
    }

    if (PY_IS_TAGGED_INT(o)) return NULL;
    int32_t tag = py_header(o)->type_tag;
    pcc_runtime_log_event_code(7, 5, tag, 2, o);

    PyObject *type_attr = pcc_capi_type_object_getattr(o, name);
    if (type_attr != NULL || py_err_occurred()) return type_attr;

    PyObject *builtin_attr = pcc_capi_builtin_object_getattr(o, name);
    if (builtin_attr != NULL || py_err_occurred()) return builtin_attr;

    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) {
        return pcc_capi_cext_object_getattr(o, name);
    }

    if (is_instance_tag_d(tag)) {
        return py_instance_getattr((PyInstanceObject *)o, name);
    }
    if (tag == PY_TYPE_CLASS) {
        return py_class_getattr((PyClassObject *)o, name);
    }
    return py_obj_getattr(o, name);
}

PyObject *py_obj_getattr_default(PyObject *o, const char *name) {
    if (!o || !name) return py_obj_missing_attr(name);

    if (strcmp(name, "__class__") == 0) {
        return py_type_builtin(o);
    }

    if (PY_IS_TAGGED_INT(o)) return py_obj_missing_attr(name);
    int32_t tag = py_header(o)->type_tag;
    pcc_runtime_log_event_code(7, 5, tag, 1, o);

    if (is_instance_tag_d(tag)) {
        PyObject *result = py_instance_getattr_default((PyInstanceObject *)o, name);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }
    if (tag == PY_TYPE_CLASS) {
        PyObject *result = py_class_getattr((PyClassObject *)o, name);
        if (result != NULL || py_err_occurred()) return result;
        return py_obj_missing_attr(name);
    }
    return py_obj_getattr(o, name);
}

int64_t py_obj_setattr(PyObject *o, const char *name, PyObject *v) {
    if (!o || !name) return py_obj_attr_status_failed(name);
    if (PY_IS_TAGGED_INT(o)) return py_obj_attr_status_failed(name);
    int32_t tag = py_header(o)->type_tag;
    pcc_runtime_log_event_code(7, 6, tag, 0, o);

    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) {
        int64_t rc = pcc_capi_cext_object_setattr(o, name, v);
        if (rc == 0 || py_err_occurred()) return rc;
        return py_obj_attr_status_failed(name);
    }
    if (is_instance_tag_d(tag)) {
        int64_t rc = py_instance_setattr((PyInstanceObject *)o, name, v);
        if (rc == 0 || py_err_occurred()) return rc;
        return py_obj_attr_status_failed(name);
    }
    if (tag == PY_TYPE_CLASS) {
        int64_t rc = py_class_setattr((PyClassObject *)o, name, v);
        if (rc == 0 || py_err_occurred()) return rc;
    }
    if (tag == PY_TYPE_FUNC) {
        PyFuncObject *f = (PyFuncObject *)o;
        if (strcmp(name, "__dict__") == 0) {
            if (py_type_of(v) != PY_TYPE_DICT) {
                py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                    "function __dict__ must be set to a dictionary"));
                return -1;
            }
            pcc_gc_store_ptr(o, &f->attrs, v);
            return 0;
        }
        PyObject *attrs = pcc_gc_load_ptr(o, &f->attrs);
        int attrs_created = 0;
        if (attrs == NULL) {
            attrs = py_dict_new();
            if (attrs == NULL) return py_obj_attr_status_failed(name);
            pcc_gc_store_ptr(o, &f->attrs, attrs);
            attrs_created = 1;
        }
        PyObject *key = py_str_new(name, (int64_t)strlen(name));
        if (key == NULL) {
            if (attrs_created) py_decref(attrs);
            return py_obj_attr_status_failed(name);
        }
        py_dict_set(attrs, key, v);
        py_decref(key);
        if (attrs_created) py_decref(attrs);
        return 0;
    }
    return py_obj_attr_status_failed(name);
}

int64_t py_obj_delattr(PyObject *o, const char *name) {
    if (!o || !name) return -1;
    if (PY_IS_TAGGED_INT(o)) return -1;
    int32_t tag = py_header(o)->type_tag;
    pcc_runtime_log_event_code(7, 7, tag, 0, o);

    if (is_instance_tag_d(tag)) {
        return py_instance_delattr((PyInstanceObject *)o, name);
    }
    if (tag == PY_TYPE_CLASS) {
        return py_class_delattr((PyClassObject *)o, name);
    }
    return -1;
}

static PyObject *dispatch_require_call_result(
    PyObject *result,
    const char *callee,
    const char *message
) {
    if (result == NULL && !py_err_occurred()) {
        py_runtime_error_if_unset(callee, message);
    }
    return result;
}

static const char *dispatch_not_callable_message(int32_t tag) {
    if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
        return "instance has no __call__ method";
    }
    switch (tag) {
        case PY_TYPE_NONE: return "'NoneType' object is not callable";
        case PY_TYPE_BOOL: return "'bool' object is not callable";
        case PY_TYPE_INT: return "'int' object is not callable";
        case PY_TYPE_FLOAT: return "'float' object is not callable";
        case PY_TYPE_STR: return "'str' object is not callable";
        case PY_TYPE_LIST: return "'list' object is not callable";
        case PY_TYPE_DICT: return "'dict' object is not callable";
        case PY_TYPE_TUPLE: return "'tuple' object is not callable";
        case PY_TYPE_SET: return "'set' object is not callable";
        case PY_TYPE_BYTES: return "'bytes' object is not callable";
        case PY_TYPE_BYTEARRAY: return "'bytearray' object is not callable";
        case PY_TYPE_MEMORYVIEW: return "'memoryview' object is not callable";
        default: return "object type has no callable protocol";
    }
}

static PyObject *dispatch_raise_not_callable(PyObject *callable, int32_t tag) {
    pcc_runtime_log_event_code(7, 10, tag, 0, callable);
    py_raise_owned(py_exc_new(
        PY_EXC_TYPEERROR,
        dispatch_not_callable_message(tag)
    ));
    return NULL;
}

PyObject *py_obj_call(PyObject *callable, PyObject *args, PyObject *kwargs) {
    if (!callable) {
        return py_runtime_error_if_unset(
            "py_obj_call",
            "py_obj_call received NULL callable"
        );
    }
    if (PY_IS_TAGGED_INT(callable)) {
        return dispatch_raise_not_callable(callable, PY_TYPE_INT);
    }
    int32_t tag = py_header(callable)->type_tag;
    pcc_runtime_log_event_code(7, 8, tag, 0, callable);

    if (tag == PY_TYPE_STATICMETHOD) {
        PyObject *func = pcc_gc_load_ptr(
            callable, &((PyStaticMethodObject *)callable)->func
        );
        return py_obj_call(func, args, kwargs);
    }

    if (pcc_capi_type_object_is_callable(callable)) {
        return dispatch_require_call_result(
            pcc_capi_call_type_object(callable, args, kwargs),
            "pcc_capi_call_type_object",
            "pcc_capi_call_type_object returned NULL without setting an exception"
        );
    }

    if (tag == PY_TYPE_CLASS) {
        PyClassObject *cls = (PyClassObject *)callable;
        int64_t nargs = args != NULL ? py_tuple_len(args) : 0;
        int64_t nkwargs = (
            kwargs != NULL && kwargs != py_None && py_type_of(kwargs) == PY_TYPE_DICT
        ) ? py_dict_len(kwargs) : 0;
        if (
            cls == pcc_type_cls_bool || cls == pcc_type_cls_int ||
            cls == pcc_type_cls_float || cls == pcc_type_cls_str ||
            cls == pcc_type_cls_list || cls == pcc_type_cls_dict ||
            cls == pcc_type_cls_tuple
        ) {
            PyObject *arg = NULL;
            PyObject *out = NULL;
            if (nkwargs != 0 || nargs > 1) {
                py_raise_owned(py_exc_new(
                    PY_EXC_TYPEERROR,
                    "native builtin constructor accepts at most one positional argument"
                ));
                return NULL;
            }
            if (nargs == 1) arg = py_tuple_get(args, 0);
            if (cls == pcc_type_cls_bool) {
                out = py_bool_from_bit(arg != NULL ? (int32_t)py_obj_truthy(arg) : 0);
            } else if (cls == pcc_type_cls_int) {
                if (arg == NULL) {
                    out = py_int_from_i64(0);
                } else if (py_type_of(arg) == PY_TYPE_INT) {
                    py_incref(arg);
                    out = arg;
                } else if (py_type_of(arg) == PY_TYPE_BOOL) {
                    out = py_int_from_i64(py_obj_truthy(arg));
                } else if (py_type_of(arg) == PY_TYPE_FLOAT) {
                    out = py_int_from_i64((int64_t)py_float_to_f64(arg));
                } else if (py_type_of(arg) == PY_TYPE_STR) {
                    out = py_int_from_cstr_or_raise(py_str_utf8(arg), 10);
                } else {
                    py_raise_owned(py_exc_new(
                        PY_EXC_TYPEERROR,
                        "int() argument must be a string or a real number"
                    ));
                }
            } else if (cls == pcc_type_cls_float) {
                out = py_float_from_f64(arg != NULL ? py_float_value_of(arg) : 0.0);
            } else if (cls == pcc_type_cls_str) {
                out = arg != NULL ? py_obj_str(arg) : py_str_new("", 0);
            } else if (cls == pcc_type_cls_list) {
                out = py_list_new(0);
                if (out != NULL && arg != NULL) {
                    py_list_extend(out, arg);
                    if (py_err_occurred()) {
                        py_decref(out);
                        out = NULL;
                    }
                }
            } else if (cls == pcc_type_cls_tuple) {
                if (arg == NULL) {
                    out = py_tuple_new(0);
                } else if (py_type_of(arg) == PY_TYPE_TUPLE) {
                    py_incref(arg);
                    out = arg;
                } else {
                    PyObject *items = py_list_new(0);
                    if (items != NULL) py_list_extend(items, arg);
                    if (items != NULL && !py_err_occurred()) {
                        out = py_tuple_from_list(items);
                    }
                    if (items != NULL) py_decref(items);
                }
            } else if (cls == pcc_type_cls_dict) {
                out = py_dict_new();
                if (out != NULL && arg != NULL) {
                    if (py_type_of(arg) != PY_TYPE_DICT) {
                        py_decref(out);
                        py_raise_owned(py_exc_new(
                            PY_EXC_NOTIMPLEMENTEDERROR,
                            "pcc dict(iterable) currently requires a dict"
                        ));
                        out = NULL;
                    } else {
                        py_dict_update(out, arg);
                    }
                }
            }
            dispatch_require_call_result(
                out,
                "native builtin constructor",
                "native builtin constructor returned NULL without setting an exception"
            );
            if (arg != NULL) py_decref(arg);
            return out;
        }
        PyObject *inst = py_instance_new(cls);
        if (inst == NULL) {
            return dispatch_require_call_result(
                NULL,
                "py_instance_new",
                "py_instance_new returned NULL without setting an exception"
            );
        }
        PyObject *init_method = py_class_lookup(cls, "__init__");
        if (init_method != NULL) {
            PyObject *result = dispatch_call_method_with_args(
                init_method,
                inst,
                args,
                kwargs
            );
            if (result == NULL) {
                dispatch_require_call_result(
                    NULL,
                    "class __init__",
                    "class __init__ returned NULL without setting an exception"
                );
                py_decref(inst);
                return NULL;
            }
            py_decref(result);
        }
        return inst;
    }
    if (tag == PY_TYPE_FUNC) {
        return dispatch_require_call_result(
            py_func_call_kwargs(callable, args, kwargs),
            "py_func_call_kwargs",
            "py_func_call_kwargs returned NULL without setting an exception"
        );
    }
    if (tag == PY_TYPE_WEAKREF) {
        (void)args;
        (void)kwargs;
        return dispatch_require_call_result(
            py_weakref_call(callable),
            "py_weakref_call",
            "py_weakref_call returned NULL without setting an exception"
        );
    }
    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) {
        return dispatch_require_call_result(
            pcc_capi_call_cext_object(callable, args, kwargs),
            "pcc_capi_call_cext_object",
            "pcc_capi_call_cext_object returned NULL without setting an exception"
        );
    }
    if (is_instance_tag_d(tag)) {
        PyInstanceObject *inst = (PyInstanceObject *)callable;
        PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
            callable,
            (PyObject **)&inst->cls
        );
        PyObject *method = py_class_lookup(cls, "__call__");
        if (method != NULL) {
            return dispatch_require_call_result(
                dispatch_call_method_with_args(method, callable, args, kwargs),
                "instance __call__",
                "instance __call__ returned NULL without setting an exception"
            );
        }
    }
    return dispatch_raise_not_callable(callable, tag);
}

PyObject *py_obj_call_method1(PyObject *o, const char *name, PyObject *arg) {
    if (o == NULL) {
        return py_runtime_error_if_unset(
            "py_obj_call_method1",
            "py_obj_call_method1 received NULL object"
        );
    }
    if (name == NULL) {
        return py_runtime_error_if_unset(
            "py_obj_call_method1",
            "py_obj_call_method1 received NULL method name"
        );
    }
    if (arg == NULL) {
        return py_runtime_error_if_unset(
            "py_obj_call_method1",
            "py_obj_call_method1 received NULL argument"
        );
    }
    PyObject *method = py_obj_getattr(o, name);
    if (method == NULL) {
        return dispatch_require_call_result(
            NULL,
            "py_obj_getattr",
            "py_obj_getattr returned NULL without setting an exception"
        );
    }
    PyObject *args = py_tuple_new(2);
    if (args == NULL) {
        dispatch_require_call_result(
            NULL,
            "py_tuple_new",
            "py_obj_call_method1 could not allocate its argument tuple"
        );
        py_decref(method);
        return NULL;
    }
    py_tuple_set_item(args, 0, o);
    py_tuple_set_item(args, 1, arg);
    PyObject *out = py_obj_call(method, args, py_None);
    dispatch_require_call_result(
        out,
        "py_obj_call",
        "py_obj_call_method1 callee returned NULL without setting an exception"
    );
    py_decref(method);
    py_decref(args);
    return out;
}

int64_t py_obj_isinstance(PyObject *o, PyObject *cls) {
    if (!o || !cls) return 0;
    if (PY_IS_TAGGED_INT(cls)) return 0;
    if (py_header(cls)->type_tag == PY_TYPE_TUPLE) {
        int64_t count = py_tuple_len(cls);
        for (int64_t index = 0; index < count; index++) {
            PyObject *candidate = py_tuple_get(cls, index);
            int64_t matched = py_obj_isinstance(o, candidate);
            py_decref(candidate);
            if (matched != 0) return matched;
        }
        return 0;
    }
    if (py_header(cls)->type_tag != PY_TYPE_CLASS) return 0;
    if (cls == (PyObject *)pcc_type_cls_type) {
        return py_type_of(o) == PY_TYPE_CLASS || pcc_capi_is_type_object_value(o);
    }
    if (cls == (PyObject *)pcc_type_cls_bool) return py_type_of(o) == PY_TYPE_BOOL;
    if (cls == (PyObject *)pcc_type_cls_int) {
        int32_t tag = py_type_of(o);
        return tag == PY_TYPE_INT || tag == PY_TYPE_BOOL;
    }
    if (cls == (PyObject *)pcc_type_cls_float) return py_type_of(o) == PY_TYPE_FLOAT;
    if (cls == (PyObject *)pcc_type_cls_str) return py_type_of(o) == PY_TYPE_STR;
    if (cls == (PyObject *)pcc_type_cls_list) return py_type_of(o) == PY_TYPE_LIST;
    if (cls == (PyObject *)pcc_type_cls_dict) return py_type_of(o) == PY_TYPE_DICT;
    if (cls == (PyObject *)pcc_type_cls_tuple) return py_type_of(o) == PY_TYPE_TUPLE;
    pcc_runtime_log_event_code(7, 9, py_type_of(o), py_type_of(cls), o);
    return py_isinstance(o, (PyClassObject *)cls);
}

int64_t py_obj_issubclass(PyObject *derived, PyObject *cls) {
    if (!derived || PY_IS_TAGGED_INT(derived)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "issubclass() arg 1 must be a class"));
        return -1;
    }
    int64_t derived_is_capi_type = pcc_capi_is_type_object_value(derived);
    if (!derived_is_capi_type && py_header(derived)->type_tag != PY_TYPE_CLASS) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "issubclass() arg 1 must be a class"));
        return -1;
    }
    if (!cls || PY_IS_TAGGED_INT(cls)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "issubclass() arg 2 must be a class"));
        return -1;
    }
    int64_t cls_is_capi_type = pcc_capi_is_type_object_value(cls);
    if (!cls_is_capi_type && py_header(cls)->type_tag != PY_TYPE_CLASS) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "issubclass() arg 2 must be a class"));
        return -1;
    }
    if (derived_is_capi_type || cls_is_capi_type) {
        if (!derived_is_capi_type || !cls_is_capi_type) return 0;
        return pcc_capi_type_object_issubclass(derived, cls);
    }
    PyClassObject *derived_cls = (PyClassObject *)pcc_gc_note_relocation_read(
        derived
    );
    PyClassObject *target_cls = (PyClassObject *)pcc_gc_note_relocation_read(
        cls
    );
    if (derived_cls == target_cls) return 1;
    for (int32_t i = 0; i < derived_cls->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)derived_cls,
            (PyObject **)&derived_cls->mro[i]
        );
        if (m == target_cls) return 1;
    }
    return 0;
}
