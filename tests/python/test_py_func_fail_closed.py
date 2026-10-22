from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pcc.runtime.py.py_abi_constants import PY_TYPE_FUNC, PY_TYPE_INT, PY_TYPE_NONE, PY_TYPE_TUPLE


REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "pcc" / "runtime"


def _compile_and_run(tmp_path: Path, archive: Path) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "py_func_fail_closed.c"
    exe = tmp_path / "py_func_fail_closed"
    source.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"
            #include <stdio.h>
            #include <string.h>

            static PyObject *silent_null(PyObject *captures, PyObject *args) {
                (void)captures;
                (void)args;
                return NULL;
            }

            static PyObject *dimension_error(PyObject *captures, PyObject *args) {
                (void)captures;
                (void)args;
                py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "bad dimension"));
                return NULL;
            }

            static PyObject *signature_func(int explicit_binding_error) {
                PyObject *inner = py_tuple_new(0);
                PyObject *signature = py_tuple_new(5);
                PyObject *magic = py_str_new("__pcc_func_signature_v1__", 25);
                PyObject *captures = py_tuple_new(2);
                PyObject *names = NULL;
                PyObject *kinds = NULL;
                PyObject *has_defaults = NULL;
                PyObject *defaults = NULL;
                PyObject *name = NULL;
                PyObject *fn = NULL;
                if (
                    inner == NULL || signature == NULL || magic == NULL ||
                    captures == NULL
                ) goto done;

                py_tuple_set_item(signature, 0, magic);
                if (explicit_binding_error) {
                    /* A valid signature marker with inconsistent component
                     * lengths makes the binder raise its own TypeError. */
                    names = py_tuple_new(1);
                    kinds = py_tuple_new(0);
                    has_defaults = py_tuple_new(0);
                    defaults = py_tuple_new(0);
                    name = py_str_new("value", 5);
                    if (
                        names == NULL || kinds == NULL ||
                        has_defaults == NULL || defaults == NULL || name == NULL
                    ) goto done;
                    py_tuple_set_item(names, 0, name);
                    py_tuple_set_item(signature, 1, names);
                    py_tuple_set_item(signature, 2, kinds);
                    py_tuple_set_item(signature, 3, has_defaults);
                    py_tuple_set_item(signature, 4, defaults);
                }
                /* Leaving signature slots 1..4 NULL exercises the internal
                 * binder's historically silent NULL return. */
                py_tuple_set_item(captures, 0, inner);
                py_tuple_set_item(captures, 1, signature);
                fn = py_func_new((void *)silent_null, captures);

            done:
                if (name != NULL) py_decref(name);
                if (defaults != NULL) py_decref(defaults);
                if (has_defaults != NULL) py_decref(has_defaults);
                if (kinds != NULL) py_decref(kinds);
                if (names != NULL) py_decref(names);
                if (captures != NULL) py_decref(captures);
                if (magic != NULL) py_decref(magic);
                if (signature != NULL) py_decref(signature);
                if (inner != NULL) py_decref(inner);
                return fn;
            }

            static int message_is(const char *expected) {
                PyObject *exc = py_current_exception();
                if (exc == NULL) return 0;
                PyObject *message = py_exc_get_message(exc);
                if (message == NULL) return 0;
                const char *text = py_str_utf8(message);
                return text != NULL && strcmp(text, expected) == 0;
            }

            int main(void) {
                PyObject *args = py_tuple_new(0);
                PyObject *silent = py_func_new_named(
                    (void *)silent_null,
                    NULL,
                    "silent_null"
                );
                PyObject *explicit_error = py_func_new(
                    (void *)dimension_error,
                    NULL
                );
                PyObject *silent_binding = signature_func(0);
                PyObject *invalid_signature = signature_func(1);
                if (
                    args == NULL || silent == NULL || explicit_error == NULL ||
                    silent_binding == NULL || invalid_signature == NULL
                ) return 10;

                py_clear_exception();
                PyObject *out = py_func_call_kwargs(silent, args, NULL);
                if (out != NULL) return 11;
                if (!py_err_occurred()) return 12;
                if (!message_is(
                    "compiled native function returned NULL without exception"
                )) return 13;
                if (py_exc_traceback_len(py_current_exception()) != 1) return 35;
                PyObject *traceback = py_exc_traceback_format_exc(
                    py_current_exception()
                );
                if (traceback == NULL) return 36;
                const char *traceback_text = py_str_utf8(traceback);
                if (traceback_text == NULL) return 37;
                if (strstr(
                    traceback_text,
                    "File \"<pcc runtime>\", line 0, in silent_null"
                ) == NULL) return 38;
                if (strstr(
                    traceback_text,
                    "runtime contract: NULL result without an exception"
                ) == NULL) return 39;
                py_decref(traceback);

                py_clear_exception();
                out = py_func_call_kwargs(explicit_error, args, NULL);
                if (out != NULL) return 14;
                if (!py_err_occurred()) return 15;
                if (!message_is("bad dimension")) return 16;
                if (py_exc_traceback_len(py_current_exception()) != 0) return 40;

                py_clear_exception();
                out = py_func_call_kwargs(py_int_from_i64(7), args, NULL);
                if (out != NULL) return 17;
                if (!py_err_occurred()) return 18;
                if (!message_is(
                    "native function call requires a function object"
                )) return 19;

                py_clear_exception();
                void **entry_slot = (void **)((char *)silent + 56);
                void *saved_entry = *entry_slot;
                *entry_slot = NULL;
                out = py_func_call_kwargs(silent, args, NULL);
                *entry_slot = saved_entry;
                if (out != NULL) return 20;
                if (!py_err_occurred()) return 21;
                if (!message_is("native function object has no entry point")) {
                    return 22;
                }

                py_clear_exception();
                out = py_func_call_kwargs(silent_binding, args, NULL);
                if (out != NULL) return 23;
                if (!py_err_occurred()) return 24;
                if (!message_is(
                    "native function signature names tuple is NULL"
                )) return 25;

                py_clear_exception();
                out = py_func_call_kwargs(invalid_signature, args, NULL);
                if (out != NULL) return 26;
                if (!py_err_occurred()) return 27;
                if (!message_is("invalid native function signature")) return 28;

                py_clear_exception();
                out = py_obj_call(silent, args, NULL);
                if (out != NULL) return 29;
                if (!py_err_occurred()) return 30;
                if (!message_is(
                    "compiled native function returned NULL without exception"
                )) return 31;
                if (py_exc_traceback_len(py_current_exception()) != 1) return 41;

                py_clear_exception();
                out = py_obj_call(explicit_error, args, NULL);
                if (out != NULL) return 32;
                if (!py_err_occurred()) return 33;
                if (!message_is("bad dimension")) return 34;

                py_clear_exception();
                py_decref(invalid_signature);
                py_decref(silent_binding);
                py_decref(explicit_error);
                py_decref(silent);
                py_decref(args);
                puts("py-func-fail-closed-ok");
                return 0;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    command = [
        os.environ.get("CC", "cc"),
        "-std=c11",
        f"-I{RUNTIME / 'include'}",
        str(source),
        str(archive),
        "-pthread",
        "-lm",
    ]
    if sys.platform.startswith("linux"):
        command.append("-ldl")
    command.extend(["-o", str(exe)])
    built = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize(
    "archive_fixture",
    ["pcc_runtime_archive", "pcc_runtime_archive"],
)
def test_py_func_null_result_sets_or_preserves_exception(
    archive_fixture,
    request,
    tmp_path,
):
    archive = request.getfixturevalue(archive_fixture)
    run = _compile_and_run(tmp_path, archive)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "py-func-fail-closed-ok\n"


def test_py_func_call_kwargs_fail_closed_contract_is_mirrored():
    py_source = (RUNTIME / "py" / "py_func.py").read_text(encoding="utf-8")

    for message in (
        "native function call received NULL callable",
        "native function call requires a function object",
        "native function object has no entry point",
        "native function could not create its argument tuple",
        "native function signature has no captures tuple",
        "native function argument binding returned NULL without exception",
        "compiled native function returned NULL without exception",
    ):
        assert message in py_source
    assert "if ptr_is_null(result):" in py_source
    assert "if py_err_occurred() != 0:" in py_source

    py_binding_guard = py_source.index(
        '"native function argument binding returned NULL without exception"'
    )
    py_binding_cleanup = py_source.index("_func_clear_call_root(slots, pins, 32)", py_binding_guard)
    assert py_binding_guard < py_binding_cleanup

    py_entry_call = py_source.index("result = call_ptr2(")
    py_entry_guard = py_source.index(
        '"compiled native function returned NULL without exception"',
        py_entry_call,
    )
    py_entry_cleanup = py_source.index("if owns_call_args != 0:", py_entry_call)
    assert py_entry_call < py_entry_guard < py_entry_cleanup


class _Tuple:
    def __init__(self, items):
        self.items = list(items)
        self.tag = PY_TYPE_TUPLE
        self.references = 1


class _Function:
    tag = PY_TYPE_FUNC

    def __init__(self, name):
        self.name = name


class _BindingOracle:
    """Run the actual fast binder; replace raw tuple/exception primitives."""

    def __init__(self):
        self.pending = None
        self.diagnostics = []
        self.allocated = []
        self.namespace = {
            "PY_TYPE_FUNC": PY_TYPE_FUNC, "PY_TYPE_INT": PY_TYPE_INT,
            "PY_TYPE_NONE": PY_TYPE_NONE, "PY_TYPE_TUPLE": PY_TYPE_TUPLE,
            "null": lambda: None, "ptr_is_null": lambda value: value is None,
            "is_tagged_int": lambda value: isinstance(value, int),
            "load_i32": lambda value, _offset: value.tag,
            "load_i64": lambda value, offset: len(value.items) if offset == 16 else None,
            "load_i8": lambda value, _offset: ord(value[0]) if value else 0,
            "load_ptr": lambda value, offset: value.name if offset == 72 else None,
            "ptr_add": lambda value, offset: (value, offset),
            "pcc_gc_load_ptr": lambda _owner, slot: slot[0].items[(slot[1] - 24) // 8],
            "cstr": lambda value: value,
            "py_tuple_new": self.tuple_new,
            "py_tuple_set_item": self.tuple_set,
            "py_dict_new": dict,
            "py_int_value_i64": int,
            "py_obj_truthy": bool,
            "py_incref": self.incref, "py_decref": self.decref,
            "py_err_occurred": lambda: self.pending is not None,
            "py_runtime_error_if_unset": self.runtime_error,
            "py_exc_new": lambda tag, message: (tag, message, None),
            "py_raise": lambda value: setattr(self, "pending", value),
        }
        path = RUNTIME / "py/py_func.py"
        names = {"_bind_signature_no_kwargs", "_tuple_borrow_known", "_copy_varargs_known",
                 "_signature_default_kind", "_is_none_or_null", "_is_tuple",
                 "_func_type_error", "_func_runtime_error_if_unset",
                 "_signature_runtime_error_if_unset"}
        parsed = ast.parse(path.read_text(), filename=str(path))
        functions = [node for node in parsed.body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"),
             self.namespace)

    def tuple_new(self, size):
        value = _Tuple([None] * size)
        self.allocated.append(value)
        return value

    def tuple_set(self, value, index, item):
        value.items[index] = item
        self.incref(item)

    def incref(self, value):
        if isinstance(value, _Tuple):
            value.references += 1

    def decref(self, value):
        if isinstance(value, _Tuple):
            value.references -= 1

    def runtime_error(self, context, message):
        assert self.pending is None
        self.pending = (7, message, context)
        self.diagnostics.append(self.pending)
        return None

    def bind(self, signature, args, name="binding_target"):
        return self.namespace["_bind_signature_no_kwargs"](signature, args, _Function(name))


def _signature(kinds=(0,), flags=(True,), defaults=None):
    if defaults is None:
        defaults = (object(),) * len(kinds)
    return _Tuple(["__pcc_func_signature_v1__", _Tuple(["value"] * len(kinds)),
                   _Tuple(kinds), _Tuple(flags), _Tuple(defaults)])


@pytest.mark.parametrize("slot,word", [
    (1, "names"), (2, "kinds"), (3, "default flags"), (4, "defaults"),
])
def test_fast_binding_missing_signature_tuple_reports_runtime_error_with_name(slot, word):
    memory = _BindingOracle()
    signature = _signature()
    signature.items[slot] = None
    assert memory.bind(signature, _Tuple([])) is None
    assert memory.pending == (7, f"native function signature {word} tuple is NULL", "binding_target")


@pytest.mark.parametrize("slot,word", [
    (2, "kind"), (3, "default flag"), (4, "default"),
])
def test_fast_binding_missing_signature_entry_reports_specific_failure(slot, word):
    memory = _BindingOracle()
    signature = _signature()
    signature.items[slot].items[0] = None
    assert memory.bind(signature, _Tuple([])) is None
    assert memory.pending == (7, f"native function signature {word} entry is NULL", "binding_target")
    assert all(value.references == 0 for value in memory.allocated)


@pytest.mark.parametrize("name", [None, ""])
def test_fast_binding_unnamed_context_and_existing_exception_are_preserved(name):
    memory = _BindingOracle()
    signature = _signature()
    signature.items[1] = None
    assert memory.bind(signature, _Tuple([]), name) is None
    assert memory.pending[2] == "py_func_bind_signature"
    original = (2, "original ValueError", "original_context")
    memory.pending = original
    assert memory.bind(signature, _Tuple([]), name) is None
    assert memory.pending is original
    assert len(memory.diagnostics) == 1


def test_fast_binding_missing_positional_entry_and_exact_arity_kind_are_specific():
    memory = _BindingOracle()
    signature = _signature((0, 0), (False, True))
    assert memory.bind(signature, _Tuple([None])) is None
    assert memory.pending == (7, "native function positional argument entry is NULL", "binding_target")
    memory.pending = None
    signature = _signature((None,))
    assert memory.bind(signature, _Tuple([object()])) is None
    assert memory.pending == (7, "native function signature kind entry is NULL", "binding_target")


def test_fast_binding_explicit_type_error_and_successful_tuple_default_are_unchanged():
    memory = _BindingOracle()
    signature = _signature()
    signature.items[2] = _Tuple([])
    assert memory.bind(signature, _Tuple([])) is None
    assert memory.pending == (3, "invalid native function signature", None)
    assert not memory.diagnostics
    memory.pending = None
    default = _Tuple([5, 7])
    signature = _signature(defaults=(default,))
    result = memory.bind(signature, _Tuple([]))
    assert result.items == [default] and result.items[0] is default
    assert result.references == 1 and default.references == 2
    assert memory.pending is None and not memory.diagnostics


def test_fast_binding_varargs_null_argument_has_function_context():
    memory = _BindingOracle()
    signature = _signature((3,), (False,))
    assert memory.bind(signature, _Tuple([None, object()])) is None
    assert memory.pending == (7, "native function varargs argument entry is NULL", "binding_target")
    assert all(value.references == 0 for value in memory.allocated)


@pytest.mark.parametrize("failure,message", [
    ("bound", "native function bound argument tuple allocation returned NULL"),
    ("varargs", "native function varargs tuple allocation returned NULL"),
    ("kwargs", "native function empty kwargs dict allocation returned NULL"),
    ("factory", "native function default factory binding returned NULL"),
])
def test_fast_binding_silent_internal_failure_sets_specific_runtime_error(failure, message):
    memory = _BindingOracle()
    args = _Tuple([])
    signature = _signature()
    if failure == "bound":
        memory.namespace["py_tuple_new"] = lambda _size: None
    elif failure == "varargs":
        signature = _signature((3,), (False,))
        args = _Tuple([object(), object()])
        original_new = memory.tuple_new
        memory.namespace["py_tuple_new"] = lambda size: None if size == 2 else original_new(size)
    elif failure == "kwargs":
        signature = _signature((4,), (False,))
        memory.namespace["py_dict_new"] = lambda: None
    else:
        signature = _signature((0,), (2,))
        memory.namespace["_bind_dataclass_factory"] = lambda *_args: None
    assert memory.bind(signature, args) is None
    assert memory.pending == (7, message, "binding_target")
    assert all(value.references == 0 for value in memory.allocated)


@pytest.mark.parametrize("triple", [
    "arm64-apple-darwin", "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
])
def test_fast_binding_diagnostic_reaches_owned_emitter(tmp_path, monkeypatch, triple):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.owned_runtime_build import runtime_ir_passes
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "py_runtime_func" / "py" / "py_func.py"
    source.parent.mkdir(parents=True)
    source.write_text((RUNTIME / "py/py_func.py").read_text())
    output = tmp_path / "py_func.ll"
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", runtime_ir_passes(str(RUNTIME)))
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple=triple)
    ir_text = output.read_text()
    assert "@user_py_func__signature_runtime_error_if_unset(" in ir_text
    payload = emit_owned_object(ir_text, triple)
    assert len(payload) > 64
    (tmp_path / "py_func.o").write_bytes(payload)
