/* Host-C oracle for py/py_protocol_runtime.py.
 *
 * User protocol dunder dispatch.
 *
 * This file centralizes dynamic data-model lookups for protocols that are
 * implemented by generic object operations:
 *
 *   __len__ / __bool__ / __contains__
 *   __getitem__ / __setitem__ / __delitem__
 *
 * The class method table currently supports both raw C function pointers and
 * PY_TYPE_FUNC wrappers.  The helpers below support both representations.
 */

#include "py_internal.h"
#include <math.h>
#include <stdint.h>
#include <string.h>

static int ptr_can_have_header(void *ptr) {
    return pcc_gc_pointer_is_managed((PyObject *)ptr) != 0;
}

/* Forward declarations for the dict-subclass fallback (defined at the end of
 * this file). Declared up here so the generic user-dispatch helpers above can
 * route to the backing dict for classes that subclass the builtin ``dict``. */
static int class_is_dict_subclass(PyObject *o);
static PyObject *dict_subclass_backing(PyObject *o, int create);
PyObject *py_dict_subclass_getitem(PyObject *o, PyObject *key);

static int is_user_instance(PyObject *o) {
    if (o == NULL || PY_IS_TAGGED_INT(o)) return 0;
    int32_t tag = py_type_of(o);
    /* C-extension tags share the high numeric range with pcc user classes,
     * but their instance layout is PyObject-compatible rather than
     * PyInstanceObject-compatible.  Treating one as a user instance makes
     * lookup_dunder read an arbitrary word as ``inst->cls``. */
    if (pcc_capi_is_cext_type_tag((int64_t)tag) != 0) return 0;
    return tag == PY_TYPE_INSTANCE || tag >= PY_TYPE_USER_CLASS_START;
}

static PyObject *lookup_dunder(PyObject *o, const char *name) {
    if (!is_user_instance(o)) return NULL;
    PyInstanceObject *inst = (PyInstanceObject *)o;
    if (inst->cls == NULL) return NULL;
    return py_class_lookup(inst->cls, name);
}

static PyObject *protocol_require_result(
    PyObject *result,
    const char *helper_name,
    const char *message
) {
    if (result == NULL) {
        py_runtime_error_if_unset(helper_name, message);
    }
    return result;
}

static PyObject *call_unary(PyObject *method, PyObject *self) {
    /* ``method == NULL`` is the lookup sentinel owned by the caller.  Every
     * NULL after a method has been selected is an error result. */
    if (method == NULL) return NULL;
    if (ptr_can_have_header(method) && !PY_IS_TAGGED_INT(method)
        && py_type_of(method) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(1);
        if (args == NULL) {
            return protocol_require_result(
                NULL,
                "py_tuple_new",
                "user protocol argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        PyObject *out = py_func_call(method, args);
        protocol_require_result(
            out,
            "user protocol call",
            "user protocol callback returned NULL without an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*Unary)(PyObject *);
    return protocol_require_result(
        ((Unary)(uintptr_t)method)(self),
        "user protocol call",
        "user protocol callback returned NULL without an exception"
    );
}

static PyObject *call_binary(PyObject *method, PyObject *self, PyObject *arg) {
    if (method == NULL) return NULL;
    if (ptr_can_have_header(method) && !PY_IS_TAGGED_INT(method)
        && py_type_of(method) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(2);
        if (args == NULL) {
            return protocol_require_result(
                NULL,
                "py_tuple_new",
                "user protocol argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        py_tuple_set_item(args, 1, arg);
        PyObject *out = py_func_call(method, args);
        protocol_require_result(
            out,
            "user protocol call",
            "user protocol callback returned NULL without an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*Binary)(PyObject *, PyObject *);
    return protocol_require_result(
        ((Binary)(uintptr_t)method)(self, arg),
        "user protocol call",
        "user protocol callback returned NULL without an exception"
    );
}

static PyObject *call_ternary(PyObject *method, PyObject *self,
                              PyObject *a, PyObject *b) {
    if (method == NULL) return NULL;
    if (ptr_can_have_header(method) && !PY_IS_TAGGED_INT(method)
        && py_type_of(method) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(3);
        if (args == NULL) {
            return protocol_require_result(
                NULL,
                "py_tuple_new",
                "user protocol argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        py_tuple_set_item(args, 1, a);
        py_tuple_set_item(args, 2, b);
        PyObject *out = py_func_call(method, args);
        protocol_require_result(
            out,
            "user protocol call",
            "user protocol callback returned NULL without an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*Ternary)(PyObject *, PyObject *, PyObject *);
    return protocol_require_result(
        ((Ternary)(uintptr_t)method)(self, a, b),
        "user protocol call",
        "user protocol callback returned NULL without an exception"
    );
}

int64_t py_user_len_dispatch(PyObject *o, int64_t *handled) {
    if (handled) *handled = 0;
    PyObject *method = lookup_dunder(o, "__len__");
    if (method == NULL) {
        /* No user __len__: a dict subclass reports its backing-dict size
         * (inherited dict.__len__). */
        if (class_is_dict_subclass(o)) {
            if (handled) *handled = 1;
            PyObject *d = dict_subclass_backing(o, 0);
            return d != NULL ? py_dict_len(d) : 0;
        }
        return 0;
    }
    if (handled) *handled = 1;
    PyObject *result = call_unary(method, o);
    if (result == NULL) return 0;
    int overflow = 0;
    int64_t value = py_int_to_i64(result, &overflow);
    py_decref(result);
    if (overflow || value < 0) return 0;
    return value;
}

/* abs(obj) for a user instance: dispatch __abs__ (unbound-func + explicit self,
 * avoiding the bound-method double-self bug). Returns NULL with NO pending
 * exception when the object has no __abs__ (caller then raises TypeError);
 * NULL with a pending exception means __abs__ itself raised. */
PyObject *py_user_abs_dispatch(PyObject *o) {
    PyObject *method = lookup_dunder(o, "__abs__");
    if (method == NULL) return NULL;
    return call_unary(method, o);
}

int64_t py_user_bool_dispatch(PyObject *o, int64_t *handled) {
    if (handled) *handled = 0;
    PyObject *method = lookup_dunder(o, "__bool__");
    if (method == NULL) return 0;
    if (handled) *handled = 1;
    PyObject *result = call_unary(method, o);
    if (result == NULL) return 0;
    int64_t truth = py_obj_truthy(result);
    py_decref(result);
    return truth ? 1 : 0;
}

int64_t py_obj_index_i64(PyObject *o) {
    if (o == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object cannot be interpreted as an integer"));
        return 0;
    }
    if (PY_IS_TAGGED_INT(o)) return py_int_value_i64(o);
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_INT) return py_int_value_i64(o);
    if (tag == PY_TYPE_BOOL) return o == py_True ? 1 : 0;
    if (!is_user_instance(o)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object cannot be interpreted as an integer"));
        return 0;
    }
    PyObject *method = lookup_dunder(o, "__index__");
    if (method == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object cannot be interpreted as an integer"));
        return 0;
    }
    PyObject *result = call_unary(method, o);
    if (result == NULL) return 0;
    if (PY_IS_TAGGED_INT(result) || py_type_of(result) == PY_TYPE_INT) {
        int overflow = 0;
        int64_t value = py_int_to_i64(result, &overflow);
        py_decref(result);
        if (!overflow) return value;
    } else {
        py_decref(result);
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "__index__ returned non-int"));
    return 0;
}

static int64_t slice_integer_i64(PyObject *value) {
    int overflow = 0;
    int64_t result = py_int_to_i64(value, &overflow);
    if (!overflow) return result;
    PyObject *zero = py_int_from_i64(0);
    int sign = py_int_cmp(value, zero);
    py_decref(zero);
    return sign < 0 ? INT64_MIN : INT64_MAX;
}

int64_t py_slice_index_i64(PyObject *o, int64_t default_value) {
    if (o == NULL || o == py_None) return default_value;
    if (PY_IS_TAGGED_INT(o) || py_type_of(o) == PY_TYPE_INT)
        return slice_integer_i64(o);
    if (py_type_of(o) == PY_TYPE_BOOL) return o == py_True ? 1 : 0;
    PyObject *method = lookup_dunder(o, "__index__");
    if (method == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
            "slice indices must be integers or None or have an __index__ method"));
        return 0;
    }
    PyObject *result = call_unary(method, o);
    if (result == NULL) return 0;
    if (PY_IS_TAGGED_INT(result) || py_type_of(result) == PY_TYPE_INT) {
        int64_t value = slice_integer_i64(result);
        py_decref(result);
        return value;
    }
    py_decref(result);
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "__index__ returned non-int"));
    return 0;
}

int64_t py_user_contains_dispatch(PyObject *o, PyObject *item,
                                  int64_t *handled) {
    if (handled) *handled = 0;
    PyObject *method = lookup_dunder(o, "__contains__");
    if (method == NULL) {
        /* No user __contains__: a dict subclass tests key membership against
         * its backing dict (inherited dict.__contains__). */
        if (class_is_dict_subclass(o)) {
            if (handled) *handled = 1;
            PyObject *d = dict_subclass_backing(o, 0);
            return (d != NULL && py_dict_contains(d, item)) ? 1 : 0;
        }
        return 0;
    }
    if (handled) *handled = 1;
    PyObject *result = call_binary(method, o, item);
    if (result == NULL) return 0;
    int64_t truth = py_obj_truthy(result);
    py_decref(result);
    return truth ? 1 : 0;
}

/* Dispatch a user __eq__ for py_obj_eq (used by dict/set key lookup, the ``==``
 * runtime path, etc.). Returns a TRI-STATE: -1 = no __eq__ defined (caller
 * falls back to identity), 0 = __eq__ said not-equal, 1 = __eq__ said equal.
 * Uses lookup_dunder (unbound func) + call_binary, avoiding the bound-method
 * double-self bug. A NotImplemented result also yields -1 (fall back). */
int64_t py_user_eq_dispatch(PyObject *a, PyObject *b) {
    /* Recursion guard: a user __eq__ that compares fields which route back
     * through py_obj_eq -> here (nested / self-referential structures) could
     * recurse to a stack overflow. Bail to identity (-1) past a depth well
     * above realistic nesting but far below the C stack limit. Thread-local so
     * concurrent comparisons don't clobber the counter. This is the
     * self-host-safety guard for the py_obj_eq instance dispatch. */
    static __thread int _eq_depth = 0;
    if (_eq_depth >= 64) return -1;
    PyObject *method = lookup_dunder(a, "__eq__");
    if (method != NULL) {
        _eq_depth++;
        PyObject *result = call_binary(method, a, b);
        _eq_depth--;
        if (result == NULL) return 0;    /* __eq__ raised: error already set */
        if (result != py_NotImplemented) {
            int64_t truth = py_obj_truthy(result);
            py_decref(result);
            return truth ? 1 : 0;
        }
        py_decref(result);
    }
    if (py_type_of(a) == py_type_of(b)) return -1;
    /* Equality reflection uses the same __eq__ name with reversed operands.
     * This is required when the stored/left key returns NotImplemented but
     * the query/right key knows how to compare itself to the left. */
    method = lookup_dunder(b, "__eq__");
    if (method == NULL) return -1;
    _eq_depth++;
    PyObject *result = call_binary(method, b, a);
    _eq_depth--;
    if (result == NULL) return 0;
    if (result == py_NotImplemented) {
        py_decref(result);
        return -1;
    }
    int64_t truth = py_obj_truthy(result);
    py_decref(result);
    return truth ? 1 : 0;
}

PyObject *py_user_getitem_dispatch(PyObject *o, PyObject *key) {
    PyObject *method = lookup_dunder(o, "__getitem__");
    if (method == NULL) {
        /* No user __getitem__: a dict subclass still supports item access via
         * its inherited dict.__getitem__ (backing dict + __missing__). */
        if (class_is_dict_subclass(o)) {
            return py_dict_subclass_getitem(o, key);
        }
        return NULL;
    }
    return call_binary(method, o, key);
}

PyObject *py_user_matmul_dispatch(PyObject *a, PyObject *b) {
    PyObject *method = lookup_dunder(a, "__matmul__");
    if (method != NULL) {
        PyObject *result = call_binary(method, a, b);
        if (result != py_NotImplemented) return result;
        py_decref(result);
    }
    method = lookup_dunder(b, "__rmatmul__");
    if (method != NULL) {
        PyObject *result = call_binary(method, b, a);
        if (result != py_NotImplemented) return result;
        py_decref(result);
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "unsupported operand type(s) for @"));
    return NULL;
}

/* Generic user binary-dunder dispatch (J family: __add__/__radd__,
 * __sub__/__rsub__, __mul__/__rmul__, ...). Mirrors the matmul shape:
 * try a.<name>(b); on NotImplemented try b.<rname>(a); else raise the
 * caller-provided TypeError message (a static string literal — it is
 * NOT copied defensively here). A NULL from call_binary means the user
 * dunder raised: the pending exception propagates as-is. */
PyObject *py_user_binop_dispatch(
    PyObject *a,
    PyObject *b,
    const char *name,
    const char *rname,
    const char *type_err_msg
) {
    PyObject *method = lookup_dunder(a, name);
    if (method != NULL) {
        PyObject *result = call_binary(method, a, b);
        if (result != py_NotImplemented) return result;
        py_decref(result);
    }
    method = lookup_dunder(b, rname);
    if (method != NULL) {
        PyObject *result = call_binary(method, b, a);
        if (result != py_NotImplemented) return result;
        py_decref(result);
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, type_err_msg));
    return NULL;
}

/* Generic ``a // b`` for dynamically-typed operands (C-only helper;
 * the Dyn `//` emission used to coerce instances through the i64 fast
 * path — silently wrong values). int/bool pairs keep Python floor
 * semantics via py_int_floordiv; any-float numeric pairs floor the
 * double quotient; instances dispatch __floordiv__/__rfloordiv__. */
PyObject *py_obj_floordiv(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) {
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR, "unsupported operand type(s) for //"));
        return NULL;
    }
    int32_t at = py_type_of(a);
    int32_t bt = py_type_of(b);
    if (
        (at == PY_TYPE_INT || at == PY_TYPE_BOOL)
        && (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL)
    ) {
        return py_int_floordiv(a, b);
    }
    int a_num = (at == PY_TYPE_INT || at == PY_TYPE_BOOL
                 || at == PY_TYPE_FLOAT);
    int b_num = (bt == PY_TYPE_INT || bt == PY_TYPE_BOOL
                 || bt == PY_TYPE_FLOAT);
    if (a_num && b_num) {
        double bd = py_float_to_f64(b);
        if (bd == 0.0) {
            py_raise_owned(py_exc_new(
                PY_EXC_ZERODIVISIONERROR, "float floor division by zero"));
            return NULL;
        }
        return py_float_from_f64(floor(py_float_to_f64(a) / bd));
    }
    if (at == PY_TYPE_INSTANCE || at >= PY_TYPE_USER_CLASS_START
        || bt == PY_TYPE_INSTANCE || bt >= PY_TYPE_USER_CLASS_START) {
        return py_user_binop_dispatch(
            a, b, "__floordiv__", "__rfloordiv__",
            "unsupported operand type(s) for //");
    }
    py_raise_owned(py_exc_new(
        PY_EXC_TYPEERROR, "unsupported operand type(s) for //"));
    return NULL;
}

/* Augmented-assignment dispatch (a += b and friends): CPython tries
 * type(a).__iop__ FIRST (NotImplemented falls through), then the
 * plain binary protocol. op_code: 0:+ 1:- 2:* 3:/ 4:// 5:% */
PyObject *py_obj_inplace_op(PyObject *a, PyObject *b, int64_t op_code) {
    static const char *inames[6] = {
        "__iadd__", "__isub__", "__imul__",
        "__itruediv__", "__ifloordiv__", "__imod__",
    };
    if (a != NULL && !PY_IS_TAGGED_INT(a) && op_code >= 0 && op_code < 6) {
        int32_t at = py_type_of(a);
        if (pcc_capi_is_cext_type_tag((int64_t)at) != 0) {
            PyObject *result = pcc_capi_cext_inplace_number(a, b, op_code);
            if (result == NULL) return NULL;
            if (result != py_NotImplemented) return result;
            py_decref(result);
        } else if (is_user_instance(a)) {
            PyObject *method = lookup_dunder(a, inames[op_code]);
            if (method != NULL) {
                PyObject *result = call_binary(method, a, b);
                if (result != py_NotImplemented) return result;
                py_decref(result);
            }
        }
    }
    switch (op_code) {
        case 0: return py_obj_add(a, b);
        case 1: return py_obj_sub(a, b);
        case 2: return py_obj_mul(a, b);
        case 3: return py_obj_truediv(a, b);
        case 4: return py_obj_floordiv(a, b);
        case 5: return py_obj_mod(a, b);
        default:
            py_raise_owned(py_exc_new(
                PY_EXC_TYPEERROR, "unsupported in-place operand"));
            return NULL;
    }
}

int64_t py_user_setitem_dispatch(PyObject *o, PyObject *key, PyObject *value,
                                 int64_t *handled) {
    if (handled) *handled = 0;
    PyObject *method = lookup_dunder(o, "__setitem__");
    if (method == NULL) {
        /* No user __setitem__: a dict subclass stores into its backing dict
         * (inherited dict.__setitem__). */
        if (class_is_dict_subclass(o)) {
            PyObject *d = dict_subclass_backing(o, 1);
            if (d == NULL) return -1;
            py_dict_set(d, key, value);
            if (handled) *handled = 1;
            return py_err_occurred() ? -1 : 0;
        }
        return -1;
    }
    if (handled) *handled = 1;
    PyObject *result = call_ternary(method, o, key, value);
    if (result == NULL) return -1;
    py_decref(result);
    return 0;
}

int64_t py_user_delitem_dispatch(PyObject *o, PyObject *key,
                                 int64_t *handled) {
    if (handled) *handled = 0;
    PyObject *method = lookup_dunder(o, "__delitem__");
    if (method == NULL) {
        /* No user __delitem__: a dict subclass deletes from its backing dict
         * (inherited dict.__delitem__). */
        if (class_is_dict_subclass(o)) {
            PyObject *d = dict_subclass_backing(o, 0);
            if (handled) *handled = 1;
            if (d == NULL) {
                py_raise_owned(py_exc_new(PY_EXC_KEYERROR, "key not found"));
                return -1;
            }
            return py_dict_del(d, key);
        }
        return -1;
    }
    if (handled) *handled = 1;
    PyObject *result = call_binary(method, o, key);
    if (result == NULL) return -1;
    py_decref(result);
    return 0;
}

/* ====================================================================== *
 * dict-subclass inherited behavior.
 *
 * A user class that subclasses the builtin ``dict`` (e.g. collections.Counter,
 * OrderedDict, defaultdict) is created by the frontend as a plain
 * PyInstanceObject: the ``dict`` base is not a user PyClassObject, so it is
 * dropped from the native MRO and none of dict's item storage / methods are
 * inherited. Without a fallback, ``self[k] = v`` (no user __setitem__),
 * ``self.get(k, d)`` and iteration over ``self`` all fail with TypeError /
 * AttributeError inside the pcc1-compiled stdlib.
 *
 * The frontend marks such classes with PY_CLASS_FLAG_DICT_SUBCLASS (bit 2).
 * Item storage lives in a dedicated dict held UNDER A RESERVED KEY inside the
 * instance's existing ``__dict__`` (dynamic-attribute) dict, so dict items and
 * ordinary instance attributes (e.g. defaultdict.default_factory, or the
 * synthetic ``args`` a foreign super().__init__() may store) never collide.
 * The reserved key ("\x00pcc.dict.items") is not a valid Python identifier and
 * is skipped by attribute access. User __getitem__ / __setitem__ overrides
 * still win because py_user_getitem_dispatch / py_user_setitem_dispatch consult
 * the user MRO first; the backing dict is only used when the user class defined
 * no override.
 * ====================================================================== */

#define PY_CLASS_FLAG_SLOTS_ONLY 2
#define PY_CLASS_FLAG_DICT_SUBCLASS 4

/* Reserved __dict__ key under which the dict-subclass item storage lives. The
 * leading NUL guarantees it can never be produced as a normal attribute name
 * (attribute names are NUL-terminated C strings) so items and attrs stay
 * separated even though both live in the one instance __dict__. */
static const char PCC_DICT_ITEMS_KEY[] = "\x00pcc.dict.items";
#define PCC_DICT_ITEMS_KEY_LEN ((int64_t)(sizeof(PCC_DICT_ITEMS_KEY) - 1))

static int class_is_dict_subclass(PyObject *o) {
    if (!is_user_instance(o)) return 0;
    PyInstanceObject *inst = (PyInstanceObject *)o;
    PyClassObject *cls = inst->cls;
    if (cls == NULL) return 0;
    return (cls->h.flags & PY_CLASS_FLAG_DICT_SUBCLASS) != 0;
}

/* Return the instance's __dict__ (dynamic-attribute) dict, materializing it if
 * necessary. Mirrors dynamic_attr_slot() in py_class.c. Returns a BORROWED
 * reference owned by the instance, or NULL. */
static PyObject *dict_subclass_env(PyObject *o, int create) {
    if (!class_is_dict_subclass(o)) return NULL;
    PyInstanceObject *inst = (PyInstanceObject *)o;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)inst, (PyObject **)&inst->cls);
    if (cls == NULL) return NULL;
    if ((cls->h.flags & PY_CLASS_FLAG_SLOTS_ONLY) != 0) return NULL;
    int32_t n_fields = cls->n_fields;
    if (n_fields < 0) n_fields = 0;
    PyObject **slot = &inst->fields[n_fields];
    PyObject *env = pcc_gc_load_ptr(o, slot);
    if (env == NULL && create) {
        env = py_dict_new();
        if (env == NULL) return NULL;
        pcc_gc_store_ptr(o, slot, env);
        py_decref(env);  /* slot now owns it */
        env = pcc_gc_load_ptr(o, slot);
    }
    return env;
}

/* Fetch the dict-subclass item-storage dict (lives under PCC_DICT_ITEMS_KEY in
 * the instance __dict__), materializing an empty one on first use when
 * ``create`` is set. Returns a BORROWED reference, or NULL. */
static PyObject *dict_subclass_backing(PyObject *o, int create) {
    PyObject *env = dict_subclass_env(o, create);
    if (env == NULL) return NULL;
    PyObject *key = py_str_new(PCC_DICT_ITEMS_KEY, PCC_DICT_ITEMS_KEY_LEN);
    if (key == NULL) return NULL;
    PyObject *d = py_dict_get(env, key);  /* owned or NULL */
    if (d == NULL && create) {
        d = py_dict_new();
        if (d == NULL) { py_decref(key); return NULL; }
        py_dict_set(env, key, d);   /* env now holds a ref */
        py_decref(key);
        py_decref(d);               /* drop our creation ref; env owns it */
        return dict_subclass_backing(o, 0);  /* re-fetch as borrowed */
    }
    py_decref(key);
    if (d != NULL) py_decref(d);    /* normalize to borrowed (env owns it) */
    return d;
}

/* --- native bound-method entries -----------------------------------------
 * Each entry receives ``captures = (instance,)`` and the positional call args
 * tuple. They operate on the instance's backing dict. Returned refs follow the
 * runtime convention (owned). */

static PyObject *dictsub_get_entry(PyObject *captures, PyObject *args) {
    PyObject *self = py_tuple_get(captures, 0);
    if (self == NULL) return NULL;
    int64_t nargs = (args != NULL && !PY_IS_TAGGED_INT(args)
                     && py_type_of(args) == PY_TYPE_TUPLE)
                        ? py_tuple_len(args) : 0;
    PyObject *key = nargs >= 1 ? py_tuple_get(args, 0) : NULL;
    PyObject *dflt = nargs >= 2 ? py_tuple_get(args, 1) : py_None;
    if (nargs < 2) py_incref(dflt);  /* borrow py_None -> normalize to owned */
    PyObject *d = dict_subclass_backing(self, 0);
    PyObject *out;
    if (d == NULL) {
        out = dflt;         /* empty backing -> default */
        py_incref(out);
    } else {
        out = py_dict_get_default(d, key, dflt);
    }
    py_decref(self);
    if (key != NULL) py_decref(key);
    py_decref(dflt);
    return out;
}

static PyObject *dictsub_keys_entry(PyObject *captures, PyObject *args) {
    (void)args;
    PyObject *self = py_tuple_get(captures, 0);
    if (self == NULL) return NULL;
    PyObject *d = dict_subclass_backing(self, 1);
    PyObject *out = d != NULL ? py_dict_keys(d) : NULL;
    py_decref(self);
    return out;
}

static PyObject *dictsub_values_entry(PyObject *captures, PyObject *args) {
    (void)args;
    PyObject *self = py_tuple_get(captures, 0);
    if (self == NULL) return NULL;
    PyObject *d = dict_subclass_backing(self, 1);
    PyObject *out = d != NULL ? py_dict_values(d) : NULL;
    py_decref(self);
    return out;
}

static PyObject *dictsub_items_entry(PyObject *captures, PyObject *args) {
    (void)args;
    PyObject *self = py_tuple_get(captures, 0);
    if (self == NULL) return NULL;
    PyObject *d = dict_subclass_backing(self, 1);
    PyObject *out = d != NULL ? py_dict_items(d) : NULL;
    py_decref(self);
    return out;
}

static PyObject *dictsub_pop_entry(PyObject *captures, PyObject *args) {
    PyObject *self = py_tuple_get(captures, 0);
    if (self == NULL) return NULL;
    int64_t nargs = (args != NULL && !PY_IS_TAGGED_INT(args)
                     && py_type_of(args) == PY_TYPE_TUPLE)
                        ? py_tuple_len(args) : 0;
    PyObject *key = nargs >= 1 ? py_tuple_get(args, 0) : NULL;
    PyObject *d = dict_subclass_backing(self, 0);
    PyObject *existing = (d != NULL && key != NULL) ? py_dict_get(d, key) : NULL;
    PyObject *out;
    if (existing != NULL) {
        out = existing;              /* py_dict_get returns owned */
        py_dict_del(d, key);
    } else if (nargs >= 2) {
        out = py_tuple_get(args, 1); /* default */
    } else {
        py_raise_owned(py_exc_new(PY_EXC_KEYERROR, "pop(): key not found"));
        out = NULL;
    }
    py_decref(self);
    if (key != NULL) py_decref(key);
    return out;
}

static PyObject *dictsub_setdefault_entry(PyObject *captures, PyObject *args) {
    PyObject *self = py_tuple_get(captures, 0);
    if (self == NULL) return NULL;
    int64_t nargs = (args != NULL && !PY_IS_TAGGED_INT(args)
                     && py_type_of(args) == PY_TYPE_TUPLE)
                        ? py_tuple_len(args) : 0;
    PyObject *key = nargs >= 1 ? py_tuple_get(args, 0) : NULL;
    PyObject *dflt = nargs >= 2 ? py_tuple_get(args, 1) : py_None;
    if (nargs < 2) py_incref(dflt);
    PyObject *d = dict_subclass_backing(self, 1);
    PyObject *out = NULL;
    if (d != NULL && key != NULL) {
        PyObject *existing = py_dict_get(d, key);
        if (existing != NULL) {
            out = existing;
        } else {
            py_dict_set(d, key, dflt);
            out = dflt;
            py_incref(out);
        }
    }
    py_decref(self);
    if (key != NULL) py_decref(key);
    py_decref(dflt);
    return out;
}

static PyObject *dictsub_clear_entry(PyObject *captures, PyObject *args) {
    (void)args;
    PyObject *self = py_tuple_get(captures, 0);
    if (self == NULL) return NULL;
    PyObject *env = dict_subclass_env(self, 0);
    if (env != NULL) {
        PyObject *key = py_str_new(PCC_DICT_ITEMS_KEY, PCC_DICT_ITEMS_KEY_LEN);
        if (key != NULL) {
            PyObject *fresh = py_dict_new();
            if (fresh != NULL) {
                py_dict_set(env, key, fresh);  /* replace item storage */
                py_decref(fresh);
            }
            py_decref(key);
        }
    }
    py_decref(self);
    py_incref(py_None);
    return py_None;
}

/* Route a dict-subclass method name to a freshly bound native callable, or
 * NULL when the name is not an inherited dict method (caller then continues to
 * __getattr__ / AttributeError). Called from py_instance_getattr_default. */
PyObject *py_dict_subclass_getattr(PyObject *o, const char *name) {
    if (!class_is_dict_subclass(o) || name == NULL) return NULL;
    void *entry = NULL;
    if (strcmp(name, "get") == 0)             entry = (void *)dictsub_get_entry;
    else if (strcmp(name, "keys") == 0)       entry = (void *)dictsub_keys_entry;
    else if (strcmp(name, "values") == 0)     entry = (void *)dictsub_values_entry;
    else if (strcmp(name, "items") == 0)      entry = (void *)dictsub_items_entry;
    else if (strcmp(name, "pop") == 0)        entry = (void *)dictsub_pop_entry;
    else if (strcmp(name, "setdefault") == 0) entry = (void *)dictsub_setdefault_entry;
    else if (strcmp(name, "clear") == 0)      entry = (void *)dictsub_clear_entry;
    if (entry == NULL) return NULL;
    PyObject *cap = py_tuple_new(1);
    if (cap == NULL) return NULL;
    py_tuple_set_item(cap, 0, o);   /* increfs o */
    PyObject *fn = py_func_new_named(entry, cap, name);
    py_decref(cap);
    return fn;
}

/* dict-subclass item read: try the backing dict, then the class __missing__,
 * else KeyError. This is the inherited ``dict.__getitem__`` a subclass gets
 * when it does NOT define its own __getitem__. Returns owned / NULL. */
PyObject *py_dict_subclass_getitem(PyObject *o, PyObject *key) {
    PyObject *d = dict_subclass_backing(o, 0);
    if (d != NULL && key != NULL) {
        PyObject *v = py_dict_get(d, key);   /* owned or NULL */
        if (v != NULL) return v;
    }
    PyObject *missing = lookup_dunder(o, "__missing__");
    if (missing != NULL) {
        return call_binary(missing, o, key);
    }
    py_raise_owned(py_exc_new(PY_EXC_KEYERROR, "key not found"));
    return NULL;
}
