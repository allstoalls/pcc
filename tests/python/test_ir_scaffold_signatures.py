"""Independent signature guards; these do not derive expectations from IRBuilder."""

import pytest
from pathlib import Path

from pcc.tools.ir_scaffold_signatures import (
    audit_simple_signatures,
    current_signature_issues,
    definition_signatures,
)


def test_current_simple_signatures_have_no_unrecorded_drift():
    assert current_signature_issues() == []


@pytest.mark.parametrize(
    "parameter", ["arch=None", "*, arch=None", "*, arch", "**kwargs"]
)
def test_new_callee_parameter_cannot_be_silently_dropped(parameter):
    source = (
        "class IRBuilder:\n    def syscall6(self, nr, name='', "
        + parameter
        + "): pass\n"
    )
    issues = audit_simple_signatures(
        source, {"syscall6": ("ptr", 1)}, {"syscall6": ("name",)}
    )
    assert issues
    assert {item.code for item in issues} & {
        "optional-parameters",
        "parameter-kind",
        "required-count",
    }


def test_default_value_drift_is_detected_without_an_arity_change():
    source = (
        "class IRBuilder:\n    def load(self, ptr, name='changed', align=8): pass\n"
    )
    issues = audit_simple_signatures(
        source, {"load": ("ptr", 1)}, {"load": ("name", "align")}
    )
    assert [item.code for item in issues] == ["default-value", "default-value"]


def test_actual_adapters_are_checked_as_definitions():
    source = "def IRBuilder_add_incoming(phi, value, block): pass\n"
    assert audit_simple_signatures(source, {"add_incoming": ("void", 2)}, {}) == []
    source = "def IRBuilder_add_incoming(phi, value, block, new_required): pass\n"
    assert (
        audit_simple_signatures(source, {"add_incoming": ("void", 2)}, {})[0].code
        == "required-count"
    )


def test_source_signature_preserves_positional_and_keyword_only_kinds():
    source = "class IRBuilder:\n    def f(self, x, /, *args, name='', **kwargs): pass\n"
    signature = definition_signatures(source)["IRBuilder.f"]
    assert [(p.name, p.kind) for p in signature] == [
        ("self", "pos_only"),
        ("x", "pos_only"),
        ("args", "*args"),
        ("name", "kw_only"),
        ("kwargs", "**kwargs"),
    ]


@pytest.mark.parametrize(
    "target,required,optional",
    [
        (
            "IRBuilder.alloca",
            ("self", "ty"),
            (("size", "Constant(value=None)"), ("name", "Constant(value='')")),
        ),
        (
            "IRBuilder.call",
            ("self", "fn", "args"),
            (("name", "Constant(value='')"), ("tail", "Constant(value=False)")),
        ),
        (
            "IRBuilder.call4_i32",
            ("self", "fn", "arg0", "arg1", "arg2", "arg3"),
            (("name", "Constant(value='')"), ("tail", "Constant(value=False)")),
        ),
        (
            "IRBuilder.gep",
            ("self", "ptr", "indices"),
            (("inbounds", "Constant(value=False)"), ("name", "Constant(value='')")),
        ),
        ("IRBuilder.phi", ("self", "ty"), (("name", "Constant(value='')"),)),
        (
            "IRBuilder.landingpad",
            ("self", "ty"),
            (("name", "Constant(value='')"), ("cleanup", "Constant(value=False)")),
        ),
        ("IRBuilder.append_basic_block", ("self",), (("name", "Constant(value='')"),)),
        ("Function.append_basic_block", ("self",), (("name", "Constant(value='')"),)),
        ("PhiInstr.add_incoming", ("self", "value", "block"), ()),
        ("SwitchInstr.add_case", ("self", "int_value", "target"), ()),
        ("Type.as_pointer", ("self",), (("addrspace", "Constant(value=0)"),)),
    ],
)
def test_bespoke_lowering_api_signature_does_not_drift(target, required, optional):
    # The specialized adapters intentionally have different machine arities;
    # pin the Python API they adapt as well as checking effective callees.
    root = Path(__file__).absolute().parents[2]
    signatures = definition_signatures((root / "pcc/ir/ir.py").read_text())
    parameters = signatures[target]
    assert all(parameter.kind == "pos" for parameter in parameters)
    assert (
        tuple(parameter.name for parameter in parameters if parameter.required)
        == required
    )
    assert (
        tuple(
            (parameter.name, parameter.default)
            for parameter in parameters
            if not parameter.required
        )
        == optional
    )
