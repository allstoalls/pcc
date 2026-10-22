"""Imported annotation spellings retain the defining class's exact identity."""
from __future__ import annotations

import pytest


def _context(tmp_path, consumer, *, aliases=False, provider=None):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    sources = {
        "pkg.model": "class Module:\n    token: int\n",
        "pkg.other": "class Module:\n    token: int\n",
        "provider": (
            "from pkg.model import Module\n"
            "def provide() -> Module:\n    return Module()\n"
        ),
    }
    if provider is not None:
        sources["provider"] = provider
    if aliases:
        sources["facade"] = (
            "from pkg.model import Module as Payload\n"
            "from provider import provide as make\n"
        )
    sources["pkg.consumer"] = consumer
    paths = []
    for name, text in sources.items():
        path = tmp_path / (name.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        paths.append(str(path))
    names = list(sources)
    parsed, exports, derived = build_closed_world_context(paths, names)
    return paths, names, parsed, exports, derived


def _infer_consumer(tmp_path, consumer, *, mode="memory", aliases=False, provider=None):
    from pcc.frontends.python.pipeline_closed_world import _closed_world_module_dependencies
    from pcc.frontends.python.pipeline_exports import (
        _read_native_exports_wire_for_module, _write_native_exports_wire,
    )
    from pcc.frontends.python.type_infer import (
        build_unique_external_class_preload_index, infer_module,
    )

    paths, names, parsed, exports, derived = _context(tmp_path, consumer, aliases=aliases, provider=provider)
    preload = None
    if mode == "indexed":
        wire = tmp_path / "exports.indexed"
        _write_native_exports_wire(
            str(wire), exports, derived,
            module_dependencies=dict(_closed_world_module_dependencies(parsed, names, paths, names)),
            unique_class_preload_index=build_unique_external_class_preload_index(exports),
        )
        exports, derived, preload, indexed = _read_native_exports_wire_for_module(
            str(wire), "pkg.consumer",
        )
        assert indexed is True
    return infer_module(
        parsed[-1],
        external_exports={name: info for name, info in exports.items() if name != "pkg.consumer"},
        derived_class_map=derived,
        unique_external_class_preload=preload,
    )


@pytest.mark.parametrize("mode", ["memory", "indexed"])
@pytest.mark.parametrize("import_line, annotation", [
    ("from pkg import model as pa", "pa.Module"),
    ("from . import model as pa", "pa.Module"),
    ("import pkg.model as pa", "pa.Module"),
    ("import pkg.model", "pkg.model.Module"),
])
def test_submodule_annotation_uses_exact_owner_without_package_exports(
    tmp_path, mode, import_line, annotation,
):
    from pcc.frontends.python.py_ast import FuncDef

    typed = _infer_consumer(tmp_path, (
        import_line + "\nfrom provider import provide\n"
        "class Module:\n    token: int\n"
        "def relay() -> " + annotation + ":\n    return provide()\n"
    ), mode=mode)
    relay = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    assert (relay.return_ty.module, relay.return_ty.name) == ("pkg.model", "Module")
    assert (relay.body[0].value.ty.module, relay.body[0].value.ty.name) == ("pkg.model", "Module")


@pytest.mark.parametrize("mode", ["memory", "indexed"])
def test_qualified_reexport_alias_preserves_original_class_name(tmp_path, mode):
    from pcc.frontends.python.py_ast import FuncDef

    typed = _infer_consumer(tmp_path, (
        "import facade as pa\nfrom facade import make\n"
        "def relay() -> pa.Payload:\n    return make()\n"
        "def construct() -> pa.Payload:\n    return pa.Payload()\n"
    ), mode=mode, aliases=True)
    for function in typed.body:
        if isinstance(function, FuncDef):
            assert (function.return_ty.module, function.return_ty.name) == ("pkg.model", "Module")
            assert (function.body[0].value.ty.module, function.body[0].value.ty.name) == ("pkg.model", "Module")


@pytest.mark.parametrize("mode", ["memory", "indexed"])
def test_distinct_same_name_class_cannot_satisfy_qualified_annotation(tmp_path, mode):
    from pcc.frontends.python.types import PyFrontendError

    with pytest.raises(PyFrontendError, match="return type mismatch"):
        _infer_consumer(tmp_path, (
            "from pkg import model as pa\nfrom pkg.other import Module\n"
            "def wrong() -> pa.Module:\n    return Module()\n"
        ), mode=mode)


def test_unresolved_qualified_annotation_does_not_use_local_leaf(tmp_path):
    from pcc.frontends.python.types import PyFrontendError

    with pytest.raises(PyFrontendError, match="return type mismatch"):
        _infer_consumer(tmp_path, (
            "class Module:\n    token: int\n"
            "def wrong() -> missing.Module:\n    return Module()\n"
        ))


@pytest.mark.parametrize("mode", ["memory", "indexed"])
def test_exported_qualified_return_uses_provider_import_binding(tmp_path, mode):
    from pcc.frontends.python.py_ast import FuncDef

    typed = _infer_consumer(tmp_path, (
        "from pkg import model as model\nfrom provider import provide\n"
        "def relay() -> model.Module:\n    return provide()\n"
    ), mode=mode, provider=(
        "from pkg import model as origin\n"
        "def provide() -> origin.Module:\n    return origin.Module()\n"
    ))
    relay = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    assert (relay.body[0].value.ty.module, relay.body[0].value.ty.name) == ("pkg.model", "Module")


@pytest.mark.parametrize("mode", ["memory", "indexed"])
def test_consumer_alias_cannot_hijack_exported_return(tmp_path, mode):
    from pcc.frontends.python.types import PyFrontendError

    with pytest.raises(PyFrontendError, match="return type mismatch"):
        _infer_consumer(tmp_path, (
            "from pkg import other as origin\nfrom provider import provide\n"
            "def wrong() -> origin.Module:\n    return provide()\n"
        ), mode=mode, provider=(
            "from pkg import model as origin\n"
            "def provide() -> origin.Module:\n    return origin.Module()\n"
        ))


def test_export_descriptors_preserve_provider_alias_in_nested_surfaces(tmp_path):
    paths, names, parsed, exports, derived = _context(tmp_path, "", provider=(
        "from pkg import model as origin\n"
        "class Holder:\n"
        "    def __init__(self, items: list[origin.Module]):\n"
        "        self.items = items\n"
        "    def relay(self, value: origin.Module) -> origin.Module:\n"
        "        return value\n"
        "def relay(value: list[origin.Module]) -> list[origin.Module]:\n"
        "    return value\n"
    ))
    expected = ("class", "Module", "pkg.model", (), ())
    relay = exports["provider"]["relay"]
    assert relay["return_ty"] == ("list", expected)
    assert relay["param_types"] == (("list", expected),)
    assert relay["call_sig"][0]["annotation"] == ("list", expected)
    holder = exports["provider"]["Holder"]
    assert holder["field_types"] == (("items", ("list", expected)),)
    method = next(info for info in holder["methods"] if info["name"] == "relay")
    assert method["return_ty"] == expected
    assert method["param_types"][1] == expected
    assert method["call_sig"][1]["annotation"] == expected


@pytest.mark.parametrize("shadow", ["origin = None", "class origin:\n    pass"])
def test_shadowed_module_binding_does_not_qualify_export_descriptor(tmp_path, shadow):
    paths, names, parsed, exports, derived = _context(tmp_path, "", provider=(
        "from pkg import model as origin\n" + shadow + "\n"
        "def relay(value: origin.Module) -> origin.Module:\n    return value\n"
    ))
    assert exports["provider"]["relay"]["return_ty"][2] == "origin"


@pytest.mark.parametrize("shadow", ["pa = None", "class pa:\n    pass"])
def test_local_shadow_cannot_reuse_cached_module_annotation(tmp_path, shadow):
    from pcc.frontends.python.types import PyFrontendError

    with pytest.raises(PyFrontendError, match="return type mismatch"):
        _infer_consumer(tmp_path, (
            "from pkg import model as pa\nfrom provider import provide\n"
            + shadow + "\ndef wrong() -> pa.Module:\n    return provide()\n"
        ))


def test_worker_shard_and_monolithic_export_annotations_agree(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    paths, names, parsed, exports, derived = _context(tmp_path, "", provider=(
        "from pkg import model as origin\n"
        "def relay(value: origin.Module) -> origin.Module:\n    return value\n"
    ))
    index = names.index("provider")
    shard_parsed, shard_exports, shard_derived = build_closed_world_context(
        [paths[index]], [names[index]], merge_exports=False,
    )
    assert shard_exports["provider"]["relay"] == exports["provider"]["relay"]


@pytest.mark.parametrize("imported", ["ir", "ir_py", "ir_c", "ir_passes"])
def test_owned_ir_facade_annotations_keep_verified_provider(tmp_path, imported):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.py_ast import FuncDef

    sources = {
        "pcc.ir.ir": "class Value:\n    pass\n",
        "provider": (
            "from pcc.ir.compat import " + imported + " as scaffold\n"
            "def provide() -> scaffold.Value:\n    return scaffold.Value()\n"
        ),
        "consumer": (
            "from pcc.ir.ir import Value\nfrom provider import provide\n"
            "def relay() -> Value:\n    return provide()\n"
        ),
    }
    paths = []
    for name, text in sources.items():
        path = tmp_path / (name + ".py")
        path.write_text(text)
        paths.append(str(path))
    names = list(sources)
    parsed, exports, derived = build_closed_world_context(paths, names)
    expected = ("class", "Value", "pcc.ir.ir", (), ())
    assert exports["provider"]["provide"]["return_ty"] == expected
    shard_parsed, shard_exports, shard_derived = build_closed_world_context(
        [paths[1]], ["provider"], merge_exports=False,
    )
    assert shard_exports["provider"]["provide"]["return_ty"] == expected
    typed = infer_module(parsed[-1], external_exports={name: info for name, info in exports.items() if name != "consumer"})
    relay = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    assert (relay.body[0].value.ty.module, relay.body[0].value.ty.name) == ("pcc.ir.ir", "Value")


def test_unrelated_ir_alias_does_not_select_owned_scaffold(tmp_path):
    paths, names, parsed, exports, derived = _context(tmp_path, "", provider=(
        "from unrelated.compat import ir as scaffold\n"
        "def relay(value: scaffold.Value) -> scaffold.Value:\n    return value\n"
    ))
    assert exports["provider"]["relay"]["return_ty"][2] == "unrelated.compat.ir"


@pytest.mark.parametrize("prefix", [
    "",
    "from pkg import model as origin\norigin = None\n",
    "from pkg import model as origin\nfrom missing import *\n",
])
def test_unresolved_provider_prefix_never_uses_consumer_alias(tmp_path, prefix):
    from pcc.frontends.python.types import PyFrontendError

    with pytest.raises(PyFrontendError, match="return type mismatch"):
        _infer_consumer(tmp_path, (
            "from pkg import model as origin\nfrom provider import provide\n"
            "def wrong() -> origin.Module:\n    return provide()\n"
        ), provider=prefix + "def provide() -> origin.Module:\n    return None\n")
