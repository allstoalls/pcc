/* pcc/py_runtime/src/py_str_accessors.c
 *
 * PyStrObject accessors and small string helpers split out of py_str.c
 * so they can be independently replaced by py_str_accessors.py. Most
 * functions operate on raw bytes; py_str_len and py_str_find preserve
 * Python's codepoint-visible length/offset semantics.
 *
 * py_str.c now keeps only py_str_new; this file owns the rest of the
 * string ABI so it can be replaced by the pcc-Python port as one unit.
 */

#include "py_internal.h"
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <stdint.h>

static PyStrObject *str_alloc_local(int64_t byte_len);

static int64_t utf8_codepoint_count(const char *bytes, int64_t byte_len) {
    int64_t count = 0;
    for (int64_t i = 0; i < byte_len; i++) {
        unsigned char b = (unsigned char)bytes[i];
        if ((b & 0xC0) != 0x80) count++;
    }
    return count;
}

static int64_t byte_find(const char *hay, int64_t hay_len,
                        const char *need, int64_t need_len) {
    if (need_len == 0) return 0;
    if (need_len > hay_len) return -1;
    int64_t last = hay_len - need_len;
    for (int64_t i = 0; i <= last; i++) {
        if (hay[i] == need[0] && memcmp(hay + i, need, (size_t)need_len) == 0) {
            return i;
        }
    }
    return -1;
}

static int64_t byte_rfind(const char *hay, int64_t hay_len,
                         const char *need, int64_t need_len) {
    if (need_len == 0) return hay_len;
    if (need_len > hay_len) return -1;
    int64_t last = hay_len - need_len;
    for (int64_t i = last; i >= 0; i--) {
        if (hay[i] == need[0] && memcmp(hay + i, need, (size_t)need_len) == 0) {
            return i;
        }
    }
    return -1;
}
static int64_t str_cp_len(PyStrObject *s);

static int64_t byte_offset_to_cp_offset(PyStrObject *s, int64_t byte_off) {
    if (byte_off <= 0) return 0;
    if (byte_off >= s->byte_len) byte_off = s->byte_len;
    /* All-ASCII fast path, mirroring utf8_byte_offset_for_codepoint below,
     * which already had one for the opposite direction.  cp_len == byte_len
     * means every byte is its own codepoint, so the byte offset already IS the
     * codepoint offset; str_cp_len caches the count, so this is O(1) after the
     * first call.  Without it every find()/index()/rfind() on a long string
     * paid a scan from the start merely to convert its result: measured 65 ms
     * -> 405 ms under pcc1 when a character loop was replaced by repeated
     * find() calls (the same change is 6.4x FASTER on CPython, whose find is
     * C-level and whose indices are O(1)). */
    if (str_cp_len(s) == s->byte_len) return byte_off;
    return utf8_codepoint_count(s->data, byte_off);
}

static int64_t str_cp_len(PyStrObject *s) {
    if (s->cp_len < 0) {
        s->cp_len = utf8_codepoint_count(s->data, s->byte_len);
    }
    return s->cp_len;
}

static int64_t utf8_byte_offset_for_codepoint(PyStrObject *s, int64_t cp_idx) {
    if (cp_idx <= 0) return 0;
    /* Fast path for ASCII-only strings after str_cp_len() has cached
     * that codepoint count equals byte length. The native Python
     * parser/lexer indexes source text by position; rescanning from
     * byte 0 for every s[i] makes large modules quadratic. */
    if (s->cp_len == s->byte_len) {
        return cp_idx >= s->byte_len ? s->byte_len : cp_idx;
    }
    int64_t seen = 0;
    for (int64_t i = 0; i < s->byte_len; i++) {
        unsigned char b = (unsigned char)s->data[i];
        if ((b & 0xC0) != 0x80) {
            if (seen == cp_idx) return i;
            seen++;
        }
    }
    return s->byte_len;
}

static int64_t utf8_codepoint_byte_len(const PyStrObject *s, int64_t byte_off) {
    if (byte_off < 0 || byte_off >= s->byte_len) return 0;
    unsigned char b = (unsigned char)s->data[byte_off];
    if ((b & 0x80) == 0x00) return 1;
    if ((b & 0xE0) == 0xC0) return 2;
    if ((b & 0xF0) == 0xE0) return 3;
    if ((b & 0xF8) == 0xF0) return 4;
    return 1;
}

static int64_t clamp_slice_index(int64_t i, int64_t cp_len) {
    if (i < 0) {
        i += cp_len;
        if (i < 0) i = 0;
    } else if (i > cp_len) {
        i = cp_len;
    }
    return i;
}

static int64_t normalise_index(int64_t i, int64_t cp_len) {
    if (i < 0) i += cp_len;
    if (i < 0 || i >= cp_len) return -1;
    return i;
}

static int64_t int_or_default(PyObject *o, int64_t defval) {
    if (o == NULL || o == py_None) return defval;
    if (PY_IS_TAGGED_INT(o)) return py_untag_int(o);
    if (py_type_of(o) == PY_TYPE_INT) return py_int_value_i64(o);
    return defval;
}

static const char *stringlike_bytes(PyObject *o, int64_t *len) {
    if (o == NULL) {
        *len = 0;
        return NULL;
    }
    int32_t tag = py_type_of(o);
    if (tag == PY_TYPE_STR) {
        PyStrObject *s = (PyStrObject *)o;
        *len = s->byte_len;
        return s->data;
    }
    if (tag == PY_TYPE_BYTES) {
        PyBytesObject *b = (PyBytesObject *)o;
        *len = b->byte_len;
        return b->data;
    }
    if (tag == PY_TYPE_BYTEARRAY) {
        PyByteArrayObject *b = (PyByteArrayObject *)o;
        *len = b->byte_len;
        return b->data;
    }
    *len = 0;
    return NULL;
}

static PyObject *str_from_range(const char *bytes, int64_t len) {
    PyStrObject *s = str_alloc_local(len);
    if (s == NULL) return NULL;
    if (len > 0) memcpy(s->data, bytes, (size_t)len);
    return (PyObject *)s;
}

int64_t py_str_byte_len(PyObject *s) {
    if (s == NULL) return 0;
    return ((PyStrObject *)s)->byte_len;
}

const char *py_str_utf8(PyObject *s) {
    if (s == NULL) return "";
    return ((PyStrObject *)s)->data;
}

const char *pcc_capi_str_utf8_pinned(PyObject *s) {
    pcc_gc_pin(s);
    return py_str_utf8(s);
}

int64_t py_str_len(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    if (ss->cp_len < 0) {
        ss->cp_len = utf8_codepoint_count(ss->data, ss->byte_len);
    }
    return ss->cp_len;
}

static int64_t utf8_ord_at_byte(PyStrObject *ss, int64_t bo) {
    if (ss == NULL) return -1;
    if (bo < 0 || bo >= ss->byte_len) return -1;
    const unsigned char *p = (const unsigned char *)(ss->data + bo);
    int64_t remaining = ss->byte_len - bo;
    unsigned char b0 = p[0];
    if (b0 < 0x80) return (int64_t)b0;
    if ((b0 & 0xE0) == 0xC0) {
        if (remaining < 2) return -1;
        return (int64_t)(((b0 & 0x1F) << 6) | (p[1] & 0x3F));
    }
    if ((b0 & 0xF0) == 0xE0) {
        if (remaining < 3) return -1;
        return (int64_t)(
            ((b0 & 0x0F) << 12)
            | ((p[1] & 0x3F) << 6)
            | (p[2] & 0x3F)
        );
    }
    if ((b0 & 0xF8) == 0xF0) {
        if (remaining < 4) return -1;
        return (int64_t)(
            ((b0 & 0x07) << 18)
            | ((p[1] & 0x3F) << 12)
            | ((p[2] & 0x3F) << 6)
            | (p[3] & 0x3F)
        );
    }
    return -1;
}

int64_t py_str_ord(PyObject *s) {
    if (s == NULL) return -1;
    PyStrObject *ss = (PyStrObject *)s;
    return utf8_ord_at_byte(ss, 0);
}

int64_t py_str_ord_at_i64(PyObject *s, int64_t idx) {
    if (s == NULL) return -1;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t cp_len = str_cp_len(ss);
    int64_t real = normalise_index(idx, cp_len);
    if (real < 0) return -1;
    if (ss->cp_len == ss->byte_len) {
        return (int64_t)((unsigned char)ss->data[real]);
    }
    int64_t bo = utf8_byte_offset_for_codepoint(ss, real);
    return utf8_ord_at_byte(ss, bo);
}

int64_t py_str_byte_at_i64(PyObject *s, int64_t idx) {
    if (s == NULL) return -1;
    PyStrObject *ss = (PyStrObject *)s;
    if (idx < 0 || idx >= ss->byte_len) return -1;
    return (int64_t)((unsigned char)ss->data[idx]);
}

PyObject *py_str_byte_slice_i64(PyObject *s, int64_t lo, int64_t hi) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    if (lo < 0) lo = 0;
    if (hi < lo) hi = lo;
    if (lo > ss->byte_len) lo = ss->byte_len;
    if (hi > ss->byte_len) hi = ss->byte_len;
    PyObject *out = str_from_range(ss->data + lo, hi - lo);
    if (out != NULL) {
        PyStrObject *os = (PyStrObject *)out;
        if (ss->cp_len == ss->byte_len) os->cp_len = os->byte_len;
    }
    return out;
}

PyObject *py_chr_from_i64(int64_t codepoint) {
    unsigned char buf[4];
    int64_t len = 0;
    if (codepoint < 0 || codepoint > 0x10FFFF) return NULL;
    if (codepoint >= 0xD800 && codepoint <= 0xDFFF) return NULL;
    if (codepoint <= 0x7F) {
        buf[0] = (unsigned char)codepoint;
        len = 1;
    } else if (codepoint <= 0x7FF) {
        buf[0] = (unsigned char)(0xC0 | (codepoint >> 6));
        buf[1] = (unsigned char)(0x80 | (codepoint & 0x3F));
        len = 2;
    } else if (codepoint <= 0xFFFF) {
        buf[0] = (unsigned char)(0xE0 | (codepoint >> 12));
        buf[1] = (unsigned char)(0x80 | ((codepoint >> 6) & 0x3F));
        buf[2] = (unsigned char)(0x80 | (codepoint & 0x3F));
        len = 3;
    } else {
        buf[0] = (unsigned char)(0xF0 | (codepoint >> 18));
        buf[1] = (unsigned char)(0x80 | ((codepoint >> 12) & 0x3F));
        buf[2] = (unsigned char)(0x80 | ((codepoint >> 6) & 0x3F));
        buf[3] = (unsigned char)(0x80 | (codepoint & 0x3F));
        len = 4;
    }
    return str_from_range((const char *)buf, len);
}

int64_t py_str_eq(PyObject *a, PyObject *b) {
    if (a == b) return 1;
    if (a == NULL || b == NULL) return 0;
    PyStrObject *sa = (PyStrObject *)a;
    PyStrObject *sb = (PyStrObject *)b;
    if (sa->byte_len != sb->byte_len) return 0;
    if (sa->byte_len == 0) return 1;
    return memcmp(sa->data, sb->data, (size_t)sa->byte_len) == 0;
}

int64_t py_str_contains(PyObject *s, PyObject *sub) {
    if (s == NULL || sub == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sn = (PyStrObject *)sub;
    return byte_find(ss->data, ss->byte_len, sn->data, sn->byte_len) != -1;
}

int64_t py_str_find(PyObject *s, PyObject *sub) {
    if (s == NULL || sub == NULL) return -1;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sn = (PyStrObject *)sub;
    int64_t bo = byte_find(ss->data, ss->byte_len, sn->data, sn->byte_len);
    if (bo < 0) return -1;
    return byte_offset_to_cp_offset(ss, bo);
}

int64_t py_str_rfind(PyObject *s, PyObject *sub) {
    if (s == NULL || sub == NULL) return -1;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sn = (PyStrObject *)sub;
    int64_t bo = byte_rfind(ss->data, ss->byte_len, sn->data, sn->byte_len);
    if (bo < 0) return -1;
    return byte_offset_to_cp_offset(ss, bo);
}

/* CPython ADJUST_INDICES over codepoint units: normalise [start, end) in
 * place. end is clamped to [0, cp_len]; start is only wrapped for negatives
 * (a start > cp_len is deliberately left unclamped so the empty-substring
 * case still returns -1 for start past the end, matching "abc".find("",5)==... */
static void str_find_adjust_indices(int64_t cp_len, int64_t *start, int64_t *end) {
    int64_t e = *end;
    int64_t st = *start;
    if (e > cp_len) {
        e = cp_len;
    } else if (e < 0) {
        e += cp_len;
        if (e < 0) e = 0;
    }
    if (st < 0) {
        st += cp_len;
        if (st < 0) st = 0;
    }
    *start = st;
    *end = e;
}

/* str.find(sub, start[, end]) with codepoint-based start/end. Returns the
 * absolute codepoint offset of the first match in the window s[start:end],
 * or -1. Mirrors CPython stringlib_find_slice semantics. */
int64_t py_str_find_range(PyObject *s, PyObject *sub,
                          int64_t start, int64_t end) {
    if (s == NULL || sub == NULL) return -1;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sn = (PyStrObject *)sub;
    int64_t cp_len = str_cp_len(ss);
    str_find_adjust_indices(cp_len, &start, &end);
    /* A start past the end of the string never matches (this is also what
     * makes "abc".find("", cp_len+1) return -1 rather than the start). */
    if (start > cp_len) return -1;
    if (end < start) return -1;
    int64_t start_byte = utf8_byte_offset_for_codepoint(ss, start);
    int64_t end_byte = utf8_byte_offset_for_codepoint(ss, end);
    int64_t win_len = end_byte - start_byte;
    if (win_len < 0) return -1;
    int64_t bo = byte_find(ss->data + start_byte, win_len,
                           sn->data, sn->byte_len);
    if (bo < 0) return -1;
    return byte_offset_to_cp_offset(ss, start_byte + bo);
}

/* str.rfind(sub, start[, end]) with codepoint-based start/end. */
int64_t py_str_rfind_range(PyObject *s, PyObject *sub,
                           int64_t start, int64_t end) {
    if (s == NULL || sub == NULL) return -1;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sn = (PyStrObject *)sub;
    int64_t cp_len = str_cp_len(ss);
    str_find_adjust_indices(cp_len, &start, &end);
    if (start > cp_len) return -1;
    if (end < start) return -1;
    int64_t start_byte = utf8_byte_offset_for_codepoint(ss, start);
    int64_t end_byte = utf8_byte_offset_for_codepoint(ss, end);
    int64_t win_len = end_byte - start_byte;
    if (win_len < 0) return -1;
    int64_t bo = byte_rfind(ss->data + start_byte, win_len,
                            sn->data, sn->byte_len);
    if (bo < 0) return -1;
    return byte_offset_to_cp_offset(ss, start_byte + bo);
}

static int64_t str_tailmatch_one(PyObject *s, PyObject *needle,
                                 int64_t start, int64_t end, int64_t suffix) {
    if (needle == NULL || py_type_of(needle) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "prefix or suffix must be str"));
        return -1;
    }
    PyStrObject *text = (PyStrObject *)s;
    PyStrObject *part = (PyStrObject *)needle;
    int64_t length = str_cp_len(text);
    str_find_adjust_indices(length, &start, &end);
    if (start > length || end < start) return 0;
    int64_t lo = utf8_byte_offset_for_codepoint(text, start);
    int64_t hi = utf8_byte_offset_for_codepoint(text, end);
    if (part->byte_len > hi - lo) return 0;
    if (suffix) lo = hi - part->byte_len;
    return memcmp(text->data + lo, part->data, (size_t)part->byte_len) == 0;
}

int64_t py_str_tailmatch_range(PyObject *s, PyObject *needle,
                               int64_t start, int64_t end, int64_t suffix) {
    if (s == NULL || py_type_of(s) != PY_TYPE_STR) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "prefix/suffix receiver must be str"));
        return -1;
    }
    if (needle != NULL && py_type_of(needle) == PY_TYPE_TUPLE) {
        PyTupleObject *items = (PyTupleObject *)needle;
        for (int64_t index = 0; index < items->len; index++) {
            int64_t result = str_tailmatch_one(s, items->items[index], start, end, suffix);
            if (result != 0) return result;
        }
        return 0;
    }
    return str_tailmatch_one(s, needle, start, end, suffix);
}

int64_t py_str_startswith(PyObject *s, PyObject *prefix) {
    if (s == NULL || prefix == NULL) return 0;
    if (py_type_of(prefix) == PY_TYPE_TUPLE) {
        int64_t n = py_tuple_len(prefix);
        for (int64_t i = 0; i < n; i++) {
            PyObject *item = py_tuple_get(prefix, i);
            int64_t ok = py_str_startswith(s, item);
            py_decref(item);
            if (ok) return 1;
        }
        return 0;
    }
    int64_t s_len = 0;
    int64_t p_len = 0;
    const char *s_data = stringlike_bytes(s, &s_len);
    const char *p_data = stringlike_bytes(prefix, &p_len);
    if (s_data == NULL || p_data == NULL) return 0;
    if (p_len > s_len) return 0;
    if (p_len == 0) return 1;
    return memcmp(s_data, p_data, (size_t)p_len) == 0;
}

int64_t py_str_endswith(PyObject *s, PyObject *suffix) {
    if (s == NULL || suffix == NULL) return 0;
    if (py_type_of(suffix) == PY_TYPE_TUPLE) {
        int64_t n = py_tuple_len(suffix);
        for (int64_t i = 0; i < n; i++) {
            PyObject *item = py_tuple_get(suffix, i);
            int64_t ok = py_str_endswith(s, item);
            py_decref(item);
            if (ok) return 1;
        }
        return 0;
    }
    int64_t s_len = 0;
    int64_t suffix_len = 0;
    const char *s_data = stringlike_bytes(s, &s_len);
    const char *suffix_data = stringlike_bytes(suffix, &suffix_len);
    if (s_data == NULL || suffix_data == NULL) return 0;
    if (suffix_len > s_len) return 0;
    if (suffix_len == 0) return 1;
    return memcmp(s_data + (s_len - suffix_len), suffix_data, (size_t)suffix_len) == 0;
}

int64_t py_str_isdigit(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    if (ss->byte_len == 0) return 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c < '0' || c > '9') return 0;
    }
    return 1;
}

int64_t py_str_isalpha(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    if (ss->byte_len == 0) return 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        int ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
        if (!ok) return 0;
    }
    return 1;
}

int64_t py_str_isspace(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    if (ss->byte_len == 0) return 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        int ws = (c == ' ' || c == '\t' || c == '\n' || c == '\r' ||
                  c == '\v' || c == '\f');
        if (!ws) return 0;
    }
    return 1;
}

int64_t py_str_isalnum(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    if (ss->byte_len == 0) return 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        int ok = (c >= '0' && c <= '9')
                 || (c >= 'a' && c <= 'z')
                 || (c >= 'A' && c <= 'Z');
        if (!ok) return 0;
    }
    return 1;
}

/* isupper()/islower(): true iff there is at least one cased (ASCII letter)
 * character and none of the opposite case (CPython ignores non-cased chars). */
int64_t py_str_isupper(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    int has_upper = 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c >= 'a' && c <= 'z') return 0;
        if (c >= 'A' && c <= 'Z') has_upper = 1;
    }
    return has_upper;
}

int64_t py_str_islower(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    int has_lower = 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c >= 'A' && c <= 'Z') return 0;
        if (c >= 'a' && c <= 'z') has_lower = 1;
    }
    return has_lower;
}

/* str.isascii(): True iff every byte is < 0x80 (empty string is True).
 * Exact CPython semantics (isascii is purely codepoint <= 0x7F). */
int64_t py_str_isascii(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        if ((unsigned char)ss->data[i] >= 0x80) return 0;
    }
    return 1;
}

/* str.isidentifier(): ASCII scope (mirrors py_str_isalpha's ASCII-only rule).
 * Empty -> False; first char [A-Za-z_]; remaining chars [A-Za-z0-9_].
 * CPython additionally accepts Unicode XID characters; the ASCII scope here
 * matches CPython for ASCII-only input. */
int64_t py_str_isidentifier(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    if (ss->byte_len == 0) return 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        int alpha = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
        if (i == 0) {
            if (!alpha) return 0;
        } else {
            int ok = alpha || (c >= '0' && c <= '9');
            if (!ok) return 0;
        }
    }
    return 1;
}

/* str.isprintable(): ASCII scope. Empty -> True; else every byte must be a
 * printable ASCII char in [0x20, 0x7E]. Non-ASCII bytes (>= 0x80) count as
 * non-printable here; CPython treats printable Unicode as printable, so this
 * matches CPython for ASCII-only input. */
int64_t py_str_isprintable(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c < 0x20 || c > 0x7E) return 0;
    }
    return 1;
}

/* str.isnumeric(): ASCII scope (mirrors py_str_isdigit). Empty -> False; else
 * every char must be an ASCII digit '0'..'9'. CPython additionally accepts
 * Unicode numeric characters; the ASCII scope matches for ASCII-only input. */
int64_t py_str_isnumeric(PyObject *s) {
    return py_str_isdigit(s);
}

/* str.isdecimal(): ASCII scope (mirrors py_str_isdigit). Empty -> False; else
 * every char must be an ASCII decimal digit '0'..'9'. */
int64_t py_str_isdecimal(PyObject *s) {
    return py_str_isdigit(s);
}

/* str.istitle(): ASCII scope. True iff there is at least one cased (ASCII
 * letter) char and titlecasing holds: each cased char that begins a run of
 * letters (i.e. follows a non-cased char or the start) must be uppercase, and
 * every cased char inside a run must be lowercase. Mirrors CPython's
 * do_title/istitle scan over ASCII letters. */
int64_t py_str_istitle(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    int cased = 0;          /* saw at least one cased char */
    int prev_cased = 0;     /* previous char was a cased (ASCII letter) char */
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        int is_upper = (c >= 'A' && c <= 'Z');
        int is_lower = (c >= 'a' && c <= 'z');
        if (is_upper) {
            if (prev_cased) return 0;   /* upper must not follow a cased char */
            prev_cased = 1;
            cased = 1;
        } else if (is_lower) {
            if (!prev_cased) return 0;  /* lower must follow a cased char */
            prev_cased = 1;
            cased = 1;
        } else {
            prev_cased = 0;
        }
    }
    return cased;
}

/* str.index(sub): like find() but raises ValueError when sub is absent.
 * Named *_of to avoid the existing py_str_index (s[i] subscript helper). */
int64_t py_str_index_of(PyObject *s, PyObject *sub) {
    int64_t idx = py_str_find(s, sub);
    if (idx < 0) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "substring not found"));
        return -1;
    }
    return idx;
}

/* str.rindex(sub): like rfind() but raises ValueError when sub is absent. */
int64_t py_str_rindex_of(PyObject *s, PyObject *sub) {
    int64_t idx = py_str_rfind(s, sub);
    if (idx < 0) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "substring not found"));
        return -1;
    }
    return idx;
}

/* str.index(sub, start[, end]): find_range() but raises ValueError if absent. */
int64_t py_str_index_of_range(PyObject *s, PyObject *sub,
                              int64_t start, int64_t end) {
    int64_t idx = py_str_find_range(s, sub, start, end);
    if (idx < 0) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "substring not found"));
        return -1;
    }
    return idx;
}

/* str.rindex(sub, start[, end]): rfind_range() but raises ValueError if absent. */
int64_t py_str_rindex_of_range(PyObject *s, PyObject *sub,
                               int64_t start, int64_t end) {
    int64_t idx = py_str_rfind_range(s, sub, start, end);
    if (idx < 0) {
        py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "substring not found"));
        return -1;
    }
    return idx;
}

/* ---- ASCII whitespace strip variants (byte-level) ---- */

PyObject *py_textwrap_dedent(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    const char *data = ss->data;
    int64_t n = ss->byte_len;
    int64_t margin_start = -1;
    int64_t margin_len = 0;
    int64_t start = 0;

    while (start < n) {
        int64_t next = start;
        while (next < n && data[next] != '\n') next++;
        if (next < n) next++;
        int64_t body_end = next;
        if (body_end > start && data[body_end - 1] == '\n') body_end--;
        if (body_end > start && data[body_end - 1] == '\r') body_end--;

        int blank = 1;
        for (int64_t i = start; i < body_end; i++) {
            if (data[i] != ' ' && data[i] != '\t') {
                blank = 0;
                break;
            }
        }
        if (!blank) {
            int64_t indent_len = 0;
            while (start + indent_len < body_end
                   && (data[start + indent_len] == ' '
                       || data[start + indent_len] == '\t')) {
                indent_len++;
            }
            if (margin_start < 0) {
                margin_start = start;
                margin_len = indent_len;
            } else {
                int64_t common = 0;
                int64_t limit = margin_len < indent_len ? margin_len : indent_len;
                while (common < limit
                       && data[margin_start + common] == data[start + common]) {
                    common++;
                }
                margin_len = common;
            }
        }
        start = next;
    }

    char *out = (char *)malloc((size_t)(n > 0 ? n : 1));
    if (out == NULL) return NULL;
    int64_t out_len = 0;
    start = 0;
    while (start < n) {
        int64_t next = start;
        while (next < n && data[next] != '\n') next++;
        if (next < n) next++;
        int64_t body_end = next;
        if (body_end > start && data[body_end - 1] == '\n') body_end--;
        if (body_end > start && data[body_end - 1] == '\r') body_end--;

        int blank = 1;
        for (int64_t i = start; i < body_end; i++) {
            if (data[i] != ' ' && data[i] != '\t') {
                blank = 0;
                break;
            }
        }
        int64_t content_start = blank ? body_end : start + margin_len;
        int64_t content_len = body_end - content_start;
        if (content_len > 0) {
            memcpy(out + out_len, data + content_start, (size_t)content_len);
            out_len += content_len;
        }
        int64_t ending_len = next - body_end;
        if (ending_len > 0) {
            memcpy(out + out_len, data + body_end, (size_t)ending_len);
            out_len += ending_len;
        }
        start = next;
    }
    PyObject *result = py_str_new(out, out_len);
    free(out);
    return result;
}

static int strip_is_ascii_ws(unsigned char c) {
    return c == ' ' || c == '\t' || c == '\n'
        || c == '\r' || c == '\v' || c == '\f';
}

PyObject *py_str_strip(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t lo = 0;
    int64_t hi = ss->byte_len;
    while (lo < hi && strip_is_ascii_ws((unsigned char)ss->data[lo])) lo++;
    while (hi > lo && strip_is_ascii_ws((unsigned char)ss->data[hi - 1])) hi--;
    return py_str_new(ss->data + lo, hi - lo);
}

PyObject *py_str_lstrip(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t lo = 0;
    int64_t hi = ss->byte_len;
    while (lo < hi && strip_is_ascii_ws((unsigned char)ss->data[lo])) lo++;
    return py_str_new(ss->data + lo, hi - lo);
}

PyObject *py_str_rstrip(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t lo = 0;
    int64_t hi = ss->byte_len;
    while (hi > lo && strip_is_ascii_ws((unsigned char)ss->data[hi - 1])) hi--;
    return py_str_new(ss->data + lo, hi - lo);
}

/* ---- chars-aware strip variants ---- */

static int strip_is_in_chars(unsigned char c, const unsigned char *chars, int64_t n) {
    for (int64_t k = 0; k < n; k++) {
        if (chars[k] == c) return 1;
    }
    return 0;
}

PyObject *py_str_strip_chars(PyObject *s, PyObject *chars) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *cs = (PyStrObject *)chars;
    int64_t lo = 0;
    int64_t hi = ss->byte_len;
    while (lo < hi && strip_is_in_chars(
        (unsigned char)ss->data[lo],
        (const unsigned char *)cs->data, cs->byte_len)) lo++;
    while (hi > lo && strip_is_in_chars(
        (unsigned char)ss->data[hi - 1],
        (const unsigned char *)cs->data, cs->byte_len)) hi--;
    return py_str_new(ss->data + lo, hi - lo);
}

PyObject *py_str_lstrip_chars(PyObject *s, PyObject *chars) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *cs = (PyStrObject *)chars;
    int64_t lo = 0;
    int64_t hi = ss->byte_len;
    while (lo < hi && strip_is_in_chars(
        (unsigned char)ss->data[lo],
        (const unsigned char *)cs->data, cs->byte_len)) lo++;
    return py_str_new(ss->data + lo, hi - lo);
}

PyObject *py_str_rstrip_chars(PyObject *s, PyObject *chars) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *cs = (PyStrObject *)chars;
    int64_t lo = 0;
    int64_t hi = ss->byte_len;
    while (hi > lo && strip_is_in_chars(
        (unsigned char)ss->data[hi - 1],
        (const unsigned char *)cs->data, cs->byte_len)) hi--;
    return py_str_new(ss->data + lo, hi - lo);
}

/* ---- concat / repeat (byte-level alloc + copy) ---- */

/* Local replica of py_str_alloc (static in py_str.c). Allocate a
 * fresh PyStrObject sized for byte_len + NUL terminator. */
static PyStrObject *str_alloc_local(int64_t byte_len) {
    if (byte_len < 0) return NULL;
    if (byte_len > INT64_MAX - (int64_t)sizeof(PyStrObject) - 1) return NULL;
    int64_t total = (int64_t)sizeof(PyStrObject) + byte_len + 1;
    PyStrObject *s = (PyStrObject *)pcc_gc_alloc(total, PY_TYPE_STR, 0);
    if (s == NULL) return NULL;
    s->byte_len   = byte_len;
    s->cp_len     = -1;
    s->hash       = -1;
    s->data[byte_len] = '\0';
    return s;
}

PyObject *py_str_upper(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *out = str_alloc_local(ss->byte_len);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c >= 'a' && c <= 'z') c = (unsigned char)(c - ('a' - 'A'));
        out->data[i] = (char)c;
    }
    out->cp_len = ss->cp_len;
    return (PyObject *)out;
}

PyObject *py_str_lower(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *out = str_alloc_local(ss->byte_len);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c >= 'A' && c <= 'Z') c = (unsigned char)(c + ('a' - 'A'));
        out->data[i] = (char)c;
    }
    out->cp_len = ss->cp_len;
    return (PyObject *)out;
}

PyObject *py_str_capitalize(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *out = str_alloc_local(ss->byte_len);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (i == 0) {
            if (c >= 'a' && c <= 'z') c = (unsigned char)(c - ('a' - 'A'));
        } else {
            if (c >= 'A' && c <= 'Z') c = (unsigned char)(c + ('a' - 'A'));
        }
        out->data[i] = (char)c;
    }
    out->cp_len = ss->cp_len;
    return (PyObject *)out;
}

/* ASCII-only case transforms, mirroring py_str_upper/lower/capitalize. */
PyObject *py_str_swapcase(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *out = str_alloc_local(ss->byte_len);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c >= 'a' && c <= 'z') c = (unsigned char)(c - ('a' - 'A'));
        else if (c >= 'A' && c <= 'Z') c = (unsigned char)(c + ('a' - 'A'));
        out->data[i] = (char)c;
    }
    out->cp_len = ss->cp_len;
    return (PyObject *)out;
}

PyObject *py_str_title(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *out = str_alloc_local(ss->byte_len);
    if (out == NULL) return NULL;
    int prev_alpha = 0;  /* previous char was a cased ASCII letter */
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        int is_alpha = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
        if (is_alpha) {
            if (!prev_alpha) {
                if (c >= 'a' && c <= 'z') c = (unsigned char)(c - ('a' - 'A'));
            } else {
                if (c >= 'A' && c <= 'Z') c = (unsigned char)(c + ('a' - 'A'));
            }
        }
        out->data[i] = (char)c;
        prev_alpha = is_alpha;
    }
    out->cp_len = ss->cp_len;
    return (PyObject *)out;
}

PyObject *py_str_casefold(PyObject *s) {
    /* ASCII casefold == lower (same ASCII-only scope as the other case ops;
     * Unicode-specific folding is not handled). */
    return py_str_lower(s);
}

PyObject *py_str_concat(PyObject *a, PyObject *b) {
    if (a == NULL || b == NULL) return NULL;
    int64_t backend = pcc_gc_backend();
    int moving_inputs = (
        backend == PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR
        || backend == PCC_GC_KIND_COLORED_RELOCATING
    );
    if (moving_inputs) {
        a = pcc_gc_note_relocation_read(a);
        b = pcc_gc_note_relocation_read(b);
    }
    int32_t tag_a = py_type_of(a);
    int32_t tag_b = py_type_of(b);
    if (tag_a != PY_TYPE_STR || tag_b != PY_TYPE_STR) {
        pcc_debug_bad_str_concat(a, b, tag_a, tag_b);
        return NULL;
    }
    PyStrObject *sa = (PyStrObject *)a;
    PyStrObject *sb = (PyStrObject *)b;
    if (sa->byte_len < 0 || sb->byte_len < 0) {
        pcc_debug_bad_str_concat(a, b, tag_a, tag_b);
        return NULL;
    }
    if (sa->byte_len > INT64_MAX - sb->byte_len) {
        pcc_debug_bad_str_concat(a, b, tag_a, tag_b);
        return NULL;
    }
    int64_t total = sa->byte_len + sb->byte_len;
    int64_t cp_len = -1;
    if (sa->cp_len >= 0 && sb->cp_len >= 0) {
        cp_len = sa->cp_len + sb->cp_len;
    }
    char *tmp = NULL;
    if (moving_inputs && total > 0) {
        if ((uint64_t)total > (uint64_t)SIZE_MAX) return NULL;
        tmp = (char *)malloc((size_t)total);
        if (tmp == NULL) return NULL;
        if (sa->byte_len > 0) memcpy(tmp, sa->data, (size_t)sa->byte_len);
        if (sb->byte_len > 0) {
            memcpy(tmp + sa->byte_len, sb->data, (size_t)sb->byte_len);
        }
    }
    PyStrObject *out = str_alloc_local(total);
    if (out == NULL) {
        if (tmp != NULL) free(tmp);
        return NULL;
    }
    if (moving_inputs && total > 0) {
        memcpy(out->data, tmp, (size_t)total);
        free(tmp);
    } else {
        if (sa->byte_len > 0) memcpy(out->data, sa->data, (size_t)sa->byte_len);
        if (sb->byte_len > 0) memcpy(out->data + sa->byte_len, sb->data, (size_t)sb->byte_len);
    }
    if (cp_len >= 0) out->cp_len = cp_len;
    return (PyObject *)out;
}

PyObject *py_str_repeat(PyObject *s, PyObject *n) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t count = 0;
    if (n != NULL && n != py_None && py_type_of(n) == PY_TYPE_INT) {
        count = py_int_value_i64(n);
    }
    if (count <= 0 || ss->byte_len == 0) return py_str_new("", 0);
    if (count > INT64_MAX / ss->byte_len) return NULL;
    int64_t total = count * ss->byte_len;
    PyStrObject *out = str_alloc_local(total);
    if (out == NULL) return NULL;
    for (int64_t i = 0; i < count; i++) {
        memcpy(out->data + i * ss->byte_len, ss->data, (size_t)ss->byte_len);
    }
    if (ss->cp_len >= 0) {
        out->cp_len = ss->cp_len * count;
    }
    return (PyObject *)out;
}

PyObject *py_str_slice(PyObject *s, PyObject *lo, PyObject *hi, PyObject *step) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t cp_len = str_cp_len(ss);
    int64_t step_v = (step == NULL || step == py_None) ? 1 : int_or_default(step, 1);
    if (step_v == 0) return NULL;

    int64_t lo_v, hi_v;
    if (step_v > 0) {
        lo_v = (lo == NULL || lo == py_None) ? 0      : int_or_default(lo, 0);
        hi_v = (hi == NULL || hi == py_None) ? cp_len : int_or_default(hi, cp_len);
        lo_v = clamp_slice_index(lo_v, cp_len);
        hi_v = clamp_slice_index(hi_v, cp_len);
        if (lo_v >= hi_v) return py_str_new("", 0);

        if (step_v == 1) {
            int64_t bo_lo = utf8_byte_offset_for_codepoint(ss, lo_v);
            int64_t bo_hi = utf8_byte_offset_for_codepoint(ss, hi_v);
            return str_from_range(ss->data + bo_lo, bo_hi - bo_lo);
        }

        int64_t bo_lo = utf8_byte_offset_for_codepoint(ss, lo_v);
        int64_t bo_hi = utf8_byte_offset_for_codepoint(ss, hi_v);
        int64_t cap = bo_hi - bo_lo;
        PyStrObject *out = str_alloc_local(cap);
        if (out == NULL) return NULL;
        int64_t out_bytes = 0;
        int64_t out_cps   = 0;
        int64_t cp_index  = lo_v;
        int64_t b = bo_lo;
        int64_t next_target = lo_v;
        while (b < bo_hi) {
            int64_t w = utf8_codepoint_byte_len(ss, b);
            if (cp_index == next_target) {
                memcpy(out->data + out_bytes, ss->data + b, (size_t)w);
                out_bytes += w;
                out_cps++;
                next_target += step_v;
            }
            b += w;
            cp_index++;
        }
        out->byte_len = out_bytes;
        out->data[out_bytes] = '\0';
        out->cp_len = out_cps;
        return (PyObject *)out;
    }

    int64_t default_lo = cp_len - 1;
    int64_t default_hi = -1;
    lo_v = (lo == NULL || lo == py_None) ? default_lo : int_or_default(lo, default_lo);
    hi_v = (hi == NULL || hi == py_None) ? default_hi : int_or_default(hi, default_hi);

    if (lo_v < 0) lo_v += cp_len;
    if (lo_v >= cp_len) lo_v = cp_len - 1;
    if (lo_v < 0) return py_str_new("", 0);

    if (hi != NULL && hi != py_None) {
        if (hi_v < 0) hi_v += cp_len;
        if (hi_v < -1) hi_v = -1;
        if (hi_v > cp_len) hi_v = cp_len;
    }

    if (lo_v <= hi_v) return py_str_new("", 0);

    int64_t span = lo_v - hi_v;
    int64_t pos_step = -step_v;
    int64_t out_n = (span + pos_step - 1) / pos_step;

    int64_t *cp_off = (int64_t *)malloc(sizeof(int64_t) * (size_t)(cp_len + 1));
    if (cp_off == NULL) return NULL;
    {
        int64_t cp = 0;
        for (int64_t i = 0; i < ss->byte_len; i++) {
            unsigned char b = (unsigned char)ss->data[i];
            if ((b & 0xC0) != 0x80) {
                cp_off[cp++] = i;
            }
        }
        cp_off[cp_len] = ss->byte_len;
    }

    PyStrObject *out = str_alloc_local(ss->byte_len);
    if (out == NULL) { free(cp_off); return NULL; }
    int64_t out_bytes = 0;
    for (int64_t k = 0; k < out_n; k++) {
        int64_t cp = lo_v + step_v * k;
        int64_t start = cp_off[cp];
        int64_t end   = cp_off[cp + 1];
        int64_t w = end - start;
        memcpy(out->data + out_bytes, ss->data + start, (size_t)w);
        out_bytes += w;
    }
    out->byte_len = out_bytes;
    out->data[out_bytes] = '\0';
    out->cp_len = out_n;
    free(cp_off);
    return (PyObject *)out;
}

PyObject *py_str_index(PyObject *s, PyObject *i) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t idx = int_or_default(i, 0);
    int64_t cp_len = str_cp_len(ss);
    int64_t real = normalise_index(idx, cp_len);
    if (real < 0) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "string index out of range"));
        return NULL;
    }
    int64_t bo = utf8_byte_offset_for_codepoint(ss, real);
    int64_t w = utf8_codepoint_byte_len(ss, bo);
    PyObject *out = str_from_range(ss->data + bo, w);
    if (out != NULL) {
        ((PyStrObject *)out)->cp_len = 1;
    }
    return out;
}

/* ---- count ---- */

int64_t py_str_count_range(PyObject *s, PyObject *sub,
                           PyObject *start, PyObject *end) {
    if (s == NULL || sub == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *ps = (PyStrObject *)sub;
    int64_t cp_len = str_cp_len(ss);
    int64_t lo = int_or_default(start, 0);
    int64_t hi = int_or_default(end, cp_len);

    /* str.count uses slice-style codepoint bounds, except that a start past
     * len(s) yields zero even for an empty substring.  Preserve that edge
     * before clamping so ``"abc".count("", 4) == 0``. */
    if (lo > cp_len) return 0;
    if (lo < 0) {
        lo += cp_len;
        if (lo < 0) lo = 0;
    }
    if (hi < 0) {
        hi += cp_len;
        if (hi < 0) hi = 0;
    } else if (hi > cp_len) {
        hi = cp_len;
    }
    if (hi < lo) return 0;
    if (ps->byte_len == 0) return hi - lo + 1;

    int64_t byte_lo = utf8_byte_offset_for_codepoint(ss, lo);
    int64_t byte_hi = utf8_byte_offset_for_codepoint(ss, hi);
    int64_t count = 0;
    int64_t i = byte_lo;
    while (i + ps->byte_len <= byte_hi) {
        if (ss->data[i] == ps->data[0]
            && memcmp(ss->data + i, ps->data, (size_t)ps->byte_len) == 0) {
            count++;
            i += ps->byte_len;
        } else {
            i++;
        }
    }
    return count;
}

int64_t py_str_count(PyObject *s, PyObject *sub) {
    return py_str_count_range(s, sub, NULL, NULL);
}

int64_t py_str_hash(PyObject *s) {
    if (s == NULL) return 0;
    PyStrObject *ss = (PyStrObject *)s;
    if (ss->hash != -1) return ss->hash;

    uint64_t h = 0xcbf29ce484222325ull;
    const uint64_t prime = 0x100000001b3ull;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        h ^= (unsigned char)ss->data[i];
        h *= prime;
    }

    int64_t result = (int64_t)h;
    if (result == -1) result = -2;
    ss->hash = result;
    return result;
}

static PyObject *py_str_splitlines_impl(PyObject *s, int keepends) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyObject *list = py_list_new(4);
    if (list == NULL) return NULL;
    int64_t start = 0;
    int64_t i = 0;
    while (i < ss->byte_len) {
        unsigned char c = (unsigned char)ss->data[i];
        if (c == '\r' || c == '\n') {
            int64_t end = i;
            int64_t after;
            if (c == '\r' && i + 1 < ss->byte_len && ss->data[i + 1] == '\n') {
                after = i + 2;
            } else {
                after = i + 1;
            }
            int64_t frag_end = keepends ? after : end;
            PyObject *part = str_from_range(ss->data + start, frag_end - start);
            if (part == NULL) { py_decref(list); return NULL; }
            py_list_append(list, part);
            py_decref(part);
            i = after;
            start = after;
        } else {
            i += 1;
        }
    }
    if (start < ss->byte_len) {
        PyObject *tail = str_from_range(ss->data + start, ss->byte_len - start);
        if (tail == NULL) { py_decref(list); return NULL; }
        py_list_append(list, tail);
        py_decref(tail);
    }
    return list;
}

PyObject *py_str_splitlines_keepends(PyObject *s, int keepends) {
    return py_str_splitlines_impl(s, keepends ? 1 : 0);
}

PyObject *py_str_splitlines(PyObject *s) {
    return py_str_splitlines_impl(s, 0);
}

static PyObject *py_str_split_whitespace(PyStrObject *ss) {
    PyObject *list = py_list_new(4);
    if (list == NULL) return NULL;
    int64_t i = 0;
    while (i < ss->byte_len) {
        while (i < ss->byte_len && strip_is_ascii_ws((unsigned char)ss->data[i])) i++;
        if (i >= ss->byte_len) break;
        int64_t start = i;
        while (i < ss->byte_len && !strip_is_ascii_ws((unsigned char)ss->data[i])) i++;
        PyObject *part = str_from_range(ss->data + start, i - start);
        if (part == NULL) { py_decref(list); return NULL; }
        py_list_append(list, part);
        py_decref(part);
    }
    return list;
}

PyObject *py_str_split(PyObject *s, PyObject *sep) {
    if (s == NULL) return NULL;
    if (py_type_of(s) != PY_TYPE_STR) {
        /* The dyn-receiver ``.split`` fast path can reach non-str objects
         * (e.g. re.Pattern); dispatch generically instead of casting.
         * getattr + call (not py_obj_call_method1, which prepends the
         * receiver and breaks instance-dict function attributes). */
        PyObject *method = py_obj_getattr(s, "split");
        PyObject *args;
        PyObject *out;
        if (method == NULL) return NULL;
        args = py_tuple_new(1);
        if (args == NULL) {
            py_decref(method);
            return NULL;
        }
        py_tuple_set_item(args, 0, sep == NULL ? py_None : sep);
        out = py_obj_call(method, args, NULL);
        py_decref(method);
        py_decref(args);
        return out;
    }
    PyStrObject *ss = (PyStrObject *)s;
    if (sep == NULL || sep == py_None) {
        return py_str_split_whitespace(ss);
    }

    PyStrObject *sp = (PyStrObject *)sep;
    if (sp->byte_len == 0) return py_list_new(0);

    PyObject *list = py_list_new(4);
    if (list == NULL) return NULL;

    int64_t start = 0;
    int64_t i = 0;
    while (i + sp->byte_len <= ss->byte_len) {
        if (ss->data[i] == sp->data[0]
            && memcmp(ss->data + i, sp->data, (size_t)sp->byte_len) == 0)
        {
            PyObject *part = str_from_range(ss->data + start, i - start);
            if (part == NULL) { py_decref(list); return NULL; }
            py_list_append(list, part);
            py_decref(part);
            i += sp->byte_len;
            start = i;
        } else {
            i++;
        }
    }

    PyObject *tail = str_from_range(ss->data + start, ss->byte_len - start);
    if (tail == NULL) { py_decref(list); return NULL; }
    py_list_append(list, tail);
    py_decref(tail);
    return list;
}

static int64_t fill_byte_count(int64_t pad, PyObject *fillobj) {
    if (fillobj == NULL) return pad;
    return pad * ((PyStrObject *)fillobj)->byte_len;
}

static int64_t fill_pad(char *buf, int64_t pos, int64_t pad, PyObject *fillobj) {
    if (fillobj == NULL) {
        for (int64_t p = 0; p < pad; p++) buf[pos++] = ' ';
        return pos;
    }
    PyStrObject *f = (PyStrObject *)fillobj;
    for (int64_t q = 0; q < pad; q++) {
        for (int64_t b = 0; b < f->byte_len; b++) buf[pos++] = f->data[b];
    }
    return pos;
}

PyObject *py_str_rjust(PyObject *s, int64_t width, PyObject *fillobj) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t n = py_str_len(s);                  /* codepoints */
    if (width <= n) { py_incref(s); return s; }
    int64_t pad = width - n;
    int64_t pad_bytes = fill_byte_count(pad, fillobj);
    char *buf = (char *)malloc((size_t)(ss->byte_len + pad_bytes + 1));
    if (buf == NULL) return NULL;
    int64_t pos = fill_pad(buf, 0, pad, fillobj);
    memcpy(buf + pos, ss->data, (size_t)ss->byte_len);
    pos += ss->byte_len;
    PyObject *out = py_str_new(buf, pos);
    free(buf);
    return out;
}

PyObject *py_str_ljust(PyObject *s, int64_t width, PyObject *fillobj) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t n = py_str_len(s);
    if (width <= n) { py_incref(s); return s; }
    int64_t pad = width - n;
    int64_t pad_bytes = fill_byte_count(pad, fillobj);
    char *buf = (char *)malloc((size_t)(ss->byte_len + pad_bytes + 1));
    if (buf == NULL) return NULL;
    memcpy(buf, ss->data, (size_t)ss->byte_len);
    int64_t pos = fill_pad(buf, ss->byte_len, pad, fillobj);
    PyObject *out = py_str_new(buf, pos);
    free(buf);
    return out;
}

static int re_escape_is_special(unsigned char c) {
    switch (c) {
        case '(': case ')': case '[': case ']': case '{': case '}':
        case '?': case '*': case '+': case '-': case '|': case '^':
        case '$': case '\\': case '.': case '&': case '~': case '#':
        case ' ': case '\t': case '\n': case '\r': case '\v': case '\f':
            return 1;
        default:
            return 0;
    }
}

PyObject *py_re_escape(PyObject *s) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    char *buf = (char *)malloc((size_t)(ss->byte_len * 2 + 1));
    if (buf == NULL) return NULL;
    int64_t pos = 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        if (re_escape_is_special(c)) buf[pos++] = '\\';
        buf[pos++] = (char)c;
    }
    PyObject *out = py_str_new(buf, pos);
    free(buf);
    return out;
}

PyObject *py_str_rsplit_maxsplit(PyObject *s, PyObject *sep, int64_t maxsplit) {
    if (s == NULL) return NULL;
    if (maxsplit < 0) return py_str_split(s, sep);
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sp = (PyStrObject *)sep;
    if (sp->byte_len == 0) return py_list_new(0);
    if (maxsplit == 0) {
        PyObject *out0 = py_list_new(1);
        if (out0 == NULL) return NULL;
        PyObject *whole = str_from_range(ss->data, ss->byte_len);
        if (whole == NULL) { py_decref(out0); return NULL; }
        py_list_append(out0, whole);
        py_decref(whole);
        return out0;
    }
    int64_t *positions = (int64_t *)malloc(sizeof(int64_t) * (size_t)maxsplit);
    if (positions == NULL) return NULL;
    int64_t count = 0;
    int64_t i = ss->byte_len - sp->byte_len;
    while (i >= 0 && count < maxsplit) {
        if (ss->data[i] == sp->data[0]
            && memcmp(ss->data + i, sp->data, (size_t)sp->byte_len) == 0)
        {
            positions[maxsplit - 1 - count] = i;
            count++;
            i -= sp->byte_len;
        } else {
            i--;
        }
    }
    PyObject *out = py_list_new(count + 1);
    if (out == NULL) { free(positions); return NULL; }
    int64_t prev = 0;
    for (int64_t j = maxsplit - count; j < maxsplit; j++) {
        int64_t p = positions[j];
        PyObject *part = str_from_range(ss->data + prev, p - prev);
        if (part == NULL) { free(positions); py_decref(out); return NULL; }
        py_list_append(out, part);
        py_decref(part);
        prev = p + sp->byte_len;
    }
    free(positions);
    PyObject *tail = str_from_range(ss->data + prev, ss->byte_len - prev);
    if (tail == NULL) { py_decref(out); return NULL; }
    py_list_append(out, tail);
    py_decref(tail);
    return out;
}

PyObject *py_str_center(PyObject *s, int64_t width, PyObject *fillobj) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t n = py_str_len(s);
    if (width <= n) { py_incref(s); return s; }
    int64_t marg = width - n;
    int64_t left = marg / 2 + (marg & width & 1);   /* CPython center split */
    int64_t right = marg - left;
    int64_t pad_l = fill_byte_count(left, fillobj);
    int64_t pad_r = fill_byte_count(right, fillobj);
    char *buf = (char *)malloc((size_t)(ss->byte_len + pad_l + pad_r + 1));
    if (buf == NULL) return NULL;
    int64_t pos = fill_pad(buf, 0, left, fillobj);
    memcpy(buf + pos, ss->data, (size_t)ss->byte_len);
    pos += ss->byte_len;
    pos = fill_pad(buf, pos, right, fillobj);
    PyObject *out = py_str_new(buf, pos);
    free(buf);
    return out;
}

PyObject *py_str_zfill(PyObject *s, int64_t width) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t n = py_str_len(s);
    if (width <= n) { py_incref(s); return s; }
    int64_t pad = width - n;
    int sign = 0;
    if (ss->byte_len > 0) {
        unsigned char c0 = (unsigned char)ss->data[0];
        if (c0 == '+' || c0 == '-') sign = 1;
    }
    char *buf = (char *)malloc((size_t)(ss->byte_len + pad + 1));
    if (buf == NULL) return NULL;
    int64_t pos = 0;
    if (sign) { buf[0] = ss->data[0]; pos = 1; }
    for (int64_t z = 0; z < pad; z++) buf[pos++] = '0';
    for (int64_t k = sign; k < ss->byte_len; k++) buf[pos++] = ss->data[k];
    PyObject *out = py_str_new(buf, pos);
    free(buf);
    return out;
}

/* str.expandtabs(tabsize): replace each '\t' with spaces up to the next
 * tabsize column boundary; '\n'/'\r' reset the column. Byte/column tracking is
 * ASCII-oriented (matches the other byte-level str helpers). */
PyObject *py_str_expandtabs(PyObject *s, int64_t tabsize) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t mult = tabsize > 1 ? tabsize : 1;
    char *buf = (char *)malloc((size_t)(ss->byte_len * mult + 1));
    if (buf == NULL) return NULL;
    int64_t pos = 0;
    int64_t col = 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        char c = ss->data[i];
        if (c == '\t') {
            if (tabsize > 0) {
                int64_t spaces = tabsize - (col % tabsize);
                for (int64_t k = 0; k < spaces; k++) buf[pos++] = ' ';
                col += spaces;
            }
        } else if (c == '\n' || c == '\r') {
            buf[pos++] = c;
            col = 0;
        } else {
            buf[pos++] = c;
            col++;
        }
    }
    PyObject *out = py_str_new(buf, pos);
    free(buf);
    return out;
}

/* str.translate(table): map each byte through table (dict {ord: ord|str|None}).
 * Absent -> keep; None -> delete; int -> that byte; str -> its bytes. Two-pass
 * (size then fill) so str replacements are sized correctly. Byte/ASCII-oriented
 * like the other str helpers (non-ASCII codepoint keys are a follow-on). */
PyObject *py_str_translate(PyObject *s, PyObject *table) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    int64_t out_len = 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        PyObject *key = py_int_from_i64((int64_t)c);
        PyObject *val = py_dict_get(table, key);
        py_decref(key);
        if (val == NULL) {
            out_len += 1;
        } else if (val == py_None) {
            py_decref(val);
        } else if (py_type_of(val) == PY_TYPE_STR) {
            out_len += ((PyStrObject *)val)->byte_len;
            py_decref(val);
        } else {
            out_len += 1;
            py_decref(val);
        }
    }
    char *buf = (char *)malloc((size_t)out_len + 1);
    if (buf == NULL) return NULL;
    int64_t pos = 0;
    for (int64_t i = 0; i < ss->byte_len; i++) {
        unsigned char c = (unsigned char)ss->data[i];
        PyObject *key = py_int_from_i64((int64_t)c);
        PyObject *val = py_dict_get(table, key);
        py_decref(key);
        if (val == NULL) {
            buf[pos++] = (char)c;
        } else if (val == py_None) {
            py_decref(val);
        } else if (py_type_of(val) == PY_TYPE_STR) {
            PyStrObject *vs = (PyStrObject *)val;
            for (int64_t j = 0; j < vs->byte_len; j++) buf[pos++] = vs->data[j];
            py_decref(val);
        } else {
            buf[pos++] = (char)(py_int_value_i64(val) & 0xFF);
            py_decref(val);
        }
    }
    PyObject *out = py_str_new(buf, pos);
    free(buf);
    return out;
}

/* str.maketrans(x, y): build the translation dict {ord(x[i]): ord(y[i])} for the
 * two-arg form (x and y must have equal length). Byte/ASCII-oriented like the
 * other str helpers. py_dict_set increfs key+value, so the fresh ints are
 * decref'd after. (1-arg dict / 3-arg delete forms are follow-ons.) */
PyObject *py_str_maketrans(PyObject *x, PyObject *y) {
    PyStrObject *sx = (PyStrObject *)x;
    PyStrObject *sy = (PyStrObject *)y;
    if (sx->byte_len != sy->byte_len) {
        py_raise_owned(py_exc_new(
            PY_EXC_VALUEERROR,
            "the first two maketrans arguments must have equal length"));
        return NULL;
    }
    PyObject *d = py_dict_new();
    if (d == NULL) return NULL;
    for (int64_t i = 0; i < sx->byte_len; i++) {
        PyObject *k = py_int_from_i64((int64_t)(unsigned char)sx->data[i]);
        PyObject *v = py_int_from_i64((int64_t)(unsigned char)sy->data[i]);
        py_dict_set(d, k, v);
        py_decref(k);
        py_decref(v);
    }
    return d;
}

PyObject *py_str_removeprefix(PyObject *s, PyObject *prefix) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *pp = (PyStrObject *)prefix;
    if (pp->byte_len > 0 && pp->byte_len <= ss->byte_len
        && memcmp(ss->data, pp->data, (size_t)pp->byte_len) == 0)
    {
        return str_from_range(ss->data + pp->byte_len,
                              ss->byte_len - pp->byte_len);
    }
    py_incref(s);
    return s;
}

PyObject *py_str_removesuffix(PyObject *s, PyObject *suffix) {
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sf = (PyStrObject *)suffix;
    if (sf->byte_len > 0 && sf->byte_len <= ss->byte_len
        && memcmp(ss->data + ss->byte_len - sf->byte_len,
                  sf->data, (size_t)sf->byte_len) == 0)
    {
        return str_from_range(ss->data, ss->byte_len - sf->byte_len);
    }
    py_incref(s);
    return s;
}

PyObject *py_str_partition(PyObject *s, PyObject *sep) {
    /* (before, sep, after) on first occurrence; (s, "", "") if not found.
     * Byte-level: sep boundaries fall on codepoint boundaries for valid
     * UTF-8.  py_tuple_set_item increfs, so we decref our created refs. */
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sp = (PyStrObject *)sep;
    int64_t found = -1;
    if (sp->byte_len > 0) {
        int64_t i = 0;
        while (i + sp->byte_len <= ss->byte_len) {
            if (ss->data[i] == sp->data[0]
                && memcmp(ss->data + i, sp->data, (size_t)sp->byte_len) == 0)
            {
                found = i;
                break;
            }
            i++;
        }
    }
    PyObject *t = py_tuple_new(3);
    if (t == NULL) return NULL;
    if (found < 0) {
        py_tuple_set_item(t, 0, s);             /* borrowed s; set_item increfs */
        PyObject *e1 = str_from_range(ss->data, 0);
        py_tuple_set_item(t, 1, e1);
        py_decref(e1);
        PyObject *e2 = str_from_range(ss->data, 0);
        py_tuple_set_item(t, 2, e2);
        py_decref(e2);
    } else {
        PyObject *before = str_from_range(ss->data, found);
        py_tuple_set_item(t, 0, before);
        py_decref(before);
        PyObject *mid = str_from_range(ss->data + found, sp->byte_len);
        py_tuple_set_item(t, 1, mid);
        py_decref(mid);
        PyObject *after = str_from_range(
            ss->data + found + sp->byte_len,
            ss->byte_len - found - sp->byte_len);
        py_tuple_set_item(t, 2, after);
        py_decref(after);
    }
    return t;
}

PyObject *py_str_rpartition(PyObject *s, PyObject *sep) {
    /* (before, sep, after) on the LAST occurrence; ("", "", s) if not found
     * (note: rpartition puts the original at the END, unlike partition). */
    if (s == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *sp = (PyStrObject *)sep;
    int64_t found = -1;
    if (sp->byte_len > 0 && sp->byte_len <= ss->byte_len) {
        int64_t i = ss->byte_len - sp->byte_len;
        while (i >= 0) {
            if (ss->data[i] == sp->data[0]
                && memcmp(ss->data + i, sp->data, (size_t)sp->byte_len) == 0)
            {
                found = i;
                break;
            }
            i--;
        }
    }
    PyObject *t = py_tuple_new(3);
    if (t == NULL) return NULL;
    if (found < 0) {
        PyObject *e0 = str_from_range(ss->data, 0);
        py_tuple_set_item(t, 0, e0);
        py_decref(e0);
        PyObject *e1 = str_from_range(ss->data, 0);
        py_tuple_set_item(t, 1, e1);
        py_decref(e1);
        py_tuple_set_item(t, 2, s);             /* original at the END */
    } else {
        PyObject *before = str_from_range(ss->data, found);
        py_tuple_set_item(t, 0, before);
        py_decref(before);
        PyObject *mid = str_from_range(ss->data + found, sp->byte_len);
        py_tuple_set_item(t, 1, mid);
        py_decref(mid);
        PyObject *after = str_from_range(
            ss->data + found + sp->byte_len,
            ss->byte_len - found - sp->byte_len);
        py_tuple_set_item(t, 2, after);
        py_decref(after);
    }
    return t;
}

static PyObject *py_str_split_whitespace_maxsplit(
    PyStrObject *ss, int64_t maxsplit
) {
    if (maxsplit < 0) return py_str_split_whitespace(ss);
    PyObject *list = py_list_new(4);
    if (list == NULL) return NULL;
    int64_t i = 0;
    int64_t splits = 0;
    while (i < ss->byte_len) {
        while (
            i < ss->byte_len
            && strip_is_ascii_ws((unsigned char)ss->data[i])
        ) {
            i++;
        }
        if (i >= ss->byte_len) break;
        if (splits >= maxsplit) {
            PyObject *tail = str_from_range(
                ss->data + i, ss->byte_len - i
            );
            if (tail == NULL) { py_decref(list); return NULL; }
            py_list_append(list, tail);
            py_decref(tail);
            return list;
        }
        int64_t start = i;
        while (
            i < ss->byte_len
            && !strip_is_ascii_ws((unsigned char)ss->data[i])
        ) {
            i++;
        }
        PyObject *part = str_from_range(ss->data + start, i - start);
        if (part == NULL) { py_decref(list); return NULL; }
        py_list_append(list, part);
        py_decref(part);
        splits++;
    }
    return list;
}

PyObject *py_str_split_maxsplit(
    PyObject *s, PyObject *sep, int64_t maxsplit
) {
    if (s == NULL) return NULL;
    if (py_type_of(s) != PY_TYPE_STR) {
        /* generic dispatch for non-str receivers (see py_str_split) */
        PyObject *method = py_obj_getattr(s, "split");
        PyObject *args;
        PyObject *ms;
        PyObject *out;
        if (method == NULL) return NULL;
        args = py_tuple_new(2);
        if (args == NULL) {
            py_decref(method);
            return NULL;
        }
        ms = py_int_from_i64(maxsplit);
        if (ms == NULL) {
            py_decref(method);
            py_decref(args);
            return NULL;
        }
        py_tuple_set_item(args, 0, sep == NULL ? py_None : sep);
        py_tuple_set_item(args, 1, ms);
        py_decref(ms);
        out = py_obj_call(method, args, NULL);
        py_decref(method);
        py_decref(args);
        return out;
    }
    PyStrObject *ss = (PyStrObject *)s;
    if (maxsplit < 0) return py_str_split(s, sep);
    if (sep == NULL || sep == py_None) {
        return py_str_split_whitespace_maxsplit(ss, maxsplit);
    }

    PyStrObject *sp = (PyStrObject *)sep;
    if (sp->byte_len == 0) return py_list_new(0);

    PyObject *list = py_list_new(4);
    if (list == NULL) return NULL;

    int64_t start = 0;
    int64_t i = 0;
    int64_t splits = 0;
    while (i + sp->byte_len <= ss->byte_len && splits < maxsplit) {
        if (ss->data[i] == sp->data[0]
            && memcmp(ss->data + i, sp->data, (size_t)sp->byte_len) == 0)
        {
            PyObject *part = str_from_range(ss->data + start, i - start);
            if (part == NULL) { py_decref(list); return NULL; }
            py_list_append(list, part);
            py_decref(part);
            i += sp->byte_len;
            start = i;
            splits++;
        } else {
            i++;
        }
    }

    PyObject *tail = str_from_range(ss->data + start, ss->byte_len - start);
    if (tail == NULL) { py_decref(list); return NULL; }
    py_list_append(list, tail);
    py_decref(tail);
    return list;
}

PyObject *py_str_join(PyObject *sep, PyObject *list) {
    if (sep == NULL || list == NULL) return NULL;
    sep = pcc_gc_note_relocation_read(sep);
    list = pcc_gc_note_relocation_read(list);
    int32_t sequence_tag = py_type_of(list);
    if (py_type_of(sep) != PY_TYPE_STR ||
        (sequence_tag != PY_TYPE_LIST && sequence_tag != PY_TYPE_TUPLE)) {
        return NULL;
    }
    PyStrObject *sp = (PyStrObject *)sep;
    int64_t length = sequence_tag == PY_TYPE_LIST
        ? ((PyListObject *)list)->length
        : ((PyTupleObject *)list)->len;

    if (length == 0) return py_str_new("", 0);

    int64_t total = 0;
    for (int64_t i = 0; i < length; i++) {
        PyObject **slot = sequence_tag == PY_TYPE_LIST
            ? &((PyListObject *)list)->items[i]
            : &((PyTupleObject *)list)->items[i];
        PyObject *e = pcc_gc_load_ptr(list, slot);
        if (e == NULL || py_type_of(e) != PY_TYPE_STR) return NULL;
        if (i > 0) total += sp->byte_len;
        total += ((PyStrObject *)e)->byte_len;
    }

    PyStrObject *out = str_alloc_local(total);
    if (out == NULL) return NULL;

    /* Result allocation may relocate the rooted inputs.  Refresh both object
     * pointers before reading the separator or the list's items buffer. */
    sep = pcc_gc_note_relocation_read(sep);
    list = pcc_gc_note_relocation_read(list);
    sp = (PyStrObject *)sep;

    int64_t off = 0;
    for (int64_t i = 0; i < length; i++) {
        PyObject **slot = sequence_tag == PY_TYPE_LIST
            ? &((PyListObject *)list)->items[i]
            : &((PyTupleObject *)list)->items[i];
        PyStrObject *e = (PyStrObject *)pcc_gc_load_ptr(list, slot);
        if (i > 0 && sp->byte_len > 0) {
            memcpy(out->data + off, sp->data, (size_t)sp->byte_len);
            off += sp->byte_len;
        }
        if (e->byte_len > 0) {
            memcpy(out->data + off, e->data, (size_t)e->byte_len);
            off += e->byte_len;
        }
    }
    return (PyObject *)out;
}

static PyObject *py_str_replace_impl(
    PyObject *s, PyObject *old, PyObject *new_, int64_t maxreplace
) {
    if (s == NULL || old == NULL || new_ == NULL) return NULL;
    PyStrObject *ss = (PyStrObject *)s;
    PyStrObject *so = (PyStrObject *)old;
    PyStrObject *sn = (PyStrObject *)new_;

    if (so->byte_len == 0) {
        return py_str_new(ss->data, ss->byte_len);
    }

    if (maxreplace == 0) {
        return py_str_new(ss->data, ss->byte_len);
    }

    int64_t matches = 0;
    {
        int64_t i = 0;
        while (i + so->byte_len <= ss->byte_len) {
            if (ss->data[i] == so->data[0]
                && memcmp(ss->data + i, so->data, (size_t)so->byte_len) == 0)
            {
                matches++;
                if (maxreplace > 0 && matches >= maxreplace) break;
                i += so->byte_len;
            } else {
                i++;
            }
        }
    }
    if (matches == 0) {
        return py_str_new(ss->data, ss->byte_len);
    }

    int64_t delta = (sn->byte_len - so->byte_len) * matches;
    int64_t total = ss->byte_len + delta;
    if (total < 0) return NULL;

    PyStrObject *out = str_alloc_local(total);
    if (out == NULL) return NULL;

    int64_t read = 0;
    int64_t write = 0;
    int64_t replaced = 0;
    while (read + so->byte_len <= ss->byte_len) {
        if (ss->data[read] == so->data[0]
            && memcmp(ss->data + read, so->data, (size_t)so->byte_len) == 0)
        {
            if (maxreplace < 0 || replaced < maxreplace) {
                if (sn->byte_len > 0) {
                    memcpy(out->data + write, sn->data, (size_t)sn->byte_len);
                    write += sn->byte_len;
                }
                read += so->byte_len;
                replaced++;
                continue;
            }
            if (so->byte_len > 0) {
                memcpy(out->data + write, so->data, (size_t)so->byte_len);
                write += so->byte_len;
            }
            read += so->byte_len;
        } else {
            out->data[write++] = ss->data[read++];
        }
    }
    while (read < ss->byte_len) {
        out->data[write++] = ss->data[read++];
    }
    return (PyObject *)out;
}

PyObject *py_str_replace(PyObject *s, PyObject *old, PyObject *new_) {
    return py_str_replace_impl(s, old, new_, -1);
}

PyObject *py_str_replace_count(
    PyObject *s, PyObject *old, PyObject *new_, int64_t maxreplace
) {
    return py_str_replace_impl(s, old, new_, maxreplace);
}
