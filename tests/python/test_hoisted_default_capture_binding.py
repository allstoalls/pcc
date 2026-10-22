"""Nested defaults belong to a definition-time callable, not capture kwargs."""
import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Assign, FuncDef, Name
from pcc.frontends.python.py_lift import parse_and_lift


MINIMAL = '''def outer(ast_module):
    def existing_top_or_hoisted_names(include_classes=False):
        return ast_module
    def analyze_names(fd, excluded, own_name=None, outer_scope_names=()):
        names = existing_top_or_hoisted_names()
        return names[0] + fd + len(excluded) + len(outer_scope_names)
    def mutable_captures_in_fd(fd, excluded):
        return analyze_names(fd, excluded)
    return mutable_captures_in_fd(5, (2,))
def main():
    assert outer([36]) == 42
    print("HOIST_CAPTURE_DEFAULT_OK")
main()
'''

KWONLY = '''def outer(seed):
    def inner(value, /, *, offset=seed):
        return value + offset + seed
    seed = 23
    return inner(2)
def main():
    assert outer(17) == 42
    print("HOIST_KWONLY_DEFAULT_OK")
main()
'''

EFFECTS = '''events = []
def make():
    value = []
    events.append(value)
    return value
def outer():
    def inner(value=make()):
        return value
    assert len(events) == 1
    first = inner()
    second = inner()
    assert first is second and first is events[0]
    return first
def main():
    first = outer()
    events.clear()
    second = outer()
    assert first is not second
    print("HOIST_DEFAULT_EFFECTS_OK")
main()
'''

LITERAL_DUPLICATE_KW = '''def outer(ast_module):
    def inner(value, default=()):
        return value + ast_module
    try:
        inner(1, ast_module=99)
    except TypeError:
        pass
    else:
        raise AssertionError("capture keyword was accepted")
    try:
        inner(value=1, **{"value": 2})
    except TypeError:
        return 42
    raise AssertionError("duplicate keyword was accepted")
def main():
    assert outer(41) == 42
    print("HOIST_INVALID_KW_OK")
main()
'''

INVALID_KW = LITERAL_DUPLICATE_KW.replace(
    '    try:\n        inner(value=1, **{"value": 2})',
    '    mapping = {}\n    mapping["value"] = 2\n    try:\n        inner(value=1, **mapping)',
)

RECURSIVE = '''def outer():
    source = [42]
    def recursive(count, value=source):
        if count:
            return recursive(count - 1)
        return value[0]
    source = [99]
    return recursive(3)
def main():
    assert outer() == 42
    print("HOIST_RECURSIVE_DEFAULT_OK")
main()
'''

PROGRAMS = {
    "minimal": (MINIMAL, "HOIST_CAPTURE_DEFAULT_OK\n"),
    "kwonly": (KWONLY, "HOIST_KWONLY_DEFAULT_OK\n"),
    "effects": (EFFECTS, "HOIST_DEFAULT_EFFECTS_OK\n"),
    "invalid_kw": (INVALID_KW, "HOIST_INVALID_KW_OK\n"),
    "recursive": (RECURSIVE, "HOIST_RECURSIVE_DEFAULT_OK\n"),
}

NO_DEFAULT_MAPPING = '''def outer(seed):
    def inner(value):
        return value + seed
    mapping = {}
    mapping["value"] = 2
    return inner(**mapping)
def main():
    assert outer(40) == 42
    print("HOIST_NO_DEFAULT_MAPPING_OK")
main()
'''

NO_DEFAULT_INVALID_KW = '''def outer(seed):
    def inner(value):
        return value + seed
    try:
        inner(2, seed=99)
    except TypeError:
        pass
    else:
        raise AssertionError("capture was treated as a user formal")
    return inner(value=2)
def main():
    assert outer(40) == 42
    print("HOIST_NO_DEFAULT_INVALID_KW_OK")
main()
'''

NO_DEFAULT_PROGRAMS = {
    "no_default_mapping": (NO_DEFAULT_MAPPING, "HOIST_NO_DEFAULT_MAPPING_OK\n"),
    "no_default_invalid_kw": (NO_DEFAULT_INVALID_KW, "HOIST_NO_DEFAULT_INVALID_KW_OK\n"),
}

ARITY = '''def outer(seed):
    def inner(value):
        return value + seed
    rejected = 0
    try:
        inner()
    except TypeError:
        rejected += 1
    try:
        inner(1, 2)
    except TypeError:
        rejected += 1
    try:
        inner(1, value=2)
    except TypeError:
        rejected += 1
    assert rejected == 3
    return inner(2)
def main():
    assert outer(40) == 42
    print("HOIST_ARITY_OK")
main()
'''

STAR_ARGS = '''def outer(seed):
    def inner(value):
        return value + seed
    positional = [2]
    return inner(*positional)
def main():
    assert outer(40) == 42
    print("HOIST_STAR_ARGS_OK")
main()
'''

VARARGS = '''def outer(seed):
    def inner(*values, **options):
        return seed + sum(values) + options["offset"]
    positional = [1, 2]
    mapping = {}
    mapping["offset"] = 1
    return inner(*positional, **mapping)
def main():
    assert outer(38) == 42
    print("HOIST_VARARGS_OK")
main()
'''

SHADOW_REBIND = '''def outer(seed):
    def inner(value):
        return value + seed
    def shadow(inner):
        return inner(value=2)
    def replacement(value):
        return value + 97
    inner = replacement
    assert shadow(inner) == 99
    mapping = {}
    mapping["value"] = 2
    return inner(**mapping)
def main():
    assert outer(40) == 99
    print("HOIST_SHADOW_REBIND_OK")
main()
'''

PROGRAMS.update(NO_DEFAULT_PROGRAMS)
PROGRAMS.update({
    "arity": (ARITY, "HOIST_ARITY_OK\n"),
    "star_args": (STAR_ARGS, "HOIST_STAR_ARGS_OK\n"),
    "varargs": (VARARGS, "HOIST_VARARGS_OK\n"),
    "shadow_rebind": (SHADOW_REBIND, "HOIST_SHADOW_REBIND_OK\n"),
})


def test_proven_plain_positional_call_preserves_internal_path_and_ignores_shadow():
    source = '''def outer(seed):
    def inner(value):
        return seed + value
    def shadow(inner):
        return inner(value=2)
    return inner(2)
'''
    codegen, text = _generate(source)
    outer = next(node for node in codegen.ast_module.body if isinstance(node, FuncDef) and node.name == "outer")
    assert not any(isinstance(node, Assign) and isinstance(node.value, Name)
                   and "inner" in node.value.ident for node in outer.body), outer.body
    assert "@user_nested_default___nested_inner(" in text


@pytest.mark.parametrize("name", NO_DEFAULT_PROGRAMS)
def test_no_default_keyword_calls_keep_source_signature_binding(name):
    codegen, text = _generate(NO_DEFAULT_PROGRAMS[name][0])
    outer = next(node for node in codegen.ast_module.body if isinstance(node, FuncDef) and node.name == "outer")
    assert any(isinstance(node, Assign) and isinstance(node.value, Name)
               and "inner" in node.value.ident for node in outer.body), outer.body
    assert len(emit_owned_object(text, "arm64-apple-darwin")) > 0


def _generate(source):
    module = type_infer.infer_module(parse_and_lift(source, "<nested-default>", "nested_default"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    text = str(codegen.generate(module))
    return codegen, text


def test_default_bearing_direct_only_nested_definition_has_a_live_binding():
    codegen, _text = _generate(MINIMAL)
    outer = next(node for node in codegen.ast_module.body if isinstance(node, FuncDef) and node.name == "outer")
    assert any(isinstance(node, Assign) and isinstance(node.value, Name)
               and "analyze_names" in node.value.ident for node in outer.body), outer.body


def test_keyword_only_source_arguments_and_captures_use_native_definition_order():
    _codegen, text = _generate(KWONLY)
    match = re.search(r"^define[^\n]*@user_nested_default___nested_inner_native_adapter\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, text
    call = next(line for line in match.group(1).splitlines()
                if "@user_nested_default___nested_inner(" in line)
    assert call.index("%arg.0") < call.index("%cap.0") < call.index("%arg.1"), call


def test_default_factory_is_emitted_once_at_the_definition_not_each_call():
    _codegen, text = _generate(EFFECTS)
    match = re.search(r"^define[^\n]*@user_nested_default_outer\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, text
    body = match.group(1)
    assert len(re.findall(r"\bcall\b[^\n]*@user_nested_default_make\(", body)) == 1, body
    # py_func_new_signature_slots replaced py_func_new_named in d0b8b6720.
    assert len(re.findall(r"\bcall\b[^\n]*@py_func_new_(?:named|signature_slots)\(", body)) == 1, body


@pytest.mark.parametrize("name", PROGRAMS)
def test_definition_default_controls_follow_cpython(capsys, name):
    source, expected = PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("name", PROGRAMS)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_default_capture_and_user_kwargs_shapes_reach_owned_objects(name, target):
    _codegen, text = _generate(PROGRAMS[name][0])
    assert len(emit_owned_object(text, target)) > 0


def test_literal_dict_duplicate_kwargs_reaches_owned_x86_object():
    # This literal form exposed a separate initializer-expression emitter
    # gap. Keep it explicit while runtime-built kwargs isolate closure binding.
    _codegen, text = _generate(LITERAL_DUPLICATE_KW)
    assert len(emit_owned_object(text, "x86_64-unknown-linux-gnu")) > 0


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_nested_default_capture_controls_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name):
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
