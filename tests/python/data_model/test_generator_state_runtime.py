from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_generator_next_send_finish_and_stop_value_native(
    tmp_path, pcc_runtime_archive
):
    src = tmp_path / "gen_probe.c"
    exe = tmp_path / "gen_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdio.h>

            static PyObject *resume(PyObject *gen, PyObject *frame) {
                (void)frame;
                int64_t state = py_gen_state(gen);
                if (state == 0) {
                    PyObject *sent = py_gen_take_send(gen);
                    if (sent != py_None) return py_gen_finish(gen, py_int_from_i64(-1));
                    py_gen_set_state(gen, 1);
                    return py_int_from_i64(10);
                }
                if (state == 1) {
                    PyObject *sent = py_gen_take_send(gen);
                    int64_t v = py_int_to_i64(sent, NULL);
                    py_gen_set_state(gen, 2);
                    return py_int_from_i64(v + 1);
                }
                return py_gen_finish(gen, py_int_from_i64(99));
            }

            static PyObject *silent_resume(PyObject *gen, PyObject *frame) {
                (void)gen;
                (void)frame;
                return NULL;
            }

            int main(void) {
                PyObject *frame = py_tuple_new(0);
                PyObject *gen = py_gen_new((void *)resume, frame);

                PyObject *a = py_gen_next(gen);
                if (py_int_to_i64(a, NULL) != 10) return 1;
                if (py_gen_is_done(gen) != 0) return 2;

                PyObject *b = py_gen_send(gen, py_int_from_i64(20));
                if (py_int_to_i64(b, NULL) != 21) return 3;

                PyObject *c = py_gen_next(gen);
                if (c != NULL) return 4;
                PyObject *stop = py_current_exception();
                if (!py_exc_matches(stop, (PyObject *)py_exc_builtin_class(PY_EXC_STOPITERATION))) return 5;
                PyObject *msg = py_exc_get_message(stop);
                if (py_int_to_i64(msg, NULL) != 99) return 6;
                py_clear_exception();

                if (py_gen_is_done(gen) != 1) return 7;

                PyObject *d = py_gen_next(gen);
                if (d != NULL) return 8;
                if (!py_exc_matches(py_current_exception(), (PyObject *)py_exc_builtin_class(PY_EXC_STOPITERATION))) return 9;
                py_clear_exception();

                PyObject *silent = py_gen_new((void *)silent_resume, frame);
                if (py_gen_next(silent) != NULL || !py_err_occurred()) return 10;
                py_clear_exception();
                if (py_gen_new(NULL, frame) != NULL || !py_err_occurred()) return 11;
                py_clear_exception();

                printf("generator-ok\\n");
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
            str(src),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        check=True,
    )
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.strip() == "generator-ok"


def test_generator_finish_symbols_are_wired():
    py_src = Path("pcc/runtime/py/py_gen.py").read_text(encoding="utf-8")
    header = Path("pcc/runtime/include/py_runtime.h").read_text(encoding="utf-8")
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(encoding="utf-8")

    assert '@c_abi_export("py_gen_finish")' in py_src
    assert "PyObject *py_gen_finish(PyObject *gen, PyObject *value);" in header
    assert '"py_gen_finish": (_PYOBJ, [_PYOBJ, _PYOBJ], False)' in abi


def test_generator_resume_boundaries_attribute_silent_null_results():
    py_src = Path("pcc/runtime/py/py_gen.py").read_text(encoding="utf-8")

    messages = (
        "generator construction received a NULL resume thunk or frame",
        "generator construction could not allocate generator state",
        "generator finish could not allocate StopIteration",
        "generator resume returned NULL without StopIteration or an exception",
        "coroutine send returned NULL without setting an exception",
        "generator send returned NULL without StopIteration or an exception",
        "generator throw returned NULL without setting an exception",
        "generator close resume returned NULL without setting an exception",
    )
    for source in (py_src,):
        assert "py_runtime_error_if_unset" in source
        for message in messages:
            assert message in source

    for source, resume, guard in ((
            py_src,
            "call_ptr2(resume, gen, frame)",
            "generator resume returned NULL without StopIteration or an exception",
        ),):
        resume_pos = source.index(resume)
        guard_pos = source.index(guard, resume_pos)
        assert resume_pos < guard_pos
