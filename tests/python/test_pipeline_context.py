"""Focused contracts for the extracted closed-world context seam."""

from __future__ import annotations

import pytest


def test_contextual_failure_preserves_module_and_cause(tmp_path, monkeypatch, capsys):
    from pcc.frontends.python import pipeline_context, type_infer

    source = tmp_path / "broken.py"
    source.write_text("value = 1\n")

    def fail(*args, **kwargs):
        raise ValueError("injected context failure")

    monkeypatch.setattr(type_infer, "infer_module", fail)
    counts = pipeline_context.compile_contextual_per_module_fallback_counts(
        [str(source)], ["broken"], ["broken"],
        ir_scaffold_mode="on", emit_ir_dir=str(tmp_path),
    )
    assert counts == {"broken": -1}
    assert "broken: ValueError: injected context failure" in capsys.readouterr().err
    assert "ValueError: injected context failure" in (tmp_path / "broken.error.txt").read_text()


def test_pipeline_context_facade_has_single_function_owners():
    from pcc.frontends.python import pipeline
    from pcc.frontends.python import pipeline_context as context

    for name in (
        "build_closed_world_context",
        "_closed_world_derived_class_map",
        "_merge_l1_mixin_stack_methods",
        "_merge_l1_codegen_methods",
        "_contextual_host_export_surface",
        "_contextual_host_params_for_module",
        "count_py_cpy_fallback_calls",
        "_copy_native_module_exports",
        "_module_uses_default_native_exports",
        "compile_contextual_per_module_fallback_counts",
    ):
        assert getattr(pipeline, name) is getattr(context, name)


def test_contextual_host_export_surface_contains_only_schema_owners():
    from pcc.frontends.python.pipeline_context import _contextual_host_export_surface

    host_info = {"kind": "class", "class_name": "L1CodeGen"}
    class_info = {"kind": "class", "class_name": "ClassInfo"}
    lowering_info = {"kind": "class", "class_name": "ClassLowering"}
    exports = {
        "pcc.frontends.python.codegen.layer1": {
            "L1CodeGen": host_info,
            "unrelated": {"kind": "function"},
        },
        "pcc.frontends.python.codegen.class_gen": {
            "ClassInfo": class_info,
            "ClassLowering": lowering_info,
            "unrelated": {"kind": "function"},
        },
    }
    assert _contextual_host_export_surface(exports) == {
        "pcc.frontends.python.codegen.layer1": {"L1CodeGen": host_info},
        "pcc.frontends.python.codegen.class_gen": {
            "ClassInfo": class_info,
            "ClassLowering": lowering_info,
        },
    }


def test_context_fallback_counter_counts_calls_not_declarations():
    from pcc.frontends.python.pipeline_context import count_py_cpy_fallback_calls

    ir = """\
declare ptr @py_cpy_import(ptr)
%one = call ptr @py_cpy_import(ptr %name)
%two = call i1 @py_cpy_truthy(ptr %one)
"""
    assert count_py_cpy_fallback_calls(ir) == 2


def test_closed_world_derived_map_only_selects_unique_owners():
    from pcc.frontends.python.pipeline_context import _closed_world_derived_class_map

    exports = {
        "left": {
            "Only": {"kind": "class", "base_names": ("Unique",)},
            "First": {"kind": "class", "base_names": ("Shared",)},
        },
        "right": {
            "Second": {"kind": "class", "base_names": ("Shared",)},
        },
    }

    assert _closed_world_derived_class_map(exports) == {
        "Unique": ("left", "Only")
    }


def test_computed_raw_int_module_global_exports_provider_storage_abi(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    src = tmp_path / "provider.py"
    src.write_text("FLAG = 1 << 1\n", encoding="utf-8")

    _modules, exports, _derived = build_closed_world_context(
        [str(src)],
        ["pcc.provider"],
        merge_exports=False,
    )

    exported = exports["pcc.provider"]["FLAG"]
    assert exported["kind"] == "module_global"
    assert exported["value_ty"][0] == "int"
    assert exported["box_int_abi"] is False


def test_unpacked_init_fields_keep_export_and_type_schema_order(tmp_path):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.py_ast import ClassType
    from pcc.frontends.python.type_infer import infer_module

    src = tmp_path / "provider.py"
    src.write_text(
        "class Record:\n"
        "    def __init__(self, name: str):\n"
        "        self.name = name\n"
        "        self.left, self.right = (1, 2)\n"
        "        self.size = 3\n"
        "\n"
        "def read(record: Record):\n"
        "    return record.size\n",
        encoding="utf-8",
    )

    _modules, exports, _derived = build_closed_world_context(
        [str(src)],
        ["provider"],
        merge_exports=False,
    )
    assert exports["provider"]["Record"]["field_names"] == (
        "name",
        "left",
        "right",
        "size",
    )

    typed = infer_module(
        parse_and_lift(src.read_text(encoding="utf-8"), str(src), "provider")
    )
    read_fn = typed.body[1]
    record_ty = read_fn.args[0].annotation
    assert isinstance(record_ty, ClassType)
    assert tuple(name for name, _ty in record_ty.fields) == (
        "name",
        "left",
        "right",
        "size",
    )


def test_cleanup_method_does_not_replace_declared_field_type(tmp_path):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import ListType
    from pcc.frontends.python.type_infer import infer_module

    source = """
class Record:
    values: list[int]
    def __init__(self):
        try:
            self.values: list[int] = []
        except Exception:
            raise
    def close(self):
        self.values = ()

def read(record: Record):
    return record.values
""".lstrip()
    typed = infer_module(parse_and_lift(source, str(tmp_path / "owner.py"), "owner"))
    fields = dict(typed.body[-1].args[0].annotation.fields)
    assert isinstance(fields["values"], ListType)


def test_dataclass_unannotated_class_constant_is_not_an_exported_field(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    src = tmp_path / "provider.py"
    src.write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "@dataclass\n"
        "class Record:\n"
        "    marker = 7\n"
        "    value: int\n",
        encoding="utf-8",
    )

    _modules, exports, _derived = build_closed_world_context(
        [str(src)],
        ["provider"],
        merge_exports=False,
    )
    record = exports["provider"]["Record"]
    assert record["field_names"] == ("value",)
    init = next(method for method in record["methods"] if method["name"] == "__init__")
    assert tuple(arg["name"] for arg in init["call_sig"]) == ("self", "value")


def test_constructor_field_type_wins_over_earlier_method_write(tmp_path):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.export_meta import encode_type
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module

    source = """
class Record:
    def close(self, replacement: tuple[int]):
        self.before = 0
        self.values = replacement
    def __init__(self, values: list[int]):
        self.values = values
        self.after = 1

def read(record: Record):
    return record.values
""".lstrip()
    path = tmp_path / "provider.py"
    path.write_text(source, encoding="utf-8")
    _, exports, _ = build_closed_world_context([str(path)], ["provider"])
    typed = infer_module(parse_and_lift(source, str(path), "provider"))
    inferred_fields = dict(typed.body[-1].args[0].annotation.fields)
    record = exports["provider"]["Record"]
    assert record["field_names"] == ("before", "values", "after")
    assert dict(record["field_types"])["values"] == encode_type(inferred_fields["values"])
    assert dict(record["field_types"])["values"][0] == "list"


def test_declared_instance_field_type_wins_over_method_write(tmp_path):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.export_meta import encode_type
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.type_infer import infer_module

    source = """
class Record:
    values: list[int]
    class_only: str
    def close(self, replacement: tuple[int]):
        self.values = replacement

def read(record: Record):
    return record.values
""".lstrip()
    path = tmp_path / "provider.py"
    path.write_text(source, encoding="utf-8")
    _, exports, _ = build_closed_world_context([str(path)], ["provider"])
    typed = infer_module(parse_and_lift(source, str(path), "provider"))
    inferred_fields = dict(typed.body[-1].args[0].annotation.fields)
    record = exports["provider"]["Record"]
    assert record["field_names"] == ("values",)
    assert dict(record["field_types"])["values"] == encode_type(inferred_fields["values"])
    assert dict(record["field_types"])["values"][0] == "list"


def test_untyped_constructor_write_does_not_export_cleanup_method_type(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    constructor = "    def __init__(self):\n        self.values = []\n"
    cleanup = (
        "    def close(self, replacement: tuple[int]):\n"
        "        self.values = replacement\n"
    )
    for methods in (cleanup + constructor, constructor + cleanup):
        path = tmp_path / "provider.py"
        path.write_text("class Record:\n" + methods, encoding="utf-8")
        _, exports, _ = build_closed_world_context([str(path)], ["provider"])
        record = exports["provider"]["Record"]
        assert record["field_names"] == ("values",)
        # The export pass does not infer a literal constructor RHS. Preserve
        # that unknown type instead of borrowing the cleanup method's tuple.
        assert dict(record["field_types"]).get("values", ("dyn",)) == ("dyn",)


def test_dataclass_method_field_does_not_add_constructor_parameter(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    source = """
from dataclasses import dataclass

@dataclass
class Record:
    value: int
    def prepare(self, text: str):
        self.cache = text

@dataclass
class Child(Record):
    other: str
""".lstrip()
    path = tmp_path / "provider.py"
    path.write_text(source, encoding="utf-8")
    _, exports, _ = build_closed_world_context([str(path)], ["provider"])
    for name, expected in (
        ("Record", ("self", "value")),
        ("Child", ("self", "value", "other")),
    ):
        record = exports["provider"][name]
        assert "cache" in record["field_names"]
        assert dict(record["field_types"])["cache"] == ("str",)
        init = next(method for method in record["methods"] if method["name"] == "__init__")
        assert tuple(arg["name"] for arg in init["call_sig"]) == expected


def _constructor_field_projection_source(initializer):
    return """
def valueclass(cls):
    return cls

@valueclass
class Pair:
    first: int
    second: int

class Arena:
    def get_pair(self, index: int) -> Pair:
        return Pair(index, index + 1)

class Seed:
    def __init__(self):
        self.arena = Arena()

class Holder:
    arena: Arena
    def __init__(self, seed: Seed, flag: bool):
""".lstrip() + initializer + """
        self.other = Arena()
        self.other = seed.arena
        self.unknown = seed.arena

    def get_pair(self, index: int) -> Pair:
        return self.arena.get_pair(index)

def projected(holder: Holder):
    return holder.arena
"""


_CONSTRUCTOR_FIELD_INITIALIZERS = (
    "        self.arena = Arena() if flag else seed.arena\n",
    "        if flag:\n"
    "            self.arena = Arena()\n"
    "        else:\n"
    "            self.arena = seed.arena\n",
)


@pytest.mark.parametrize("initializer", _CONSTRUCTOR_FIELD_INITIALIZERS,
                         ids=("conditional", "adopted_attribute"))
def test_unknown_constructor_rhs_preserves_established_field_type(tmp_path, initializer):
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.py_ast import ClassType, DynType
    from pcc.frontends.python.type_infer import infer_module

    source = _constructor_field_projection_source(initializer)
    typed = infer_module(parse_and_lift(source, str(tmp_path / "owner.py"), "pcc.owner"))
    fields = dict(typed.body[-1].args[0].annotation.fields)
    assert tuple(fields) == ("arena", "other", "unknown")
    for name in ("arena", "other"):
        assert isinstance(fields[name], ClassType)
        assert fields[name].name == "Arena"
    assert isinstance(fields["unknown"], DynType)


@pytest.mark.parametrize("initializer", _CONSTRUCTOR_FIELD_INITIALIZERS,
                         ids=("conditional", "adopted_attribute"))
def test_declared_arena_field_keeps_direct_aggregate_getter_ir(tmp_path, initializer):
    from pcc.diagnostics.ir_diff import IrSummary
    from pcc.frontends.python.pipeline_context import compile_contextual_per_module_fallback_counts

    path = tmp_path / "owner.py"
    path.write_text(_constructor_field_projection_source(initializer), encoding="utf-8")
    counts = compile_contextual_per_module_fallback_counts(
        [str(path)], ["pcc.owner"], ["pcc.owner"],
        ir_scaffold_mode="on", strict_no_libpython=True, emit_ir_dir=str(tmp_path),
    )
    assert counts == {"pcc.owner": 0}
    ir_text = (tmp_path / "pcc_owner.ll").read_text(encoding="utf-8")
    getter = IrSummary.parse(ir_text).functions["user_pcc_owner_Holder_get_pair"]
    assert "user_pcc_owner_Arena_get_pair" in getter.calls
    assert "py_obj_getattr" not in getter.calls
    assert "py_obj_call" not in getter.calls
    assert "py_valuebox_get_field" not in getter.calls
