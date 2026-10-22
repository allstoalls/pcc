"""Only proven compiler-consumed single-entry unsafe edges may be omitted."""

from __future__ import annotations

import ast
from dataclasses import fields
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_dependency_closure as closure
from pcc.frontends.python import pipeline_unsafe_imports as proof
from pcc.frontends.python.unsafe_intrinsic_policy import (
    UNSAFE_PROVIDER_INDEPENDENT_ZERO_ARG_RETURNS as SIGNATURES,
)


ROOT = Path(__file__).resolve().parents[2]
IMPORT = "from pcc.unsafe import gc_backend_current as backend\n"
CALL = "print(backend())\n"


def _canary_source():
    tree = ast.parse((ROOT / "scripts/bootstrap_platform.py").read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_text"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "canary"):
            return ast.literal_eval(node.args[0])
    raise AssertionError("production canary source missing")


@pytest.fixture
def owned_sources(tmp_path, monkeypatch):
    """Real owned provider; bounded resolver avoids unrelated package closure."""
    provider = ROOT / "pcc/unsafe/__init__.py"
    typing = tmp_path / "typing.py"
    typing.write_text("Any = None\n")
    routes = {"pcc.unsafe": str(provider), "typing": str(typing)}
    monkeypatch.setattr(
        closure, "_resolve_module_src_for_import",
        lambda root, module: routes.get(module),
    )
    return routes


def test_exact_production_canary_omits_only_consumed_provider(tmp_path, owned_sources):
    source = _canary_source()
    assert hashlib.sha256(source.encode()).hexdigest() == (
        "6150f4bdad843781a1ca20df37deaf40081a6df8ba655597d639c052d3d5a7bc"
    )
    assert proof.source_has_only_consumed_unsafe_imports(source)
    entry = tmp_path / "canary.py"
    entry.write_text(source)
    paths, names = closure._collect_relative_module_closure(str(entry))
    assert paths == [str(entry)]
    assert names == ["canary"]
    # Target helpers do not prune unless their single-entry caller proved it.
    ordinary = closure._top_level_import_targets(
        str(tmp_path), source, top_level_only=False,
    )
    assert (owned_sources["pcc.unsafe"], "pcc.unsafe") in ordinary


@pytest.mark.parametrize("recursive_stdlib", [False, True])
def test_direct_indexed_canary_stays_pruned_at_production_admission(
    tmp_path, monkeypatch, recursive_stdlib,
):
    from pcc.frontends.python import pipeline

    # Exercise ordinary nearest-first project resolution, not a mocked resolver.
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "pcc").symlink_to(ROOT / "pcc", target_is_directory=True)
    directory = tmp_path / "build/platform-qualification/gc4"
    directory.mkdir(parents=True)
    entry = directory / "canary.py"
    entry.write_text(_canary_source())
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    prepare = pipeline._prepare_multi_source_compile_closure
    captured = []

    class AdmissionObserved(Exception):
        pass

    def observe(*args, **kwargs):
        paths, names = prepare(*args, **kwargs)
        captured.extend(zip(paths, names))
        raise AdmissionObserved

    monkeypatch.setattr(pipeline, "_prepare_multi_source_compile_closure", observe)
    with pytest.raises(AdmissionObserved):
        pipeline.compile_python(
            str(entry), str(directory / "not-produced"), backend="self",
            libpython_mode="off", ir_scaffold_mode="on",
            recursive_stdlib=recursive_stdlib,
        )
    assert captured == [(str(entry), "canary")]
    assert not (directory / "not-produced").exists()


def test_multiline_aliases_and_separate_explicit_imports_are_consumed():
    assert proof.source_has_only_consumed_unsafe_imports(
        "from pcc.unsafe import (gc_backend_current as backend,\n"
        "    getpid as pid)\n"
        "from pcc.unsafe import null\n"
        "def run():\n    return backend() + pid()\n"
        "print(null())\n"
    )


@pytest.mark.parametrize("name", tuple(SIGNATURES))
def test_all_twelve_signatures_have_only_zero_argument_eligibility(name):
    prefix = "from pcc.unsafe import " + name + " as intrinsic\n"
    assert proof.source_has_only_consumed_unsafe_imports(prefix + "intrinsic()\n")
    for args in ("1", "flag=1", "*()", "**{}"):
        assert not proof.source_has_only_consumed_unsafe_imports(
            prefix + "intrinsic(" + args + ")\n"
        )


def test_shared_signature_metadata_matches_existing_zero_arg_contract():
    expected = {
        "pointer": {"null", "frame_address", "initial_environ", "va_start",
                    "darwin_errno_location"},
        "int": {"darwin_current_rss_bytes", "darwin_peak_rss_bytes", "getpid",
                "kqueue_create", "gc_backend_current"},
        "dynamic": {"target_sys_platform", "target_platform_machine"},
        "none": {"thread_safepoint"},
    }
    assert len(SIGNATURES) == 13
    for result, names in expected.items():
        assert {name for name, kind in SIGNATURES.items() if kind == result} == names
    provider = ast.parse((ROOT / "pcc/unsafe/__init__.py").read_text())
    wrappers = {node.name: node for node in provider.body if isinstance(node, ast.FunctionDef)}
    for name in SIGNATURES:
        args = wrappers[name].args
        assert not (args.args or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg)


@pytest.mark.parametrize("tail", [
    "",                                      # unused import
    "value = backend\n",                    # value escape
    "values = [backend]\n",                 # container escape
    "consume(backend)\n",                   # callback escape
    "def f():\n    return backend\n",
    "backend = other\n" + CALL,
    "backend: int = 0\n" + CALL,
    "backend += 1\n" + CALL,
    "del backend\n" + CALL,
    "def backend():\n    pass\n" + CALL,
    "class backend:\n    pass\n" + CALL,
    "def f(backend):\n    return backend()\n",
    "def f(*backend):\n    return backend()\n",
    "def f(**backend):\n    return backend()\n",
    "fn = lambda backend: backend()\n",
    "def f():\n    global backend\n    return backend()\n",
    "def f():\n    nonlocal backend\n    return backend()\n",
    "for backend in values:\n    pass\n" + CALL,
    "with value as backend:\n    pass\n" + CALL,
    "try:\n    pass\nexcept Exception as backend:\n    pass\n" + CALL,
    "items = [backend() for backend in values]\n",
    "print((backend := other))\n" + CALL,
    "def f(value=backend):\n    pass\n" + CALL,
    "def f(value=backend()):\n    pass\n" + CALL,
    "def f(value: backend):\n    pass\n" + CALL,
    "def f() -> backend:\n    pass\n" + CALL,
    "@backend()\ndef f():\n    pass\n" + CALL,
    "class C(backend()):\n    pass\n" + CALL,
    "globals()\n" + CALL,
    "g = globals\n" + CALL,
    "locals()\n" + CALL,
    "vars()\n" + CALL,
    "eval('backend()')\n" + CALL,
    "exec('backend = 1')\n" + CALL,
    "__import__('pcc.unsafe')\n" + CALL,
    "importlib.import_module('pcc.unsafe')\n" + CALL,
    "getattr(value, 'backend')\n" + CALL,
    "help(value)\n" + CALL,
    "breakpoint()\n" + CALL,
    "def f():\n    pass\nf.__globals__\n" + CALL,
    "print(f'{backend()}')\n",               # unknown raw syntax
    "from pcc.unsafe import getpid\n" + CALL, # unused second binding
    IMPORT + CALL,                            # duplicate binding
    "from pcc.unsafe import syscall6\n" + CALL,
    "from pcc.unsafe import unknown\n" + CALL,
    "from pcc.unsafe import *\n" + CALL,
    "from other import *\n" + CALL,
    "import pcc.unsafe\n" + CALL,
    "import pcc.unsafe as unsafe\n" + CALL,
    "from pcc import unsafe\n" + CALL,
    "from .unsafe import gc_backend_current\n" + CALL,
    "from other import value\n" + CALL,
    "from __future__ import annotations\n" + CALL,
    "def f():\n    import helper\n" + CALL,
    "def f():\n    from pcc.unsafe import getpid\n    return getpid()\n" + CALL,
    "if True:\n    from pcc.unsafe import getpid\n    getpid()\n" + CALL,
])
def test_ambiguous_bindings_imports_or_reflection_keep_provider(tail):
    assert not proof.source_has_only_consumed_unsafe_imports(IMPORT + tail)


@pytest.mark.parametrize("source", [
    "print(backend())\n" + IMPORT,
    "def f():\n    return backend()\n" + IMPORT,
    "class C:\n    def f(self):\n        return backend()\n" + IMPORT,
    "if enabled:\n    " + IMPORT + CALL,
    "try:\n    " + IMPORT + "except ImportError:\n    pass\n" + CALL,
    "def f():\n    " + IMPORT + "    return backend()\n",
    "from pcc.unsafe import (gc_backend_current, unknown)\ngc_backend_current()\n",
    "from pcc.unsafe import gc_backend_current\nthis is invalid syntax\n",
])
def test_late_conditional_local_and_mixed_imports_keep_provider(source):
    assert not proof.source_has_only_consumed_unsafe_imports(source)


@pytest.mark.parametrize("attribute", [
    "gi_frame", "cr_frame", "ag_frame", "tb_frame", "f_globals", "f_locals",
    "f_builtins", "f_back", "gi_code", "cr_code", "ag_code", "f_code",
    "co_consts", "co_names", "co_new_field", "f_new_field",
])
def test_frame_reflection_keeps_provider(attribute):
    assert not proof.source_has_only_consumed_unsafe_imports(
        IMPORT + "value." + attribute + "['__builtins__']\n" + CALL
    )


@pytest.mark.parametrize("alias", ["__class__", "__name__", "__builtins__"])
def test_implicit_special_aliases_are_not_proven_unshadowed(alias):
    assert not proof.source_has_only_consumed_unsafe_imports(
        "from pcc.unsafe import gc_backend_current as " + alias + "\n"
        "class C:\n    def probe(self):\n        return " + alias + "()\n"
    )


@pytest.mark.parametrize("body", [
    "def value(self):\n        return __backend",
    "def __backend(self):\n        return 1",
    "__backend = other",
])
def test_mangled_aliases_do_not_hide_value_reads_or_bindings(body):
    assert not proof.source_has_only_consumed_unsafe_imports(
        "from pcc.unsafe import gc_backend_current as _C__backend\n"
        "print(_C__backend())\nclass C:\n    " + body + "\n"
    )


def test_unknown_raw_nodes_and_parser_field_drift_fail_closed():
    from pcc.frontends.python import py_parse

    for name, expected in proof._RAW_FIELDS.items():
        actual = tuple(field.name for field in fields(getattr(py_parse, name))
                       if field.name != "line")
        assert actual == expected, name
    module = py_parse.parse(IMPORT + CALL)
    module.body.append(SimpleNamespace(new_syntax=True))
    assert not proof._only_consumed_unsafe_imports(module)


@pytest.mark.parametrize("route", ["single", "multi_admission"])
@pytest.mark.parametrize("provider_kind", ["copy", "same_name", "symlink"])
def test_only_canonical_owned_provider_can_be_omitted(
    tmp_path, owned_sources, provider_kind, route,
):
    provider = tmp_path / "pcc/unsafe/__init__.py"
    provider.parent.mkdir(parents=True)
    original = Path(owned_sources["pcc.unsafe"])
    if provider_kind == "symlink":
        provider.symlink_to(original)
    elif provider_kind == "copy":
        provider.write_bytes(original.read_bytes())
    else:
        provider.write_text("print('ordinary provider side effect')\n")
    owned_sources["pcc.unsafe"] = str(provider)
    entry = tmp_path / "entry.py"
    entry.write_text(IMPORT + CALL)
    if route == "single":
        paths, names = closure._collect_relative_module_closure(str(entry))
    else:
        paths, names = closure._prepare_multi_source_compile_closure(
            [str(entry)], ["entry"], recursive_stdlib=False,
            ir_scaffold_mode="on",
        )
    assert ("pcc.unsafe" in names) is (provider_kind != "symlink")
    if provider_kind != "symlink":
        assert str(provider) in paths


def test_canonicalization_failure_retains_the_ordinary_provider(tmp_path, owned_sources, monkeypatch):
    def unavailable(path):
        raise OSError("canonicalization unavailable")

    monkeypatch.setattr(closure.os.path, "realpath", unavailable)
    targets = closure._top_level_import_targets(
        str(tmp_path), IMPORT + CALL, top_level_only=False,
        consumed_unsafe_imports=True,
    )
    assert (owned_sources["pcc.unsafe"], "pcc.unsafe") in targets


def test_other_import_retains_unsafe_for_helper_lazy_dependency(tmp_path, owned_sources):
    helper = tmp_path / "helper.py"
    helper.write_text(
        "def run():\n    import importlib\n"
        "    return importlib.import_module('pcc.unsafe')\n"
    )
    owned_sources["helper"] = str(helper)
    entry = tmp_path / "entry.py"
    entry.write_text(IMPORT + "from helper import run\n" + CALL)
    paths, names = closure._collect_relative_module_closure(str(entry))
    assert {"entry", "helper", "pcc.unsafe", "typing"} <= set(names)
    assert str(helper) in paths


def test_multi_source_and_explicit_provider_inputs_are_never_pruned(tmp_path, owned_sources):
    package = tmp_path / "pcc"
    package.mkdir()
    entry = package / "entry.py"
    entry.write_text(IMPORT + CALL)
    helper = package / "helper.py"
    helper.write_text(
        "def lazy():\n    import importlib\n"
        "    return importlib.import_module('pcc.unsafe')\n"
    )
    for paths, names in (
        ([str(entry), str(helper)], ["pcc.entry", "pcc.helper"]),
        ([str(entry), owned_sources["pcc.unsafe"]], ["pcc.entry", "pcc.unsafe"]),
    ):
        actual_paths, actual_names = closure._collect_multi_source_relative_closure(paths, names)
        assert actual_paths[:2] == paths
        assert actual_names[:2] == names
        assert "pcc.unsafe" in actual_names


@pytest.mark.parametrize("module_name", ["entry", "pcc.entry"])
@pytest.mark.parametrize("tail", [CALL, "value = backend\n", "globals()\n" + CALL])
def test_single_root_admission_requires_the_complete_consumed_edge_proof(
    tmp_path, owned_sources, module_name, tail,
):
    entry = tmp_path / "entry.py"
    entry.write_text(IMPORT + tail)
    paths, names = closure._prepare_multi_source_compile_closure(
        [str(entry)], [module_name], recursive_stdlib=False,
        ir_scaffold_mode="on",
    )
    if tail == CALL:
        assert paths == [str(entry)]
        assert names == [module_name]
    else:
        assert {module_name, "pcc.unsafe"} <= set(names)


def test_single_root_admission_never_prunes_a_discovered_provider(
    tmp_path, owned_sources,
):
    helper = tmp_path / "helper.py"
    helper.write_text(IMPORT + CALL)
    owned_sources["helper"] = str(helper)
    entry = tmp_path / "entry.py"
    entry.write_text("import helper\n")
    paths, names = closure._prepare_multi_source_compile_closure(
        [str(entry)], ["entry"], recursive_stdlib=False,
        ir_scaffold_mode="on",
    )
    assert {"entry", "helper", "pcc.unsafe"} <= set(names)
    assert str(helper) in paths


def test_single_package_entry_uses_same_owned_edge_proof(tmp_path, owned_sources):
    package = tmp_path / "pcc"
    package.mkdir()
    (package / "__init__.py").write_text("print('package side effect')\n")
    entry = package / "entry.py"
    entry.write_text(IMPORT + CALL)
    paths, names = closure._collect_relative_module_closure(
        str(entry), include_same_package_absolute=True,
        recurse_same_package_absolute=True,
    )
    assert paths == [str(entry)]
    assert names == ["pcc.entry"]


@pytest.mark.parametrize("name,result", tuple(SIGNATURES.items()))
@pytest.mark.parametrize("freestanding,pointer_lane", [(False, False), (False, True), (True, True)])
def test_shared_signatures_preserve_all_return_projections(name, result, freestanding, pointer_lane):
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen.unsafe_lowering import UNSAFE_INTRINSICS
    from pcc.frontends.python.types import TYPE_DYN, TYPE_I64, TYPE_INT, TYPE_NONE

    assert name in UNSAFE_INTRINSICS
    assert (name in type_infer._POINTER_INTRINSICS) is (result == "pointer")
    expected = TYPE_DYN
    if result == "int":
        expected = TYPE_I64 if freestanding else TYPE_INT
    elif result == "none":
        expected = TYPE_NONE
    elif result == "pointer" and not pointer_lane and not freestanding:
        expected = TYPE_INT
    ctx = SimpleNamespace(freestanding=freestanding, pointer_lane=pointer_lane)
    assert type_infer._unsafe_intrinsic_return_type(ctx, name) == expected


@pytest.mark.parametrize("name", tuple(SIGNATURES))
def test_direct_call_inference_is_equal_with_or_without_wrapper_exports(name):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    source = "from pcc.unsafe import " + name + " as intrinsic\ndef probe():\n    return intrinsic()\n"
    module = parse_and_lift(source, "probe.py", "probe")
    exports = {"pcc.unsafe": {name: {
        "kind": "function", "param_types": (), "return_ty": ("dyn",),
    }}}
    without = infer_module(module)
    with_provider = infer_module(module, external_exports=exports)
    assert without.body[1].body[0].value.ty == with_provider.body[1].body[0].value.ty


def test_canary_codegen_retains_real_backend_query_and_runtime_work(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    entry = tmp_path / "canary.py"
    output = tmp_path / "canary.ll"
    entry.write_text(_canary_source())
    compile_python(str(entry), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    ir = output.read_text()
    verify_ir_text(ir)
    assert "@pcc_gc_backend(" in ir
    assert "@pcc_gc_config_initialized" in ir
    assert "@pcc_gc_backend_selected" in ir
    assert "user_pcc_unsafe_gc_backend_current" not in ir
    assert "@user_canary_run(" in ir
    assert "@user_canary_Box___init__(" in ir
    assert "strict.nolib" not in ir


def test_va_start_keeps_its_variadic_context_diagnostic():
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin
    from pcc.frontends.python.py_lift import parse_and_lift

    module = parse_and_lift("va_start()\n", "probe.py", "probe")
    host = SimpleNamespace(current_function=None, _unsafe_expect_arity=lambda *args: None)
    with pytest.raises(NotImplementedError, match="requires @c_abi_variadic_export"):
        UnsafeIntrinsicMixin._emit_unsafe_va_numeric_f64(host, "va_start", module.body[0].expr)


def test_new_classifier_and_metadata_have_strict_owned_codegen(tmp_path):
    """Host IR shape only: no native compiler/runtime build or execution."""
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from tests.owned_ir_validation import verify_ir_text

    leaves = ("pipeline_unsafe_imports", "unsafe_intrinsic_policy",
              "pipeline_import_scan", "py_parse")
    names = ["pcc.frontends.python." + leaf for leaf in leaves]
    paths = [str(ROOT / "pcc/frontends/python" / (leaf + ".py")) for leaf in leaves]
    modules, exports, _ = build_closed_world_context(paths, names)
    for module in modules[:2]:
        typed = infer_module(module, external_exports=exports)
        codegen = L1CodeGen(typed, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        codegen._skip_program_main = True
        ir = str(codegen.generate(typed))
        (tmp_path / (module.name + ".ll")).write_text(ir)
        verify_ir_text(ir)
        assert "strict.nolib.stub" not in ir
        assert not re.search(r"\bcall [^\n]*@py_cpy_", ir)
        if module.name.endswith("pipeline_unsafe_imports"):
            assert "@user_pcc_frontends_python_py_parse_parse(" in ir
            assert "@user_pcc_frontends_python_pipeline_unsafe_imports__only_consumed_unsafe_imports(" in ir
