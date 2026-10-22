"""Source function scopes own closure names and calls across statement blocks."""

import dataclasses
import os
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Call, FuncDef, Name
from pcc.frontends.python.py_lift import parse_and_lift


SHADOW_PARAMETER = '''def outer(seed):
    def inner(value):
        return seed + value
    def shadow(inner):
        return inner(value=2)
    def replacement(value):
        return value + 97
    assert shadow(replacement) == 99
    return inner(2)
def main():
    assert outer(40) == 42
    print("HOIST_LEXICAL_SHADOW_OK")
main()
'''

SHADOW_LOCAL = '''def outer(seed):
    def inner(value):
        return seed + value
    def replacement(value):
        return value + 97
    def shadow():
        inner = replacement
        return inner(value=2)
    assert shadow() == 99
    return inner(2)
def main():
    assert outer(40) == 42
    print("HOIST_LOCAL_SHADOW_OK")
main()
'''

SHADOW_IMPORT = '''def outer(seed):
    def inner(value):
        return seed + value
    def shadow():
        from builtins import abs as inner
        return inner(-99)
    assert shadow() == 99
    return inner(2)
def main():
    assert outer(40) == 42
    print("HOIST_IMPORT_SHADOW_OK")
main()
'''

SHADOW_GLOBAL = '''def inner(value):
    return value + 97
def outer(seed):
    def inner(value):
        return seed + value
    def shadow():
        global inner
        return inner(value=2)
    assert shadow() == 99
    return inner(2)
def main():
    assert outer(40) == 42
    print("HOIST_GLOBAL_SHADOW_OK")
main()
'''

SHADOW_DECLARATION = '''def outer(seed):
    def inner(value):
        return seed + value
    def shadow():
        def inner(value):
            return value + 97
        return inner(2)
    assert shadow() == 99
    return inner(2)
def main():
    assert outer(40) == 42
    print("HOIST_DECLARATION_SHADOW_OK")
main()
'''

SHADOW_SELF_PARAMETER = '''def outer():
    def inner(inner):
        return inner(value=2)
    def replacement(value):
        return value + 97
    return inner(replacement)
def main():
    assert outer() == 99
    print("HOIST_SELF_PARAMETER_OK")
main()
'''

OUTER_DEFAULT = '''def outer(seed):
    def inner(value):
        return seed + value
    def shadow(inner=inner):
        return inner(value=2)
    def replacement(value):
        return value + 97
    assert shadow() == 42
    assert shadow(replacement) == 99
    return inner(2)
def main():
    assert outer(40) == 42
    print("HOIST_OUTER_DEFAULT_OK")
main()
'''

NONLOCAL = '''def outer(seed):
    def inner(value):
        return seed + value
    def shadow():
        nonlocal inner
        return inner(value=2)
    assert shadow() == 42
    def replacement(value):
        return value + 97
    inner = replacement
    return shadow()
def main():
    assert outer(40) == 99
    print("HOIST_NONLOCAL_OK")
main()
'''

CONDITIONAL_CALL = '''def outer(seed):
    if seed:
        def inner(value):
            return value + seed
    mapping = {}
    mapping['value'] = 2
    return inner(**mapping)
def main():
    assert outer(40) == 42
    print('HOIST_CONDITIONAL_CALL_OK')
main()
'''


def _block_program(header, footer="", prefix=""):
    return prefix + '''def outer(seed):
''' + header + '''        def inner(value):
            return value + seed
''' + footer + '''    rejected = 0
    try:
        inner(2, seed=99)
    except TypeError:
        rejected += 1
    mapping = {}
    mapping["value"] = 2
    assert inner(**mapping) == 42
    assert rejected == 1
    return inner(value=2)
def main():
    assert outer(40) == 42
    print("HOIST_BLOCK_CALL_OK")
main()
'''


BLOCK_PROGRAMS = {
    "if": _block_program("    if seed:\n"),
    "while": _block_program("    while seed:\n", "        break\n"),
    "for": _block_program("    for unused in [1]:\n"),
    "try": _block_program("    try:\n", "    finally:\n        marker = 1\n"),
    "with": _block_program("    with Manager():\n", prefix='''class Manager:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, error, trace):
        return False
'''),
}

PROGRAMS = {
    "parameter": (SHADOW_PARAMETER, "HOIST_LEXICAL_SHADOW_OK\n"),
    "local": (SHADOW_LOCAL, "HOIST_LOCAL_SHADOW_OK\n"),
    "import": (SHADOW_IMPORT, "HOIST_IMPORT_SHADOW_OK\n"),
    "global": (SHADOW_GLOBAL, "HOIST_GLOBAL_SHADOW_OK\n"),
    "declaration": (SHADOW_DECLARATION, "HOIST_DECLARATION_SHADOW_OK\n"),
    "self_parameter": (SHADOW_SELF_PARAMETER, "HOIST_SELF_PARAMETER_OK\n"),
    "outer_default": (OUTER_DEFAULT, "HOIST_OUTER_DEFAULT_OK\n"),
    "nonlocal": (NONLOCAL, "HOIST_NONLOCAL_OK\n"),
    "conditional": (CONDITIONAL_CALL, "HOIST_CONDITIONAL_CALL_OK\n"),
}
PROGRAMS.update({name: (source, "HOIST_BLOCK_CALL_OK\n") for name, source in BLOCK_PROGRAMS.items()})


def _generate(source):
    module = type_infer.infer_module(parse_and_lift(source, "<lexical-scope>", "lexical_scope"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    return codegen, str(codegen.generate(module))


def _calls(node):
    if isinstance(node, Call):
        yield node
    if isinstance(node, (tuple, list)):
        for child in node:
            yield from _calls(child)
    elif dataclasses.is_dataclass(node):
        for field in dataclasses.fields(node):
            if field.name not in ("span", "ty", "annotation", "return_ty"):
                yield from _calls(getattr(node, field.name))


@pytest.mark.parametrize("name", ("parameter", "local", "import", "global"))
def test_child_binding_blocks_parent_hoist_renaming(name):
    codegen, _text = _generate(PROGRAMS[name][0])
    shadow = next(node for node in codegen.ast_module.body
                  if isinstance(node, FuncDef) and node.name == "__nested_shadow")
    assert all(not isinstance(call.func, Name) or call.func.ident != "__nested_inner"
               for call in _calls(shadow.body)), shadow.body


@pytest.mark.parametrize("name", ("conditional", *BLOCK_PROGRAMS))
def test_statement_block_definition_uses_whole_function_call_contract(name):
    codegen, _text = _generate(PROGRAMS[name][0])
    outer = next(node for node in codegen.ast_module.body
                 if isinstance(node, FuncDef) and node.name == "outer")
    assert all(not isinstance(call.func, Name) or call.func.ident != "__nested_inner"
               for call in _calls(outer.body)), outer.body


@pytest.mark.parametrize("name", PROGRAMS)
def test_lexical_scope_controls_follow_cpython(capsys, name):
    source, expected = PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("name", PROGRAMS)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_lexical_scope_shapes_reach_owned_objects(name, target):
    _codegen, text = _generate(PROGRAMS[name][0])
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_lexical_scope_controls_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name):
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
