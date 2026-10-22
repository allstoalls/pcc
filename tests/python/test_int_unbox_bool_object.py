"""``py_int_to_i64`` must read a bool object as 1 or 0.

bool is an int subclass.  The unbox treated any heap object that is not
tagged ``PY_TYPE_INT`` as a foreign number and returned 0 with overflow set,
so an int-typed slot that received a dynamic ``True`` read 0.  pcc1 typed
``False if recv_borrowed else self._owned_release_needed(...)`` as int (the
arithmetic ``common_type(bool, bool)``), unboxed the method's ``True`` to 0
and dropped the release of every owned receiver decided that way -- IR the
host compiler, running the same line under CPython, does not produce.
"""
from __future__ import annotations

from pathlib import Path

from tests.python.test_class_lookup_cache_runtime import _compile_and_run


SOURCE = r'''
    #include "py_internal.h"
    #include <stdint.h>

    int main(void) {
        if (pcc_gc_set_backend(UNBOX_BACKEND) != 0) return 1;
        int overflow = 7;
        if (py_int_to_i64(py_True, &overflow) != 1 || overflow != 0) return 10;
        overflow = 7;
        if (py_int_to_i64(py_False, &overflow) != 0 || overflow != 0) return 11;
        overflow = 0;
        PyObject *text = py_str_new("x", 1);
        (void)py_int_to_i64(text, &overflow);
        if (overflow == 0) return 12;
        overflow = 7;
        if (py_int_to_i64(py_int_from_i64(41), &overflow) != 41 || overflow != 0) return 13;
        return 0;
    }
'''


def test_bool_objects_unbox_as_zero_and_one(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    for backend in range(5):
        backend_source = SOURCE.replace("UNBOX_BACKEND", str(backend))
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"int_unbox_bool_backend{backend}_{runtime_name}",
                backend_source,
                archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: rc={result.returncode} "
                + result.stdout
                + result.stderr
            )
