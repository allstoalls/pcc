"""Hoisting preserves executable source bindings and per-definition identity."""

import dataclasses
import os
import re
import subprocess
import textwrap

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.hoist_lowering import _hoist_definition_needs_binding
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Assign, Delete, FuncDef, Name
from pcc.frontends.python.py_lift import parse_and_lift


CONDITIONAL = '''def outer(enabled):
    if enabled:
        def f():
            return 42
    try:
        answer = f()
    except UnboundLocalError:
        assert not enabled
        return 0
    assert enabled
    return answer
def main():
    assert outer(False) == 0
    assert outer(True) == 42
    print("HOIST_CONDITIONAL_BINDING_OK")
main()
'''

DELETE_REBIND = '''def replacement():
    return 99
def outer():
    def f():
        return 42
    assert f() == 42
    del f
    try:
        f()
    except UnboundLocalError:
        pass
    else:
        raise AssertionError("deleted function remained callable")
    f = replacement
    assert f() == 99
    return f()
def main():
    assert outer() == 99
    print("HOIST_DELETE_REBIND_OK")
main()
'''

PRIOR_CALL = '''def outer():
    try:
        f()
    except UnboundLocalError:
        pass
    else:
        raise AssertionError("function called before its definition")
    def f():
        return 42
    return f()
def main():
    assert outer() == 42
    print("HOIST_PRIOR_CALL_OK")
main()
'''

LOOP_IDENTITY = '''def outer(count):
    functions = []
    for unused in range(count):
        def f():
            return 42
        functions.append(f)
    if not count:
        try:
            f()
        except UnboundLocalError:
            return functions
        raise AssertionError("empty loop bound its function")
    assert f is functions[-1]
    return functions
def main():
    assert outer(0) == []
    first = outer(2)
    second = outer(2)
    assert first[0] is not first[1]
    assert first[0] is not second[0]
    assert first[0]() == 42
    print("HOIST_LOOP_FUNCTION_IDENTITY_OK")
main()
'''

CLASS_VALUES = '''def outer():
    def f(value):
        return value
    class C:
        first = f
        second = f
    assert C.first is C.second
    assert C.first(42) == 42
    return C
def main():
    first = outer()
    second = outer()
    assert first.first is not second.first
    print("HOIST_CLASS_FUNCTION_IDENTITY_OK")
main()
'''

LAMBDA_DEFAULT = '''def outer():
    def f(value):
        return value
    callback = lambda g=f: g
    assert callback() is callback()
    return callback
def main():
    first = outer()
    second = outer()
    assert first is not second
    assert first() is not second()
    assert first()(42) == 42
    print("HOIST_LAMBDA_DEFAULT_IDENTITY_OK")
main()
'''

LAMBDA_SHADOW_DEFAULT = LAMBDA_DEFAULT.replace("lambda g=f: g", "lambda f=f: f").replace(
    "HOIST_LAMBDA_DEFAULT_IDENTITY_OK", "HOIST_LAMBDA_SHADOW_DEFAULT_OK"
)

CLASS_METHOD_VALUES = '''def outer():
    def f(value):
        return value
    class C:
        f = 42
        def method(self):
            alias = f
            return alias
    sample = C()
    assert sample.method() is sample.method()
    return sample
def main():
    first = outer()
    second = outer()
    assert first.method() is not second.method()
    assert first.method()(42) == 42
    print("HOIST_CLASS_METHOD_FUNCTION_IDENTITY_OK")
main()
'''

LAMBDA_EFFECTS = '''def make(calls):
    calls.append(1)
    return [42]
def outer(calls):
    callback = lambda value=make(calls): value
    assert callback() is callback()
    assert calls == [1]
    return callback
def main():
    calls = []
    first = outer(calls)
    assert first() == [42]
    assert calls == [1]
    print("HOIST_LAMBDA_DEFAULT_EFFECTS_OK")
main()
'''

PROGRAMS = {
    "conditional": (CONDITIONAL, "HOIST_CONDITIONAL_BINDING_OK\n"),
    "delete_rebind": (DELETE_REBIND, "HOIST_DELETE_REBIND_OK\n"),
    "prior_call": (PRIOR_CALL, "HOIST_PRIOR_CALL_OK\n"),
    "loop_identity": (LOOP_IDENTITY, "HOIST_LOOP_FUNCTION_IDENTITY_OK\n"),
    "class_values": (CLASS_VALUES, "HOIST_CLASS_FUNCTION_IDENTITY_OK\n"),
    "class_method_values": (CLASS_METHOD_VALUES, "HOIST_CLASS_METHOD_FUNCTION_IDENTITY_OK\n"),
    "lambda_default": (LAMBDA_DEFAULT, "HOIST_LAMBDA_DEFAULT_IDENTITY_OK\n"),
    "lambda_shadow_default": (LAMBDA_SHADOW_DEFAULT, "HOIST_LAMBDA_SHADOW_DEFAULT_OK\n"),
    "lambda_effects": (LAMBDA_EFFECTS, "HOIST_LAMBDA_DEFAULT_EFFECTS_OK\n"),
}


def _generate(source):
    module = type_infer.infer_module(parse_and_lift(source, "<source-binding>", "source_binding"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    has_unavailable_function = "strict.nolib.stub:" in text
    assert not has_unavailable_function
    assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
    return codegen, text


def _walk(node):
    yield node
    if isinstance(node, (tuple, list)):
        for child in node:
            yield from _walk(child)
    elif dataclasses.is_dataclass(node):
        for field in dataclasses.fields(node):
            if field.name not in ("span", "ty", "annotation", "return_ty"):
                yield from _walk(getattr(node, field.name))


_CONSTRUCTOR = r"call [^\n]*@py_func_new_(?:named|signature_slots|static_def)\([^\n]*?"


def _outer_text(text):
    return next(body for body in re.findall(r"define [^\n]+\n.*?^\}", text, re.M | re.S)
                if "@user_source_binding_outer(" in body.splitlines()[0])


def test_conditional_definition_has_an_executable_binding():
    codegen, text = _generate(CONDITIONAL)
    outer = next(node for node in codegen.ast_module.body if isinstance(node, FuncDef) and node.name == "outer")
    bindings = [node for node in _walk(outer.body) if isinstance(node, Assign)
                and isinstance(node.value, Name) and node.value.ident == "__nested_f"]
    assert len(bindings) == 1
    assert ".bound." + bindings[0].targets[0].ident in _outer_text(text)


def test_delete_rebind_targets_the_materialized_local_callable():
    codegen, _text = _generate(DELETE_REBIND)
    outer = next(node for node in codegen.ast_module.body if isinstance(node, FuncDef) and node.name == "outer")
    deletion = next(node for node in _walk(outer.body) if isinstance(node, Delete))
    assert deletion.targets[0].ident != "__nested_f"
    assert any(isinstance(node, Assign) and isinstance(node.targets[0], Name)
               and node.targets[0].ident == deletion.targets[0].ident
               for node in _walk(outer.body))


def test_prior_call_keeps_the_source_local_bound_check():
    _codegen, text = _generate(PRIOR_CALL)
    outer = _outer_text(text)
    assert "f.bound.error" in outer
    direct_calls = re.findall(r"call ptr \(\) @user_source_binding___nested_f\(", outer)
    assert not direct_calls


@pytest.mark.parametrize("name", ("class_values", "class_method_values", "lambda_default", "lambda_shadow_default"))
def test_enclosing_callable_is_materialized_once_for_default_and_class_values(name):
    _codegen, text = _generate(PROGRAMS[name][0])
    outer = _outer_text(text)
    # Function objects are built by py_func_new_signature_slots since
    # d0b8b6720, or by py_func_new_static_def when capture-free with a static
    # signature; the contract is one construction per callable.
    function_count = len(re.findall(_CONSTRUCTOR + r"@user_source_binding_(?:__nested_)?f_native_adapter", outer))
    assert function_count == 1
    if name.startswith("lambda_"):
        callback_count = len(re.findall(_CONSTRUCTOR + r"@user_source_binding___nested_callback_native_adapter", outer))
        assert callback_count == 1


@pytest.mark.parametrize("name", PROGRAMS)
def test_source_binding_controls_follow_cpython(capsys, name):
    source, expected = PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("name", PROGRAMS)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_source_bindings_reach_owned_objects(name, target):
    _codegen, text = _generate(PROGRAMS[name][0])
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.parametrize("header,body,expected", (
    ("", "callback = lambda g=f: g\nreturn callback()", True),
    ("", "callback = lambda f=f: f\nreturn callback()", True),
    ("", "callback = lambda f: f\nreturn callback", False),
    ("", "callback = lambda: (f := 42)\nreturn callback", False),
    ("", "class C:\n    first = f\n    second = f\nreturn C", True),
    ("", "class C:\n    f = 42\n    first = f\nreturn C", False),
    ("", "class C:\n    f = 42\n    def method(self):\n        return f\nreturn C", True),
    ("", "class C:\n    def method(self, f):\n        return f\nreturn C", False),
    ("", "class C:\n    global f\n    first = f\nreturn C", False),
))
def test_class_and_lambda_value_uses_respect_lexical_owners(header, body, expected):
    source = ("def outer(" + header + "):\n"
              "    def f(value):\n        return value\n" + textwrap.indent(body, "    ") + "\n")
    module = parse_and_lift(source, "<value-scope>", "value_scope")
    outer = module.body[0]
    assert _hoist_definition_needs_binding(outer.body, outer.body[0]) is expected


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_source_binding_controls_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name):
    source, expected = PROGRAMS[name]
    path = tmp_path / (name + ".py")
    path.write_text(source)
    binary = tmp_path / name
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(path), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        assert result.returncode == 0 and result.stdout == expected and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)
