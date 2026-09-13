/* Host-C oracle for class-level variable storage.
 *
 * Production libpy_runtime_pcc_py.a owns these ABIs in py/py_class.py.
 *
 * Keep class variables out of PyClassObject's method table.  PyClassObject
 * now carries an owned attrs dict slot so moving collectors can trace and
 * update the edge directly.  The side table remains only as a pointer-keyed
 * index for older lookup paths; it does not own the dict.
 */

#include "py_internal.h"
#include <stdlib.h>
#include <string.h>

#define PCC_FUNC_SIGNATURE_MAGIC "__pcc_func_signature_v1__"

extern int32_t py_class_attr_cache_epoch;

typedef struct PccClassAttrsNode {
    PyClassObject *cls;
    PyObject *attrs;
    struct PccClassAttrsNode *next;
} PccClassAttrsNode;

static PccClassAttrsNode *pcc_class_attrs_head = NULL;

static PyObject *pcc_class_attrs_require_result(
    PyObject *result,
    const char *helper_name,
    const char *message
) {
    if (result == NULL) {
        py_runtime_error_if_unset(helper_name, message);
    }
    return result;
}

static int pcc_class_attrs_is_class(PyClassObject *cls) {
    if (cls == NULL) return 0;
    if (PY_IS_TAGGED_INT((PyObject *)cls)) return 0;
    return py_type_of((PyObject *)cls) == PY_TYPE_CLASS;
}

static PccClassAttrsNode *pcc_class_attrs_find(PyClassObject *cls) {
    for (PccClassAttrsNode *n = pcc_class_attrs_head; n != NULL; n = n->next) {
        if (n->cls == cls) return n;
    }
    return NULL;
}

static PyObject *pcc_class_attrs_sync(
    PyClassObject *cls,
    PccClassAttrsNode *node
) {
    if (!pcc_class_attrs_is_class(cls)) return NULL;
    PyObject *attrs = cls->attrs;
    if (attrs != NULL) {
        attrs = pcc_gc_load_ptr((PyObject *)cls, &cls->attrs);
    }
    if (node != NULL) node->attrs = attrs;
    return attrs;
}

static PyObject *pcc_class_attrs_call_pyfunc_bound_args(
    PyObject *func,
    PyObject *bound_args
);
static int pcc_class_attrs_pointer_can_have_header(void *ptr);

static PyObject *pcc_classmethod_bound_entry(
    PyObject *captures,
    PyObject *args
) {
    PyObject *func = py_tuple_get(captures, 0);
    PyObject *cls = py_tuple_get(captures, 1);
    if (func == NULL || cls == NULL) {
        if (func != NULL) py_decref(func);
        if (cls != NULL) py_decref(cls);
        return NULL;
    }
    int64_t n_args = py_tuple_len(args);
    PyObject *full_args = py_tuple_new(n_args + 1);
    if (full_args == NULL) {
        pcc_class_attrs_require_result(
            NULL,
            "py_tuple_new",
            "class callback argument tuple allocation failed"
        );
        py_decref(func);
        py_decref(cls);
        return NULL;
    }
    py_tuple_set_item(full_args, 0, cls);
    for (int64_t i = 0; i < n_args; i++) {
        PyObject *arg = py_tuple_get(args, i);
        if (arg == NULL) {
            pcc_class_attrs_require_result(
                NULL,
                "py_tuple_get",
                "class callback argument lookup failed"
            );
            py_decref(full_args);
            py_decref(func);
            py_decref(cls);
            return NULL;
        }
        py_tuple_set_item(full_args, i + 1, arg);
        py_decref(arg);
    }
    PyObject *out = NULL;
    if (
        pcc_class_attrs_pointer_can_have_header(func) &&
        !PY_IS_TAGGED_INT(func) &&
        py_type_of(func) == PY_TYPE_FUNC
    ) {
        out = pcc_class_attrs_call_pyfunc_bound_args(func, full_args);
    } else {
        out = py_obj_call(func, full_args, py_None);
    }
    pcc_class_attrs_require_result(
        out,
        "class callback",
        "class callback returned NULL without setting an exception"
    );
    py_decref(full_args);
    py_decref(func);
    py_decref(cls);
    return out;
}

static int pcc_class_attrs_pointer_can_have_header(void *ptr) {
    return pcc_gc_pointer_is_managed((PyObject *)ptr) != 0;
}

static int pcc_class_attrs_func_signature_valid(PyObject *sig) {
    if (sig == NULL || PY_IS_TAGGED_INT(sig)) return 0;
    if (py_type_of(sig) != PY_TYPE_TUPLE) return 0;
    if (py_tuple_len(sig) < 5) return 0;

    PyObject *magic = py_tuple_get(sig, 0);
    if (magic == NULL) return 0;
    PyObject *expected = py_str_new(
        PCC_FUNC_SIGNATURE_MAGIC,
        (int64_t)strlen(PCC_FUNC_SIGNATURE_MAGIC)
    );
    int ok = expected != NULL && py_str_eq(magic, expected) != 0;
    py_decref(magic);
    if (expected != NULL) py_decref(expected);
    return ok;
}

static PyObject *pcc_class_attrs_func_signature(PyObject *func) {
    if (func == NULL || PY_IS_TAGGED_INT(func)) return NULL;
    if (py_type_of(func) != PY_TYPE_FUNC) return NULL;
    PyObject *captures = pcc_gc_load_ptr(
        func,
        &((PyFuncObject *)func)->captures
    );
    if (captures == NULL || PY_IS_TAGGED_INT(captures)) return NULL;
    if (py_type_of(captures) != PY_TYPE_TUPLE || py_tuple_len(captures) != 2) {
        return NULL;
    }
    PyObject *candidate = py_tuple_get(captures, 1);
    if (!pcc_class_attrs_func_signature_valid(candidate)) {
        if (candidate != NULL) py_decref(candidate);
        return NULL;
    }
    return candidate;
}

static PyObject *pcc_class_attrs_bound_signature(PyObject *func) {
    PyObject *sig = pcc_class_attrs_func_signature(func);
    if (sig == NULL) return NULL;

    PyObject *names = py_tuple_get(sig, 1);
    PyObject *kinds = py_tuple_get(sig, 2);
    PyObject *has_defaults = py_tuple_get(sig, 3);
    PyObject *defaults = py_tuple_get(sig, 4);
    if (
        names == NULL || kinds == NULL ||
        has_defaults == NULL || defaults == NULL
    ) {
        if (names != NULL) py_decref(names);
        if (kinds != NULL) py_decref(kinds);
        if (has_defaults != NULL) py_decref(has_defaults);
        if (defaults != NULL) py_decref(defaults);
        py_decref(sig);
        return NULL;
    }

    int64_t n = py_tuple_len(names);
    if (
        n <= 0 ||
        py_tuple_len(kinds) != n ||
        py_tuple_len(has_defaults) != n ||
        py_tuple_len(defaults) != n
    ) {
        py_decref(names);
        py_decref(kinds);
        py_decref(has_defaults);
        py_decref(defaults);
        py_decref(sig);
        return NULL;
    }

    PyObject *out_names = py_tuple_new(n - 1);
    PyObject *out_kinds = py_tuple_new(n - 1);
    PyObject *out_has_defaults = py_tuple_new(n - 1);
    PyObject *out_defaults = py_tuple_new(n - 1);
    PyObject *out_sig = py_tuple_new(5);
    if (
        out_names == NULL || out_kinds == NULL ||
        out_has_defaults == NULL || out_defaults == NULL || out_sig == NULL
    ) {
        if (out_names != NULL) py_decref(out_names);
        if (out_kinds != NULL) py_decref(out_kinds);
        if (out_has_defaults != NULL) py_decref(out_has_defaults);
        if (out_defaults != NULL) py_decref(out_defaults);
        if (out_sig != NULL) py_decref(out_sig);
        py_decref(names);
        py_decref(kinds);
        py_decref(has_defaults);
        py_decref(defaults);
        py_decref(sig);
        return NULL;
    }

    for (int64_t i = 1; i < n; i++) {
        PyObject *name = py_tuple_get(names, i);
        PyObject *kind = py_tuple_get(kinds, i);
        PyObject *has_default = py_tuple_get(has_defaults, i);
        PyObject *default_obj = py_tuple_get(defaults, i);
        if (
            name == NULL || kind == NULL ||
            has_default == NULL || default_obj == NULL
        ) {
            if (name != NULL) py_decref(name);
            if (kind != NULL) py_decref(kind);
            if (has_default != NULL) py_decref(has_default);
            if (default_obj != NULL) py_decref(default_obj);
            py_decref(out_names);
            py_decref(out_kinds);
            py_decref(out_has_defaults);
            py_decref(out_defaults);
            py_decref(out_sig);
            py_decref(names);
            py_decref(kinds);
            py_decref(has_defaults);
            py_decref(defaults);
            py_decref(sig);
            return NULL;
        }
        int64_t out_i = i - 1;
        py_tuple_set_item(out_names, out_i, name);
        py_tuple_set_item(out_kinds, out_i, kind);
        py_tuple_set_item(out_has_defaults, out_i, has_default);
        py_tuple_set_item(out_defaults, out_i, default_obj);
        py_decref(name);
        py_decref(kind);
        py_decref(has_default);
        py_decref(default_obj);
    }

    PyObject *magic = py_tuple_get(sig, 0);
    if (magic == NULL) {
        py_decref(out_names);
        py_decref(out_kinds);
        py_decref(out_has_defaults);
        py_decref(out_defaults);
        py_decref(out_sig);
        py_decref(names);
        py_decref(kinds);
        py_decref(has_defaults);
        py_decref(defaults);
        py_decref(sig);
        return NULL;
    }
    py_tuple_set_item(out_sig, 0, magic);
    py_tuple_set_item(out_sig, 1, out_names);
    py_tuple_set_item(out_sig, 2, out_kinds);
    py_tuple_set_item(out_sig, 3, out_has_defaults);
    py_tuple_set_item(out_sig, 4, out_defaults);
    py_decref(magic);
    py_decref(out_names);
    py_decref(out_kinds);
    py_decref(out_has_defaults);
    py_decref(out_defaults);
    py_decref(names);
    py_decref(kinds);
    py_decref(has_defaults);
    py_decref(defaults);
    py_decref(sig);
    return out_sig;
}

static PyObject *pcc_class_attrs_wrap_bound_captures(
    PyObject *method,
    PyObject *captures
) {
    PyObject *signature = pcc_class_attrs_bound_signature(method);
    if (signature == NULL) return captures;
    PyObject *wrapped = py_tuple_new(2);
    if (wrapped == NULL) {
        py_decref(signature);
        return captures;
    }
    py_tuple_set_item(wrapped, 0, captures);
    py_tuple_set_item(wrapped, 1, signature);
    py_decref(signature);
    return wrapped;
}

static PyObject *pcc_class_attrs_call_pyfunc_bound_args(
    PyObject *func,
    PyObject *bound_args
) {
    if (func == NULL || PY_IS_TAGGED_INT(func)) return NULL;
    if (py_type_of(func) != PY_TYPE_FUNC) return NULL;
    PyFuncObject *f = (PyFuncObject *)func;
    if (f->entry == NULL) return NULL;
    PyObject *captures = pcc_gc_load_ptr(func, &f->captures);
    PyObject *actual_captures = captures;
    int owns_actual = 0;
    if (
        captures != NULL &&
        !PY_IS_TAGGED_INT(captures) &&
        py_type_of(captures) == PY_TYPE_TUPLE &&
        py_tuple_len(captures) == 2
    ) {
        PyObject *candidate = py_tuple_get(captures, 1);
        if (pcc_class_attrs_func_signature_valid(candidate)) {
            PyObject *inner = py_tuple_get(captures, 0);
            if (inner != NULL) {
                actual_captures = inner;
                owns_actual = 1;
            }
        }
        if (candidate != NULL) py_decref(candidate);
    }
    PyObject *out = f->entry(actual_captures, bound_args);
    if (owns_actual) py_decref(actual_captures);
    return out;
}

static PyObject *pcc_instance_bound_method_entry(
    PyObject *captures,
    PyObject *args
) {
    PyObject *func = py_tuple_get(captures, 0);
    PyObject *self = py_tuple_get(captures, 1);
    if (func == NULL || self == NULL) {
        if (func != NULL) py_decref(func);
        if (self != NULL) py_decref(self);
        return NULL;
    }
    int64_t n_args = py_tuple_len(args);
    if (pcc_class_attrs_pointer_can_have_header(func)
        && !PY_IS_TAGGED_INT(func)
        && py_type_of(func) == PY_TYPE_FUNC) {
        PyObject *full_args = py_tuple_new(n_args + 1);
        if (full_args == NULL) {
            pcc_class_attrs_require_result(
                NULL,
                "py_tuple_new",
                "class callback argument tuple allocation failed"
            );
            py_decref(func);
            py_decref(self);
            return NULL;
        }
        py_tuple_set_item(full_args, 0, self);
        for (int64_t i = 0; i < n_args; i++) {
            PyObject *arg = py_tuple_get(args, i);
            if (arg == NULL) {
                pcc_class_attrs_require_result(
                    NULL,
                    "py_tuple_get",
                    "class callback argument lookup failed"
                );
                py_decref(full_args);
                py_decref(func);
                py_decref(self);
                return NULL;
            }
            py_tuple_set_item(full_args, i + 1, arg);
            py_decref(arg);
        }
        PyObject *out = pcc_class_attrs_call_pyfunc_bound_args(func, full_args);
        pcc_class_attrs_require_result(
            out,
            "class callback",
            "class callback returned NULL without setting an exception"
        );
        py_decref(full_args);
        py_decref(func);
        py_decref(self);
        return out;
    }
    PyObject *out = NULL;
    if (n_args == 0) {
        typedef PyObject *(*UnaryMethod)(PyObject *);
        UnaryMethod meth = (UnaryMethod)(uintptr_t)func;
        out = meth(self);
    } else if (n_args == 1) {
        PyObject *arg0 = py_tuple_get(args, 0);
        if (arg0 != NULL) {
            typedef PyObject *(*BinaryMethod)(PyObject *, PyObject *);
            BinaryMethod meth = (BinaryMethod)(uintptr_t)func;
            out = meth(self, arg0);
            py_decref(arg0);
        }
    } else if (n_args == 2) {
        PyObject *arg0 = py_tuple_get(args, 0);
        PyObject *arg1 = py_tuple_get(args, 1);
        if (arg0 != NULL && arg1 != NULL) {
            typedef PyObject *(*TernaryMethod)(PyObject *, PyObject *, PyObject *);
            TernaryMethod meth = (TernaryMethod)(uintptr_t)func;
            out = meth(self, arg0, arg1);
        }
        if (arg0 != NULL) py_decref(arg0);
        if (arg1 != NULL) py_decref(arg1);
    } else if (n_args == 3) {
        /* ``__exit__(self, exc_type, exc, tb)`` is the most common
         * 3-arg method bound this way. Without this branch the
         * fall-through leaves ``out == NULL`` and callers report a
         * spurious "exit returned NULL" failure even when the underlying
         * method returns a real value. */
        PyObject *arg0 = py_tuple_get(args, 0);
        PyObject *arg1 = py_tuple_get(args, 1);
        PyObject *arg2 = py_tuple_get(args, 2);
        if (arg0 != NULL && arg1 != NULL && arg2 != NULL) {
            typedef PyObject *(*QuaternaryMethod)(
                PyObject *, PyObject *, PyObject *, PyObject *);
            QuaternaryMethod meth = (QuaternaryMethod)(uintptr_t)func;
            out = meth(self, arg0, arg1, arg2);
        }
        if (arg0 != NULL) py_decref(arg0);
        if (arg1 != NULL) py_decref(arg1);
        if (arg2 != NULL) py_decref(arg2);
    }
    pcc_class_attrs_require_result(
        out,
        "class callback",
        "class callback returned NULL without setting an exception"
    );
    py_decref(func);
    py_decref(self);
    return out;
}

PyObject *py_instance_bind_method(PyObject *method, PyObject *self, const char *name) {
    if (method == NULL || self == NULL) return NULL;
    PyObject *captures = py_tuple_new(2);
    if (captures == NULL) return NULL;
    py_tuple_set_item(captures, 0, method);
    py_tuple_set_item(captures, 1, self);
    const char *bound_name = name;
    if (pcc_class_attrs_pointer_can_have_header(method)
        && !PY_IS_TAGGED_INT(method)
        && py_type_of(method) == PY_TYPE_FUNC) {
        PyFuncObject *func = (PyFuncObject *)method;
        if (func->name != NULL) bound_name = func->name;
    }
    PyObject *bound_captures = pcc_class_attrs_wrap_bound_captures(
        method,
        captures
    );
    PyObject *bound = py_func_new_bound(
        (void *)pcc_instance_bound_method_entry,
        bound_captures,
        bound_name,
        self
    );
    if (bound_captures != captures) py_decref(bound_captures);
    py_decref(captures);
    return bound;
}

static PyObject *pcc_class_attrs_call_ternary_method(
    PyObject *func,
    PyObject *self,
    PyObject *arg0,
    PyObject *arg1
) {
    if (func == NULL) return NULL;
    if (pcc_class_attrs_pointer_can_have_header(func)
        && !PY_IS_TAGGED_INT(func)
        && py_type_of(func) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(3);
        if (args == NULL) {
            return pcc_class_attrs_require_result(
                NULL,
                "py_tuple_new",
                "class callback argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        py_tuple_set_item(args, 1, arg0);
        py_tuple_set_item(args, 2, arg1);
        PyObject *out = py_func_call(func, args);
        pcc_class_attrs_require_result(
            out,
            "class callback",
            "class callback returned NULL without setting an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*TernaryMethod)(PyObject *, PyObject *, PyObject *);
    TernaryMethod meth = (TernaryMethod)(uintptr_t)func;
    return pcc_class_attrs_require_result(
        meth(self, arg0, arg1),
        "class callback",
        "class callback returned NULL without setting an exception"
    );
}

static PyObject *pcc_class_attrs_call_unary_callable(
    PyObject *func,
    PyObject *arg0
) {
    if (func == NULL) return NULL;
    PyObject *args = py_tuple_new(1);
    if (args == NULL) {
        return pcc_class_attrs_require_result(
            NULL,
            "py_tuple_new",
            "class callback argument tuple allocation failed"
        );
    }
    py_tuple_set_item(args, 0, arg0);
    PyObject *out = py_obj_call(func, args, py_None);
    pcc_class_attrs_require_result(
        out,
        "class callback",
        "class callback returned NULL without setting an exception"
    );
    py_decref(args);
    return out;
}

static PyObject *pcc_class_attrs_call_binary_callable(
    PyObject *func,
    PyObject *arg0,
    PyObject *arg1
) {
    if (func == NULL) return NULL;
    PyObject *args = py_tuple_new(2);
    if (args == NULL) {
        return pcc_class_attrs_require_result(
            NULL,
            "py_tuple_new",
            "class callback argument tuple allocation failed"
        );
    }
    py_tuple_set_item(args, 0, arg0);
    py_tuple_set_item(args, 1, arg1);
    PyObject *out = py_obj_call(func, args, py_None);
    pcc_class_attrs_require_result(
        out,
        "class callback",
        "class callback returned NULL without setting an exception"
    );
    py_decref(args);
    return out;
}

static PyObject *pcc_class_attrs_call_binary_method(
    PyObject *func,
    PyObject *self,
    PyObject *arg0
) {
    if (func == NULL) return NULL;
    if (pcc_class_attrs_pointer_can_have_header(func)
        && !PY_IS_TAGGED_INT(func)
        && py_type_of(func) == PY_TYPE_FUNC) {
        PyObject *args = py_tuple_new(2);
        if (args == NULL) {
            return pcc_class_attrs_require_result(
                NULL,
                "py_tuple_new",
                "class callback argument tuple allocation failed"
            );
        }
        py_tuple_set_item(args, 0, self);
        py_tuple_set_item(args, 1, arg0);
        PyObject *out = py_func_call(func, args);
        pcc_class_attrs_require_result(
            out,
            "class callback",
            "class callback returned NULL without setting an exception"
        );
        py_decref(args);
        return out;
    }
    typedef PyObject *(*BinaryMethod)(PyObject *, PyObject *);
    BinaryMethod meth = (BinaryMethod)(uintptr_t)func;
    return pcc_class_attrs_require_result(
        meth(self, arg0),
        "class callback",
        "class callback returned NULL without setting an exception"
    );
}

PyObject *py_classmethod_new(PyObject *func) {
    if (func == NULL) return NULL;
    PyClassMethodObject *cm = (PyClassMethodObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyClassMethodObject),
        PY_TYPE_CLASSMETHOD,
        0
    );
    if (cm == NULL) return NULL;
    cm->func = NULL;
    pcc_gc_store_ptr((PyObject *)cm, &cm->func, func);
    py_gc_track((PyObject *)cm);
    pcc_gc_publish_initialized((PyObject *)cm);
    return (PyObject *)cm;
}

PyObject *py_staticmethod_new(PyObject *func) {
    if (func == NULL) return NULL;
    PyStaticMethodObject *sm = (PyStaticMethodObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyStaticMethodObject), PY_TYPE_STATICMETHOD, 0
    );
    if (sm == NULL) return NULL;
    sm->func = NULL;
    pcc_gc_store_ptr((PyObject *)sm, &sm->func, func);
    py_gc_track((PyObject *)sm);
    pcc_gc_publish_initialized((PyObject *)sm);
    return (PyObject *)sm;
}

PyObject *py_property_new(PyObject *fget, PyObject *fset, PyObject *fdel) {
    PyPropertyObject *prop = (PyPropertyObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyPropertyObject),
        PY_TYPE_PROPERTY,
        0
    );
    if (prop == NULL) return NULL;
    prop->fget = NULL;
    prop->fset = NULL;
    prop->fdel = NULL;
    if (fget != NULL && fget != py_None) {
        pcc_gc_store_ptr((PyObject *)prop, &prop->fget, fget);
    }
    if (fset != NULL && fset != py_None) {
        pcc_gc_store_ptr((PyObject *)prop, &prop->fset, fset);
    }
    if (fdel != NULL && fdel != py_None) {
        pcc_gc_store_ptr((PyObject *)prop, &prop->fdel, fdel);
    }
    py_gc_track((PyObject *)prop);
    pcc_gc_publish_initialized((PyObject *)prop);
    return (PyObject *)prop;
}

static PyObject *pcc_classmethod_bind(PyObject *descriptor, PyClassObject *cls) {
    if (descriptor == NULL || cls == NULL) return NULL;
    if (PY_IS_TAGGED_INT(descriptor) || py_type_of(descriptor) != PY_TYPE_CLASSMETHOD) {
        return descriptor;
    }
    PyClassMethodObject *cm = (PyClassMethodObject *)descriptor;
    PyObject *func = pcc_gc_load_ptr(descriptor, &cm->func);
    if (func == NULL) return NULL;
    PyObject *captures = py_tuple_new(2);
    if (captures == NULL) return NULL;
    py_tuple_set_item(captures, 0, func);
    py_tuple_set_item(captures, 1, (PyObject *)cls);
    const char *name = NULL;
    if (!PY_IS_TAGGED_INT(func) && py_type_of(func) == PY_TYPE_FUNC) {
        name = ((PyFuncObject *)func)->name;
    }
    PyObject *bound_captures = pcc_class_attrs_wrap_bound_captures(
        func,
        captures
    );
    PyObject *bound = py_func_new_bound(
        (void *)pcc_classmethod_bound_entry,
        bound_captures,
        name,
        (PyObject *)cls
    );
    if (bound_captures != captures) py_decref(bound_captures);
    py_decref(captures);
    return bound;
}

static PyClassObject *pcc_descriptor_instance_class(PyObject *descriptor) {
    if (descriptor == NULL || PY_IS_TAGGED_INT(descriptor)) return NULL;
    int32_t tag = py_type_of(descriptor);
    if (tag != PY_TYPE_INSTANCE && tag < PY_TYPE_USER_CLASS_START) return NULL;
    PyInstanceObject *desc_inst = (PyInstanceObject *)descriptor;
    PyClassObject *cls = (PyClassObject *)pcc_gc_load_ptr(
        descriptor,
        (PyObject **)&desc_inst->cls
    );
    if (!pcc_class_attrs_is_class(cls)) return NULL;
    return cls;
}

static PyObject *pcc_descriptor_method(PyObject *descriptor, const char *name) {
    PyClassObject *desc_cls = pcc_descriptor_instance_class(descriptor);
    if (desc_cls == NULL) return NULL;
    return py_class_lookup(desc_cls, name);
}

static PyObject *pcc_descriptor_call_get(
    PyObject *descriptor,
    PyObject *obj,
    PyClassObject *owner
) {
    if (descriptor != NULL && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_STATICMETHOD) {
        PyObject *func = pcc_gc_load_ptr(
            descriptor, &((PyStaticMethodObject *)descriptor)->func
        );
        py_incref(func);
        return func;
    }
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        PyPropertyObject *prop = (PyPropertyObject *)descriptor;
        PyObject *fget = pcc_gc_load_ptr(descriptor, &prop->fget);
        if (fget == NULL) {
            py_raise_owned(py_exc_new(PY_EXC_ATTRIBUTEERROR, "unreadable attribute"));
            return NULL;
        }
        if (obj == NULL || obj == py_None) {
            py_incref(descriptor);
            return descriptor;
        }
        (void)owner;
        return pcc_class_attrs_call_unary_callable(fget, obj);
    }
    PyObject *get_method = pcc_descriptor_method(descriptor, "__get__");
    if (get_method == NULL) return NULL;
    return pcc_class_attrs_call_ternary_method(
        get_method,
        descriptor,
        obj != NULL ? obj : py_None,
        (PyObject *)owner
    );
}

static int pcc_descriptor_is_data(PyObject *descriptor) {
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        return 1;
    }
    return pcc_descriptor_method(descriptor, "__set__") != NULL
        || pcc_descriptor_method(descriptor, "__delete__") != NULL;
}

static int64_t pcc_descriptor_call_set(
    PyObject *descriptor,
    PyObject *obj,
    PyObject *value
) {
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        PyPropertyObject *prop = (PyPropertyObject *)descriptor;
        PyObject *fset = pcc_gc_load_ptr(descriptor, &prop->fset);
        if (fset == NULL) {
            py_raise_owned(py_exc_new(PY_EXC_ATTRIBUTEERROR, "can't set attribute"));
            return -1;
        }
        PyObject *out = pcc_class_attrs_call_binary_callable(fset, obj, value);
        if (out == NULL) return -1;
        py_decref(out);
        return 0;
    }
    PyObject *set_method = pcc_descriptor_method(descriptor, "__set__");
    if (set_method == NULL) return -1;
    PyObject *out = pcc_class_attrs_call_ternary_method(
        set_method,
        descriptor,
        obj,
        value
    );
    if (out == NULL) return -1;
    py_decref(out);
    return 0;
}

static int64_t pcc_descriptor_call_delete(PyObject *descriptor, PyObject *obj) {
    if (descriptor != NULL
        && !PY_IS_TAGGED_INT(descriptor)
        && py_type_of(descriptor) == PY_TYPE_PROPERTY) {
        PyPropertyObject *prop = (PyPropertyObject *)descriptor;
        PyObject *fdel = pcc_gc_load_ptr(descriptor, &prop->fdel);
        if (fdel == NULL) {
            py_raise_owned(py_exc_new(PY_EXC_ATTRIBUTEERROR, "can't delete attribute"));
            return -1;
        }
        PyObject *out = pcc_class_attrs_call_unary_callable(fdel, obj);
        if (out == NULL) return -1;
        py_decref(out);
        return 0;
    }
    PyObject *delete_method = pcc_descriptor_method(descriptor, "__delete__");
    if (delete_method == NULL) return -1;
    PyObject *out = pcc_class_attrs_call_binary_method(
        delete_method,
        descriptor,
        obj
    );
    if (out == NULL) return -1;
    py_decref(out);
    return 0;
}

static PccClassAttrsNode *pcc_class_attrs_ensure(PyClassObject *cls) {
    if (!pcc_class_attrs_is_class(cls)) return NULL;
    PccClassAttrsNode *existing = pcc_class_attrs_find(cls);
    if (existing != NULL) {
        if (cls->attrs == NULL && existing->attrs != NULL) {
            pcc_gc_store_ptr((PyObject *)cls, &cls->attrs, existing->attrs);
        }
        pcc_class_attrs_sync(cls, existing);
        return existing;
    }

    PyObject *attrs = pcc_class_attrs_sync(cls, NULL);
    if (attrs == NULL) {
        PyObject *created_attrs = py_dict_new();
        if (created_attrs == NULL) return NULL;
        pcc_gc_store_ptr((PyObject *)cls, &cls->attrs, created_attrs);
        py_decref(created_attrs);
        attrs = pcc_class_attrs_sync(cls, NULL);
        if (attrs == NULL) return NULL;
    }

    PccClassAttrsNode *n = (
        PccClassAttrsNode *
    )calloc(1, sizeof(PccClassAttrsNode));
    if (n == NULL) {
        return NULL;
    }
    n->cls = cls;
    n->attrs = attrs;
    n->next = pcc_class_attrs_head;
    pcc_class_attrs_head = n;
    return n;
}

PyObject *py_class_attrs_dict(PyClassObject *cls, int64_t create) {
    if (!pcc_class_attrs_is_class(cls)) return NULL;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    PccClassAttrsNode *n = create ? pcc_class_attrs_ensure(cls) : pcc_class_attrs_find(cls);
    return pcc_class_attrs_sync(cls, n);
}

static PyObject *pcc_class_attr_lookup_in_mro(
    PyClassObject *cls,
    const char *name
) {
    if (!pcc_class_attrs_is_class(cls) || name == NULL) return NULL;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    PyObject *key = py_str_new(name, (int64_t)strlen(name));
    if (key == NULL) return NULL;
    for (int32_t i = 0; i < cls->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)cls,
            (PyObject **)&cls->mro[i]
        );
        if (m == NULL) continue;
        PyObject *attrs = py_class_attrs_dict(m, 0);
        if (attrs == NULL) continue;
        PyObject *value = py_dict_get(attrs, key);
        if (value != NULL) {
            py_decref(key);
            return value;
        }
    }
    py_decref(key);
    return NULL;
}

static PyObject *pcc_metaclass_attr_lookup(
    PyClassObject *cls,
    const char *name,
    PyClassObject **metaclass_out
) {
    if (metaclass_out != NULL) *metaclass_out = NULL;
    if (!pcc_class_attrs_is_class(cls) || name == NULL) return NULL;
    PyClassObject *metaclass = (PyClassObject *)pcc_gc_load_ptr(
        (PyObject *)cls,
        (PyObject **)&cls->metaclass
    );
    if (!pcc_class_attrs_is_class(metaclass)) return NULL;
    metaclass = (PyClassObject *)pcc_gc_note_relocation_read(
        (PyObject *)metaclass
    );
    if (metaclass_out != NULL) *metaclass_out = metaclass;
    return pcc_class_attr_lookup_in_mro(metaclass, name);
}

PyObject *py_class_getattr(PyClassObject *cls, const char *name) {
    if (!pcc_class_attrs_is_class(cls) || name == NULL) return NULL;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    if (strcmp(name, "__dict__") == 0) {
        PyObject *attrs = py_class_attrs_dict(cls, 1);
        if (attrs != NULL) py_incref(attrs);
        return attrs;
    }

    PyClassObject *metaclass = NULL;
    PyObject *meta_attr = pcc_metaclass_attr_lookup(cls, name, &metaclass);
    if (meta_attr != NULL) {
        if (pcc_descriptor_is_data(meta_attr)) {
            PyObject *out = pcc_descriptor_call_get(
                meta_attr,
                (PyObject *)cls,
                metaclass
            );
            py_decref(meta_attr);
            if (out != NULL || py_err_occurred()) return out;
        } else {
            py_decref(meta_attr);
        }
    }

    PyObject *key = py_str_new(name, (int64_t)strlen(name));
    if (key == NULL) return NULL;
    for (int32_t i = 0; i < cls->n_mro; i++) {
        PyClassObject *m = (PyClassObject *)pcc_gc_load_ptr(
            (PyObject *)cls,
            (PyObject **)&cls->mro[i]
        );
        if (m == NULL) continue;
        PyObject *attrs = py_class_attrs_dict(m, 0);
        if (attrs == NULL) continue;
        PyObject *value = py_dict_get(attrs, key);
        if (value != NULL) {
            PyObject *bound = pcc_classmethod_bind(value, cls);
            if (bound != value) {
                py_decref(value);
                py_decref(key);
                return bound;
            }
            PyObject *descriptor_value = pcc_descriptor_call_get(value, py_None, cls);
            if (descriptor_value != NULL || py_err_occurred()) {
                py_decref(value);
                py_decref(key);
                return descriptor_value;
            }
            py_decref(key);
            return bound;
        }
    }
    py_decref(key);
    /* py_class_lookup returns a BORROWED method-table ref, but the attrs-dict
     * branch above and the public py_obj_getattr contract return OWNED. Return
     * owned uniformly: otherwise a caller that releases the getattr result
     * (e.g. the candidate-path CALL lowering) frees the method-table slot,
     * and the next Class.method access reads a dangling slot -> <null>. */
    PyObject *m = py_class_lookup(cls, name);
    if (m != NULL) py_incref(m);
    return m;
}

int64_t py_class_setattr_raw(
    PyClassObject *cls,
    const char *name,
    PyObject *value
) {
    if (!pcc_class_attrs_is_class(cls) || name == NULL || value == NULL) return -1;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    PyObject *attrs = py_class_attrs_dict(cls, 1);
    if (attrs == NULL) return -1;
    PyObject *key = py_str_new(name, (int64_t)strlen(name));
    if (key == NULL) return -1;
    if (strcmp(name, "__del__") == 0) {
        extern int32_t pcc_class_del_defined_count;
        __atomic_add_fetch(&pcc_class_del_defined_count, 1, __ATOMIC_RELEASE);
    }
    py_dict_set(attrs, key, value);
    py_decref(key);
    __atomic_add_fetch(&py_class_attr_cache_epoch, 1, __ATOMIC_RELEASE);
    return 0;
}

int64_t py_class_setattr(PyClassObject *cls, const char *name, PyObject *value) {
    if (!pcc_class_attrs_is_class(cls) || name == NULL || value == NULL) return -1;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    PyClassObject *metaclass = NULL;
    PyObject *meta_attr = pcc_metaclass_attr_lookup(cls, name, &metaclass);
    if (meta_attr != NULL) {
        if (pcc_descriptor_is_data(meta_attr)) {
            int64_t rc = pcc_descriptor_call_set(
                meta_attr,
                (PyObject *)cls,
                value
            );
            py_decref(meta_attr);
            return rc;
        }
        py_decref(meta_attr);
    }
    return py_class_setattr_raw(cls, name, value);
}

int64_t py_class_apply_namespace_dict(PyClassObject *cls, PyObject *ns) {
    if (!pcc_class_attrs_is_class(cls)) return -1;
    if (ns == NULL || PY_IS_TAGGED_INT(ns) || py_type_of(ns) != PY_TYPE_DICT) {
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "type.__new__() argument 3 must be dict"
        ));
        return -1;
    }
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    PyObject *keys = py_dict_keys(ns);
    if (keys == NULL) return -1;
    int64_t n = py_list_len(keys);
    for (int64_t i = 0; i < n; i++) {
        PyObject *key = py_list_get(keys, i);
        if (key == NULL) {
            py_decref(keys);
            return -1;
        }
        const char *name = py_str_utf8(key);
        if (name == NULL) {
            py_decref(key);
            py_decref(keys);
            return -1;
        }
        PyObject *value = py_dict_get(ns, key);
        if (value == NULL) {
            py_decref(key);
            py_decref(keys);
            return -1;
        }
        int64_t rc = py_class_setattr_raw(cls, name, value);
        py_decref(value);
        py_decref(key);
        if (rc != 0) {
            py_decref(keys);
            return rc;
        }
    }
    py_decref(keys);
    return 0;
}

PyObject *py_class_new_from_objects(PyObject *name_obj, PyObject *bases_obj, PyObject *ns) {
    if (name_obj == NULL || PY_IS_TAGGED_INT(name_obj) || py_type_of(name_obj) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "type.__new__() argument 1 must be str"));
        return NULL;
    }
    const char *name = py_str_utf8(name_obj);
    if (name == NULL) return NULL;

    int64_t n64 = 0;
    int is_tuple = 0;
    if (bases_obj == NULL || bases_obj == py_None) {
        n64 = 0;
    } else if (!PY_IS_TAGGED_INT(bases_obj) && py_type_of(bases_obj) == PY_TYPE_TUPLE) {
        n64 = py_tuple_len(bases_obj);
        is_tuple = 1;
    } else if (!PY_IS_TAGGED_INT(bases_obj) && py_type_of(bases_obj) == PY_TYPE_LIST) {
        n64 = py_list_len(bases_obj);
    } else {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "type.__new__() argument 2 must be tuple"));
        return NULL;
    }
    if (n64 < 0 || n64 > 2147483647LL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "too many base classes"));
        return NULL;
    }

    PyClassObject **bases = NULL;
    if (n64 > 0) {
        bases = (PyClassObject **)malloc(sizeof(PyClassObject *) * (size_t)n64);
        if (bases == NULL) return NULL;
        for (int64_t i = 0; i < n64; i++) {
            PyObject *item = is_tuple ? py_tuple_get(bases_obj, i) : py_list_get(bases_obj, i);
            if (!pcc_class_attrs_is_class((PyClassObject *)item)) {
                free(bases);
                if (item != NULL) py_decref(item);
                py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "type.__new__() base must be class"));
                return NULL;
            }
            bases[i] = (PyClassObject *)item;
        }
    }

    PyClassObject *cls = py_class_new(name, bases, (int32_t)n64, NULL, 0);
    free(bases);
    if (cls == NULL) return NULL;
    if (ns != NULL && ns != py_None) {
        if (py_class_apply_namespace_dict(cls, ns) != 0) {
            py_decref((PyObject *)cls);
            return NULL;
        }
    }
    return (PyObject *)cls;
}

int64_t py_class_delattr(PyClassObject *cls, const char *name) {
    if (!pcc_class_attrs_is_class(cls) || name == NULL) return -1;
    cls = (PyClassObject *)pcc_gc_note_relocation_read((PyObject *)cls);
    PyClassObject *metaclass = NULL;
    PyObject *meta_attr = pcc_metaclass_attr_lookup(cls, name, &metaclass);
    if (meta_attr != NULL) {
        if (pcc_descriptor_is_data(meta_attr)) {
            int64_t rc = pcc_descriptor_call_delete(meta_attr, (PyObject *)cls);
            py_decref(meta_attr);
            return rc;
        }
        py_decref(meta_attr);
    }
    PyObject *attrs = py_class_attrs_dict(cls, 0);
    if (attrs == NULL) return -1;
    PyObject *key = py_str_new(name, (int64_t)strlen(name));
    if (key == NULL) return -1;
    int64_t rc = py_dict_del(attrs, key);
    py_decref(key);
    if (rc == 0) {
        __atomic_add_fetch(
            &py_class_attr_cache_epoch, 1, __ATOMIC_RELEASE
        );
    }
    return rc;
}

void py_class_attrs_dispose(PyClassObject *cls) {
    if (cls == NULL) return;
    PyObject *attrs = pcc_class_attrs_sync(cls, pcc_class_attrs_find(cls));
    PccClassAttrsNode **cur = &pcc_class_attrs_head;
    while (*cur != NULL) {
        if ((*cur)->cls == cls) {
            PccClassAttrsNode *dead = *cur;
            *cur = dead->next;
            free(dead);
            break;
        }
        cur = &(*cur)->next;
    }
    if (attrs != NULL) {
        cls->attrs = NULL;
        py_decref(attrs);
    }
}

int64_t py_class_attrs_retarget(PyClassObject *from, PyClassObject *to) {
    if (from == NULL || to == NULL) return -1;
    if (!pcc_class_attrs_is_class(from) || !pcc_class_attrs_is_class(to)) {
        return -1;
    }
    PccClassAttrsNode *node = pcc_class_attrs_find(from);
    if (node == NULL) return 0;
    if (pcc_class_attrs_find(to) != NULL) return -1;
    PyObject *attrs = pcc_class_attrs_sync(from, node);
    if (to->attrs == NULL && attrs != NULL) {
        pcc_gc_store_ptr((PyObject *)to, &to->attrs, attrs);
    }
    node->cls = to;
    pcc_class_attrs_sync(to, node);
    return 0;
}
