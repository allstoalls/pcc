"""Qualified owned bases retain their runtime identity, fields and methods."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import pytest


PROVIDER = '''class Root:
    def __init__(self, value):
        self.value = value
    def read(self):
        return self.value
'''
MIDDLE = '''import alpha as provider
class Middle(provider.Root):
    pass
class One(provider.Root):
    def side(self):
        return "one"
class Two(provider.Root):
    def side(self):
        return "two"
class Diamond(One, Two):
    pass
'''
CONSUMER = '''import alpha as left
import beta as right
import middle
from alpha import Root as NamedRoot
class Root:
    pass
class Left(left.Root):
    pass
class Right(right.Root):
    pass
class Grandchild(Left):
    pass
class Named(NamedRoot):
    pass
class External(middle.Middle):
    pass
class ImportedDiamond(middle.Diamond):
    pass
def check():
    first = Left("left")
    second = Right("right")
    third = Grandchild("grandchild")
    print(isinstance(first, left.Root), isinstance(first, right.Root), isinstance(first, Root))
    print(isinstance(second, right.Root), isinstance(second, left.Root))
    print(isinstance(third, left.Root), isinstance(third, Left))
    print(issubclass(Left, left.Root), issubclass(Right, right.Root))
    print(first.read(), second.read(), third.read())
    print(isinstance(Named("named"), left.Root))
    print(External("external").read())
    diamond = ImportedDiamond("diamond")
    print(diamond.read(), diamond.side())
    print(isinstance(diamond, left.Root), isinstance(diamond, middle.One), isinstance(diamond, middle.Two))
check()
'''
EXPECTED = "True False False\nTrue False\nTrue True\nTrue True\nleft right grandchild\nTrue\nexternal\ndiamond one\nTrue True True\n"
MODULES = ["alpha", "beta", "middle", "consumer"]


def _sources(tmp_path):
    sources = []
    for name, text in zip(MODULES, (PROVIDER, PROVIDER, MIDDLE, CONSUMER)):
        path = tmp_path / (name + ".py")
        path.write_text(text)
        sources.append(str(path))
    return sources


def test_qualified_base_type_inference_keeps_module_identity(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import _InferCtx, _class_bases_from_def
    from pcc.frontends.python.py_ast import ClassDef, ClassType

    parsed, exports, _derived = build_closed_world_context(_sources(tmp_path), MODULES)
    module = parsed[3]
    context = _InferCtx(module, external_exports=exports)
    local_root = ClassType(name="Root", module="consumer", fields=(), bases=())
    context.register_class_type("Root", local_root)
    seen = {}
    for statement in module.body:
        if isinstance(statement, ClassDef) and statement.name in ("Left", "Right"):
            seen[statement.name] = [(base.module, base.name) for base in _class_bases_from_def(context, statement)]
    assert seen == {"Left": [("alpha", "Root")], "Right": [("beta", "Root")]}
    assert context.class_types["Root"] is local_root


def test_qualified_base_ir_preserves_native_mro(tmp_path):
    from pcc.frontends.python.pipeline import compile_python_multi

    output = tmp_path / "qualified.ll"
    compile_python_multi(_sources(tmp_path), str(output), module_names=MODULES,
                         entry_module="consumer", backend="self", libpython_mode="off",
                         ir_scaffold_mode="on", emit_llvm_only=True)
    ir_text = output.read_text()
    for name in ("Left", "Right", "External", "ImportedDiamond"):
        calls = [line for line in ir_text.splitlines() if re.search(r"%class\." + name + r"\.\d+", line) and "@py_class_new(" in line]
        assert calls, name
        assert all(re.search(r"ptr %[^,]+, i32 1, ptr", line) for line in calls), calls
    assert "load ptr, ptr @.class.alpha.Root" in ir_text
    assert "load ptr, ptr @.class.beta.Root" in ir_text


def test_qualified_base_identity_and_inherited_methods_execute_natively(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    output = tmp_path / "qualified"
    compile_python_multi(_sources(tmp_path), str(output), module_names=MODULES,
                         entry_module="consumer", backend="self", libpython_mode="off",
                         ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc), PATH=""))
        assert result.returncode == 0, f"GC{gc}: " + result.stdout + result.stderr
        assert result.stderr == "", f"GC{gc}: " + result.stderr
        assert result.stdout == EXPECTED, f"GC{gc}: " + result.stdout


def test_qualified_base_exports_keep_cross_module_diamond_fields(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import _InferCtx, _class_type_from_export

    parsed, exports, _derived = build_closed_world_context(_sources(tmp_path), MODULES)
    assert exports["middle"]["Middle"]["base_names"] == ("alpha.Root",)
    assert exports["middle"]["Middle"]["field_names"] == ("value",)
    assert exports["middle"]["Diamond"]["field_names"] == ("value",)
    assert exports["consumer"]["External"]["field_names"] == ("value",)
    assert exports["consumer"]["ImportedDiamond"]["field_names"] == ("value",)
    ctx = _InferCtx(parsed[3], external_exports=exports)
    cls = _class_type_from_export(ctx, "middle", exports["middle"]["Middle"], exports["middle"], {})
    assert [(base.module, base.name) for base in cls.bases] == [("alpha", "Root")]


@pytest.mark.parametrize("base", ["MISSING", "VALUE"])
def test_invalid_qualified_owned_base_is_diagnosed(tmp_path, monkeypatch, capfd, base):
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.frontends.python.codegen.errors import L1CodegenError
    from pcc.frontends.python.codegen.class_gen import ClassLoweringError

    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1")

    provider = tmp_path / "alpha.py"
    provider.write_text("VALUE = 42\n")
    source = tmp_path / "invalid.py"
    source.write_text("import alpha\nclass Invalid(alpha." + base + "):\n    pass\n")
    with pytest.raises((ClassLoweringError, L1CodegenError, RuntimeError)) as caught:
        compile_python_multi([str(provider), str(source)], str(tmp_path / "invalid.ll"),
                             module_names=["alpha", "invalid"], entry_module="invalid",
                             backend="self", libpython_mode="off", emit_llvm_only=True)
    diagnostic = type(caught.value).__name__ + ": " + str(caught.value) + capfd.readouterr().err
    detail = "is unavailable" if base == "MISSING" else "is not a class"
    assert "ClassLoweringError: native class base " + detail + ": alpha." + base in diagnostic


def test_cyclic_qualified_base_exports_are_diagnosed():
    from pcc.frontends.python.pipeline_closed_world import _flatten_closed_world_class_export_fields
    from pcc.frontends.python.pipeline_modes import PyPipelineError

    exports = {}
    for owner, other in (("alpha", "beta"), ("beta", "alpha")):
        exports[owner] = {"Root": {"kind": "class", "class_name": "Root",
                                   "owning_module": owner,
                                   "base_names": (other + ".Root",),
                                   "field_names": (), "field_types": ()}}
    with pytest.raises(PyPipelineError, match="cyclic native class base graph"):
        _flatten_closed_world_class_export_fields(exports)


def test_qualified_base_helpers_have_native_export_mirrors():
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports
    from pcc.frontends.python.type_infer import _CLASS_LOWERING_HOST_METHODS

    exports = _default_native_module_exports("pcc.frontends.python.codegen.class_gen")["pcc.frontends.python.pipeline_closed_world"]
    for name, arity in (("qualified_class_base_name", 5), ("resolve_class_base_export", 3)):
        assert exports[name]["kind"] == "function"
        assert len(exports[name]["param_types"]) == arity
        assert len(exports[name]["call_sig"]) == arity
    assert "_normalize_native_class_bases" in _CLASS_LOWERING_HOST_METHODS


def test_qualified_base_helpers_emit_without_cpython(tmp_path):
    from dataclasses import replace
    import pcc
    from pcc.frontends.python import pipeline_closed_world
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.py_ast import FuncDef
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    root = Path(pcc.__file__).parent
    modules = ["pcc.frontends.python.pipeline_closed_world", "pcc.frontends.python.pipeline_exports",
               "pcc.frontends.python.pipeline_ast_wire", "pcc.driver.paths", "pcc.frontends.python.py_ast"]
    paths = [str(root / (name[4:].replace(".", "/") + ".py")) for name in modules]
    parsed, exports, derived = build_closed_world_context(paths, modules)
    selected = {"qualified_class_base_name", "resolve_class_base_export", "_resolve_ast_import_from_module"}
    # Exercise the exact new production bodies with their real sibling export
    # context. Other unrelated functions in the module are outside this gate.
    module = replace(parsed[0], body=tuple(statement for statement in parsed[0].body
                                         if not isinstance(statement, FuncDef) or statement.name in selected))
    external = {name: info for name, info in exports.items() if name != modules[0]}
    typed = infer_module(module, external_exports=external, derived_class_map=derived)
    generator = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    generator._native_module_exports = external
    generator._strict_no_libpython = True
    generator._prefer_native_callable_values = True
    generator._module_source_path = pipeline_closed_world.__file__
    generator._python_library = True
    generator._skip_program_main = True
    ir_text = str(generator.generate(typed))
    (tmp_path / "base_helpers.ll").write_text(ir_text)
    for name in ("qualified_class_base_name", "resolve_class_base_export"):
        symbol = "user_pcc_frontends_python_pipeline_closed_world_" + name
        found = re.search(r"define[^\n]*@" + symbol + r"\([^\n]*\)[^{]*\{(.*?)\n\}", ir_text, re.S)
        assert found is not None, name
        assert "@py_cpy_" not in found.group(1)
        assert "strict.nolib.stub" not in found.group(1)
