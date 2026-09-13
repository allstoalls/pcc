/* pcc/py_runtime/src/py_dunder.c
 *
 * Small dynamic dunder helpers that runtime-high pcc-Python modules can
 * call through extern(). These stay in C for now because pcc-Python cannot
 * yet call arbitrary function pointers loaded from class method tables.
 */

#include "py_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

PyObject *py_int_to_str_obj(PyObject *o) {
    if (o == NULL) return NULL;

    if (PY_IS_TAGGED_INT(o)) {
        int64_t v = py_untag_int(o);
        uint64_t mag;
        int neg = v < 0;
        if (neg) {
            mag = (uint64_t)(-(v + 1)) + 1u;
        } else {
            mag = (uint64_t)v;
        }

        char buf[32];
        char *end = buf + sizeof(buf);
        char *p = end;
        do {
            *--p = (char)('0' + (mag % 10u));
            mag /= 10u;
        } while (mag != 0);
        if (neg) *--p = '-';
        return py_str_new(p, (int64_t)(end - p));
    }

    if (py_type_of(o) != PY_TYPE_INT) return NULL;

    PyIntObject *b = py_bigint_from_any(o);
    if (b == NULL) return NULL;

    char *raw = py_bigint_to_cstr(b);
    free(b);
    if (raw == NULL) return NULL;

    PyObject *s = py_str_new(raw, (int64_t)strlen(raw));
    free(raw);
    return s;
}

/* float -> PyStr, matching py_print_fmt.c::py_format_float ("%g" plus a
 * trailing ".0" when the result looks integral).  Lives here (not in
 * py_print_fmt.c) so the pcc-Python runtime ports can extern it on the strict
 * no-libpython path, where py_print_fmt.c is replaced by py_print_fmt.py. */
PyObject *py_float_to_str_obj(PyObject *o) {
    if (o == NULL || py_type_of(o) != PY_TYPE_FLOAT) return NULL;
    PyFloatObject *f = (PyFloatObject *)o;
    char buf[64];
    int n = snprintf(buf, sizeof(buf), "%g", f->value);
    if (n < 0) return NULL;
    if (n >= (int)sizeof(buf)) n = (int)sizeof(buf) - 1;
    int needs_dot = 1;
    for (int i = 0; i < n; i++) {
        if (buf[i] == '.' || buf[i] == 'e' || buf[i] == 'E') {
            needs_dot = 0;
            break;
        }
    }
    if (needs_dot
        && strcmp(buf, "nan") != 0
        && strcmp(buf, "inf") != 0
        && strcmp(buf, "-inf") != 0
        && n + 2 < (int)sizeof(buf)) {
        buf[n] = '.';
        buf[n + 1] = '0';
        buf[n + 2] = '\0';
        n += 2;
    }
    return py_str_new(buf, (int64_t)n);
}

PyObject *py_int_format_hex(PyObject *o, int64_t width, int64_t zero_pad) {
    int overflow = 0;
    int64_t v = py_int_to_i64(o, &overflow);
    if (overflow) {
        return py_int_to_str_obj(o);
    }

    int neg = v < 0;
    uint64_t mag;
    if (neg) {
        mag = (uint64_t)(-(v + 1)) + 1u;
    } else {
        mag = (uint64_t)v;
    }

    char rev[32];
    int ndigits = 0;
    do {
        unsigned digit = (unsigned)(mag & 0xFu);
        rev[ndigits++] = (char)(digit < 10 ? '0' + digit : 'a' + digit - 10);
        mag >>= 4;
    } while (mag != 0 && ndigits < (int)sizeof(rev));

    if (width < 0) width = 0;
    if (width > 120) width = 120;
    int min_len = ndigits + neg;
    int pad = (int)width - min_len;
    if (pad < 0) pad = 0;

    char buf[128];
    int pos = 0;
    if (neg && zero_pad) {
        buf[pos++] = '-';
    }
    char pad_ch = zero_pad ? '0' : ' ';
    for (int i = 0; i < pad && pos < (int)sizeof(buf); i++) {
        buf[pos++] = pad_ch;
    }
    if (neg && !zero_pad && pos < (int)sizeof(buf)) {
        buf[pos++] = '-';
    }
    for (int i = ndigits - 1; i >= 0 && pos < (int)sizeof(buf); i--) {
        buf[pos++] = rev[i];
    }
    return py_str_new(buf, pos);
}

/* bin()/hex()/oct() builtins: a base-prefixed string with the sign before the
 * prefix for negatives, matching CPython exactly:
 *   bin(5) -> "0b101", hex(255) -> "0xff", oct(8) -> "0o10",
 *   hex(-255) -> "-0xff", bin(0) -> "0b0".
 * Accepts int (tagged or heap PY_TYPE_INT within i64) and bool; raises
 * TypeError on other types and ValueError on >i64 bignums (not yet supported). */
static PyObject *py_int_based_repr(PyObject *o, unsigned base, char prefix_ch) {
    if (o == NULL) return NULL;
    int64_t v;
    if (PY_IS_TAGGED_INT(o)) {
        v = py_untag_int(o);
    } else {
        int32_t tag = py_header(o)->type_tag;
        if (tag == PY_TYPE_BOOL) {
            v = (o == py_True) ? 1 : 0;
        } else if (tag == PY_TYPE_INT) {
            int overflow = 0;
            v = py_int_to_i64(o, &overflow);
            if (overflow) {
                /* Bignum exceeding i64: convert the full magnitude in base. */
                char *s = py_bigint_to_base_cstr(
                    (const PyIntObject *)o, base, prefix_ch);
                if (s == NULL) return NULL;
                PyObject *r = py_str_new(s, (int64_t)strlen(s));
                free(s);
                return r;
            }
        } else {
            py_raise_owned(py_exc_new(
                PY_EXC_TYPEERROR,
                "'object' cannot be interpreted as an integer"));
            return NULL;
        }
    }
    int neg = v < 0;
    uint64_t mag = neg ? (uint64_t)(-(v + 1)) + 1u : (uint64_t)v;
    char rev[72];
    int nd = 0;
    do {
        unsigned d = (unsigned)(mag % base);
        rev[nd++] = (char)(d < 10 ? '0' + (int)d : 'a' + (int)d - 10);
        mag /= base;
    } while (mag != 0 && nd < (int)sizeof(rev));
    char buf[96];
    int pos = 0;
    if (neg) buf[pos++] = '-';
    buf[pos++] = '0';
    buf[pos++] = prefix_ch;
    for (int i = nd - 1; i >= 0; i--) buf[pos++] = rev[i];
    return py_str_new(buf, (int64_t)pos);
}

PyObject *py_builtin_bin(PyObject *o) { return py_int_based_repr(o, 2u, 'b'); }
PyObject *py_builtin_hex(PyObject *o) { return py_int_based_repr(o, 16u, 'x'); }
PyObject *py_builtin_oct(PyObject *o) { return py_int_based_repr(o, 8u, 'o'); }

/* ``callable(x)``: mirror py_obj_call's dispatch classification. Functions,
 * classes and weakrefs are callable; an instance is callable iff its class
 * defines ``__call__``. Tagged ints, None, and any other type tag are not. */
PyObject *py_builtin_callable(PyObject *o) {
    if (o == NULL) return py_bool_from_bit(0);
    if (PY_IS_TAGGED_INT(o)) return py_bool_from_bit(0);
    int32_t tag = py_header(o)->type_tag;
    if (tag == PY_TYPE_FUNC || tag == PY_TYPE_CLASS || tag == PY_TYPE_WEAKREF
        || tag == PY_TYPE_STATICMETHOD) {
        return py_bool_from_bit(1);
    }
    if (tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START) {
        PyInstanceObject *inst = (PyInstanceObject *)o;
        PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
            o,
            (PyObject **)&inst->cls
        );
        PyObject *method = py_class_lookup(cls, "__call__");
        return py_bool_from_bit(method != NULL ? 1 : 0);
    }
    return py_bool_from_bit(0);
}

PyObject *py_int_format_decimal(
    PyObject *o, int64_t width, int64_t zero_pad, int64_t comma
) {
    int overflow = 0;
    int64_t v = py_int_to_i64(o, &overflow);
    if (overflow) {
        return py_int_to_str_obj(o);
    }

    int neg = v < 0;
    uint64_t mag;
    if (neg) {
        mag = (uint64_t)(-(v + 1)) + 1u;
    } else {
        mag = (uint64_t)v;
    }

    char rev[32];
    int ndigits = 0;
    do {
        rev[ndigits++] = (char)('0' + (mag % 10u));
        mag /= 10u;
    } while (mag != 0 && ndigits < (int)sizeof(rev));

    int comma_count = 0;
    if (comma && ndigits > 3) {
        comma_count = (ndigits - 1) / 3;
    }
    if (width < 0) width = 0;
    if (width > 120) width = 120;
    int min_len = ndigits + comma_count + neg;
    int pad = (int)width - min_len;
    if (pad < 0) pad = 0;

    char buf[160];
    int pos = 0;
    if (neg && zero_pad) {
        buf[pos++] = '-';
    }
    char pad_ch = zero_pad ? '0' : ' ';
    for (int i = 0; i < pad && pos < (int)sizeof(buf); i++) {
        buf[pos++] = pad_ch;
    }
    if (neg && !zero_pad && pos < (int)sizeof(buf)) {
        buf[pos++] = '-';
    }
    for (int i = ndigits - 1; i >= 0 && pos < (int)sizeof(buf); i--) {
        buf[pos++] = rev[i];
        if (comma && i > 0 && (i % 3) == 0 && pos < (int)sizeof(buf)) {
            buf[pos++] = (char)comma;
        }
    }
    return py_str_new(buf, pos);
}

static int pcc_dunder_pointer_can_have_header(void *ptr) {
    return pcc_gc_pointer_is_managed((PyObject *)ptr) != 0;
}

static int pcc_dunder_is_user_instance(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    int32_t tag = py_header(o)->type_tag;
    if (tag != PY_TYPE_INSTANCE && tag < PY_TYPE_USER_CLASS_START) return 0;
    return 1;
}

static PyObject *pcc_user_dunder_lookup(PyObject *o, const char *name) {
    if (!pcc_dunder_is_user_instance(o)) return NULL;
    PyInstanceObject *inst = (PyInstanceObject *)o;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        o, (PyObject **)&inst->cls
    );
    if (cls == NULL) return NULL;
    return py_class_lookup(cls, name);
}

static PyObject *dunder_require_result(
    PyObject *result,
    const char *helper_name,
    const char *message
) {
    if (result == NULL) {
        py_runtime_error_if_unset(helper_name, message);
    }
    return result;
}

static PyObject *pcc_call_user_unary_method(PyObject *func, PyObject *self) {
    /* ``func == NULL`` is the deliberate "dunder not defined" sentinel. */
    if (func == NULL) return NULL;
    if (pcc_dunder_pointer_can_have_header(func)
        && !PY_IS_TAGGED_INT(func)
        && py_type_of(func) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(1);
        if (args == NULL) {
            return dunder_require_result(
                NULL,
                "py_tuple_new",
                "user dunder argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        PyObject *out = py_func_call(func, args);
        dunder_require_result(
            out,
            "user dunder call",
            "user dunder callback returned NULL without an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*UnaryMethod)(PyObject *);
    UnaryMethod meth = (UnaryMethod)(uintptr_t)func;
    return dunder_require_result(
        meth(self),
        "user dunder call",
        "user dunder callback returned NULL without an exception"
    );
}

static void pcc_call_user_unary_method_void(PyObject *func, PyObject *self) {
    if (func == NULL) return;
    if (pcc_dunder_pointer_can_have_header(func)
        && !PY_IS_TAGGED_INT(func)
        && py_type_of(func) == PY_TYPE_FUNC) {
        PyObject *result = pcc_call_user_unary_method(func, self);
        if (result != NULL) py_decref(result);
        return;
    }
    typedef void (*VoidUnaryMethod)(PyObject *);
    VoidUnaryMethod meth = (VoidUnaryMethod)(uintptr_t)func;
    meth(self);
}

PyObject *py_user_str_dispatch(PyObject *o) {
    PyObject *func = pcc_user_dunder_lookup(o, "__str__");
    return pcc_call_user_unary_method(func, o);
}

PyObject *py_user_repr_dispatch(PyObject *o) {
    PyObject *func = pcc_user_dunder_lookup(o, "__repr__");
    return pcc_call_user_unary_method(func, o);
}

int64_t py_user_hash_dispatch(PyObject *o, int64_t *handled) {
    if (handled != NULL) *handled = 0;
    PyObject *func = pcc_user_dunder_lookup(o, "__hash__");
    if (func == NULL) return 0;
    if (func == py_None) {
        if (handled != NULL) *handled = 1;
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unhashable type"));
        return 0;
    }
    PyObject *result = pcc_call_user_unary_method(func, o);
    if (result == NULL) {
        if (handled != NULL) *handled = 1;
        return 0;
    }
    int overflow = 0;
    int64_t value = py_int_to_i64(result, &overflow);
    py_decref(result);
    if (overflow) {
        if (handled != NULL) *handled = 1;
        return 0;
    }
    if (handled != NULL) *handled = 1;
    return value == -1 ? -2 : value;
}

PyObject *py_user_iter_dispatch(PyObject *o) {
    PyObject *func = pcc_user_dunder_lookup(o, "__iter__");
    return pcc_call_user_unary_method(func, o);
}

PyObject *py_user_next_dispatch(PyObject *o) {
    PyObject *func = pcc_user_dunder_lookup(o, "__next__");
    return pcc_call_user_unary_method(func, o);
}

void py_user_del_dispatch(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return;
    PyObjectHeader *h = py_header(o);
    int32_t tag = h->type_tag;
    if (tag == PY_TYPE_GEN) {
        py_gen_finalize(o);
        return;
    }
    if (tag != PY_TYPE_INSTANCE && tag < PY_TYPE_USER_CLASS_START) return;
    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) return;
    /* No class ever defined __del__: the MRO lookup cannot find one.  Mirrors
     * the port's pcc_class_del_defined_count gate. */
    {
        extern int32_t pcc_class_del_defined_count;
        if (__atomic_load_n(&pcc_class_del_defined_count, __ATOMIC_ACQUIRE) == 0) return;
    }
    if ((h->flags & PY_FLAG_FINALIZED) != 0) {
        pcc_runtime_log_event("finalizer", "skipped", tag, 1, o);
        return;
    }

    PyInstanceObject *inst = (PyInstanceObject *)o;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        o, (PyObject **)&inst->cls
    );
    if (cls == NULL) return;

    /* Match the pcc-Python runtime: del_method is a borrowed update-only GC
     * alias, not a second semantic cache.  Looking through the MRO here keeps
     * finalizer dispatch correct if class metadata changes after creation. */
    PyObject *func = py_class_lookup(cls, "__del__");
    if (func == NULL) return;

    h->flags |= PY_FLAG_FINALIZED;

    /* A finalizer may run while an exception from the operation that dropped
     * the last reference is already pending (for example, a failing
     * __init__).  Give __del__ an empty exception slot so any error it raises
     * is independently suppressible, then restore the caller's exception.
     * py_tls_exc_set is a raw slot transfer, so keep one temporary reference
     * while the slot is detached. */
    PyObject *saved_exc = py_current_exception();
    if (saved_exc != NULL) {
        py_incref(saved_exc);
        py_tls_exc_set(NULL);
    }
    pcc_runtime_log_event("finalizer", "call", tag, 0, o);
    pcc_call_user_unary_method_void(func, o);
    pcc_runtime_log_event("finalizer", "done", tag, 0, o);

    /* CPython reports most finalizer exceptions as unraisable and keeps
     * running. The warning channel is a later diagnostics task; the
     * important invariant here is that stale TLS exception state cannot
     * poison the caller after a best-effort finalizer dispatch. */
    py_clear_exception();
    if (saved_exc != NULL) {
        py_tls_exc_set(saved_exc);
        py_decref(saved_exc);
    }
}
