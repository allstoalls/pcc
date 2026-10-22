from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_user_dunder_str_hash_iter_next_native(tmp_path, pcc_runtime_archive):
    src = tmp_path / "dunder_probe.c"
    exe = tmp_path / "dunder_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_internal.h"
            #include <stdio.h>
            #include <stdint.h>
            #include <string.h>

            static PyObject *my_str(PyObject *self) {
                (void)self;
                return py_str_new("custom", 6);
            }

            static PyObject *my_hash(PyObject *self) {
                (void)self;
                return py_int_from_i64(12345);
            }

            static PyObject *my_iter(PyObject *self) {
                PyObject *lst = py_list_new(0);
                py_list_append(lst, py_int_from_i64(7));
                py_list_append(lst, py_int_from_i64(8));
                return py_obj_iter(lst);
            }

            static PyObject *my_next(PyObject *self) {
                (void)self;
                return py_int_from_i64(99);
            }

            int main(void) {
                PyClassObject *cls = py_class_new("D", NULL, 0, NULL, 0);
                py_class_add_method(cls, "__str__", (PyObject *)(uintptr_t)my_str);
                py_class_add_method(cls, "__hash__", (PyObject *)(uintptr_t)my_hash);
                py_class_add_method(cls, "__iter__", (PyObject *)(uintptr_t)my_iter);
                py_class_add_method(cls, "__next__", (PyObject *)(uintptr_t)my_next);

                PyObject *obj = py_instance_new(cls);
                PyObject *s = py_obj_str(obj);
                if (strcmp(py_str_utf8(s), "custom") != 0) return 1;

                if (py_obj_hash(obj) != 12345) return 2;

                PyObject *it = py_obj_iter(obj);
                PyObject *first = py_obj_next(it);
                PyObject *second = py_obj_next(it);
                if (py_int_to_i64(first, NULL) != 7) return 3;
                if (py_int_to_i64(second, NULL) != 8) return 4;

                PyObject *n = py_obj_next(obj);
                if (py_int_to_i64(n, NULL) != 99) return 5;

                /* Public iterator entrypoints must turn invalid NULL operands
                 * into a pending, attributed runtime error instead of passing
                 * a silent NULL deeper into the dispatch graph. */
                if (py_obj_iter(NULL) != NULL || !py_err_occurred()) return 6;
                py_clear_exception();
                if (py_iter_callable_new(NULL, obj) != NULL ||
                    !py_err_occurred()) return 7;
                py_clear_exception();
                if (py_obj_next(NULL) != NULL || !py_err_occurred()) return 8;
                py_clear_exception();

                printf("dunder-ok\\n");
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-I",
            str(pcc_runtime_archive.parent / "include"),
            "-I",
            str(pcc_runtime_archive.parent / "src"),
            str(src),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        check=True,
    )
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.strip() == "dunder-ok"


def test_user_dunder_sources_are_wired():
    dunder_py = Path("pcc/runtime/py/py_dunder.py").read_text(encoding="utf-8")
    iter_py = Path("pcc/runtime/py/py_iter.py").read_text(encoding="utf-8")

    assert '@c_abi_export("py_user_hash_dispatch")' in dunder_py
    assert "py_user_iter_dispatch" in iter_py
    assert "py_user_next_dispatch" in iter_py


def test_iterator_runtime_guards_silent_null_before_state_cleanup():
    iter_py = Path("pcc/runtime/py/py_iter.py").read_text(encoding="utf-8")

    messages = (
        "iter(callable, sentinel) received NULL operand",
        "iter(callable, sentinel) could not allocate its state tuple",
        "sequence iterator allocation failed without setting an exception",
        "dictionary iterator snapshot failed without setting an exception",
        "set iterator snapshot failed without setting an exception",
        "generator next returned NULL without StopIteration or an exception",
        "callable iterator lost its callable",
        "callable iterator lost its sentinel",
        "callable iterator could not allocate its argument tuple",
        "callable iterator returned NULL without setting an exception",
        "iterator element lookup returned NULL without setting an exception",
        "enumerate could not allocate its result list",
        "enumerate could not allocate an output pair",
        "enumerate could not allocate an index object",
    )
    for source in (iter_py,):
        assert "py_runtime_error_if_unset" in source
        for message in messages[:-3]:
            assert message in source
    for source in (iter_py,):
        assert "py_runtime_error_if_unset" in source
        for message in messages[-3:]:
            assert message in source

    # The callable iterator reloads its moving roots for the call and
    # retires the argument root only after the NULL-result guard.
    for source, call, guard, cleanup in ((
            iter_py,
            "result = py_obj_call(\n            _iter_reload_moving_root(callable_slot, callable_handle),",
            "callable iterator returned NULL without setting an exception",
            "_iter_release_owned_root(args_slot, args_handle)",
        ),):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos

    py_item_guard = iter_py.index(
        "iterator element lookup returned NULL without setting an exception"
    )
    # The cursor advances by the item's width only after the guard.
    assert py_item_guard < iter_py.index(
        "store_i64(it_obj, 24, index + step)", py_item_guard
    )

    for source, allocation, guard, cleanup in ((
            iter_py,
            "index_obj = py_int_from_i64(index)",
            "enumerate could not allocate an index object",
            "_iter_release_owned_root(item_slot, item_handle)",
        ),):
        allocation_pos = source.index(allocation)
        guard_pos = source.index(guard, allocation_pos)
        cleanup_pos = source.index(cleanup, guard_pos)
        assert allocation_pos < guard_pos < cleanup_pos
