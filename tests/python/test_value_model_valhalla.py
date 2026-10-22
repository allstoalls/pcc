from __future__ import annotations

import subprocess

import pytest

from pcc import valueclass
from pcc.support.compiler_hot_objects import migrated_value_model_hot_objects
from pcc.value_model import (
    ValueBox,
    box_value,
    flatten_fields,
    specialize_generic_signature,
    specialized_array,
    unbox_value,
    value_model_status,
)
from pcc.frontends.python.py_ast import ClassType, ValueClassType


def test_valueclass_host_projection_box_flatten_and_specialize():
    @valueclass
    class Point:
        x: int
        y: int

    p = Point(1, 2)
    with pytest.raises(Exception):
        p.x = 3
    boxed = box_value(p)
    assert isinstance(boxed, ValueBox)
    assert unbox_value(boxed).values == (1, 2)
    assert flatten_fields(Point) == (("x", "int"), ("y", "int"))
    arr = specialized_array([p, Point(3, 4)])
    assert len(arr) == 2
    assert arr[1].unbox().values == (3, 4)
    spec = specialize_generic_signature("Pair", Point, int)
    assert spec.payload_abi.endswith(".Point,int]::value_payload")


def test_type_infer_marks_pcc_valueclass_as_value_class_type():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer

    source = "import pcc\n\n@pcc.valueclass\nclass Point:\n    x: int\n    y: int\n"
    ast_mod = parse_and_lift(source, "<valueclass>", "value_mod")
    typed = type_infer.infer_module(ast_mod)
    class_stmt = typed.body[1]
    assert class_stmt.name == "Point"
    call_mod = parse_and_lift(source + "p = Point(1, 2)\n", "<valueclass>", "value_mod")
    typed_call = type_infer.infer_module(call_mod)
    assign = typed_call.body[-1]
    assert issubclass(ValueClassType, ClassType)
    assert isinstance(assign.value.ty, ClassType)
    assert assign.value.ty.valueclass is True
    assert tuple(name for name, _ in assign.value.ty.fields) == ("x", "y")


def test_valueclass_compiles_and_keeps_frozen_semantics(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "valueclass_smoke.py"
    exe = tmp_path / "valueclass_smoke"
    src.write_text(
        "import pcc\n"
        "@pcc.valueclass\n"
        "class Point:\n"
        "    x: int\n"
        "    y: int\n"
        "p = Point(4, 5)\n"
        "print(p.x + p.y)\n"
    , encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", ir_scaffold_mode="on")
    proc = subprocess.run([str(exe)], text=True, capture_output=True, check=True, timeout=30)
    assert proc.stdout.strip() == "9"


def test_value_model_status_distinguishes_implementation_from_scaffolding():
    status = value_model_status()
    assert (
        status["implemented_through"]
        == "V1-direct-scalar-and-nested-payload-eq-checked-marshal-"
        "v2-pointer-and-nested-dyn-boundary-partial"
    )
    assert status["scaffolding_through"] == "V6"
    assert status["production_runtime"] is False
    v1_local_payload = (
        "V1 scalar-field value payload lowering for local constructor assignment "
        "and field reads"
    )
    v1_arg_return_payload = (
        "V1 direct function argument and constructor-return payload ABI for "
        "scalar-field valueclasses"
    )
    v1_method_receiver = (
        "V1 direct method receiver payload ABI for scalar-field valueclasses"
    )
    v1_payload_eq = "V1 fieldwise equality for direct scalar-field valueclass payloads"
    v1_nested_payload_eq = (
        "V1 recursive fieldwise equality for non-recursive nested valueclass "
        "direct payloads"
    )
    v1_payload_box = (
        "V1 scalar-field value payload to ordinary pcc object boxing at "
        "Dyn/object boundaries"
    )
    v1_payload_unbox = (
        "V1 ordinary pcc object to scalar-field value payload unboxing at "
        "typed boundaries"
    )
    v1_payload_checked_unbox = (
        "V1 type-checked ordinary pcc object to scalar-field value payload "
        "unboxing failure path"
    )
    v1_recursive_reject = (
        "V1 diagnostics rejecting recursive and mutually-recursive valueclass payloads"
    )
    v2_nested_dyn_return = (
        "V2 selected nested valueclass constructor returns to Any/Dyn through "
        "ValueBox projection"
    )
    assert v1_local_payload in status["implemented"]
    assert v1_arg_return_payload in status["implemented"]
    assert v1_method_receiver in status["implemented"]
    assert v1_payload_eq in status["implemented"]
    assert v1_nested_payload_eq in status["implemented"]
    assert (
        "V1 non-recursive nested valueclass direct payload ABI for focused typed calls/returns"
        in status["implemented"]
    )
    assert v1_payload_box in status["implemented"]
    assert v1_payload_unbox in status["implemented"]
    assert v1_payload_checked_unbox in status["implemented"]
    assert v1_recursive_reject in status["implemented"]
    assert v2_nested_dyn_return in status["implemented"]
    assert "C runtime PyValueBox object and GC tracing" in status["implemented"]
    migrations = migrated_value_model_hot_objects()
    assert {m.name for m in migrations} >= {"SourceSpan", "ValuePayload"}
