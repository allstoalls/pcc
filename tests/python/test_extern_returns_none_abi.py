"""Regression: schema-carried ``returns_none`` must win over degraded
``("dyn",)`` return descriptors for plain-function extern declarations.

Under the self-hosted compiler (pcc1 building pcc2), ``encode_type``'s
isinstance chain can see foreign node identity and degrade a ``-> None``
return annotation to ``("dyn",)``. If the extern-declaration paths trust
the descriptor, a caller module declares the sibling function as
returning a pointer against a ``ret void`` definition and then
roots/increfs whatever the callee left in x0 — the deterministic GC4
graph-lock deadlock in pcc1-built pcc2
(docs/investigations/gc4-pcc2-graph-lock-deadlock-stage2-miscompile.md).

The ``returns_none`` bool (pipeline._export_returns_none) survives the
schema round trip; class_gen's method plans already consume it. These
tests pin the two plain-function consumers to the same contract.
"""

from pcc.frontends.python.codegen.extern_func_info_lowering import (
    ExternFuncInfoLoweringMixin,
)
from pcc.frontends.python.codegen.native_modules import NativeModuleAliasMixin
from pcc.frontends.python.py_ast import NoneType


class _Probe(NativeModuleAliasMixin, ExternFuncInfoLoweringMixin):
    """Bare mixin host; the paths under test never touch instance state."""


def test_return_ir_type_honors_returns_none_over_degraded_dyn():
    out = _Probe()._extern_user_function_return_ir_type(
        {"return_ty": ("dyn",), "returns_none": True},
        box_int_abi=False,
    )
    assert type(out).__name__ == "VoidType"


def test_return_ir_type_still_void_for_intact_none_descriptor():
    out = _Probe()._extern_user_function_return_ir_type(
        {"return_ty": ("none",), "returns_none": True},
        box_int_abi=False,
    )
    assert type(out).__name__ == "VoidType"


def test_extern_funcdef_return_ty_honors_returns_none_over_degraded_dyn():
    fd = _Probe()._extern_info_to_funcdef(
        "sink",
        {
            "call_sig": (),
            "return_ty": ("dyn",),
            "returns_none": True,
        },
    )
    assert fd is not None
    assert isinstance(fd.return_ty, NoneType) or (
        getattr(fd.return_ty, "name", "") == "None"
    )


def test_extern_funcdef_return_ty_keeps_dyn_without_returns_none():
    fd = _Probe()._extern_info_to_funcdef(
        "sink",
        {
            "call_sig": (),
            "return_ty": ("dyn",),
        },
    )
    assert fd is not None
    assert getattr(fd.return_ty, "name", "") == "dyn"
