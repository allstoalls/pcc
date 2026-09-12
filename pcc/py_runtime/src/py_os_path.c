/* pcc/py_runtime/src/py_os_path.c
 *
 * os.path helpers split out of py_os.c. Holds path_join /
 * path_basename / path_exists; the env helpers live in
 * py_os_env.c so they can be independently replaced by the
 * pcc-Python port.
 */

#include "py_internal.h"

#include <stdio.h>
#include <sys/stat.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static PyObject *coerce_path_str(PyObject *o, PyObject **owned) {
    *owned = NULL;
    if (o == NULL) return NULL;
    if (py_type_of(o) == PY_TYPE_STR) return o;
    *owned = py_obj_str(o);
    return *owned;
}

static int64_t path_seq_len(PyObject *parts) {
    if (parts == NULL || PY_IS_TAGGED_INT(parts)) return -1;
    switch (py_type_of(parts)) {
        case PY_TYPE_LIST:
            return ((PyListObject *)parts)->length;
        case PY_TYPE_TUPLE:
            return ((PyTupleObject *)parts)->len;
        default:
            return -1;
    }
}

static PyObject *path_seq_borrow(PyObject *parts, int64_t i) {
    if (parts == NULL || PY_IS_TAGGED_INT(parts)) return NULL;
    switch (py_type_of(parts)) {
        case PY_TYPE_LIST:
            return pcc_gc_load_ptr(
                parts,
                &((PyListObject *)parts)->items[i]
            );
        case PY_TYPE_TUPLE:
            return pcc_gc_load_ptr(
                parts,
                &((PyTupleObject *)parts)->items[i]
            );
        default:
            return NULL;
    }
}

PyObject *py_os_makedirs(PyObject *path, int64_t mode, int32_t exist_ok) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "path must be string-like"));
        return NULL;
    }
    const char *raw = py_str_utf8(item);
    int64_t raw_len = py_str_byte_len(item);
    if (raw == NULL || raw_len <= 0) {
        py_decref(owned);
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "cannot create empty path"));
        return NULL;
    }

    char *buf = (char *)malloc((size_t)raw_len + 1);
    if (buf == NULL) {
        py_decref(owned);
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "could not allocate path"));
        return NULL;
    }
    memcpy(buf, raw, (size_t)raw_len);
    buf[raw_len] = '\0';

    int64_t end = raw_len;
    while (end > 1 && buf[end - 1] == '/') end--;
    buf[end] = '\0';

    for (int64_t i = 1; i <= end; i++) {
        if (i != end && buf[i] != '/') continue;
        char saved = buf[i];
        buf[i] = '\0';
        int final_component = (i == end);
        mode_t component_mode = final_component ? (mode_t)mode : (mode_t)0777;
        if (mkdir(buf, component_mode) != 0) {
            int32_t kind = py_path_stat_kind(buf);
            if (kind != 2 || (final_component && !exist_ok)) {
                buf[i] = saved;
                free(buf);
                py_decref(owned);
                py_raise_owned(py_exc_new(PY_EXC_OSERROR, "could not create directory"));
                return NULL;
            }
        }
        buf[i] = saved;
    }

    free(buf);
    py_decref(owned);
    return py_None;
}

PyObject *py_os_unlink(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "path must be string-like"));
        return NULL;
    }
    int rc = unlink(py_str_utf8(item));
    py_decref(owned);
    if (rc != 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "could not unlink path"));
        return NULL;
    }
    return py_None;
}

PyObject *py_os_rmdir(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "path must be string-like"));
        return NULL;
    }
    int rc = rmdir(py_str_utf8(item));
    py_decref(owned);
    if (rc != 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "could not remove directory"));
        return NULL;
    }
    return py_None;
}

PyObject *py_os_replace(PyObject *source, PyObject *destination) {
    PyObject *source_owned = NULL;
    PyObject *destination_owned = NULL;
    PyObject *source_item = coerce_path_str(source, &source_owned);
    PyObject *destination_item = coerce_path_str(destination, &destination_owned);
    if (source_item == NULL || destination_item == NULL) {
        py_decref(source_owned);
        py_decref(destination_owned);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "paths must be string-like"));
        return NULL;
    }
    int rc = rename(py_str_utf8(source_item), py_str_utf8(destination_item));
    py_decref(source_owned);
    py_decref(destination_owned);
    if (rc != 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "could not replace path"));
        return NULL;
    }
    return py_None;
}

PyObject *py_os_chmod(PyObject *path, int64_t mode) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "path must be string-like"));
        return NULL;
    }
    int rc = chmod(py_str_utf8(item), (mode_t)mode);
    py_decref(owned);
    if (rc != 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "could not change path mode"));
        return NULL;
    }
    return py_None;
}

PyObject *py_os_fsync(int64_t fd) {
    if (fsync((int)fd) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_OSERROR, "could not synchronize file"));
        return NULL;
    }
    return py_None;
}

static int buf_reserve(char **buf, int64_t *cap, int64_t want) {
    if (want <= *cap) return 0;
    int64_t new_cap = (*cap > 0) ? *cap : 32;
    while (new_cap < want) {
        new_cap *= 2;
    }
    char *grown = (char *)realloc(*buf, (size_t)new_cap);
    if (grown == NULL) return -1;
    *buf = grown;
    *cap = new_cap;
    return 0;
}

static int buf_append(
    char **buf, int64_t *len, int64_t *cap, const char *src, int64_t src_len
) {
    if (src_len <= 0) return 0;
    if (buf_reserve(buf, cap, *len + src_len + 1) != 0) return -1;
    memcpy(*buf + *len, src, (size_t)src_len);
    *len += src_len;
    (*buf)[*len] = '\0';
    return 0;
}

PyObject *py_os_path_join(PyObject *parts) {
    int64_t n = path_seq_len(parts);
    if (n < 0) return NULL;
    if (n == 0) return py_str_new("", 0);

    char *buf = NULL;
    int64_t len = 0;
    int64_t cap = 0;
    for (int64_t i = 0; i < n; i++) {
        PyObject *owned = NULL;
        PyObject *item = coerce_path_str(path_seq_borrow(parts, i), &owned);
        if (item == NULL) {
            py_decref(owned);
            free(buf);
            return NULL;
        }
        PyStrObject *s = (PyStrObject *)item;
        const char *part = s->data;
        int64_t part_len = s->byte_len;

        if (i == 0) {
            if (buf_append(&buf, &len, &cap, part, part_len) != 0) {
                py_decref(owned);
                free(buf);
                return NULL;
            }
            py_decref(owned);
            continue;
        }

        if (part_len > 0 && part[0] == '/') {
            len = 0;
            if (buf_append(&buf, &len, &cap, part, part_len) != 0) {
                py_decref(owned);
                free(buf);
                return NULL;
            }
            py_decref(owned);
            continue;
        }

        if (len > 0 && buf[len - 1] != '/') {
            if (buf_append(&buf, &len, &cap, "/", 1) != 0) {
                py_decref(owned);
                free(buf);
                return NULL;
            }
        }
        if (buf_append(&buf, &len, &cap, part, part_len) != 0) {
            py_decref(owned);
            free(buf);
            return NULL;
        }
        py_decref(owned);
    }

    PyObject *out = py_str_new(buf != NULL ? buf : "", len);
    free(buf);
    return out;
}

PyObject *py_os_path_dirname(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    int64_t n = s->byte_len;

    int64_t last = -1;
    for (int64_t i = 0; i < n; i++) {
        if (s->data[i] == '/') last = i;
    }

    int64_t head_len = last + 1;
    if (head_len == 0) {
        py_decref(owned);
        return py_str_new("", 0);
    }

    int all_slash = 1;
    for (int64_t j = 0; j < head_len; j++) {
        if (s->data[j] != '/') { all_slash = 0; break; }
    }

    int64_t out_len = head_len;
    if (!all_slash) {
        while (out_len > 0 && s->data[out_len - 1] == '/') {
            out_len--;
        }
    }

    PyObject *out = py_str_new(s->data, out_len);
    py_decref(owned);
    return out;
}

PyObject *py_os_path_basename(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    int64_t end = s->byte_len;
    while (end > 0 && s->data[end - 1] == '/') {
        end--;
    }
    if (end == 0) {
        py_decref(owned);
        return py_str_new("", 0);
    }
    int64_t start = end;
    while (start > 0 && s->data[start - 1] != '/') {
        start--;
    }
    PyObject *out = py_str_new(s->data + start, end - start);
    py_decref(owned);
    return out;
}

PyObject *py_os_path_split(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    int64_t n = s->byte_len;

    int64_t split_at = 0;
    for (int64_t i = 0; i < n; i++) {
        if (s->data[i] == '/') split_at = i + 1;
    }

    int64_t head_len = split_at;
    if (head_len > 0) {
        int all_slash = 1;
        for (int64_t j = 0; j < head_len; j++) {
            if (s->data[j] != '/') { all_slash = 0; break; }
        }
        if (!all_slash) {
            while (head_len > 0 && s->data[head_len - 1] == '/') {
                head_len--;
            }
        }
    }

    PyObject *head = py_str_new(s->data, head_len);
    PyObject *tail = py_str_new(s->data + split_at, n - split_at);
    PyObject *out = py_tuple_new(2);
    if (out != NULL) {
        py_tuple_set_item(out, 0, head);
        py_tuple_set_item(out, 1, tail);
    } else {
        py_decref(head);
        py_decref(tail);
    }
    py_decref(owned);
    return out;
}

int py_os_path_isfile(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return 0;
    }
    const char *raw = py_str_utf8(item);
    int ok = (py_path_stat_kind(raw) == 1) ? 1 : 0;
    py_decref(owned);
    return ok;
}

int py_os_path_isabs(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return 0;
    }
    PyStrObject *s = (PyStrObject *)item;
    int ok = (s->byte_len > 0 && s->data[0] == '/') ? 1 : 0;
    py_decref(owned);
    return ok;
}

int py_os_path_isdir(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return 0;
    }
    const char *raw = py_str_utf8(item);
    int ok = (py_path_stat_kind(raw) == 2) ? 1 : 0;
    py_decref(owned);
    return ok;
}

/* Simplified abspath: if `path` is already absolute, return a copy
 * unchanged; otherwise prepend cwd + '/'. Does not normalize . / ..
 * — pipeline.py callers don't feed unnormalized inputs, and the
 * full normpath algorithm is left for a follow-up if regressions
 * surface. */
PyObject *py_os_path_abspath(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    int64_t in_len = s->byte_len;

    if (in_len > 0 && s->data[0] == '/') {
        PyObject *out = py_str_new(s->data, in_len);
        py_decref(owned);
        return out;
    }

    const char *cwd = py_path_getcwd();
    if (cwd == NULL) {
        py_decref(owned);
        return NULL;
    }
    int64_t cwd_len = (int64_t)strlen(cwd);
    if (in_len == 0) {
        py_decref(owned);
        return py_str_new(cwd, cwd_len);
    }
    int64_t total = cwd_len + 1 + in_len;
    PyObject *out = py_str_new(NULL, total);
    if (out == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *outs = (PyStrObject *)out;
    memcpy(outs->data, cwd, (size_t)cwd_len);
    outs->data[cwd_len] = '/';
    memcpy(outs->data + cwd_len + 1, s->data, (size_t)in_len);
    py_decref(owned);
    return out;
}

PyObject *py_os_path_expanduser(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    int64_t n = s->byte_len;

    /* Only a bare "~" or "~/..." prefix expands to $HOME. A "~user" prefix
       (no '/' right after '~') and any path without a leading '~' are
       returned unchanged (CPython posixpath.expanduser). */
    int is_home = (n >= 1 && s->data[0] == '~' &&
                   (n == 1 || s->data[1] == '/'));
    const char *home = is_home ? getenv("HOME") : NULL;
    if (!is_home || home == NULL || home[0] == '\0') {
        PyObject *out = py_str_new(s->data, n);
        py_decref(owned);
        return out;
    }

    /* userhome = home.rstrip('/'); result = (userhome + path[1:]) or "/". */
    int64_t home_len = (int64_t)strlen(home);
    while (home_len > 0 && home[home_len - 1] == '/') {
        home_len--;
    }
    int64_t rest_len = n - 1; /* path[1:] */
    int64_t total = home_len + rest_len;
    if (total == 0) {
        py_decref(owned);
        return py_str_new("/", 1);
    }
    PyObject *out = py_str_new(NULL, total);
    if (out == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *outs = (PyStrObject *)out;
    memcpy(outs->data, home, (size_t)home_len);
    memcpy(outs->data + home_len, s->data + 1, (size_t)rest_len);
    py_decref(owned);
    return out;
}

PyObject *py_os_path_realpath(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    const char *raw = py_str_utf8(item);
    const char *resolved = (raw != NULL) ? py_path_realpath(raw) : NULL;
    if (resolved != NULL) {
        PyObject *out = py_str_new(resolved, (int64_t)strlen(resolved));
        py_decref(owned);
        return out;
    }
    /* realpath(3) failed (path or a component does not exist): fall back to
       lexical normpath(abspath(path)) — absolute with "." / ".." collapsed.
       Matches CPython os.path.realpath except that symlinks inside the
       existing prefix of a non-existent path are not resolved (rare). */
    py_decref(owned);
    PyObject *abs = py_os_path_abspath(path);
    if (abs == NULL) {
        return NULL;
    }
    PyObject *out = py_os_path_normpath(abs);
    py_decref(abs);
    return out;
}

PyObject *py_os_path_getmtime(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    const char *raw = py_str_utf8(item);
    double t = py_path_stat_mtime(raw);
    py_decref(owned);
    return py_float_from_f64(t);
}

PyObject *py_os_path_getsize(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    const char *raw = py_str_utf8(item);
    int64_t size = py_path_stat_size(raw);
    py_decref(owned);
    return py_int_from_i64(size);
}

PyObject *py_os_path_splitext(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    int64_t n = s->byte_len;

    int64_t slash = -1;
    int64_t dot = -1;
    for (int64_t i = 0; i < n; i++) {
        if (s->data[i] == '/') {
            slash = i;
            dot = -1;
        } else if (s->data[i] == '.') {
            dot = i;
        }
    }

    PyObject *base, *ext;
    if (dot <= slash + 1) {
        base = py_str_new(s->data, n);
        ext = py_str_new("", 0);
    } else {
        base = py_str_new(s->data, dot);
        ext = py_str_new(s->data + dot, n - dot);
    }

    PyObject *out = py_tuple_new(2);
    if (out != NULL) {
        py_tuple_set_item(out, 0, base);
        py_tuple_set_item(out, 1, ext);
    } else {
        py_decref(base);
        py_decref(ext);
    }
    py_decref(owned);
    return out;
}

PyObject *py_os_path_normcase(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    PyObject *out = py_str_new(s->data, s->byte_len);
    py_decref(owned);
    return out;
}

static int path_component_is_dotdot(const char *data, int64_t len) {
    return len == 2 && data[0] == '.' && data[1] == '.';
}

PyObject *py_os_path_normpath(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    const char *data = s->data;
    int64_t n = s->byte_len;
    int64_t work_cap = n > 0 ? n + 2 : 2;
    char *work = (char *)malloc((size_t)work_cap);
    int64_t *starts = (int64_t *)malloc(sizeof(int64_t) * (size_t)(n + 1));
    int64_t *lens = (int64_t *)malloc(sizeof(int64_t) * (size_t)(n + 1));
    if (work == NULL || starts == NULL || lens == NULL) {
        free(work);
        free(starts);
        free(lens);
        py_decref(owned);
        return NULL;
    }

    int64_t initial = 0;
    if (n > 0 && data[0] == '/') {
        initial = 1;
        if (n > 1 && data[1] == '/' && (n == 2 || data[2] != '/')) {
            initial = 2;
        }
    }

    int64_t out_len = 0;
    for (int64_t k = 0; k < initial; k++) {
        work[out_len++] = '/';
    }
    int64_t base_len = initial;
    int64_t comps = 0;
    int64_t i = initial;
    while (i < n) {
        while (i < n && data[i] == '/') i++;
        int64_t start = i;
        while (i < n && data[i] != '/') i++;
        int64_t len = i - start;
        if (len == 0 || (len == 1 && data[start] == '.')) {
            continue;
        }
        int is_dotdot = path_component_is_dotdot(data + start, len);
        if (is_dotdot) {
            if (comps > 0) {
                int64_t last_start = starts[comps - 1];
                int64_t last_len = lens[comps - 1];
                int last_is_dotdot = path_component_is_dotdot(
                    work + last_start,
                    last_len
                );
                if (!last_is_dotdot) {
                    comps--;
                    out_len = last_start;
                    if (out_len > base_len && work[out_len - 1] == '/') {
                        out_len--;
                    }
                    continue;
                }
            }
            if (initial > 0) {
                continue;
            }
        }
        if (out_len > base_len && work[out_len - 1] != '/') {
            work[out_len++] = '/';
        }
        starts[comps] = out_len;
        lens[comps] = len;
        memcpy(work + out_len, data + start, (size_t)len);
        out_len += len;
        comps++;
    }

    PyObject *out = NULL;
    if (out_len == 0) {
        out = py_str_new(".", 1);
    } else {
        out = py_str_new(work, out_len);
    }
    free(work);
    free(starts);
    free(lens);
    py_decref(owned);
    return out;
}

PyObject *py_os_path_splitdrive(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return NULL;
    }
    PyStrObject *s = (PyStrObject *)item;
    PyObject *drive = py_str_new("", 0);
    PyObject *tail = py_str_new(s->data, s->byte_len);
    PyObject *out = py_tuple_new(2);
    if (out != NULL) {
        py_tuple_set_item(out, 0, drive);
        py_tuple_set_item(out, 1, tail);
    } else {
        py_decref(drive);
        py_decref(tail);
    }
    py_decref(owned);
    return out;
}

PyObject *py_os_path_commonprefix(PyObject *paths) {
    int64_t n = path_seq_len(paths);
    if (n < 0) return NULL;
    if (n == 0) return py_str_new("", 0);

    PyObject *first_owned = NULL;
    PyObject *first = coerce_path_str(path_seq_borrow(paths, 0), &first_owned);
    if (first == NULL) {
        py_decref(first_owned);
        return NULL;
    }
    PyStrObject *first_s = (PyStrObject *)first;
    const char *first_data = first_s->data;
    int64_t common_len = first_s->byte_len;

    for (int64_t i = 1; i < n; i++) {
        PyObject *owned = NULL;
        PyObject *item = coerce_path_str(path_seq_borrow(paths, i), &owned);
        if (item == NULL) {
            py_decref(owned);
            py_decref(first_owned);
            return NULL;
        }
        PyStrObject *s = (PyStrObject *)item;
        int64_t limit = common_len < s->byte_len ? common_len : s->byte_len;
        int64_t j = 0;
        while (j < limit && first_data[j] == s->data[j]) {
            j++;
        }
        common_len = j;
        py_decref(owned);
        if (common_len == 0) break;
    }

    PyObject *out = py_str_new(first_data, common_len);
    py_decref(first_owned);
    return out;
}

int py_os_path_exists(PyObject *path) {
    PyObject *owned = NULL;
    PyObject *item = coerce_path_str(path, &owned);
    if (item == NULL) {
        py_decref(owned);
        return 0;
    }
    const char *raw = py_str_utf8(item);
    int ok = (raw != NULL && access(raw, F_OK) == 0) ? 1 : 0;
    py_decref(owned);
    return ok;
}
