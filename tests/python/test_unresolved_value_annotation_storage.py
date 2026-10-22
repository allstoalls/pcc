"""Opaque local annotations cannot erase a concrete imported value payload."""
from __future__ import annotations

import re

import pytest


def _context(tmp_path, annotation="Pair", import_type=False):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    sources = {
        "records": (
            "def valueclass(cls):\n    return cls\n"
            "@valueclass\nclass Pair:\n    first: int\n    second: int\n"
        ),
        "provider": (
            "from records import Pair\n"
            "def make() -> Pair:\n    return Pair(17, 25)\n"
        ),
        "other": (
            "class Pair:\n    first: int\n    second: int\n"
            "class Other:\n    first: int\n    second: int\n"
        ),
        "consumer": (
            "from provider import make\n"
            + ("from other import Pair, Other\n" if import_type else "")
            + "def read() -> int:\n    value: " + annotation
            + " = make()\n    return value.first + value.second\n"
        ),
    }
    paths = []
    for name, source in sources.items():
        path = tmp_path / (name + ".py")
        path.write_text(source)
        paths.append(str(path))
    names = list(sources)
    parsed, exports, derived = build_closed_world_context(paths, names)
    return paths, names, parsed, exports, derived


def _infer(tmp_path, annotation="Pair", import_type=False, mode="memory"):
    from pcc.frontends.python.pipeline_closed_world import _closed_world_module_dependencies
    from pcc.frontends.python.pipeline_exports import (
        _read_native_exports_wire_for_module, _write_native_exports_wire,
    )
    from pcc.frontends.python.type_infer import (
        build_unique_external_class_preload_index, infer_module,
    )

    paths, names, parsed, exports, derived = _context(tmp_path, annotation, import_type)
    preload = None
    if mode == "indexed":
        wire = tmp_path / "exports.indexed"
        _write_native_exports_wire(
            str(wire), exports, derived,
            module_dependencies=dict(_closed_world_module_dependencies(parsed, names, paths, names)),
            unique_class_preload_index=build_unique_external_class_preload_index(exports),
        )
        exports, derived, preload, indexed = _read_native_exports_wire_for_module(str(wire), "consumer")
        assert indexed is True
    return infer_module(
        parsed[-1],
        external_exports={name: info for name, info in exports.items() if name != "consumer"},
        derived_class_map=derived,
        unique_external_class_preload=preload,
    )


@pytest.mark.parametrize("mode", ["memory", "indexed"])
@pytest.mark.parametrize("annotation", ["Pair", "UnknownLocalType"])
def test_unresolved_local_annotation_preserves_concrete_value_storage(tmp_path, mode, annotation):
    from pcc.frontends.python.py_ast import FuncDef
    from pcc.frontends.python.types import type_eq

    typed = _infer(tmp_path, annotation, mode=mode)
    function = next(stmt for stmt in typed.body if isinstance(stmt, FuncDef))
    assignment = function.body[0]
    for ty in (assignment.annotation, assignment.targets[0].ty, assignment.value.ty):
        assert (ty.module, ty.name, ty.valueclass) == ("records", "Pair", True)
        assert [name for name, _ in ty.fields] == ["first", "second"]
    assert type_eq(assignment.annotation, assignment.value.ty)


@pytest.mark.parametrize("mode", ["memory", "indexed"])
@pytest.mark.parametrize("annotation", ["Pair", "Other"])
def test_resolved_annotation_still_rejects_wrong_class(tmp_path, mode, annotation):
    from pcc.frontends.python.types import PyFrontendError

    with pytest.raises(PyFrontendError, match="cannot assign value"):
        _infer(tmp_path, annotation, import_type=True, mode=mode)


@pytest.mark.parametrize("mode", ["memory", "indexed"])
def test_qualified_unresolved_annotation_is_not_rebound_to_value_owner(tmp_path, mode):
    from pcc.frontends.python.types import PyFrontendError

    with pytest.raises(PyFrontendError, match="cannot assign value"):
        _infer(tmp_path, "missing.Pair", mode=mode)


@pytest.mark.parametrize("annotation", ["Pair", "UnknownLocalType"])
def test_unresolved_local_annotation_compiles_direct_aggregate_storage(tmp_path, annotation):
    from pcc.frontends.python.pipeline_context import compile_contextual_per_module_fallback_counts

    paths, names, _, _, _ = _context(tmp_path, annotation)
    result = compile_contextual_per_module_fallback_counts(
        paths, names, ["consumer"], ir_scaffold_mode="on", strict_no_libpython=True,
        emit_ir_dir=str(tmp_path),
    )
    assert result == {"consumer": 0}
    ir = (tmp_path / "consumer.ll").read_text()
    assert "call { i64, i64 } () @user_provider_make(" in ir
    # The local keeps the aggregate: direct storage and field reads, never a
    # dynamic object attribute lookup.
    read = re.search(r"^define[^\n]*@user_consumer_read\(.*?^}", ir, re.M | re.S).group(0)
    assert "%value.addr.2 = alloca { i64, i64 }" in read
    assert re.search(r"getelementptr inbounds \{ i64, i64 \}, ptr %value\.addr\.2, i32 0, i32 0", read)
    assert re.search(r"getelementptr inbounds \{ i64, i64 \}, ptr %value\.addr\.2, i32 0, i32 1", read)
    assert "@py_obj_getattr" not in read
