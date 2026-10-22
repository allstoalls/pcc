"""Lexical targets follow Python scopes before closure conversion."""

from __future__ import annotations

import os
import subprocess
import symtable
import textwrap

import pytest

from pcc.frontends.python.codegen.hoist_boxing import function_local_bindings
from pcc.frontends.python.codegen.hoist_free_names import compute_free_names
from pcc.frontends.python.py_ast import FuncDef
from pcc.frontends.python.py_lift import parse_and_lift


CASES = {
    "augassign": "inner += source",
    "delete": "del inner",
    "tuple_unpack": "inner, other = source",
    "list_unpack": "[inner, other] = source",
    "nested_star_unpack": "[[inner, *rest], other] = source",
    "delete_list": "del [inner, other]",
    "delete_mixed_targets": "del (inner, owner[index])",
    "walrus": "value = (inner := source)",
    "walrus_index": "owner[(inner := index)] = source",
    "augassign_attribute": "inner.value += source",
    "augassign_subscript": "inner[index] += source",
    "delete_attribute": "del inner.value",
    "delete_subscript": "del inner[index]",
    "global_augassign": "global inner\ninner += source",
    "global_delete": "global inner\ndel inner",
    "global_walrus": "global inner\nvalue = (inner := source)",
    "nonlocal_augassign": "nonlocal inner\ninner += source",
    "nonlocal_delete": "nonlocal inner\ndel inner",
    "nonlocal_walrus": "nonlocal inner\nvalue = (inner := source)",
    "global_unpack": "global inner\n[inner, other] = source",
    "nonlocal_unpack": "nonlocal inner\n[inner, other] = source",
    "function_default": "def child(value=(inner := source)):\n    return value",
    "function_decorator": "@(inner := deco)\ndef child(value=source):\n    return value",
    "lambda_default": "callback = lambda value=(inner := source): value",
    "lambda_body": "callback = lambda: (inner := source)",
    "nested_lambda_default": "callback = lambda: (lambda value=(inner := source): value)",
    "comprehension_target": "value = [inner for inner in source]",
    "comprehension_walrus": "value = [(inner := item) for item in source]",
    "class_headers": "@(dec := deco)\nclass Child((base := Base), metaclass=(meta := Meta)):\n    pass",
    "child_function_body": "def child():\n    inner = source\n    return inner",
    "child_class_body": "class Child:\n    value = (inner := source)",
    "loop_else": "for item in source:\n    pass\nelse:\n    inner = item",
    "while_else": "while source:\n    break\nelse:\n    inner = source",
    "handler": "try:\n    pass\nexcept Exception as inner:\n    pass",
}

PARENT_NAMES = ("inner", "source", "owner", "index", "deco", "Base", "Meta")


def _source(body):
    return (
        "def parent(" + ", ".join(PARENT_NAMES) + "):\n"
        "    def scope():\n" + textwrap.indent(body, "        ") + "\n"
        "    return scope\n"
    )


def _scope(body):
    source = _source(body)
    module = parse_and_lift(source, "<lexical-target>", "lexical_target")
    parent = next(node for node in module.body if isinstance(node, FuncDef))
    scope = next(node for node in parent.body if isinstance(node, FuncDef))
    reference = symtable.symtable(source, "<lexical-target>", "exec")
    # Python 3.15 includes implicit __annotate__ child tables.
    parent_table = next(table for table in reference.get_children() if table.get_name() == "parent")
    table = next(table for table in parent_table.get_children() if table.get_name() == "scope")
    return scope, table


@pytest.mark.parametrize("name", CASES)
def test_lexical_target_bindings_match_cpython_symbol_table(name):
    scope, table = _scope(CASES[name])
    expected = set(table.get_locals())
    if name.startswith("comprehension_"):
        # PEP 709 exposes inlined iteration slots in the symbol table; their
        # save/restore namespace is still separate from the enclosing owner.
        expected = {item for item in expected if not table.lookup(item).is_comp_iter()}
    assert set(function_local_bindings(scope)) == expected


@pytest.mark.parametrize("name", tuple(name for name in CASES if not name.startswith("child_")))
def test_free_name_analysis_uses_the_same_lexical_bindings(name):
    scope, table = _scope(CASES[name])
    free = compute_free_names(scope, (), None, PARENT_NAMES, (), lambda unused: (), {}, False, {})
    assert set(free) == set(table.get_frees())


LOCAL_CONTROL_BODIES = {
    "augassign": "try:\n    inner += 1\nexcept UnboundLocalError:\n    return 99\nreturn 0",
    "delete": "try:\n    del inner\nexcept UnboundLocalError:\n    return 99\nreturn 0",
    "walrus_prior_read": "try:\n    inner(value=2)\nexcept UnboundLocalError:\n    pass\nelse:\n    return 0\n(inner := replacement)\nassert inner is replacement\nreturn inner(value=2)",
    "bound_callable": "inner = replacement\ntry:\n    inner += 1\nexcept TypeError:\n    assert inner(value=2) == 99\n    return 99\nreturn 0",
}


def _local_control_source(stem):
    return (
        "def outer(seed):\n"
        "    def inner(value):\n        return seed + value\n"
        "    def replacement(value):\n        return value + 97\n"
        "    def shadow():\n" + textwrap.indent(LOCAL_CONTROL_BODIES[stem], "        ") + "\n"
        "    assert shadow() == 99\n    assert inner(2) == 42\n    return inner(2)\n"
        "def main():\n    assert outer(40) == 42\n"
        "    print('HOIST_LOCAL_" + stem.upper() + "_OK')\n"
        "main()\n"
    )


@pytest.mark.parametrize("stem", LOCAL_CONTROL_BODIES)
def test_local_targets_do_not_capture_an_outer_same_named_callable(stem):
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.hoist_lowering import hoist_nested_funcdefs

    source = _local_control_source(stem)
    module = type_infer.infer_module(parse_and_lift(source, "<lexical-target-control>", "lexical_target"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    hoist_nested_funcdefs(codegen)
    shadow = next(node for node in codegen.ast_module.body if isinstance(node, FuncDef) and node.name == "__nested_shadow")
    assert all(arg.name not in ("inner", "__pcc_closure_value___nested_inner") for arg in shadow.args)


@pytest.mark.parametrize("stem", LOCAL_CONTROL_BODIES)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_shadowed_callable_local_targets_reach_owned_objects(stem, target):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    module = type_infer.infer_module(parse_and_lift(_local_control_source(stem), "<lexical-target-execution>", "lexical_target"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    if stem == "bound_callable":
        # ``inner = replacement`` precedes every read: definitely bound, so
        # no unbound-local check is needed (CPython raises none here).
        assert "inner.bound.error" not in text
    else:
        assert "inner.bound.error" in text
    assert len(emit_owned_object(text, target)) > 0


def test_walrus_callable_store_marks_the_local_bound():
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    module = type_infer.infer_module(parse_and_lift(_local_control_source("walrus_prior_read"), "<walrus-boundness>", "lexical_target"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = codegen.generate(module)
    shadow = next(function for function in codegen.module.functions if function.name.endswith("__nested_shadow") and "adapter" not in function.name)
    body = []
    active = False
    for line in text.splitlines():
        if line.startswith("define "):
            active = ("@" + shadow.name + "(") in line
        elif active and line == "}":
            break
        elif active:
            body.append(line)
    assert any("store i1 1" in line and ".bound.inner.owned" in line for line in body)


@pytest.mark.parametrize("header,body,expected", (
    ("", "alias = replacement\nreturn alias is replacement", True),
    ("replacement", "alias = replacement\nreturn alias is replacement", False),
    ("", "global replacement\nalias = replacement", False),
    ("", "replacement = 1\nreturn replacement", False),
    ("value=replacement", "return value", True),
))
def test_descendant_value_uses_preserve_the_defining_callable_binding(header, body, expected):
    from pcc.frontends.python.codegen.hoist_lowering import _hoist_definition_needs_binding

    source = (
        "def outer():\n"
        "    def replacement(value):\n        return value\n"
        "    def child(" + header + "):\n" + textwrap.indent(body, "        ") + "\n"
        "    return child()\n"
    )
    module = parse_and_lift(source, "<descendant-function-identity>", "descendant_identity")
    outer = module.body[0]
    replacement = next(node for node in outer.body if isinstance(node, FuncDef) and node.name == "replacement")
    assert _hoist_definition_needs_binding(outer.body, replacement) is expected


@pytest.mark.integration
@pytest.mark.parametrize("stem", LOCAL_CONTROL_BODIES)
def test_shadowed_callable_local_targets_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, stem):
    source = tmp_path / (stem + ".py")
    source.write_text(_local_control_source(stem))
    binary = tmp_path / stem
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="/nonexistent"))
        assert result.returncode == 0 and result.stdout == "HOIST_LOCAL_" + stem.upper() + "_OK\n" and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)
