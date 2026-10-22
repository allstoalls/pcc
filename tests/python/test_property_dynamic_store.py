"""A store through ``py_obj_setattr`` must run a property's setter.

``py_instance_setattr`` asked ``_descriptor_method(class_attr, "__set__")``,
which answers only for descriptors that are instances of a class with
``__set__``.  A property object never is, so a dynamic ``obj.prop = v`` -- any
receiver the compiler cannot type, such as ``ir_arg`` in ``for ir_arg, ast_arg
in zip(fn.args, params)`` -- skipped the setter and parked the value in the
instance dict, where the getter never looks.  pcc1 therefore emitted every
function parameter as ``%.1``, ``%.2`` instead of ``%x``: different IR from
the host compiler for the same module.  ``del obj.prop`` had the same gap.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python.pipeline import compile_python
from tests.python.test_class_lookup_cache_runtime import _compile_and_run

PROGRAM = '''
class Argument:
    def __init__(self, index: int) -> None:
        self.index = index
        self._name = ""
        self._ref = "%." + str(index + 1)
        self.log = []

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = value
        self._ref = "%" + str(value) if value else "%." + str(self.index + 1)

    @name.deleter
    def name(self) -> None:
        self.log.append("del")
        self._name = ""
        self._ref = "%." + str(self.index + 1)

    @property
    def fixed(self) -> int:
        return 7


class Function:
    def __init__(self, n: int) -> None:
        self.args = tuple(Argument(i) for i in range(n))


class Param:
    def __init__(self, name: str) -> None:
        self.name = name


def name_args(fn: Function, params: list) -> None:
    runtime = [p for p in params if p.name != ""]
    for ir_arg, ast_arg in zip(fn.args, runtime):
        ir_arg.name = ast_arg.name


def name_first(fn: Function) -> None:
    fn.args[0].name = "self"


def name_typed(arg: Argument) -> None:
    arg.name = "typed"


def drop_name(arg) -> None:
    del arg.name


def set_fixed(arg) -> str:
    try:
        arg.fixed = 3
    except AttributeError:
        return "AttributeError"
    return "stored"


f = Function(2)
name_args(f, [Param("ty"), Param("x")])
print([a._ref for a in f.args], [a.name for a in f.args])
g = Function(2)
name_first(g)
name_typed(g.args[1])
print([a._ref for a in g.args], [a.name for a in g.args])
drop_name(g.args[0])
print(g.args[0]._ref, repr(g.args[0].name), g.args[0].log)
print(set_fixed(g.args[1]), g.args[1].fixed)
'''


@pytest.mark.parametrize("mode", ["on", "off"])
def test_dynamic_property_store_and_delete_match_cpython(tmp_path, mode):
    src = tmp_path / f"property_{mode}.py"
    exe = tmp_path / f"property_{mode}"
    src.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode=mode, backend="self",
    )
    for backend in ("0", "1", "2", "3", "4"):
        run = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert run.stdout == expected.stdout, (backend, run.stdout, expected.stdout)


SOURCE = r'''
    #include "py_internal.h"
    #include <stdint.h>

    static int64_t sets = 0;
    static int64_t deletes = 0;

    static PyObject *getter(PyObject *c, PyObject *a) {
        (void)c; (void)a;
        return py_int_from_i64(sets * 10 + deletes);
    }
    static PyObject *setter(PyObject *c, PyObject *a) {
        (void)c;
        if (py_tuple_len(a) != 2) return NULL;
        sets++;
        py_incref(py_None);
        return py_None;
    }
    static PyObject *deleter(PyObject *c, PyObject *a) {
        (void)c;
        if (py_tuple_len(a) != 1) return NULL;
        deletes++;
        py_incref(py_None);
        return py_None;
    }

    static PyObject *fn(void *entry) {
        return py_func_new(entry, py_tuple_new(0));
    }

    int main(void) {
        if (pcc_gc_set_backend(STORE_BACKEND) != 0) return 1;
        const char *fields[1] = {"plain"};
        PyClassObject *cls = py_class_new("Obj", NULL, 0, fields, 1);
        if (cls == NULL) return 2;
        PyObject *prop = py_property_new(fn((void *)getter), fn((void *)setter),
                                         fn((void *)deleter));
        PyObject *ro = py_property_new(fn((void *)getter), NULL, NULL);
        if (py_class_setattr_raw(cls, "p", prop) != 0) return 3;
        if (py_class_setattr_raw(cls, "ro", ro) != 0) return 4;
        PyInstanceObject *inst = (PyInstanceObject *)py_instance_new(cls);
        for (int round = 1; round <= 3; round++) {
            if (py_instance_setattr(inst, "p", py_int_from_i64(5)) != 0) return 10;
            if (sets != round) return 11;
            if (py_instance_delattr(inst, "p") != 0) return 12;
            if (deletes != round) return 13;
        }
        /* read-only property: AttributeError, and nothing stored */
        if (py_instance_setattr(inst, "ro", py_int_from_i64(1)) != -1) return 20;
        if (py_err_occurred() == 0) return 21;
        py_clear_exception();
        if (py_instance_delattr(inst, "ro") != -1) return 22;
        if (py_err_occurred() == 0) return 23;
        py_clear_exception();
        /* an ordinary field still stores into its slot */
        if (py_instance_setattr(inst, "plain", py_int_from_i64(9)) != 0) return 30;
        PyObject *plain = py_instance_get_field(inst, 0);
        int overflow = 0;
        if (py_int_to_i64(plain, &overflow) != 9) return 31;
        return 0;
    }
'''


def test_runtime_property_store_and_delete_in_both_runtimes(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    for backend in range(5):
        backend_source = SOURCE.replace("STORE_BACKEND", str(backend))
        for runtime_name, archive in (
            ("pcc_py", pcc_runtime_archive),
        ):
            result = _compile_and_run(
                tmp_path,
                f"property_store_backend{backend}_{runtime_name}",
                backend_source,
                archive,
            )
            assert result.returncode == 0, (
                f"backend={backend} runtime={runtime_name}: rc={result.returncode} "
                + result.stdout
                + result.stderr
            )
