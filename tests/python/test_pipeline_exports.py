"""Focused contracts for closed-world export metadata extraction."""
from __future__ import annotations

import json

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_exports
from pcc.frontends.python import type_infer
from pcc.frontends.python.export_meta import encode_type


def test_pipeline_export_metadata_facade_is_thin():
    for name in (
        "_export_param_types",
        "_export_returns_none",
        "_closed_world_is_node",
        "_normalise_export_annotation_text",
        "_export_call_sig",
        "_export_default_to_wire",
        "_export_default_from_wire",
        "_write_native_exports_wire",
        "_read_native_exports_wire",
        "_read_native_exports_wire_for_module",
        "_export_method_symbol",
    ):
        assert getattr(pipeline, name) is getattr(pipeline_exports, name)


def test_nested_annotation_text_normalises_without_driver_state():
    annotation = pipeline_exports._normalise_export_annotation_text(
        "dict[str, list[tuple[int, bytes]]]"
    )
    assert encode_type(annotation) == (
        "dict",
        ("str",),
        ("list", ("tuple", (("int", 64, True), ("bytes",)))),
    )


def test_export_default_wire_roundtrip_preserves_nested_safe_values():
    key = pipeline_exports._EXPORT_DEFAULT_WIRE_KEY
    wire = {
        key: "dict",
        "pairs": [
            (
                {key: "str", "value": "threshold"},
                {
                    key: "tuple",
                    "elems": [
                        {key: "int", "value": -21},
                        {key: "bytes", "value": [0, 127, 255]},
                    ],
                },
            )
        ],
    }
    value = pipeline_exports._export_default_from_wire(wire)
    assert pipeline_exports._export_default_to_wire(value) == wire
    assert pipeline_exports._export_default_wire_is_safe(wire)


def test_native_export_manifest_roundtrip_is_deterministic(tmp_path):
    exports = {
        "pkg.mod": {
            "f": {
                "kind": "function",
                "param_types": (("int",),),
                "return_ty": ("str",),
                "call_sig": (),
            }
        }
    }
    derived = {"pkg.Base": ("pkg.Child",)}
    uses = (("pkg.mod", "f"),)
    path = tmp_path / "native-exports.json"
    pipeline_exports._write_native_exports_wire(path, exports, derived, uses)
    assert pipeline_exports._read_native_exports_wire(
        path, include_function_object_uses=True
    ) == (exports, derived, uses)


@pytest.mark.parametrize("indexed", (False, True))
@pytest.mark.parametrize("reader", ("full", "full_with_uses", "module"))
def test_native_export_readers_read_wire_once(tmp_path, monkeypatch, indexed, reader):
    exports = {"root": {}, "dep": {}}
    derived = {"Base": ("dep", "Child")}
    uses = (("dep", "f"),)
    path = tmp_path / "native-exports.wire"
    options = {}
    if indexed:
        options = {
            "module_dependencies": {"root": ("dep",)},
            "unique_class_preload_index": (
                type_infer.build_unique_external_class_preload_index(exports)
            ),
        }
    pipeline_exports._write_native_exports_wire(path, exports, derived, uses, **options)
    events = []
    original_open = open

    class TrackedReader:
        def __init__(self, *args, **kwargs):
            events.append(("open", args, kwargs))
            self.stream = original_open(*args, **kwargs)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self):
            events.append(("read",))
            return self.stream.read()

    monkeypatch.setattr(pipeline_exports, "open", TrackedReader, raising=False)
    if reader == "module":
        actual = pipeline_exports._read_native_exports_wire_for_module(path, "root")
        preload = (
            {"types": (), "base_keys": (), "drop_keys": (), "set_keys": ()}
            if indexed
            else {"types": (), "keys": (), "dependencies": ()}
        )
        assert actual == (exports, derived, preload, indexed)
    else:
        include_uses = reader == "full_with_uses"
        actual = pipeline_exports._read_native_exports_wire(path, include_uses)
        assert actual == (
            (exports, derived, uses) if include_uses else (exports, derived)
        )
    assert events == [
        ("open", (path, "r"), {"encoding": "utf-8"}),
        ("read",),
    ]


@pytest.mark.parametrize("indexed", (False, True))
@pytest.mark.parametrize("include_uses", (False, True))
def test_native_export_text_decoder_matches_reader_without_file_access(
    tmp_path, monkeypatch, indexed, include_uses
):
    default_wire = {
        pipeline_exports._EXPORT_DEFAULT_WIRE_KEY: "int",
        "value": 1 << 100,
    }
    default = pipeline_exports._export_default_from_wire(default_wire)
    exports = {
        "pkg.mod": {
            "f": {
                "kind": "function",
                "param_types": (("int", 64, True),),
                "return_ty": ("str",),
                "call_sig": (
                    {
                        "name": "value",
                        "kind": "pos_or_kw",
                        "annotation": ("int", 64, True),
                        "has_default": True,
                        "default": default,
                    },
                ),
            }
        }
    }
    derived = {"Base": ("pkg.mod", "Child")}
    uses = (("pkg.mod", "f"),)
    path = tmp_path / "native-exports.wire"
    options = {}
    if indexed:
        options = {
            "module_dependencies": {},
            "unique_class_preload_index": (
                type_infer.build_unique_external_class_preload_index(exports)
            ),
        }
    pipeline_exports._write_native_exports_wire(path, exports, derived, uses, **options)
    expected = pipeline_exports._read_native_exports_wire(path, include_uses)
    text = path.read_text(encoding="utf-8")

    def fail_open(*args, **kwargs):
        raise AssertionError("text decoder attempted file access")

    monkeypatch.setattr(pipeline_exports, "open", fail_open, raising=False)
    actual = pipeline_exports._decode_native_exports_wire(text, include_uses)
    assert actual == expected
    assert actual == ((exports, derived, uses) if include_uses else (exports, derived))
    assert (
        pipeline_exports._export_default_to_wire(
            actual[0]["pkg.mod"]["f"]["call_sig"][0]["default"]
        )
        == default_wire
    )


@pytest.mark.parametrize("root", ("root", "other", "absent"))
def test_legacy_module_reader_preserves_root_scoped_class_preload(tmp_path, root):
    def class_info(name, owner):
        return {
            "kind": "class",
            "class_name": name,
            "owning_module": owner,
            "base_names": (),
            "field_names": (),
            "field_types": (),
        }

    exports = {
        "root": {
            "LocalOnly": class_info("LocalOnly", "root"),
            "Shared": class_info("Shared", "root"),
        },
        "other": {"Shared": class_info("Shared", "other")},
        "unique": {"Solo": class_info("Solo", "unique")},
    }
    derived = {"Base": ("unique", "Solo")}
    path = tmp_path / "native-exports.json"
    pipeline_exports._write_native_exports_wire(path, exports, derived)
    expected = type_infer.build_unique_external_class_preload(
        {name: value for name, value in exports.items() if name != root}
    )
    actual = pipeline_exports._read_native_exports_wire_for_module(path, root)
    assert actual == (exports, derived, expected, False)
    keys = {name for name, _type_id in actual[2]["keys"]}
    assert "Solo" in keys
    assert ("LocalOnly" in keys) is (root != "root")
    assert ("Shared" in keys) is (root != "absent")


@pytest.mark.parametrize("reader", ("full", "full_with_uses", "module"))
@pytest.mark.parametrize(
    "text, error_type, message",
    (
        ("{", json.JSONDecodeError, "Expecting property name"),
        ("{}", ValueError, "invalid frontend native exports file"),
        ('{"schema": "wrong"}', ValueError, "invalid frontend native exports file"),
        ("[]", AttributeError, "has no attribute 'get'"),
        (
            pipeline_exports._NATIVE_EXPORT_INDEXED_SCHEMA + "\nmalformed\n",
            ValueError,
            "invalid indexed frontend native export row",
        ),
    ),
)
def test_native_export_readers_preserve_invalid_wire_errors(
    tmp_path, text, error_type, message, reader
):
    path = tmp_path / "native-exports.wire"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(error_type, match=message):
        if reader == "module":
            pipeline_exports._read_native_exports_wire_for_module(path, "root")
        else:
            pipeline_exports._read_native_exports_wire(path, reader == "full_with_uses")


@pytest.mark.parametrize("reader", ("full", "module"))
def test_native_export_readers_preserve_file_errors(tmp_path, reader):
    path = tmp_path / "native-exports.wire"

    def read_wire():
        if reader == "module":
            return pipeline_exports._read_native_exports_wire_for_module(path, "root")
        return pipeline_exports._read_native_exports_wire(path)

    with pytest.raises(FileNotFoundError):
        read_wire()
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        read_wire()


def test_legacy_export_optional_fields_remain_optional(tmp_path):
    path = tmp_path / "native-exports.json"
    path.write_text(
        json.dumps({"schema": pipeline_exports._NATIVE_EXPORT_WIRE_SCHEMA}),
        encoding="utf-8",
    )
    assert pipeline_exports._read_native_exports_wire(path) == ({}, {})
    assert pipeline_exports._read_native_exports_wire(path, True) == ({}, {}, ())
    assert pipeline_exports._read_native_exports_wire_for_module(path, "root") == (
        {},
        {},
        {"types": (), "keys": (), "dependencies": ()},
        False,
    )


def test_indexed_native_export_wire_materializes_only_the_required_closure(
    tmp_path,
):
    function = {
        "kind": "function",
        "param_types": (),
        "return_ty": ("dyn",),
        "call_sig": (),
    }
    exports = {
        "root": {
            "entry": dict(function),
            "remote": {
                "kind": "module_global",
                "owning_module": "metadata.owner",
                "export_name": "remote",
                "value_ty": ("dyn",),
            },
        },
        "dep": {"dep": dict(function)},
        "leaf": {"leaf": dict(function)},
        "metadata.owner": {"remote": dict(function)},
        "unique.owner": {
            "Solo": {
                "kind": "class",
                "class_name": "Solo",
                "owning_module": "unique.owner",
                "base_names": (),
                "field_names": (),
                "field_types": (),
            }
        },
        "dup.a": {
            "Shared": {
                "kind": "class",
                "class_name": "Shared",
                "owning_module": "dup.a",
                "base_names": (),
                "field_names": (),
                "field_types": (),
            }
        },
        "dup.b": {
            "Shared": {
                "kind": "class",
                "class_name": "Shared",
                "owning_module": "dup.b",
                "base_names": (),
                "field_names": (),
                "field_types": (),
            }
        },
        "unrelated": {"unused": dict(function)},
    }
    dependencies = {
        "root": ("dep",),
        "dep": ("leaf",),
    }
    path = tmp_path / "native-exports.indexed"
    pipeline_exports._write_native_exports_wire(
        path,
        exports,
        {},
        module_dependencies=dependencies,
        unique_class_preload_index=(
            type_infer.build_unique_external_class_preload_index(exports)
        ),
    )

    full, _derived = pipeline_exports._read_native_exports_wire(path)
    assert full == exports
    assert pipeline_exports._read_native_exports_wire_raw_modules(path) == (
        pipeline_exports._native_export_to_wire(exports)
    )
    selected, _derived, unique_preload, indexed = (
        pipeline_exports._read_native_exports_wire_for_module(path, "root")
    )

    assert indexed is True
    assert tuple(selected) == (
        "root",
        "dep",
        "leaf",
        "metadata.owner",
    )
    assert selected == {name: exports[name] for name in selected}
    expected_preload = type_infer.build_unique_external_class_preload(
        {name: value for name, value in exports.items() if name != "root"}
    )
    dropped = set(unique_preload["drop_keys"])
    actual_by_key = {
        key: unique_preload["types"][type_id]
        for key, type_id in unique_preload["base_keys"]
        if key not in dropped
    }
    for key, type_id in unique_preload["set_keys"]:
        actual_by_key[key] = unique_preload["types"][type_id]
    expected_by_key = {
        key: expected_preload["types"][type_id]
        for key, type_id in expected_preload["keys"]
    }
    assert actual_by_key == expected_by_key
    assert "Solo" in actual_by_key
    assert "unrelated" not in selected
    assert "unique.owner" not in selected
    assert "dup.a" not in selected
    assert "dup.b" not in selected


def test_indexed_native_export_wire_rejects_stale_dependency(tmp_path):
    path = tmp_path / "native-exports.indexed"
    pipeline_exports._write_native_exports_wire(
        path,
        {"root": {}},
        {},
        module_dependencies={"root": ()},
        unique_class_preload_index=(
            type_infer.build_unique_external_class_preload_index({"root": {}})
        ),
    )
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('P\t{"root": []}', 'P\t{"root": ["missing"]}'),
        encoding="utf-8",
    )

    try:
        pipeline_exports._read_native_exports_wire_for_module(path, "root")
    except Exception as exc:
        assert "dependency" in str(exc)
    else:
        raise AssertionError("stale indexed dependency was accepted")


def test_indexed_contextual_host_surface_is_lazy_and_root_scoped(tmp_path):
    class_info = {
        "kind": "class",
        "class_name": "Host",
        "owning_module": "host.module",
        "base_names": (),
        "field_names": (),
        "field_types": (),
    }
    exports = {
        "ordinary": {},
        "contextual": {},
        "host.module": {"Host": class_info, "unused": {"kind": "constant"}},
    }
    path = tmp_path / "native-exports.indexed"
    pipeline_exports._write_native_exports_wire(
        path,
        exports,
        {},
        module_dependencies={name: () for name in exports},
        unique_class_preload_index=(
            type_infer.build_unique_external_class_preload_index(exports)
        ),
        contextual_modules=("contextual",),
        contextual_host_exports={"host.module": {"Host": class_info}},
    )

    ordinary, _derived, _preload, indexed = (
        pipeline_exports._read_native_exports_wire_for_module(path, "ordinary")
    )
    assert indexed is True
    assert tuple(ordinary) == ("ordinary",)

    contextual, _derived, _preload, indexed = (
        pipeline_exports._read_native_exports_wire_for_module(path, "contextual")
    )
    assert indexed is True
    assert tuple(contextual) == ("contextual", "host.module")
    assert contextual["host.module"] == {"Host": class_info}
