"""C/strict parity for the dict missing-key exception owner.

`py_raise` increfs the exception it stores in TLS (see
`pcc/runtime/src/py_exc_tls.c`), so a caller that created the exception
still owns its own reference and must release it.  The C runtime does this at
every dict missing-key site; the strict pcc-Python mirror did not, leaking one
KeyError per missing-key subscript, pop or popitem.

After the raise settles, exactly one reference should remain: the one held by
the thread-local exception slot.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from test_gc_threading_substrate import REPO_ROOT, _compile_runtime_probe


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_dict_missing_key_raise_leaves_single_tls_reference(
    tmp_path: Path, kind: str
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="dict_missing_key_exc_owner",
        source_text=r'''
            #include "Python.h"
            #include <stdint.h>
            #include <stdio.h>

            extern void *py_tls_exc_get(void);
            /* py_dict_pop is runtime-internal, not in the public header. */
            extern PyObject *py_dict_pop(PyObject *d, PyObject *key);

            /* refcount lives at offset 0 of every heap object header. */
            static int64_t refcount_of(PyObject *o) {
                return *(int64_t *)(void *)o;
            }

            /* Returns the refcount of the exception the call left in TLS. */
            static int64_t raise_and_measure(int which, PyObject *d,
                                             PyObject *missing) {
                py_clear_exception();
                if (which == 0) {
                    (void)py_dict_getitem(d, missing);
                } else if (which == 1) {
                    (void)py_dict_pop(d, missing);
                } else {
                    (void)py_dict_popitem(d);
                }
                PyObject *exc = (PyObject *)py_tls_exc_get();
                if (exc == NULL) return -1;
                int64_t rc = refcount_of(exc);
                py_clear_exception();
                return rc;
            }

            int main(void) {
                PyObject *d = py_dict_new();
                PyObject *present = py_str_new("here", 4);
                PyObject *missing = py_str_new("gone", 4);
                if (d == NULL || present == NULL || missing == NULL) return 2;
                py_dict_set(d, present, py_int_from_i64(1));

                int64_t getitem_rc = raise_and_measure(0, d, missing);
                int64_t pop_rc = raise_and_measure(1, d, missing);

                PyObject *empty = py_dict_new();
                if (empty == NULL) return 3;
                int64_t popitem_rc = raise_and_measure(2, empty, missing);

                if (getitem_rc != 1 || pop_rc != 1 || popitem_rc != 1) {
                    printf(
                        "exception refcounts: getitem=%lld pop=%lld "
                        "popitem=%lld (expected 1 1 1)\n",
                        (long long)getitem_rc,
                        (long long)pop_rc,
                        (long long)popitem_rc
                    );
                    return 4;
                }
                /* The dict itself must be unaffected by the failed lookups. */
                if (py_dict_len(d) != 1) return 5;
                return 0;
            }
        ''',
        extra_include_dirs=(REPO_ROOT / "utils" / "fake_libc_include",),
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, (
        f"{kind} dict missing-key exception owner returned {run.returncode}: "
        + run.stdout + run.stderr
    )
