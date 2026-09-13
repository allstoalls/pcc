/*
 * py_re_engine_obj.c — PyObject-level bridge for the E0 regex engine core.
 *
 * The core (py_re_engine.c) stays pure C / standalone so the differential
 * test can build it as a dylib; this file owns the runtime-facing wrapper
 * used by the host-C oracle runtime. Production pcc-Python ownership lives in
 * py/py_re_engine_runtime.py; this source must not enter its archive.
 */

#include <stdint.h>
#include <string.h>

#include "py_internal.h"

/* core entry points (py_re_engine.c) */
int pcc_re_engine_supported(const char *pattern);
int pcc_re_engine_supported_flags(const char *pattern, int64_t flags);
int pcc_re_engine_run(const char *pattern, const char *text, int64_t text_len,
                      int is_search, int64_t *caps, int caps_len,
                      int64_t *ngroups_out);
int pcc_re_engine_run_from(const char *pattern, const char *text,
                           int64_t text_len, int64_t start, int is_search,
                           int64_t *caps, int caps_len, int64_t *ngroups_out);
int pcc_re_engine_group_names(const char *pattern, char *out, int out_len);
int pcc_re_engine_group_names_flags(const char *pattern, int64_t flags,
                                    char *out, int out_len);
int pcc_re_engine_run_flags(const char *pattern, int64_t flags,
                            const char *text, int64_t text_len, int64_t start,
                            int is_search, int64_t *caps, int caps_len,
                            int64_t *ngroups_out);

#define PCC_RE_MATCH 1
#define PCC_RE_NOMATCH 0
#define PCC_RE_UNSUPPORTED (-1)
#define PCC_RE_LIMIT (-2)
#define PCC_RE_BADARGS (-3)
#define PCC_RE_NONASCII (-4)

#define PCC_RE_OK_FLAGS (2 | 8 | 16) /* re.I | re.M | re.S */

/* ---------------- E2 match object ---------------- */

/* method kinds encoded into each closure's captures tuple */
#define RE_M_GROUP 0
#define RE_M_START 1
#define RE_M_END 2
#define RE_M_SPAN 3
#define RE_M_GROUPS 4
#define RE_M_GROUPDICT 5

static PyObject *re_match_class_singleton(void) {
    static PyClassObject *cls = NULL;
    if (cls == NULL) {
        PyClassObject *fresh = py_class_new("re.Match", NULL, 0, NULL, 0);
        if (fresh != NULL) {
            fresh->h.flags |= PY_FLAG_IMMORTAL;
            cls = fresh;
        }
    }
    return (PyObject *)cls;
}

static int64_t re_match_span_at(PyObject *spans, int64_t idx) {
    PyObject *v = py_tuple_get(spans, idx);
    int overflow = 0;
    int64_t out;
    if (v == NULL) return -1;
    out = py_int_to_i64(v, &overflow);
    py_decref(v);
    if (overflow) return -1;
    return out;
}

/* shared entry for all match-object methods;
 * captures = (text, spans, kind, names) */
static PyObject *re_match_method_call(PyObject *captures, PyObject *args) {
    PyObject *text;
    PyObject *spans;
    PyObject *kind_obj;
    PyObject *names;
    int overflow = 0;
    int64_t kind;
    int64_t ngroups;
    int64_t nargs;
    int64_t g = 0;
    if (captures == NULL || py_tuple_len(captures) < 4) return py_None;
    text = py_tuple_get(captures, 0);
    spans = py_tuple_get(captures, 1);
    kind_obj = py_tuple_get(captures, 2);
    names = py_tuple_get(captures, 3);
    if (text == NULL || spans == NULL || kind_obj == NULL || names == NULL) {
        if (text != NULL) py_decref(text);
        if (spans != NULL) py_decref(spans);
        if (kind_obj != NULL) py_decref(kind_obj);
        if (names != NULL) py_decref(names);
        return py_None;
    }
    kind = py_int_to_i64(kind_obj, &overflow);
    py_decref(kind_obj);
    if (overflow) kind = RE_M_GROUP;
    ngroups = py_tuple_len(spans) / 2 - 1;
    nargs = args != NULL ? py_tuple_len(args) : 0;
    if (kind == RE_M_GROUPS) {
        PyObject *out = py_tuple_new(ngroups);
        int64_t i;
        for (i = 1; i <= ngroups && out != NULL; i++) {
            int64_t lo = re_match_span_at(spans, 2 * i);
            int64_t hi = re_match_span_at(spans, 2 * i + 1);
            PyObject *item;
            if (lo < 0 || hi < 0) {
                item = py_None;
            } else {
                item = py_str_byte_slice_i64(text, lo, hi);
            }
            py_tuple_set_item(out, i - 1, item);
            if (item != py_None && item != NULL) py_decref(item);
        }
        py_decref(text);
        py_decref(spans);
        py_decref(names);
        return out;
    }
    if (kind == RE_M_GROUPDICT) {
        PyObject *out = py_dict_new();
        int64_t i;
        for (i = 1; i <= ngroups && out != NULL; i++) {
            PyObject *nm = py_tuple_get(names, i - 1);
            if (nm == NULL) continue;
            if (nm == py_None) {
                py_decref(nm);
                continue;
            }
            {
                int64_t lo = re_match_span_at(spans, 2 * i);
                int64_t hi = re_match_span_at(spans, 2 * i + 1);
                PyObject *val;
                if (lo < 0 || hi < 0) {
                    val = py_None;
                } else {
                    val = py_str_byte_slice_i64(text, lo, hi);
                }
                if (val != NULL) {
                    py_dict_set(out, nm, val);
                    if (val != py_None) py_decref(val);
                }
            }
            py_decref(nm);
        }
        py_decref(text);
        py_decref(spans);
        py_decref(names);
        return out;
    }
    if (nargs >= 2) {
        py_decref(text);
        py_decref(spans);
        py_decref(names);
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: multi-group Match method arguments are not supported"
        ));
        return NULL;
    }
    if (nargs == 1) {
        PyObject *garg = py_tuple_get(args, 0);
        if (garg != NULL) {
            if (py_type_of(garg) == PY_TYPE_STR) {
                const char *want = py_str_utf8(garg);
                int64_t i;
                g = -1;
                for (i = 1; i <= ngroups && want != NULL; i++) {
                    PyObject *nm = py_tuple_get(names, i - 1);
                    if (nm == NULL) continue;
                    if (nm != py_None) {
                        const char *have = py_str_utf8(nm);
                        if (have != NULL && strcmp(have, want) == 0) {
                            py_decref(nm);
                            g = i;
                            break;
                        }
                    }
                    py_decref(nm);
                }
            } else {
                overflow = 0;
                g = py_int_to_i64(garg, &overflow);
                if (overflow) g = -1;
            }
            py_decref(garg);
        }
    }
    if (g < 0 || g > ngroups) {
        py_decref(text);
        py_decref(spans);
        py_decref(names);
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "no such group"));
        return NULL;
    }
    {
        int64_t lo = re_match_span_at(spans, 2 * g);
        int64_t hi = re_match_span_at(spans, 2 * g + 1);
        PyObject *result = NULL;
        switch ((int)kind) {
        case RE_M_GROUP:
            if (lo < 0 || hi < 0) {
                result = py_None;
            } else {
                result = py_str_byte_slice_i64(text, lo, hi);
            }
            break;
        case RE_M_START:
            result = py_int_from_i64(lo);
            break;
        case RE_M_END:
            result = py_int_from_i64(hi);
            break;
        case RE_M_SPAN: {
            PyObject *lo_obj = py_int_from_i64(lo);
            PyObject *hi_obj = py_int_from_i64(hi);
            result = py_tuple_new(2);
            if (result != NULL && lo_obj != NULL && hi_obj != NULL) {
                py_tuple_set_item(result, 0, lo_obj);
                py_tuple_set_item(result, 1, hi_obj);
            }
            if (lo_obj != NULL) py_decref(lo_obj);
            if (hi_obj != NULL) py_decref(hi_obj);
            break;
        }
        default:
            result = py_None;
            break;
        }
        py_decref(text);
        py_decref(spans);
        py_decref(names);
        return result;
    }
}

static void re_match_add_method(PyObject *inst, const char *name,
                                PyObject *text, PyObject *spans,
                                PyObject *names, int64_t kind) {
    PyObject *captures = py_tuple_new(4);
    PyObject *kind_obj;
    PyObject *fn;
    if (captures == NULL) return;
    kind_obj = py_int_from_i64(kind);
    if (kind_obj == NULL) {
        py_decref(captures);
        return;
    }
    py_tuple_set_item(captures, 0, text);
    py_tuple_set_item(captures, 1, spans);
    py_tuple_set_item(captures, 2, kind_obj);
    py_tuple_set_item(captures, 3, names);
    py_decref(kind_obj);
    fn = py_func_new_named((void *)re_match_method_call, captures, name);
    py_decref(captures);
    if (fn == NULL) return;
    py_obj_setattr(inst, name, fn);
    py_decref(fn);
}

static PyObject *re_match_object_new(PyObject *pattern, PyObject *text,
                                     const int64_t *caps, int64_t ngroups,
                                     int64_t flags) {
    PyObject *cls = re_match_class_singleton();
    PyObject *inst;
    PyObject *spans;
    PyObject *names;
    char name_buf[32 * 32];
    int names_rc = -1;
    const char *name_cursor = name_buf;
    int64_t i;
    if (cls == NULL) return py_None;
    inst = py_instance_new((PyClassObject *)cls);
    if (inst == NULL) return py_None;
    spans = py_tuple_new(2 * (ngroups + 1));
    if (spans == NULL) return inst;
    for (i = 0; i < 2 * (ngroups + 1); i++) {
        PyObject *v = py_int_from_i64(caps[i]);
        if (v == NULL) break;
        py_tuple_set_item(spans, i, v);
        py_decref(v);
    }
    names = py_tuple_new(ngroups);
    if (names == NULL) {
        py_decref(spans);
        return inst;
    }
    if (pattern != NULL && py_type_of(pattern) == PY_TYPE_STR) {
        const char *p = py_str_utf8(pattern);
        if (p != NULL) {
            names_rc = pcc_re_engine_group_names_flags(
                p, flags, name_buf, (int)sizeof(name_buf)
            );
        }
    }
    for (i = 1; i <= ngroups; i++) {
        PyObject *nm = py_None;
        if (names_rc >= 0) {
            if (name_cursor[0] != '\0') {
                nm = py_str_new(name_cursor, (int64_t)strlen(name_cursor));
            }
            name_cursor += strlen(name_cursor) + 1;
        }
        py_tuple_set_item(names, i - 1, nm == NULL ? py_None : nm);
        if (nm != NULL && nm != py_None) py_decref(nm);
    }
    re_match_add_method(inst, "group", text, spans, names, RE_M_GROUP);
    re_match_add_method(inst, "start", text, spans, names, RE_M_START);
    re_match_add_method(inst, "end", text, spans, names, RE_M_END);
    re_match_add_method(inst, "span", text, spans, names, RE_M_SPAN);
    re_match_add_method(inst, "groups", text, spans, names, RE_M_GROUPS);
    re_match_add_method(inst, "groupdict", text, spans, names, RE_M_GROUPDICT);
    py_decref(spans);
    py_decref(names);
    return inst;
}

/* ---------------- E3 findall ---------------- */

static void re_engine_raise_for(int r) {
    if (r == PCC_RE_UNSUPPORTED) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: pattern outside the native regex subset (no-libpython)"
        ));
    } else if (r == PCC_RE_NONASCII) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: non-ASCII text outside the native regex subset"
        ));
    } else {
        py_raise_owned(py_exc_new(
            PY_EXC_RUNTIMEERROR,
            "pcc re: native regex engine limit reached"
        ));
    }
}

/* group value for findall items: '' for unmatched groups (CPython) */
static PyObject *re_findall_group_str(PyObject *text, const int64_t *caps,
                                      int64_t g) {
    int64_t lo = caps[2 * g];
    int64_t hi = caps[2 * g + 1];
    if (lo < 0 || hi < 0) return py_str_byte_slice_i64(text, 0, 0);
    return py_str_byte_slice_i64(text, lo, hi);
}

/*
 * CPython-faithful findall scan for flags==0 subset patterns:
 * 0 groups -> list of group-0 strings; 1 group -> list of group-1 values
 * ('' for unmatched); >=2 groups -> list of tuples. Empty matches advance
 * by one byte. Raises like the truth runner for outside-subset patterns,
 * non-ASCII text, or the engine limit. Non-str arguments mirror the legacy
 * behavior (empty list).
 */
PyObject *py_re_engine_findall(PyObject *pattern, PyObject *text,
                               int64_t flags) {
    int64_t caps[2 * 32];
    int64_t ngroups = 0;
    const char *p;
    const char *t;
    int64_t tlen;
    int64_t pos = 0;
    PyObject *out;
    if (pattern == NULL || text == NULL) return py_list_new(0);
    if (py_type_of(pattern) != PY_TYPE_STR || py_type_of(text) != PY_TYPE_STR) {
        return py_list_new(0);
    }
    p = py_str_utf8(pattern);
    t = py_str_utf8(text);
    if (p == NULL || t == NULL) return py_list_new(0);
    tlen = (int64_t)strlen(t);
    out = py_list_new(0);
    if (out == NULL) return NULL;
    while (pos <= tlen) {
        int r = pcc_re_engine_run_flags(p, flags, t, tlen, pos, 1, caps,
                                        (int)(sizeof(caps) / sizeof(caps[0])),
                                        &ngroups);
        if (r == PCC_RE_NOMATCH) break;
        if (r != PCC_RE_MATCH) {
            py_decref(out);
            re_engine_raise_for(r);
            return NULL;
        }
        {
            int64_t lo = caps[0];
            int64_t hi = caps[1];
            PyObject *item;
            if (ngroups == 0) {
                item = py_str_byte_slice_i64(text, lo, hi);
            } else if (ngroups == 1) {
                item = re_findall_group_str(text, caps, 1);
            } else {
                int64_t g;
                item = py_tuple_new(ngroups);
                for (g = 1; g <= ngroups && item != NULL; g++) {
                    PyObject *s = re_findall_group_str(text, caps, g);
                    if (s == NULL) break;
                    py_tuple_set_item(item, g - 1, s);
                    py_decref(s);
                }
            }
            if (item == NULL) {
                py_decref(out);
                return NULL;
            }
            py_list_append(out, item);
            py_decref(item);
            pos = (hi == lo) ? hi + 1 : hi;
        }
    }
    return out;
}

/* ---------------- E3b sub / split ---------------- */

static int re_str_has_byte(PyObject *s, char needle) {
    const char *p = py_str_utf8(s);
    int64_t i;
    if (p == NULL) return 0;
    for (i = 0; p[i] != '\0'; i++) {
        if (p[i] == needle) return 1;
    }
    return 0;
}

/*
 * CPython-faithful re.sub for flags==0 subset patterns with a LITERAL
 * replacement string (no backslash escapes / group templates — those raise
 * NotImplementedError, never expand wrongly). count <= 0 replaces all.
 */
static int re_sub_hold(PyObject **slots, void **handles, int index,
                       PyObject *value, int64_t backend) {
    slots[index] = value;
    if ((backend == 3 || backend == 4) && value != NULL) {
        handles[index] = pcc_gc_scheduler_root_register_handle(&slots[index]);
        if (handles[index] == NULL) {
            py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "re.sub: cannot root temporary"));
            return 0;
        }
    }
    return 1;
}

static PyObject *re_sub_load(PyObject **slots, void **handles, int index) {
    return handles[index] != NULL ? pcc_gc_load_ptr(NULL, &slots[index]) : slots[index];
}

static void re_sub_drop(PyObject **slots, void **handles, int index) {
    PyObject *value = re_sub_load(slots, handles, index);
    if (handles[index] != NULL) pcc_gc_scheduler_root_unregister_handle(handles[index]);
    handles[index] = NULL;
    slots[index] = NULL;
    if (index >= 3) py_decref(value);
}

static int re_sub_append(PyObject **slots, void **handles, PyObject *piece, int64_t backend) {
    int ok = re_sub_hold(slots, handles, 6, piece, backend);
    if (piece == NULL) ok = 0;
    if (ok) {
        py_list_append(re_sub_load(slots, handles, 3), re_sub_load(slots, handles, 6));
        if (py_err_occurred()) ok = 0;
    }
    re_sub_drop(slots, handles, 6);
    return ok;
}

static PyObject *re_sub_callable(PyObject *pattern, PyObject *replacement,
                                 PyObject *text, int64_t count, int64_t flags) {
    PyObject *slots[7] = {NULL};
    void *handles[7] = {NULL};
    int64_t backend = pcc_gc_backend();
    int ok = re_sub_hold(slots, handles, 0, pattern, backend);
    if (ok) ok = re_sub_hold(slots, handles, 1, replacement, backend);
    if (ok) ok = re_sub_hold(slots, handles, 2, text, backend);
    if (ok) {
        PyObject *parts = py_list_new(0);
        ok = re_sub_hold(slots, handles, 3, parts, backend);
        if (parts == NULL) ok = 0;
    }
    int64_t length = ok ? (int64_t)strlen(py_str_utf8(re_sub_load(slots, handles, 2))) : 0;
    int64_t position = 0, last = 0, done = 0;
    int64_t caps[64], ngroups = 0;
    while (ok && position <= length && (count == 0 || done < count)) {
        ngroups = 0;
        int status = pcc_re_engine_run_flags(py_str_utf8(re_sub_load(slots, handles, 0)),
            flags, py_str_utf8(re_sub_load(slots, handles, 2)), length, position,
            1, caps, 64, &ngroups);
        if (status == PCC_RE_NOMATCH) break;
        if (status != PCC_RE_MATCH) {
            re_engine_raise_for(status);
            ok = 0;
            break;
        }
        int64_t lo = caps[0], hi = caps[1];
        if (lo == hi) {
            py_raise_owned(py_exc_new(PY_EXC_NOTIMPLEMENTEDERROR,
                "pcc re: callable replacement of empty matches is not supported"));
            ok = 0;
            break;
        }
        PyObject *before = py_str_byte_slice_i64(re_sub_load(slots, handles, 2), last, lo);
        ok = re_sub_append(slots, handles, before, backend);
        if (ok) {
            PyObject *match = re_match_object_new(re_sub_load(slots, handles, 0),
                re_sub_load(slots, handles, 2), caps, ngroups, flags);
            ok = re_sub_hold(slots, handles, 4, match, backend);
            if (match == NULL || match == py_None) ok = 0;
        }
        if (ok) {
            PyObject *args = py_tuple_new(1);
            ok = re_sub_hold(slots, handles, 5, args, backend);
            if (args == NULL) ok = 0;
        }
        if (ok) {
            py_tuple_set_item(re_sub_load(slots, handles, 5), 0, re_sub_load(slots, handles, 4));
            PyObject *piece = py_obj_call(re_sub_load(slots, handles, 1),
                                         re_sub_load(slots, handles, 5), NULL);
            if (piece == NULL) ok = 0;
            else if (py_type_of(piece) != PY_TYPE_STR) {
                py_decref(piece);
                py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "re.sub replacement callback must return str"));
                ok = 0;
            } else ok = re_sub_append(slots, handles, piece, backend);
        }
        re_sub_drop(slots, handles, 5);
        re_sub_drop(slots, handles, 4);
        done++;
        last = hi;
        position = hi;
    }
    PyObject *result = NULL;
    if (ok) {
        PyObject *tail = py_str_byte_slice_i64(re_sub_load(slots, handles, 2), last, length);
        ok = re_sub_append(slots, handles, tail, backend);
    }
    if (ok) {
        PyObject *empty = py_str_byte_slice_i64(re_sub_load(slots, handles, 2), 0, 0);
        ok = re_sub_hold(slots, handles, 6, empty, backend);
        if (empty != NULL && ok)
            result = py_str_join(re_sub_load(slots, handles, 6), re_sub_load(slots, handles, 3));
    }
    for (int index = 6; index >= 0; index--) re_sub_drop(slots, handles, index);
    return result;
}

PyObject *py_re_engine_sub(PyObject *pattern, PyObject *repl, PyObject *text,
                           int64_t count, int64_t flags) {
    int64_t caps[2 * 32];
    int64_t ngroups = 0;
    const char *p;
    const char *t;
    int64_t tlen;
    int64_t pos = 0;
    int64_t last = 0;
    int64_t done = 0;
    PyObject *parts;
    PyObject *empty;
    PyObject *out;
    if (pattern == NULL || repl == NULL || text == NULL) return py_None;
    if (py_type_of(pattern) != PY_TYPE_STR ||
        py_type_of(text) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "pcc re: sub expects string pattern, replacement, and text"
        ));
        return NULL;
    }
    PyObject *callable_value = py_builtin_callable(repl);
    int is_callable = py_obj_truthy(callable_value);
    py_decref(callable_value);
    if (is_callable) return re_sub_callable(pattern, repl, text, count, flags);
    if (py_type_of(repl) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "re.sub replacement must be str or callable"));
        return NULL;
    }
    if (re_str_has_byte(repl, '\\')) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: backslash replacement templates are not supported"
        ));
        return NULL;
    }
    p = py_str_utf8(pattern);
    t = py_str_utf8(text);
    if (p == NULL || t == NULL) return py_None;
    tlen = (int64_t)strlen(t);
    parts = py_list_new(0);
    if (parts == NULL) return NULL;
    while (pos <= tlen && (count <= 0 || done < count)) {
        int r = pcc_re_engine_run_flags(p, flags, t, tlen, pos, 1, caps,
                                        (int)(sizeof(caps) / sizeof(caps[0])),
                                        &ngroups);
        if (r == PCC_RE_NOMATCH) break;
        if (r != PCC_RE_MATCH) {
            py_decref(parts);
            re_engine_raise_for(r);
            return NULL;
        }
        {
            int64_t lo = caps[0];
            int64_t hi = caps[1];
            PyObject *pre = py_str_byte_slice_i64(text, last, lo);
            if (pre != NULL) {
                py_list_append(parts, pre);
                py_decref(pre);
            }
            py_list_append(parts, repl);
            done++;
            if (lo == hi) {
                if (hi < tlen) {
                    PyObject *one = py_str_byte_slice_i64(text, hi, hi + 1);
                    if (one != NULL) {
                        py_list_append(parts, one);
                        py_decref(one);
                    }
                }
                last = hi + 1;
                pos = hi + 1;
            } else {
                last = hi;
                pos = hi;
            }
        }
    }
    if (last <= tlen) {
        PyObject *tail = py_str_byte_slice_i64(text, last, tlen);
        if (tail != NULL) {
            py_list_append(parts, tail);
            py_decref(tail);
        }
    }
    empty = py_str_byte_slice_i64(text, 0, 0);
    if (empty == NULL) {
        py_decref(parts);
        return NULL;
    }
    out = py_str_join(empty, parts);
    py_decref(empty);
    py_decref(parts);
    return out;
}

/*
 * CPython-faithful re.split for flags==0 subset patterns: capturing-group
 * values are inserted between pieces (None for unmatched groups);
 * empty matches split too; maxsplit <= 0 means no limit.
 */
static int64_t re_args_int_at(PyObject *args, int64_t idx, int64_t fallback);

static int re_finditer_position(PyObject *state, int64_t position) {
    PyObject *value = py_int_from_i64(position);
    if (value == NULL) return 0;
    py_tuple_set_item(state, 3, value);
    py_decref(value);
    return 1;
}

static PyObject *re_finditer_next(PyObject *state, PyObject *args) {
    (void)args;
    PyObject *slots[7] = {NULL};
    void *handles[7] = {NULL};
    int64_t backend = pcc_gc_backend();
    int ok = re_sub_hold(slots, handles, 0, state, backend);
    int64_t position = re_args_int_at(state, 3, 0);
    int64_t end = re_args_int_at(state, 4, 0);
    int64_t flags = re_args_int_at(state, 2, 0);
    PyObject *result = NULL;
    if (ok && position <= end) {
        PyObject *pattern = py_tuple_get(re_sub_load(slots, handles, 0), 0);
        ok = re_sub_hold(slots, handles, 3, pattern, backend);
        if (pattern == NULL) ok = 0;
        if (ok) {
            PyObject *text = py_tuple_get(re_sub_load(slots, handles, 0), 1);
            ok = re_sub_hold(slots, handles, 4, text, backend);
            if (text == NULL) ok = 0;
        }
        if (ok) {
            int64_t caps[64], ngroups = 0;
            int status = pcc_re_engine_run_flags(py_str_utf8(re_sub_load(slots, handles, 3)),
                flags, py_str_utf8(re_sub_load(slots, handles, 4)), end, position,
                1, caps, 64, &ngroups);
            if (status == PCC_RE_NOMATCH) {
                if (re_finditer_position(re_sub_load(slots, handles, 0), end + 1)) result = py_None;
            } else if (status != PCC_RE_MATCH) re_engine_raise_for(status);
            else if (caps[0] == caps[1]) {
                py_raise_owned(py_exc_new(PY_EXC_NOTIMPLEMENTEDERROR,
                    "pcc re: finditer of empty matches is not supported"));
            } else if (re_finditer_position(re_sub_load(slots, handles, 0), caps[1])) {
                result = re_match_object_new(re_sub_load(slots, handles, 3),
                    re_sub_load(slots, handles, 4), caps, ngroups, flags);
            }
        }
    } else if (ok) result = py_None;
    for (int index = 6; index >= 0; index--) re_sub_drop(slots, handles, index);
    return result;
}

static PyObject *re_new_finditer(PyObject *pattern, PyObject *text,
                                int64_t flags, int64_t start, int64_t end) {
    if (text == NULL || py_type_of(text) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "pcc re: finditer expects a string"));
        return NULL;
    }
    int64_t length = ((PyStrObject *)text)->byte_len;
    if (start < 0) start = 0;
    else if (start > length) start = length;
    if (end < 0) end = 0;
    else if (end > length) end = length;
    PyObject *slots[7] = {NULL};
    void *handles[7] = {NULL};
    int64_t backend = pcc_gc_backend();
    int ok = re_sub_hold(slots, handles, 0, pattern, backend);
    if (ok) ok = re_sub_hold(slots, handles, 1, text, backend);
    if (ok) {
        PyObject *captures = py_tuple_new(5);
        ok = re_sub_hold(slots, handles, 3, captures, backend);
        if (captures == NULL) ok = 0;
    }
    if (ok) {
        py_tuple_set_item(re_sub_load(slots, handles, 3), 0, re_sub_load(slots, handles, 0));
        py_tuple_set_item(re_sub_load(slots, handles, 3), 1, re_sub_load(slots, handles, 1));
        for (int index = 2; index < 5 && ok; index++) {
            int64_t number = index == 2 ? flags : (index == 3 ? start : end);
            PyObject *value = py_int_from_i64(number);
            if (value == NULL) ok = 0;
            else {
                py_tuple_set_item(re_sub_load(slots, handles, 3), index, value);
                py_decref(value);
            }
        }
    }
    PyObject *result = NULL;
    if (ok) {
        PyObject *callback = py_func_new_named(re_finditer_next,
            re_sub_load(slots, handles, 3), "re.finditer.next");
        ok = re_sub_hold(slots, handles, 4, callback, backend);
        if (callback != NULL && ok) result = py_iter_callable_new(re_sub_load(slots, handles, 4), py_None);
    }
    for (int index = 6; index >= 0; index--) re_sub_drop(slots, handles, index);
    return result;
}

PyObject *py_re_finditer_flags(PyObject *pattern, PyObject *text, int64_t flags) {
    if (pattern == NULL || py_type_of(pattern) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "pcc re: finditer pattern must be a string"));
        return NULL;
    }
    if ((flags & ~26) != 0 || !pcc_re_engine_supported_flags(py_str_utf8(pattern), flags)) {
        py_raise_owned(py_exc_new(PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: finditer pattern or flags outside the native subset"));
        return NULL;
    }
    return re_new_finditer(pattern, text, flags, 0, INT64_MAX);
}

PyObject *py_re_engine_split(PyObject *pattern, PyObject *text,
                             int64_t maxsplit, int64_t flags) {
    int64_t caps[2 * 32];
    int64_t ngroups = 0;
    const char *p;
    const char *t;
    int64_t tlen;
    int64_t pos = 0;
    int64_t last = 0;
    int64_t done = 0;
    PyObject *out;
    if (pattern == NULL || text == NULL) return py_None;
    if (py_type_of(pattern) != PY_TYPE_STR || py_type_of(text) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "pcc re: split expects string pattern and text"
        ));
        return NULL;
    }
    p = py_str_utf8(pattern);
    t = py_str_utf8(text);
    if (p == NULL || t == NULL) return py_None;
    tlen = (int64_t)strlen(t);
    out = py_list_new(0);
    if (out == NULL) return NULL;
    while (pos <= tlen && (maxsplit <= 0 || done < maxsplit)) {
        int r = pcc_re_engine_run_flags(p, flags, t, tlen, pos, 1, caps,
                                        (int)(sizeof(caps) / sizeof(caps[0])),
                                        &ngroups);
        if (r == PCC_RE_NOMATCH) break;
        if (r != PCC_RE_MATCH) {
            py_decref(out);
            re_engine_raise_for(r);
            return NULL;
        }
        {
            int64_t lo = caps[0];
            int64_t hi = caps[1];
            int64_t g;
            PyObject *piece = py_str_byte_slice_i64(text, last, lo);
            if (piece != NULL) {
                py_list_append(out, piece);
                py_decref(piece);
            }
            for (g = 1; g <= ngroups; g++) {
                int64_t glo = caps[2 * g];
                int64_t ghi = caps[2 * g + 1];
                if (glo < 0 || ghi < 0) {
                    py_list_append(out, py_None);
                } else {
                    PyObject *gs = py_str_byte_slice_i64(text, glo, ghi);
                    if (gs != NULL) {
                        py_list_append(out, gs);
                        py_decref(gs);
                    }
                }
            }
            done++;
            last = hi;
            pos = (hi == lo) ? hi + 1 : hi;
        }
    }
    {
        PyObject *tail = py_str_byte_slice_i64(text, last, tlen);
        if (tail != NULL) {
            py_list_append(out, tail);
            py_decref(tail);
        }
    }
    return out;
}

/* ---------------- E1 pattern object ---------------- */

PyObject *py_re_engine_truth(PyObject *pattern, PyObject *text, int64_t search);
PyObject *py_re_engine_truth_flags(PyObject *pattern, PyObject *text,
                                   int64_t flags, int64_t search);
PyObject *py_re_engine_truth_flags_from(PyObject *pattern, PyObject *text,
                                        int64_t flags, int64_t search,
                                        int64_t start, int64_t endpos);

static PyObject *re_pattern_class_singleton(void) {
    static PyClassObject *cls = NULL;
    if (cls == NULL) {
        PyClassObject *fresh = py_class_new("re.Pattern", NULL, 0, NULL, 0);
        if (fresh != NULL) {
            fresh->h.flags |= PY_FLAG_IMMORTAL;
            cls = fresh;
        }
    }
    return (PyObject *)cls;
}

/* int arg helper: returns fallback when missing/overflowing */
static int64_t re_args_int_at(PyObject *args, int64_t idx, int64_t fallback) {
    PyObject *v;
    int overflow = 0;
    int64_t out;
    if (args == NULL || py_tuple_len(args) <= idx) return fallback;
    v = py_tuple_get(args, idx);
    if (v == NULL) return fallback;
    out = py_int_to_i64(v, &overflow);
    py_decref(v);
    if (overflow) return fallback;
    return out;
}

static PyObject *re_finditer_from_args(PyObject *pattern, PyObject *args, int64_t flags) {
    int64_t count = args != NULL ? py_tuple_len(args) : 0;
    if (count < 1 || count > 3) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "Pattern.finditer expects one to three arguments"));
        return NULL;
    }
    PyObject *slots[7] = {NULL};
    void *handles[7] = {NULL};
    int64_t backend = pcc_gc_backend();
    int ok = re_sub_hold(slots, handles, 0, pattern, backend);
    if (ok) ok = re_sub_hold(slots, handles, 1, args, backend);
    if (ok) {
        PyObject *text = py_tuple_get(re_sub_load(slots, handles, 1), 0);
        ok = re_sub_hold(slots, handles, 3, text, backend);
        if (text == NULL) ok = 0;
    }
    int64_t start = 0, end = INT64_MAX;
    for (int64_t index = 1; index < count && ok; index++) {
        PyObject *value = py_tuple_get(re_sub_load(slots, handles, 1), index);
        ok = re_sub_hold(slots, handles, 4, value, backend);
        if (value == NULL) ok = 0;
        else if (value == py_None) {
            py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "finditer bounds must be integers"));
            ok = 0;
        } else if (ok) {
            int64_t bound = py_index_i64_checked(re_sub_load(slots, handles, 4));
            if (py_err_occurred()) ok = 0;
            else if (index == 1) start = bound;
            else end = bound;
        }
        re_sub_drop(slots, handles, 4);
    }
    PyObject *result = NULL;
    if (ok) result = re_new_finditer(re_sub_load(slots, handles, 0),
        re_sub_load(slots, handles, 3), flags, start, end);
    for (int index = 6; index >= 0; index--) re_sub_drop(slots, handles, index);
    return result;
}

/* pattern-object method entry; captures = (pattern, kind, flags):
 * kind 0 match / 1 search / 2 findall / 3 sub / 4 split */
static PyObject *re_pattern_method_call(PyObject *captures, PyObject *args) {
    PyObject *pattern;
    PyObject *kind_obj;
    PyObject *flags_obj;
    PyObject *result = NULL;
    int overflow = 0;
    int64_t kind;
    int64_t pat_flags = 0;
    int64_t nargs;
    if (captures == NULL || py_tuple_len(captures) < 3) return py_None;
    nargs = args != NULL ? py_tuple_len(args) : 0;
    pattern = py_tuple_get(captures, 0);
    kind_obj = py_tuple_get(captures, 1);
    flags_obj = py_tuple_get(captures, 2);
    if (pattern == NULL || kind_obj == NULL || flags_obj == NULL) {
        if (pattern != NULL) py_decref(pattern);
        if (kind_obj != NULL) py_decref(kind_obj);
        if (flags_obj != NULL) py_decref(flags_obj);
        return py_None;
    }
    kind = py_int_to_i64(kind_obj, &overflow);
    py_decref(kind_obj);
    if (overflow) kind = 0;
    overflow = 0;
    pat_flags = py_int_to_i64(flags_obj, &overflow);
    py_decref(flags_obj);
    if (overflow) pat_flags = 0;
    if (kind == 5) {
        PyObject *result = re_finditer_from_args(pattern, args, pat_flags);
        py_decref(pattern);
        return result;
    }
    if (kind == 3) {
        /* sub(repl, string[, count]) */
        PyObject *repl;
        PyObject *text;
        if (nargs < 2) {
            py_decref(pattern);
            py_raise_owned(py_exc_new(
                PY_EXC_TYPEERROR,
                "pcc re: Pattern.sub expects replacement and string"
            ));
            return NULL;
        }
        repl = py_tuple_get(args, 0);
        text = py_tuple_get(args, 1);
        if (repl != NULL && text != NULL) {
            result = py_re_engine_sub(pattern, repl, text,
                                      re_args_int_at(args, 2, 0), pat_flags);
        }
        if (repl != NULL) py_decref(repl);
        if (text != NULL) py_decref(text);
        py_decref(pattern);
        return result;
    }
    if (kind == 4) {
        /* split(string[, maxsplit]) */
        PyObject *text;
        if (nargs < 1) {
            py_decref(pattern);
            py_raise_owned(py_exc_new(
                PY_EXC_TYPEERROR,
                "pcc re: Pattern.split expects a string"
            ));
            return NULL;
        }
        text = py_tuple_get(args, 0);
        if (text != NULL) {
            result = py_re_engine_split(pattern, text,
                                        re_args_int_at(args, 1, 0), pat_flags);
            py_decref(text);
        }
        py_decref(pattern);
        return result;
    }
    {
        PyObject *text;
        if (nargs < 1) {
            py_decref(pattern);
            py_raise_owned(py_exc_new(
                PY_EXC_TYPEERROR,
                "pcc re: Pattern method expects one string argument"
            ));
            return NULL;
        }
        text = py_tuple_get(args, 0);
        if (text != NULL) {
            if (kind == 2) {
                result = py_re_engine_findall(pattern, text, pat_flags);
            } else {
                result = py_re_engine_truth_flags_from(
                    pattern,
                    text,
                    pat_flags,
                    kind == 1,
                    re_args_int_at(args, 1, 0),
                    re_args_int_at(args, 2, -1)
                );
            }
            py_decref(text);
        }
        py_decref(pattern);
        return result;
    }
}

static void re_pattern_add_method(PyObject *inst, const char *name,
                                  PyObject *pattern, int64_t kind,
                                  int64_t flags) {
    PyObject *captures = py_tuple_new(3);
    PyObject *kind_obj;
    PyObject *flags_o;
    PyObject *fn;
    if (captures == NULL) return;
    kind_obj = py_int_from_i64(kind);
    flags_o = py_int_from_i64(flags);
    if (kind_obj == NULL || flags_o == NULL) {
        if (kind_obj != NULL) py_decref(kind_obj);
        if (flags_o != NULL) py_decref(flags_o);
        py_decref(captures);
        return;
    }
    py_tuple_set_item(captures, 0, pattern);
    py_tuple_set_item(captures, 1, kind_obj);
    py_tuple_set_item(captures, 2, flags_o);
    py_decref(kind_obj);
    py_decref(flags_o);
    fn = py_func_new_named((void *)re_pattern_method_call, captures, name);
    py_decref(captures);
    if (fn == NULL) return;
    py_obj_setattr(inst, name, fn);
    py_decref(fn);
}

/*
 * Construct a first-class compiled-pattern object for a flags==0 literal
 * pattern inside the engine subset. The frontend's conservative checker
 * gates lowering; this constructor re-validates and raises
 * NotImplementedError for outside-subset patterns (construction-site
 * visibility instead of silent divergence) and TypeError for non-str
 * patterns. The object carries `.pattern` plus native `.match`/`.search`
 * methods returning E2 Match objects.
 */
PyObject *py_re_compile_obj(PyObject *pattern, int64_t flags) {
    PyObject *cls;
    PyObject *inst;
    const char *p;
    if (pattern == NULL || py_type_of(pattern) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "pcc re: re.compile pattern must be a string"
        ));
        return NULL;
    }
    if ((flags & ~(int64_t)PCC_RE_OK_FLAGS) != 0) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: re.compile flags are outside the native regex subset"
        ));
        return NULL;
    }
    p = py_str_utf8(pattern);
    if (p == NULL || !pcc_re_engine_supported_flags(p, flags)) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: pattern outside the native regex subset (no-libpython)"
        ));
        return NULL;
    }
    cls = re_pattern_class_singleton();
    if (cls == NULL) return py_None;
    inst = py_instance_new((PyClassObject *)cls);
    if (inst == NULL) return py_None;
    py_obj_setattr(inst, "pattern", pattern);
    re_pattern_add_method(inst, "match", pattern, 0, flags);
    re_pattern_add_method(inst, "search", pattern, 1, flags);
    re_pattern_add_method(inst, "findall", pattern, 2, flags);
    re_pattern_add_method(inst, "sub", pattern, 3, flags);
    re_pattern_add_method(inst, "split", pattern, 4, flags);
    re_pattern_add_method(inst, "finditer", pattern, 5, flags);
    return inst;
}

/*
 * flags==0 runner for native re.match / re.search.
 * Returns a Match-like object (group/start/end/span/groups + truthy) on a
 * match, py_None on no match; raises NotImplementedError (and returns
 * NULL) for patterns outside the engine subset or non-ASCII text, and
 * RuntimeError at the engine depth limit, instead of silently diverging
 * from CPython. Non-str arguments mirror the legacy behavior (py_None).
 * Kept under its original E1a export name; the result remains truthy so
 * existing truthiness consumers are unaffected.
 */
PyObject *py_re_engine_truth_flags_from(PyObject *pattern, PyObject *text,
                                        int64_t flags, int64_t search,
                                        int64_t start, int64_t endpos) {
    int64_t caps[2 * 32];
    int64_t ngroups = 0;
    const char *p;
    const char *t;
    int r;
    if (pattern == NULL || text == NULL) return py_None;
    if (py_type_of(pattern) != PY_TYPE_STR || py_type_of(text) != PY_TYPE_STR) {
        return py_None;
    }
    p = py_str_utf8(pattern);
    t = py_str_utf8(text);
    if (p == NULL || t == NULL) return py_None;
    int64_t text_len = (int64_t)strlen(t);
    if (start < 0) start = 0;
    if (start > text_len) start = text_len;
    if (endpos < 0 || endpos > text_len) endpos = text_len;
    if (endpos < start) endpos = start;
    r = pcc_re_engine_run_flags(p, flags, t, endpos, start,
                                search != 0, caps,
                                (int)(sizeof(caps) / sizeof(caps[0])),
                                &ngroups);
    if (r == PCC_RE_MATCH) {
        return re_match_object_new(pattern, text, caps, ngroups, flags);
    }
    if (r == PCC_RE_NOMATCH) return py_None;
    if (r == PCC_RE_UNSUPPORTED) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: pattern outside the native regex subset (no-libpython)"
        ));
        return NULL;
    }
    if (r == PCC_RE_NONASCII) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: non-ASCII text outside the native regex subset"
        ));
        return NULL;
    }
    py_raise_owned(py_exc_new(
        PY_EXC_RUNTIMEERROR,
        "pcc re: native regex engine limit reached"
    ));
    return NULL;
}

PyObject *py_re_engine_truth_flags(PyObject *pattern, PyObject *text,
                                   int64_t flags, int64_t search) {
    return py_re_engine_truth_flags_from(pattern, text, flags, search, 0, -1);
}

PyObject *py_re_engine_truth(PyObject *pattern, PyObject *text, int64_t search) {
    return py_re_engine_truth_flags(pattern, text, 0, search);
}

PyObject *py_re_engine_fullmatch_flags(PyObject *pattern, PyObject *text,
                                       int64_t flags) {
    int64_t caps[2 * 32];
    int64_t ngroups = 0;
    const char *p;
    const char *t;
    int64_t text_len;
    int r;
    if (pattern == NULL || text == NULL) return py_None;
    if (py_type_of(pattern) != PY_TYPE_STR || py_type_of(text) != PY_TYPE_STR) {
        return py_None;
    }
    if ((flags & ~(int64_t)(2 | 8 | 16)) != 0) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: flags outside the native regex subset (no-libpython)"
        ));
        return NULL;
    }
    p = py_str_utf8(pattern);
    t = py_str_utf8(text);
    if (p == NULL || t == NULL) return py_None;
    text_len = (int64_t)strlen(t);
    r = pcc_re_engine_run_flags(p, flags, t, text_len, 0,
                                0, caps,
                                (int)(sizeof(caps) / sizeof(caps[0])),
                                &ngroups);
    if (r == PCC_RE_MATCH) {
        if (caps[1] == text_len) {
            return re_match_object_new(pattern, text, caps, ngroups, flags);
        }
        return py_None;
    }
    if (r == PCC_RE_NOMATCH) return py_None;
    if (r == PCC_RE_UNSUPPORTED) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: pattern outside the native regex subset (no-libpython)"
        ));
        return NULL;
    }
    if (r == PCC_RE_NONASCII) {
        py_raise_owned(py_exc_new(
            PY_EXC_NOTIMPLEMENTEDERROR,
            "pcc re: non-ASCII text outside the native regex subset"
        ));
        return NULL;
    }
    py_raise_owned(py_exc_new(
        PY_EXC_RUNTIMEERROR,
        "pcc re: native regex engine limit reached"
    ));
    return NULL;
}

/* parse-only subset probe on a pattern str object (for future E1 gating) */
int64_t py_re_engine_pattern_supported(PyObject *pattern) {
    const char *p;
    if (pattern == NULL || py_type_of(pattern) != PY_TYPE_STR) return 0;
    p = py_str_utf8(pattern);
    if (p == NULL) return 0;
    return pcc_re_engine_supported(p) ? 1 : 0;
}
