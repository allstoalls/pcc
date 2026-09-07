#include "py_internal.h"
#include <stdlib.h>
#include <string.h>

static int64_t as_index(PyObject *k) {
    if (k == NULL || py_type_of(k) != PY_TYPE_INT) return -1;
    return py_int_value_i64(k);
}

PyObject *py_bytes_new(const char *data, int64_t byte_len) {
    if (byte_len < 0) byte_len = 0;
    size_t total = sizeof(PyBytesObject) + (size_t)byte_len + 1;
    PyBytesObject *b = (PyBytesObject *)pcc_gc_alloc(
        (int64_t)total, PY_TYPE_BYTES, 0);
    if (b == NULL) return NULL;
    b->byte_len = byte_len;
    if (byte_len > 0 && data != NULL) {
        memcpy(b->data, data, (size_t)byte_len);
    }
    b->data[byte_len] = '\0';
    return (PyObject *)b;
}

static PyObject *bytearray_new_raw(const char *data, int64_t byte_len) {
    if (byte_len < 0) byte_len = 0;
    size_t total = sizeof(PyByteArrayObject) + (size_t)byte_len + 1;
    PyByteArrayObject *b = (PyByteArrayObject *)pcc_gc_alloc(
        (int64_t)total, PY_TYPE_BYTEARRAY, 0);
    if (b == NULL) return NULL;
    b->byte_len = byte_len;
    if (byte_len > 0 && data != NULL) {
        memcpy(b->data, data, (size_t)byte_len);
    }
    b->data[byte_len] = '\0';
    return (PyObject *)b;
}

static const char *bytes_data(PyObject *o, int64_t *n) {
    if (o == NULL) {
        *n = 0;
        return NULL;
    }
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_BYTES) {
        PyBytesObject *b = (PyBytesObject *)o;
        *n = b->byte_len;
        return b->data;
    }
    if (tag == PY_TYPE_BYTEARRAY) {
        PyByteArrayObject *b = (PyByteArrayObject *)o;
        *n = b->byte_len;
        return b->data;
    }
    if (tag == PY_TYPE_MEMORYVIEW) {
        PyMemoryViewObject *m = (PyMemoryViewObject *)o;
        PyObject *base = pcc_gc_load_ptr(o, &m->base);
        return bytes_data(base, n);
    }
    *n = 0;
    return NULL;
}

static int byte_from_obj(PyObject *o, int64_t *out) {
    if (o == NULL) {
        return -1;
    }
    if (PY_IS_TAGGED_INT(o)) {
        *out = py_untag_int(o);
        return 0;
    }
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_BOOL) {
        if (o == py_True) {
            *out = 1;
            return 0;
        }
        if (o == py_False) {
            *out = 0;
            return 0;
        }
        return -1;
    }
    if (tag == PY_TYPE_INT) {
        int overflow = 0;
        *out = py_int_to_i64(o, &overflow);
        return overflow ? -1 : 0;
    }
    return -1;
}

static PyObject *bytes_from_int_sequence(PyObject *o, int as_bytearray) {
    int64_t n = 0;
    char *tmp = NULL;
    PyObject *out = NULL;
    int is_list = py_type_of(o) == PY_TYPE_LIST;
    int is_tuple = py_type_of(o) == PY_TYPE_TUPLE;

    if (is_list) {
        n = py_list_len(o);
    } else if (is_tuple) {
        n = py_tuple_len(o);
    } else {
        return NULL;
    }

    if (n <= 0) {
        return as_bytearray ? bytearray_new_raw(NULL, 0) : py_bytes_new(NULL, 0);
    }
    tmp = (char *)malloc((size_t)n);
    if (tmp == NULL) {
        return NULL;
    }
    for (int64_t i = 0; i < n; i++) {
        PyObject *item = is_list ? py_list_get(o, i) : py_tuple_get(o, i);
        if (item == NULL) {
            free(tmp);
            return NULL;
        }
        int64_t byte = 0;
        if (byte_from_obj(item, &byte) != 0 || byte < 0 || byte > 255) {
            py_decref(item);
            free(tmp);
            return NULL;
        }
        tmp[i] = (char)(unsigned char)byte;
        py_decref(item);
    }
    out = as_bytearray ? bytearray_new_raw(tmp, n) : py_bytes_new(tmp, n);
    free(tmp);
    return out;
}

static int bytes_is_none_or_null(PyObject *o) {
    return o == NULL || o == py_None;
}

static int bytes_normalize_slice(PyObject *lo, PyObject *hi, PyObject *step,
                                 int64_t len, int64_t *lo_out,
                                 int64_t *hi_out, int64_t *step_out) {
    int64_t step_v = 1;
    if (!bytes_is_none_or_null(step)) {
        step_v = py_int_value_i64(step);
        if (step_v == 0) return -1;
    }

    int64_t lo_v, hi_v;
    if (step_v > 0) {
        lo_v = bytes_is_none_or_null(lo) ? 0   : py_int_value_i64(lo);
        hi_v = bytes_is_none_or_null(hi) ? len : py_int_value_i64(hi);
    } else {
        lo_v = bytes_is_none_or_null(lo) ? len - 1 : py_int_value_i64(lo);
        hi_v = bytes_is_none_or_null(hi) ? -1      : py_int_value_i64(hi);
    }

    if (step_v > 0) {
        if (lo_v < 0) {
            lo_v += len;
            if (lo_v < 0) lo_v = 0;
        }
        if (lo_v > len) lo_v = len;
        if (hi_v < 0) {
            hi_v += len;
            if (hi_v < 0) hi_v = 0;
        }
        if (hi_v > len) hi_v = len;
    } else {
        if (lo_v < 0) {
            lo_v += len;
            if (lo_v < 0) lo_v = -1;
        }
        if (lo_v >= len) lo_v = len - 1;
        if (hi_v < 0) {
            if (bytes_is_none_or_null(hi)) {
                hi_v = -1;
            } else {
                hi_v += len;
                if (hi_v < 0) hi_v = -1;
            }
        }
        if (hi_v >= len) hi_v = len - 1;
    }

    *lo_out = lo_v;
    *hi_out = hi_v;
    *step_out = step_v;
    return 0;
}

static int64_t bytes_slice_count(int64_t lo, int64_t hi, int64_t step) {
    int64_t count = 0;
    if (step > 0) {
        for (int64_t i = lo; i < hi; i += step) count++;
    } else {
        for (int64_t i = lo; i > hi; i += step) count++;
    }
    return count;
}

static PyObject *bytes_new_same_family(PyObject *src, const char *data, int64_t n) {
    if (src != NULL && py_type_of(src) == PY_TYPE_BYTEARRAY) {
        return bytearray_new_raw(data, n);
    }
    return py_bytes_new(data, n);
}

static char *bytes_mutable_data(PyObject *o, int64_t *n) {
    if (o == NULL) {
        *n = 0;
        return NULL;
    }
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_BYTES) {
        PyBytesObject *b = (PyBytesObject *)o;
        *n = b->byte_len;
        return b->data;
    }
    if (tag == PY_TYPE_BYTEARRAY) {
        PyByteArrayObject *b = (PyByteArrayObject *)o;
        *n = b->byte_len;
        return b->data;
    }
    *n = 0;
    return NULL;
}

static int bytes_concat_operand(PyObject *o) {
    int32_t tag = py_type_of(o);
    return tag == PY_TYPE_BYTES || tag == PY_TYPE_BYTEARRAY;
}

static PyObject *bytes_from_integer_count(PyObject *o, int mutable) {
    int overflow = 0;
    int64_t n = py_type_of(o) == PY_TYPE_BOOL
        ? (o == py_True ? 1 : 0) : py_int_to_i64(o, &overflow);
    if (overflow) {
        py_raise_owned(py_exc_new(PY_EXC_OVERFLOWERROR,
            "byte count does not fit in an index-sized integer"));
        return NULL;
    }
    if (n < 0) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "negative count"));
        return NULL;
    }
    if (n > INT64_MAX - (int64_t)sizeof(PyBytesObject) - 1) {
        py_raise_owned(py_exc_new(mutable ? PY_EXC_MEMORYERROR : PY_EXC_OVERFLOWERROR,
            "byte count is too large"));
        return NULL;
    }
    PyObject *result = mutable ? bytearray_new_raw(NULL, n) : py_bytes_new(NULL, n);
    if (result == NULL) {
        if (!py_err_occurred())
            py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR, "byte allocation failed"));
        return NULL;
    }
    if (n > 0) memset(((PyBytesObject *)result)->data, 0, (size_t)n);
    return result;
}

PyObject *py_bytearray_from_obj(PyObject *o) {
    if (o == NULL) {
        return bytearray_new_raw(NULL, 0);
    }
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_INT || tag == PY_TYPE_BOOL) return bytes_from_integer_count(o, 1);
    if (tag == PY_TYPE_LIST || tag == PY_TYPE_TUPLE) {
        return bytes_from_int_sequence(o, 1);
    }
    return bytearray_new_raw(data, n);
}

PyObject *py_bytes_from_obj(PyObject *o) {
    if (o == NULL) {
        return py_bytes_new(NULL, 0);
    }
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_INT || tag == PY_TYPE_BOOL) return bytes_from_integer_count(o, 0);
    if (tag == PY_TYPE_LIST || tag == PY_TYPE_TUPLE) {
        return bytes_from_int_sequence(o, 0);
    }
    return py_bytes_new(data, n);
}

PyObject *py_str_utf8_encode(PyObject *s) {
    const char *in = py_str_utf8(s);
    int64_t n = py_str_byte_len(s);
    if (in == NULL || n <= 0) {
        return py_bytes_new(NULL, 0);
    }
    return py_bytes_new(in, n);
}

PyObject *py_str_latin1_encode(PyObject *s) {
    const unsigned char *in = (const unsigned char *)py_str_utf8(s);
    int64_t n = py_str_byte_len(s);
    if (in == NULL || n <= 0) {
        return py_bytes_new(NULL, 0);
    }

    unsigned char *out = (unsigned char *)malloc((size_t)n);
    if (out == NULL) return NULL;
    int64_t i = 0;
    int64_t j = 0;
    while (i < n) {
        uint32_t cp;
        unsigned char c = in[i];
        if (c < 0x80u) {
            cp = c;
            i += 1;
        } else if ((c & 0xE0u) == 0xC0u && i + 1 < n) {
            cp = ((uint32_t)(c & 0x1Fu) << 6)
                | (uint32_t)(in[i + 1] & 0x3Fu);
            i += 2;
        } else if ((c & 0xF0u) == 0xE0u && i + 2 < n) {
            cp = ((uint32_t)(c & 0x0Fu) << 12)
                | ((uint32_t)(in[i + 1] & 0x3Fu) << 6)
                | (uint32_t)(in[i + 2] & 0x3Fu);
            i += 3;
        } else if ((c & 0xF8u) == 0xF0u && i + 3 < n) {
            cp = ((uint32_t)(c & 0x07u) << 18)
                | ((uint32_t)(in[i + 1] & 0x3Fu) << 12)
                | ((uint32_t)(in[i + 2] & 0x3Fu) << 6)
                | (uint32_t)(in[i + 3] & 0x3Fu);
            i += 4;
        } else {
            free(out);
            return NULL;
        }
        if (cp > 255u) {
            free(out);
            return NULL;
        }
        out[j++] = (unsigned char)cp;
    }
    PyObject *bytes = py_bytes_new((const char *)out, j);
    free(out);
    return bytes;
}

PyObject *py_memoryview_new(PyObject *o) {
    PyMemoryViewObject *m = (PyMemoryViewObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyMemoryViewObject), PY_TYPE_MEMORYVIEW, 0);
    if (m == NULL) return NULL;
    m->base = NULL;
    pcc_gc_store_ptr((PyObject *)m, &m->base, o);
    pcc_gc_publish_initialized((PyObject *)m);
    return (PyObject *)m;
}

void py_dealloc_memoryview(PyObject *o) {
    if (o == NULL) return;
    PyMemoryViewObject *m = (PyMemoryViewObject *)o;
    PyObject *base = pcc_gc_load_ptr(o, &m->base);
    if (base != NULL) py_decref(base);
    pcc_gc_free_object_memory(o);
}

PyObject *py_bytes_decode(PyObject *o) {
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    return py_str_new(data, n);
}

static int utf8_cont(unsigned char c) {
    return (c & 0xC0u) == 0x80u;
}

static int utf8_valid_width(const unsigned char *data, int64_t n, int64_t i) {
    unsigned char c = data[i];
    if (c < 0x80u) return 1;
    if (c >= 0xC2u && c <= 0xDFu) {
        return i + 1 < n && utf8_cont(data[i + 1]) ? 2 : 0;
    }
    if (c == 0xE0u) {
        return i + 2 < n
            && data[i + 1] >= 0xA0u && data[i + 1] <= 0xBFu
            && utf8_cont(data[i + 2]) ? 3 : 0;
    }
    if (c >= 0xE1u && c <= 0xECu) {
        return i + 2 < n
            && utf8_cont(data[i + 1])
            && utf8_cont(data[i + 2]) ? 3 : 0;
    }
    if (c == 0xEDu) {
        return i + 2 < n
            && data[i + 1] >= 0x80u && data[i + 1] <= 0x9Fu
            && utf8_cont(data[i + 2]) ? 3 : 0;
    }
    if (c >= 0xEEu && c <= 0xEFu) {
        return i + 2 < n
            && utf8_cont(data[i + 1])
            && utf8_cont(data[i + 2]) ? 3 : 0;
    }
    if (c == 0xF0u) {
        return i + 3 < n
            && data[i + 1] >= 0x90u && data[i + 1] <= 0xBFu
            && utf8_cont(data[i + 2])
            && utf8_cont(data[i + 3]) ? 4 : 0;
    }
    if (c >= 0xF1u && c <= 0xF3u) {
        return i + 3 < n
            && utf8_cont(data[i + 1])
            && utf8_cont(data[i + 2])
            && utf8_cont(data[i + 3]) ? 4 : 0;
    }
    if (c == 0xF4u) {
        return i + 3 < n
            && data[i + 1] >= 0x80u && data[i + 1] <= 0x8Fu
            && utf8_cont(data[i + 2])
            && utf8_cont(data[i + 3]) ? 4 : 0;
    }
    return 0;
}

PyObject *py_bytes_decode_utf8_ignore(PyObject *o) {
    int64_t n = 0;
    const char *raw = bytes_data(o, &n);
    if (raw == NULL || n <= 0) return py_str_new(NULL, 0);
    const unsigned char *data = (const unsigned char *)raw;
    char *tmp = (char *)malloc((size_t)n);
    if (tmp == NULL) return NULL;
    int64_t out_n = 0;
    int64_t i = 0;
    while (i < n) {
        int width = utf8_valid_width(data, n, i);
        if (width <= 0) {
            i++;
            continue;
        }
        for (int j = 0; j < width; j++) {
            tmp[out_n++] = (char)data[i + j];
        }
        i += width;
    }
    PyObject *out = py_str_new(tmp, out_n);
    free(tmp);
    return out;
}

static int pcc_ascii_lower(int c) {
    return c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c;
}

static int pcc_str_is_utf8_name(PyObject *obj) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj) || py_type_of(obj) != PY_TYPE_STR) {
        return 0;
    }
    const char *text = py_str_utf8(obj);
    int64_t n = py_str_byte_len(obj);
    if (text == NULL) return 0;
    if (n == 4) {
        return pcc_ascii_lower((unsigned char)text[0]) == 'u'
            && pcc_ascii_lower((unsigned char)text[1]) == 't'
            && pcc_ascii_lower((unsigned char)text[2]) == 'f'
            && text[3] == '8';
    }
    if (n == 5) {
        return pcc_ascii_lower((unsigned char)text[0]) == 'u'
            && pcc_ascii_lower((unsigned char)text[1]) == 't'
            && pcc_ascii_lower((unsigned char)text[2]) == 'f'
            && (text[3] == '-' || text[3] == '_')
            && text[4] == '8';
    }
    return 0;
}

static int pcc_str_is_ascii_word(PyObject *obj, const char *word) {
    if (obj == NULL || PY_IS_TAGGED_INT(obj) || py_type_of(obj) != PY_TYPE_STR) {
        return 0;
    }
    const char *text = py_str_utf8(obj);
    int64_t n = py_str_byte_len(obj);
    size_t word_n = strlen(word);
    if (text == NULL || n != (int64_t)word_n) return 0;
    for (size_t i = 0; i < word_n; i++) {
        if (pcc_ascii_lower((unsigned char)text[i]) != word[i]) return 0;
    }
    return 1;
}

PyObject *py_bytes_decode_with_encoding(
    PyObject *o,
    PyObject *encoding,
    PyObject *errors
) {
    int32_t tag = o == NULL ? -1 : py_type_of(o);
    if (
        o == NULL
        || (tag != PY_TYPE_BYTES
            && tag != PY_TYPE_BYTEARRAY
            && tag != PY_TYPE_MEMORYVIEW)
    ) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "decoding to str: need bytes-like object"));
        return NULL;
    }
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    if (!pcc_str_is_utf8_name(encoding)) {
        py_raise_owned(py_exc_new(PY_EXC_LOOKUPERROR, "pcc-native bytes decode supports utf-8 only"));
        return NULL;
    }
    if (errors == NULL || errors == py_None || pcc_str_is_ascii_word(errors, "strict")) {
        return py_str_new(data, n);
    }
    if (pcc_str_is_ascii_word(errors, "ignore")) {
        return py_bytes_decode_utf8_ignore(o);
    }
    py_raise_owned(py_exc_new(PY_EXC_LOOKUPERROR, "unsupported pcc-native bytes decode errors mode"));
    return NULL;
}

/* bytes.hex(): lowercase two-hex-digits-per-byte string (no separator). */
PyObject *py_bytes_hex(PyObject *o) {
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    if (data == NULL) return py_str_new("", 0);
    char *buf = (char *)malloc((size_t)(n * 2 + 1));
    if (buf == NULL) return NULL;
    static const char hx[] = "0123456789abcdef";
    for (int64_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)data[i];
        buf[i * 2] = hx[(c >> 4) & 0xF];
        buf[i * 2 + 1] = hx[c & 0xF];
    }
    PyObject *out = py_str_new(buf, n * 2);
    free(buf);
    return out;
}

PyObject *py_bytes_upper(PyObject *o) {
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    if (data == NULL) return NULL;
    PyObject *out = bytes_new_same_family(o, NULL, n);
    if (out == NULL) return NULL;
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != n) return NULL;
    for (int64_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c >= (unsigned char)'a' && c <= (unsigned char)'z') {
            c = (unsigned char)(c - ((unsigned char)'a' - (unsigned char)'A'));
        }
        dst[i] = (char)c;
    }
    dst[n] = '\0';
    return out;
}

PyObject *py_bytes_getitem(PyObject *o, PyObject *k) {
    int64_t i = as_index(k);
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    if (i < 0) i += n;
    if (data == NULL || i < 0 || i >= n) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "bytes index out of range"));
        return NULL;
    }
    return py_int_from_i64((unsigned char)data[i]);
}

PyObject *py_bytes_slice(PyObject *o, PyObject *lo, PyObject *hi, PyObject *step) {
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    if (data == NULL) return NULL;

    int64_t lo_v, hi_v, step_v;
    if (bytes_normalize_slice(lo, hi, step, n, &lo_v, &hi_v, &step_v) != 0) {
        return NULL;
    }
    int64_t count = bytes_slice_count(lo_v, hi_v, step_v);
    if (count <= 0) {
        return bytes_new_same_family(o, NULL, 0);
    }
    if (step_v == 1) {
        return bytes_new_same_family(o, data + lo_v, count);
    }

    char *tmp = (char *)malloc((size_t)count);
    if (tmp == NULL) return NULL;
    int64_t j = 0;
    if (step_v > 0) {
        for (int64_t i = lo_v; i < hi_v; i += step_v) {
            tmp[j++] = data[i];
        }
    } else {
        for (int64_t i = lo_v; i > hi_v; i += step_v) {
            if (i < 0 || i >= n) break;
            tmp[j++] = data[i];
        }
    }
    PyObject *out = bytes_new_same_family(o, tmp, j);
    free(tmp);
    return out;
}

PyObject *py_bytes_concat(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) return NULL;
    if (!bytes_concat_operand(a) || !bytes_concat_operand(b)) return NULL;

    int64_t an = 0;
    int64_t bn = 0;
    const char *ad = bytes_data(a, &an);
    const char *bd = bytes_data(b, &bn);
    if (ad == NULL || bd == NULL || an < 0 || bn < 0) return NULL;
    if (an > INT64_MAX - bn) return NULL;

    int64_t total = an + bn;
    PyObject *out = bytes_new_same_family(a, NULL, total);
    if (out == NULL) return NULL;
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != total) return NULL;
    if (an > 0) memcpy(dst, ad, (size_t)an);
    if (bn > 0) memcpy(dst + an, bd, (size_t)bn);
    dst[total] = '\0';
    return out;
}

PyObject *py_bytes_join(PyObject *sep, PyObject *list) {
    /* bytes.join / bytearray.join over a list or tuple of bytes-like items.
     * Mirror of py_bytes_join in py/py_obj_stubs.py. */
    if (sep == NULL || list == NULL) return NULL;
    sep = pcc_gc_note_relocation_read(sep);
    list = pcc_gc_note_relocation_read(list);
    if (!bytes_concat_operand(sep)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytes.join receiver must be bytes-like"));
        return NULL;
    }
    int32_t sequence_tag = py_type_of(list);
    if (sequence_tag != PY_TYPE_LIST && sequence_tag != PY_TYPE_TUPLE) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytes.join argument must be a list or tuple"));
        return NULL;
    }
    int64_t length = sequence_tag == PY_TYPE_LIST
        ? ((PyListObject *)list)->length
        : ((PyTupleObject *)list)->len;
    if (length == 0) return bytes_new_same_family(sep, NULL, 0);
    int64_t sep_n = 0;
    const char *sep_d = bytes_data(sep, &sep_n);
    if (sep_d == NULL || sep_n < 0) return NULL;

    int64_t total = 0;
    for (int64_t i = 0; i < length; i++) {
        PyObject **slot = sequence_tag == PY_TYPE_LIST
            ? &((PyListObject *)list)->items[i]
            : &((PyTupleObject *)list)->items[i];
        PyObject *e = pcc_gc_load_ptr(list, slot);
        if (e == NULL) return NULL;
        if (!bytes_concat_operand(e)) {
            py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "sequence item: expected a bytes-like object"));
            return NULL;
        }
        int64_t n = 0;
        if (bytes_data(e, &n) == NULL || n < 0) return NULL;
        if (i > 0) {
            if (total > INT64_MAX - sep_n) return NULL;
            total += sep_n;
        }
        if (total > INT64_MAX - n) return NULL;
        total += n;
    }

    PyObject *out = bytes_new_same_family(sep, NULL, total);
    if (out == NULL) return NULL;
    /* Result allocation may relocate the rooted inputs. */
    sep = pcc_gc_note_relocation_read(sep);
    list = pcc_gc_note_relocation_read(list);
    sep_d = bytes_data(sep, &sep_n);
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != total) return NULL;
    int64_t off = 0;
    for (int64_t i = 0; i < length; i++) {
        PyObject **slot = sequence_tag == PY_TYPE_LIST
            ? &((PyListObject *)list)->items[i]
            : &((PyTupleObject *)list)->items[i];
        PyObject *e = pcc_gc_load_ptr(list, slot);
        if (i > 0 && sep_n > 0) {
            memcpy(dst + off, sep_d, (size_t)sep_n);
            off += sep_n;
        }
        int64_t n = 0;
        const char *ed = bytes_data(e, &n);
        if (n > 0) memcpy(dst + off, ed, (size_t)n);
        off += n;
    }
    dst[total] = '\0';
    return out;
}

PyObject *py_bytes_repeat(PyObject *src, int64_t count) {
    if (src == NULL) return NULL;
    int32_t tag = py_type_of(src);
    if (tag != PY_TYPE_BYTES && tag != PY_TYPE_BYTEARRAY) return NULL;
    int64_t n = 0;
    const char *data = bytes_data(src, &n);
    if (data == NULL || n < 0) return NULL;
    if (count <= 0 || n == 0) {
        return bytes_new_same_family(src, NULL, 0);
    }
    if (count > INT64_MAX / n) return NULL;
    int64_t total = count * n;
    PyObject *out = bytes_new_same_family(src, NULL, total);
    if (out == NULL) return NULL;
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != total) return NULL;
    for (int64_t i = 0; i < count; i++) {
        memcpy(dst + i * n, data, (size_t)n);
    }
    dst[total] = '\0';
    return out;
}

PyObject *py_bytes_maketrans(PyObject *x, PyObject *y) {
    int64_t xn = 0;
    int64_t yn = 0;
    const char *xd = bytes_data(x, &xn);
    const char *yd = bytes_data(y, &yn);
    if (xd == NULL || yd == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "maketrans arguments must be bytes-like"));
        return NULL;
    }
    if (xn != yn) {
        py_raise_owned(py_exc_new(
            PY_EXC_VALUEERROR,
            "maketrans arguments must have the same length"
        ));
        return NULL;
    }
    unsigned char table[256];
    for (int64_t i = 0; i < 256; i++) {
        table[i] = (unsigned char)i;
    }
    for (int64_t i = 0; i < xn; i++) {
        table[(unsigned char)xd[i]] = (unsigned char)yd[i];
    }
    return py_bytes_new((const char *)table, 256);
}

PyObject *py_bytes_translate(PyObject *src, PyObject *table) {
    int64_t n = 0;
    int64_t table_n = 0;
    const char *data = bytes_data(src, &n);
    const char *map = bytes_data(table, &table_n);
    if (data == NULL || map == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "translate arguments must be bytes-like"));
        return NULL;
    }
    if (table_n != 256) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "translation table must be 256 characters long"));
        return NULL;
    }
    PyObject *out = bytes_new_same_family(src, NULL, n);
    if (out == NULL) return NULL;
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != n) return NULL;
    for (int64_t i = 0; i < n; i++) {
        dst[i] = map[(unsigned char)data[i]];
    }
    dst[n] = '\0';
    return out;
}

static int hex_value(unsigned char c) {
    if (c >= (unsigned char)'0' && c <= (unsigned char)'9') return (int)(c - (unsigned char)'0');
    if (c >= (unsigned char)'a' && c <= (unsigned char)'f') return (int)(c - (unsigned char)'a') + 10;
    if (c >= (unsigned char)'A' && c <= (unsigned char)'F') return (int)(c - (unsigned char)'A') + 10;
    return -1;
}

static int hex_space(unsigned char c) {
    return c == (unsigned char)' ' || c == (unsigned char)'\t'
        || c == (unsigned char)'\n' || c == (unsigned char)'\r'
        || c == (unsigned char)'\v' || c == (unsigned char)'\f';
}

/* bytes/bytearray .lower(): ASCII A-Z -> a-z, same family + length. */
PyObject *py_bytes_lower(PyObject *o) {
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    if (data == NULL) return NULL;
    PyObject *out = bytes_new_same_family(o, NULL, n);
    if (out == NULL) return NULL;
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != n) return NULL;
    for (int64_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c >= (unsigned char)'A' && c <= (unsigned char)'Z') {
            c = (unsigned char)(c + ((unsigned char)'a' - (unsigned char)'A'));
        }
        dst[i] = (char)c;
    }
    dst[n] = '\0';
    return out;
}

/* bytes/bytearray .strip(): drop leading/trailing ASCII whitespace, return the
 * remaining slice (same family). No-arg (whitespace) form only. */
PyObject *py_bytes_strip(PyObject *o) {
    int64_t n = 0;
    const char *data = bytes_data(o, &n);
    if (data == NULL) return NULL;
    int64_t lo = 0;
    int64_t hi = n;
    while (lo < hi && hex_space((unsigned char)data[lo])) lo++;
    while (hi > lo && hex_space((unsigned char)data[hi - 1])) hi--;
    return bytes_new_same_family(o, data + lo, hi - lo);
}

PyObject *py_bytes_fromhex(PyObject *text) {
    const char *data = NULL;
    int64_t n = 0;
    if (text == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "fromhex() argument must be str"));
        return NULL;
    }
    int32_t tag = py_type_of(text);
    if (tag == PY_TYPE_STR) {
        data = py_str_utf8(text);
        n = py_str_byte_len(text);
    } else {
        data = bytes_data(text, &n);
    }
    if (data == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "fromhex() argument must be str or bytes-like"));
        return NULL;
    }
    char *tmp = (char *)malloc((size_t)(n / 2 + 1));
    if (tmp == NULL) return NULL;
    int64_t out_n = 0;
    int have_hi = 0;
    int hi = 0;
    for (int64_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)data[i];
        if (hex_space(c)) continue;
        int v = hex_value(c);
        if (v < 0) {
            free(tmp);
            py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "non-hexadecimal number found in fromhex() arg"));
            return NULL;
        }
        if (!have_hi) {
            hi = v;
            have_hi = 1;
        } else {
            tmp[out_n++] = (char)((hi << 4) | v);
            have_hi = 0;
        }
    }
    if (have_hi) {
        free(tmp);
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "non-hexadecimal number found in fromhex() arg"));
        return NULL;
    }
    PyObject *out = py_bytes_new(tmp, out_n);
    free(tmp);
    return out;
}

PyObject *py_bytes_replace(PyObject *src, PyObject *old, PyObject *new_value) {
    int64_t n = 0;
    int64_t old_n = 0;
    int64_t new_n = 0;
    const char *data = bytes_data(src, &n);
    const char *old_data = bytes_data(old, &old_n);
    const char *new_data = bytes_data(new_value, &new_n);
    if (data == NULL || old_data == NULL || new_data == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "replace arguments must be bytes-like"));
        return NULL;
    }
    if (old_n <= 0) {
        return bytes_new_same_family(src, data, n);
    }
    int64_t matches = 0;
    for (int64_t i = 0; i <= n - old_n;) {
        if (memcmp(data + i, old_data, (size_t)old_n) == 0) {
            matches++;
            i += old_n;
        } else {
            i++;
        }
    }
    if (matches == 0) {
        return bytes_new_same_family(src, data, n);
    }
    int64_t out_n = n + matches * (new_n - old_n);
    PyObject *out = bytes_new_same_family(src, NULL, out_n);
    if (out == NULL) return NULL;
    int64_t actual_n = 0;
    char *dst = bytes_mutable_data(out, &actual_n);
    if (dst == NULL || actual_n != out_n) return NULL;
    int64_t i = 0;
    int64_t pos = 0;
    while (i < n) {
        if (i <= n - old_n && memcmp(data + i, old_data, (size_t)old_n) == 0) {
            if (new_n > 0) memcpy(dst + pos, new_data, (size_t)new_n);
            pos += new_n;
            i += old_n;
        } else {
            dst[pos++] = data[i++];
        }
    }
    dst[out_n] = '\0';
    return out;
}

int64_t py_bytes_len(PyObject *o) {
    int64_t n = 0;
    (void)bytes_data(o, &n);
    return n;
}

/* ---- Compiler-owned readonly signed-i64 buffer ----------------------- */

#define PCC_I64_BUFFER_LAYOUT_VERSION 1
#define PCC_GUARDED_LOOP_COUNTER_COUNT 6

static int64_t guarded_loop_counters[PCC_GUARDED_LOOP_COUNTER_COUNT];

int64_t py_guarded_loop_counter_add(int64_t counter, int64_t delta) {
    if (counter < 0 || counter >= PCC_GUARDED_LOOP_COUNTER_COUNT) return -1;
    return __atomic_add_fetch(
        &guarded_loop_counters[counter], delta, __ATOMIC_RELAXED);
}

int64_t py_guarded_loop_counter_get(int64_t counter) {
    if (counter < 0 || counter >= PCC_GUARDED_LOOP_COUNTER_COUNT) return -1;
    return __atomic_load_n(&guarded_loop_counters[counter], __ATOMIC_RELAXED);
}

static int i64_buffer_shape(PyObject *buffer, const char **data,
                            int64_t *element_count, int exact_bytes) {
    int64_t byte_len = 0;
    const char *raw = NULL;
    if (buffer != NULL && (!exact_bytes || py_type_of(buffer) == PY_TYPE_BYTES)) {
        raw = bytes_data(buffer, &byte_len);
    }
    if (raw == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "signed-i64 buffer must be bytes-like"));
        return -1;
    }
    if ((byte_len & 7) != 0) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR,
                            "signed-i64 buffer byte length must be divisible by 8"));
        return -1;
    }
    *data = raw;
    *element_count = byte_len / 8;
    return 0;
}

PyObject *py_i64_buffer_new(int64_t element_count) {
    if (element_count < 1 || element_count > 1048576) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR,
                            "pcc.i64_buffer length must be between 1 and 1048576"));
        return NULL;
    }
    int64_t byte_len = element_count * 8;
    PyObject *out = py_bytes_new(NULL, byte_len);
    if (out == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_MEMORYERROR,
                            "unable to allocate pcc.i64_buffer"));
        return NULL;
    }
    memset(((PyBytesObject *)out)->data, 0, (size_t)byte_len);
    return out;
}

int64_t py_i64_buffer_set_item(PyObject *buffer, int64_t index,
                               PyObject *value) {
    const char *raw = NULL;
    int64_t count = 0;
    if (i64_buffer_shape(buffer, &raw, &count, 1) != 0) return -1;
    if (index < 0 || index >= count) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR,
                            "pcc.i64_buffer assignment index out of range"));
        return -1;
    }
    int overflow = 0;
    int64_t integer = py_int_to_i64(value, &overflow);
    if (overflow) {
        py_raise_owned(py_exc_new(PY_EXC_OVERFLOWERROR,
                            "pcc.i64_buffer element does not fit signed i64"));
        return -1;
    }
    unsigned char *dst = (unsigned char *)(uintptr_t)raw + index * 8;
    uint64_t bits = 0;
    memcpy(&bits, &integer, sizeof(bits));
    for (int shift = 0; shift < 8; shift++) {
        dst[shift] = (unsigned char)(bits >> (shift * 8));
    }
    return 0;
}

PyObject *py_i64_buffer_get_item(PyObject *buffer, int64_t index) {
    const char *raw = NULL;
    int64_t count = 0;
    if (i64_buffer_shape(buffer, &raw, &count, 0) != 0) return NULL;
    if (index < 0 || index >= count) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR,
                            "pcc.i64_buffer index out of range"));
        return NULL;
    }
    const unsigned char *src = (const unsigned char *)raw + index * 8;
    uint64_t bits = 0;
    for (int shift = 0; shift < 8; shift++) {
        bits |= ((uint64_t)src[shift]) << (shift * 8);
    }
    int64_t integer = 0;
    memcpy(&integer, &bits, sizeof(integer));
    return py_int_from_i64(integer);
}

const char *py_i64_buffer_data(PyObject *buffer) {
    if (buffer == NULL || py_type_of(buffer) != PY_TYPE_BYTES) return NULL;
    PyBytesObject *bytes = (PyBytesObject *)buffer;
    if ((bytes->byte_len & 7) != 0) return NULL;
    return bytes->data;
}

int64_t py_i64_buffer_layout_version(PyObject *buffer) {
    return py_i64_buffer_data(buffer) != NULL
               ? PCC_I64_BUFFER_LAYOUT_VERSION
               : 0;
}

int64_t py_i64_buffer_version(PyObject *buffer) {
    /* Exact bytes are immutable, so version 1 remains valid for their entire
     * lifetime.  Mutable bytearray/memoryview values deliberately miss. */
    return py_i64_buffer_data(buffer) != NULL ? 1 : 0;
}

PyObject *py_i64_buffer_dot_scalar(PyObject *left, PyObject *right,
                                   int64_t expected_count) {
    const char *left_data = NULL;
    const char *right_data = NULL;
    int64_t left_count = 0;
    int64_t right_count = 0;
    if (i64_buffer_shape(left, &left_data, &left_count, 0) != 0) return NULL;
    if (i64_buffer_shape(right, &right_data, &right_count, 0) != 0) return NULL;
    if (left_count != expected_count || right_count != expected_count) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR,
                            "guarded_i64_dot buffers must match the declared length"));
        return NULL;
    }

    PyObject *accumulator = py_int_from_i64(0);
    if (accumulator == NULL) return NULL;
    for (int64_t index = 0; index < expected_count; index++) {
        /* Preserve the scalar oracle's observable order: left load, right
         * load, multiply, accumulate, then advance the index. */
        PyObject *left_value = py_i64_buffer_get_item(left, index);
        if (left_value == NULL) {
            py_decref(accumulator);
            return NULL;
        }
        PyObject *right_value = py_i64_buffer_get_item(right, index);
        if (right_value == NULL) {
            py_decref(left_value);
            py_decref(accumulator);
            return NULL;
        }
        PyObject *product = py_int_mul(left_value, right_value);
        py_decref(left_value);
        py_decref(right_value);
        if (product == NULL) {
            py_decref(accumulator);
            return NULL;
        }
        PyObject *updated = py_int_add(accumulator, product);
        py_decref(product);
        py_decref(accumulator);
        if (updated == NULL) return NULL;
        accumulator = updated;
    }
    return accumulator;
}

int64_t py_bytes_find(PyObject *src, PyObject *needle) {
    int64_t n = 0;
    const char *data = bytes_data(src, &n);
    if (data == NULL) return -1;

    int64_t byte = 0;
    if (byte_from_obj(needle, &byte) == 0) {
        if (byte < 0 || byte > 255) return -1;
        unsigned char target = (unsigned char)byte;
        for (int64_t i = 0; i < n; i++) {
            if ((unsigned char)data[i] == target) return i;
        }
        return -1;
    }

    int64_t needle_n = 0;
    const char *needle_data = bytes_data(needle, &needle_n);
    if (needle_data == NULL) return -1;
    if (needle_n == 0) return 0;
    if (needle_n > n) return -1;
    int64_t last = n - needle_n;
    for (int64_t i = 0; i <= last; i++) {
        if (data[i] == needle_data[0]
            && memcmp(data + i, needle_data, (size_t)needle_n) == 0) {
            return i;
        }
    }
    return -1;
}

/* bytes/bytearray .rfind(): highest index of the sub-bytes (or single byte
 * value), else -1. Byte-based, mirrors py_bytes_find but scans backward. */
int64_t py_bytes_rfind(PyObject *src, PyObject *needle) {
    int64_t n = 0;
    const char *data = bytes_data(src, &n);
    if (data == NULL) return -1;

    int64_t byte = 0;
    if (byte_from_obj(needle, &byte) == 0) {
        if (byte < 0 || byte > 255) return -1;
        unsigned char target = (unsigned char)byte;
        for (int64_t i = n - 1; i >= 0; i--) {
            if ((unsigned char)data[i] == target) return i;
        }
        return -1;
    }

    int64_t needle_n = 0;
    const char *needle_data = bytes_data(needle, &needle_n);
    if (needle_data == NULL) return -1;
    if (needle_n == 0) return n;  /* CPython: b"abc".rfind(b"") == len */
    if (needle_n > n) return -1;
    for (int64_t i = n - needle_n; i >= 0; i--) {
        if (data[i] == needle_data[0]
            && memcmp(data + i, needle_data, (size_t)needle_n) == 0) {
            return i;
        }
    }
    return -1;
}

/* bytes/bytearray .count(sub): number of non-overlapping occurrences of the
 * sub-bytes (or single byte value). Byte-based, mirrors py_bytes_find's needle
 * handling. CPython semantics: an empty sub-bytes counts len+1 positions
 * (b"abc".count(b"") == 4, b"".count(b"") == 1); non-overlapping means after a
 * match the scan skips past the whole needle (b"aaaa".count(b"aa") == 2). A
 * single byte value counts each matching byte. Returns 0 on a bad receiver or
 * an out-of-range byte value; matches find/rfind's non-raising -1/0 style so
 * the frontend does not need a py_err_occurred() check. */
int64_t py_bytes_count(PyObject *src, PyObject *needle) {
    int64_t n = 0;
    const char *data = bytes_data(src, &n);
    if (data == NULL) return 0;

    int64_t byte = 0;
    if (byte_from_obj(needle, &byte) == 0) {
        if (byte < 0 || byte > 255) return 0;
        unsigned char target = (unsigned char)byte;
        int64_t count = 0;
        for (int64_t i = 0; i < n; i++) {
            if ((unsigned char)data[i] == target) count++;
        }
        return count;
    }

    int64_t needle_n = 0;
    const char *needle_data = bytes_data(needle, &needle_n);
    if (needle_data == NULL) return 0;
    if (needle_n == 0) return n + 1;  /* CPython: len+1 empty-sub positions */
    if (needle_n > n) return 0;
    int64_t count = 0;
    int64_t last = n - needle_n;
    for (int64_t i = 0; i <= last;) {
        if (data[i] == needle_data[0]
            && memcmp(data + i, needle_data, (size_t)needle_n) == 0) {
            count++;
            i += needle_n;  /* non-overlapping: skip past the whole match */
        } else {
            i++;
        }
    }
    return count;
}

/* bytes/bytearray .split(sep): list of the same-family pieces between each
 * occurrence of the (non-empty) separator. Mirrors py_str_split but produces
 * bytes/bytearray parts. No-arg (whitespace) split is handled in the frontend
 * fallback, not here. */
PyObject *py_bytes_split(PyObject *src, PyObject *sep) {
    int64_t n = 0;
    const char *data = bytes_data(src, &n);
    if (data == NULL) return NULL;
    int64_t sep_n = 0;
    const char *sep_data = bytes_data(sep, &sep_n);
    if (sep_data == NULL) return NULL;
    if (sep_n == 0) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "empty separator"));
        return NULL;
    }
    PyObject *list = py_list_new(4);
    if (list == NULL) return NULL;
    int64_t start = 0;
    int64_t i = 0;
    while (i + sep_n <= n) {
        if (data[i] == sep_data[0]
            && memcmp(data + i, sep_data, (size_t)sep_n) == 0) {
            PyObject *part = bytes_new_same_family(src, data + start, i - start);
            if (part == NULL) { py_decref(list); return NULL; }
            py_list_append(list, part);
            py_decref(part);
            i += sep_n;
            start = i;
        } else {
            i++;
        }
    }
    PyObject *tail = bytes_new_same_family(src, data + start, n - start);
    if (tail == NULL) { py_decref(list); return NULL; }
    py_list_append(list, tail);
    py_decref(tail);
    return list;
}

/* bytes/bytearray .partition(sep): (before, sep, after) on the first sep
 * occurrence, else (copy-of-whole, b'', b''). Same-family parts.
 * py_tuple_set_item increfs, so created parts are decref'd. */
PyObject *py_bytes_partition(PyObject *src, PyObject *sep) {
    int64_t n = 0;
    const char *data = bytes_data(src, &n);
    if (data == NULL) return NULL;
    int64_t sep_n = 0;
    const char *sep_data = bytes_data(sep, &sep_n);
    if (sep_data == NULL) return NULL;
    int64_t found = -1;
    if (sep_n > 0) {
        int64_t i = 0;
        while (i + sep_n <= n) {
            if (data[i] == sep_data[0]
                && memcmp(data + i, sep_data, (size_t)sep_n) == 0) {
                found = i;
                break;
            }
            i++;
        }
    }
    PyObject *t = py_tuple_new(3);
    if (t == NULL) return NULL;
    if (found < 0) {
        PyObject *whole = bytes_new_same_family(src, data, n);
        PyObject *e1 = bytes_new_same_family(src, NULL, 0);
        PyObject *e2 = bytes_new_same_family(src, NULL, 0);
        if (whole == NULL || e1 == NULL || e2 == NULL) {
            py_decref(t); return NULL;
        }
        py_tuple_set_item(t, 0, whole); py_decref(whole);
        py_tuple_set_item(t, 1, e1); py_decref(e1);
        py_tuple_set_item(t, 2, e2); py_decref(e2);
    } else {
        PyObject *before = bytes_new_same_family(src, data, found);
        PyObject *mid = bytes_new_same_family(src, data + found, sep_n);
        PyObject *after = bytes_new_same_family(
            src, data + found + sep_n, n - found - sep_n);
        if (before == NULL || mid == NULL || after == NULL) {
            py_decref(t); return NULL;
        }
        py_tuple_set_item(t, 0, before); py_decref(before);
        py_tuple_set_item(t, 1, mid); py_decref(mid);
        py_tuple_set_item(t, 2, after); py_decref(after);
    }
    return t;
}

PyObject *py_bytearray_extend(PyObject *o, PyObject *iterable) {
    if (o == NULL || py_type_of(o) != PY_TYPE_BYTEARRAY) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytearray.extend target must be bytearray"));
        return NULL;
    }
    if (iterable == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytearray.extend argument must be iterable"));
        return NULL;
    }

    int64_t an = 0;
    int64_t bn = 0;
    const char *ad = bytes_data(o, &an);
    const char *bd = bytes_data(iterable, &bn);
    PyObject *tmp = NULL;
    if (bd == NULL) {
        int32_t tag = py_type_of(iterable);
        if (tag == PY_TYPE_LIST || tag == PY_TYPE_TUPLE) {
            tmp = bytes_from_int_sequence(iterable, 1);
            if (tmp != NULL) {
                bd = bytes_data(tmp, &bn);
            }
        }
    }
    if (ad == NULL || bd == NULL || an < 0 || bn < 0) {
        if (tmp != NULL) py_decref(tmp);
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytearray.extend argument must be bytes-like or an int sequence"));
        return NULL;
    }
    if (an > INT64_MAX - bn) {
        if (tmp != NULL) py_decref(tmp);
        py_raise_owned(py_exc_new(PY_EXC_OVERFLOWERROR, "bytearray too large"));
        return NULL;
    }

    int64_t total = an + bn;
    PyObject *out = bytearray_new_raw(NULL, total);
    if (out == NULL) {
        if (tmp != NULL) py_decref(tmp);
        return NULL;
    }
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != total) {
        if (tmp != NULL) py_decref(tmp);
        return NULL;
    }
    if (an > 0) memcpy(dst, ad, (size_t)an);
    if (bn > 0) memcpy(dst + an, bd, (size_t)bn);
    dst[total] = '\0';
    if (tmp != NULL) py_decref(tmp);
    return out;
}

PyObject *py_bytearray_append(PyObject *o, PyObject *item) {
    if (o == NULL || py_type_of(o) != PY_TYPE_BYTEARRAY) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytearray.append target must be bytearray"));
        return NULL;
    }
    if (item == NULL || py_type_of(item) != PY_TYPE_INT) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "'object' object cannot be interpreted as an integer"));
        return NULL;
    }
    int64_t byte = py_int_value_i64(item);
    if (byte < 0 || byte > 255) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "byte must be in range(0, 256)"));
        return NULL;
    }

    int64_t an = 0;
    const char *ad = bytes_data(o, &an);
    if (ad == NULL || an < 0) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytearray.append target must be bytearray"));
        return NULL;
    }
    if (an > INT64_MAX - 1) {
        py_raise_owned(py_exc_new(PY_EXC_OVERFLOWERROR, "bytearray too large"));
        return NULL;
    }

    int64_t total = an + 1;
    PyObject *out = bytearray_new_raw(NULL, total);
    if (out == NULL) return NULL;
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != total) return NULL;
    if (an > 0) memcpy(dst, ad, (size_t)an);
    dst[an] = (char)(unsigned char)byte;
    dst[total] = '\0';
    return out;
}

/* bytearray.insert(index, byte): grow the buffer by one, shifting the tail
 * right, and store ``byte`` at the clamped index. Mirrors CPython's
 * bytearray.insert index clamping (negative offsets add len; then clamp into
 * [0, len]). The inline data[] layout has no spare capacity, so growth means
 * building a fresh object; the frontend re-binds the target to the result
 * (same model as py_bytearray_append). Returns the new bytearray, or NULL
 * with an exception set. */
PyObject *py_bytearray_insert(PyObject *o, PyObject *index, PyObject *item) {
    if (o == NULL || py_type_of(o) != PY_TYPE_BYTEARRAY) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytearray.insert target must be bytearray"));
        return NULL;
    }
    if (index == NULL || py_type_of(index) != PY_TYPE_INT) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "'object' object cannot be interpreted as an integer"));
        return NULL;
    }
    if (item == NULL || py_type_of(item) != PY_TYPE_INT) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR,
                            "'object' object cannot be interpreted as an integer"));
        return NULL;
    }
    int64_t byte = py_int_value_i64(item);
    if (byte < 0 || byte > 255) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "byte must be in range(0, 256)"));
        return NULL;
    }

    int64_t an = 0;
    const char *ad = bytes_data(o, &an);
    if (ad == NULL || an < 0) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "bytearray.insert target must be bytearray"));
        return NULL;
    }
    if (an > INT64_MAX - 1) {
        py_raise_owned(py_exc_new(PY_EXC_OVERFLOWERROR, "bytearray too large"));
        return NULL;
    }

    int64_t at = py_int_value_i64(index);
    if (at < 0) {
        at += an;
        if (at < 0) at = 0;
    }
    if (at > an) at = an;

    int64_t total = an + 1;
    PyObject *out = bytearray_new_raw(NULL, total);
    if (out == NULL) return NULL;
    int64_t out_n = 0;
    char *dst = bytes_mutable_data(out, &out_n);
    if (dst == NULL || out_n != total) return NULL;
    if (at > 0) memcpy(dst, ad, (size_t)at);
    dst[at] = (char)(unsigned char)byte;
    if (an - at > 0) memcpy(dst + at + 1, ad + at, (size_t)(an - at));
    dst[total] = '\0';
    return out;
}

/* bytearray.pop([index]): remove and return the byte at ``index`` (default the
 * last element) as an int. Shrinking never needs more room, so this mutates
 * the receiver in place (memmove the tail down + decrement byte_len), matching
 * the in-place py_bytearray_del_slice model. A None/non-int index means "last
 * element" (pop() == pop(-1)). Returns the popped int, or NULL with an
 * exception set. */
PyObject *py_bytearray_pop(PyObject *o, PyObject *index) {
    if (o == NULL || py_type_of(o) != PY_TYPE_BYTEARRAY) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "pop() requires a bytearray"));
        return NULL;
    }
    PyByteArrayObject *b = (PyByteArrayObject *)o;
    int64_t len = b->byte_len;
    if (len <= 0) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "pop from empty bytearray"));
        return NULL;
    }

    int64_t at;
    if (bytes_is_none_or_null(index) || py_type_of(index) != PY_TYPE_INT) {
        at = len - 1;
    } else {
        at = py_int_value_i64(index);
        if (at < 0) at += len;
    }
    if (at < 0 || at >= len) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "pop index out of range"));
        return NULL;
    }

    int64_t byte = (int64_t)(unsigned char)b->data[at];
    int64_t tail = len - at - 1;
    if (tail > 0) {
        memmove(b->data + at, b->data + at + 1, (size_t)tail);
    }
    b->byte_len = len - 1;
    b->data[b->byte_len] = '\0';
    return py_int_from_i64(byte);
}

int64_t py_bytearray_setitem(PyObject *o, PyObject *k, PyObject *v) {
    if (py_type_of(o) != PY_TYPE_BYTEARRAY) return -1;
    int64_t i = as_index(k);
    int64_t byte = py_int_value_i64(v);
    PyByteArrayObject *b = (PyByteArrayObject *)o;
    if (i < 0) i += b->byte_len;
    if (i < 0 || i >= b->byte_len) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "bytearray index out of range"));
        return -1;
    }
    if (byte < 0 || byte > 255) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "byte must be in range(0, 256)"));
        return -1;
    }
    b->data[i] = (char)(unsigned char)byte;
    return 0;
}

static int bytearray_delete_selected(PyByteArrayObject *b,
                                     int64_t lo, int64_t hi, int64_t step) {
    int64_t len = b->byte_len;
    int64_t write = 0;
    for (int64_t read = 0; read < len; read++) {
        int selected = 0;
        if (step > 0) {
            selected = read >= lo && read < hi && ((read - lo) % step) == 0;
        } else {
            int64_t neg_step = -step;
            selected = read <= lo && read > hi && ((lo - read) % neg_step) == 0;
        }
        if (!selected) {
            b->data[write++] = b->data[read];
        }
    }
    b->byte_len = write;
    b->data[write] = '\0';
    return 0;
}

int64_t py_bytearray_del_slice(PyObject *o, PyObject *lo, PyObject *hi,
                               PyObject *step) {
    if (py_type_of(o) != PY_TYPE_BYTEARRAY) return -1;
    PyByteArrayObject *b = (PyByteArrayObject *)o;
    int64_t len = b->byte_len;
    int64_t lo_v, hi_v, step_v;
    if (bytes_normalize_slice(lo, hi, step, len, &lo_v, &hi_v, &step_v) != 0) {
        return -1;
    }
    if (step_v == 1) {
        if (hi_v <= lo_v) return 0;
        int64_t tail = len - hi_v;
        if (tail > 0) {
            memmove(b->data + lo_v, b->data + hi_v, (size_t)tail);
        }
        b->byte_len = len - (hi_v - lo_v);
        b->data[b->byte_len] = '\0';
        return 0;
    }
    return bytearray_delete_selected(b, lo_v, hi_v, step_v);
}
