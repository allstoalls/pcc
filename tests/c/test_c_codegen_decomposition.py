"""Facade contracts for the first C-codegen decomposition seams."""

from __future__ import annotations

from pcc.frontends.c.codegen import c_codegen
from pcc.frontends.c.codegen import c_declaration_state
from pcc.frontends.c.codegen import c_declaration_lowering
from pcc.frontends.c.codegen import c_control_flow
from pcc.frontends.c.codegen import c_expression_flow
from pcc.frontends.c.codegen import c_initializer_lowering
from pcc.frontends.c.codegen import c_layout
from pcc.frontends.c.codegen import c_libc_declarations
from pcc.frontends.c.codegen import c_scope_context
from pcc.frontends.c.codegen import c_ssa_lowering
from pcc.frontends.c.codegen import c_switch_flow
from pcc.frontends.c.codegen import c_types


def test_c_codegen_reexports_file_scope_state_records():
    assert (
        c_codegen.FileScopeObjectState
        is c_declaration_state.FileScopeObjectState
    )
    assert (
        c_codegen.FileScopeFunctionState
        is c_declaration_state.FileScopeFunctionState
    )


def test_c_codegen_reexports_layout_records_and_helpers():
    assert c_codegen.StructFieldLayout is c_layout.StructFieldLayout
    assert c_codegen.StructStorageSegment is c_layout.StructStorageSegment
    assert c_codegen.BitFieldRef is c_layout.BitFieldRef
    assert c_codegen._ir_type_align_static is c_layout.ir_type_align
    assert c_codegen._ir_type_size_static is c_layout.ir_type_size
    assert c_codegen._is_struct_ir_type is c_layout.is_struct_ir_type


def test_c_codegen_reexports_scope_and_error_contracts():
    assert c_codegen._NewScopeCtx is c_scope_context.NewScopeContext
    assert c_codegen._NewFunctionCtx is c_scope_context.NewFunctionContext
    assert c_codegen.CodegenError is c_declaration_state.CodegenError
    assert c_codegen.ExternGlobalRef is c_declaration_state.ExternGlobalRef


def test_c_codegen_reexports_type_projection_contracts():
    assert c_codegen.int8_t is c_types.int8_t
    assert c_codegen.int64_t is c_types.int64_t
    assert c_codegen.get_ir_type is c_types.get_ir_type
    assert c_codegen.get_ir_type_from_names is c_types.get_ir_type_from_names
    assert c_codegen.get_ir_type_from_node is c_types.get_ir_type_from_node
    assert c_codegen._resolve_node_type is c_types.resolve_node_type


def test_signedness_decision_owner_remains_in_c_codegen():
    assert c_codegen._decide_usual_integer_conversion.__module__ == (
        "pcc.frontends.c.codegen.c_codegen"
    )
    assert c_codegen.IntegerConversionDecision.__module__ == (
        "pcc.frontends.c.codegen.c_codegen"
    )


def test_c_codegen_reexports_libc_declaration_registry():
    assert c_codegen.LIBC_FUNCTIONS is c_libc_declarations.LIBC_FUNCTIONS
    assert (
        c_codegen._LEGACY_LIBC_FUNCTIONS
        is c_libc_declarations._LEGACY_LIBC_FUNCTIONS
    )
    assert (
        c_codegen.refresh_libc_registry_from_declarative
        is c_libc_declarations.refresh_libc_registry_from_declarative
    )
    assert (
        c_codegen.libc_registry_shadow_names
        is c_libc_declarations.libc_registry_shadow_names
    )


def test_c_codegen_inherits_expression_and_control_flow_seams():
    assert issubclass(c_codegen.CCodeGenerator, c_expression_flow.CExpressionFlowMixin)
    assert issubclass(c_codegen.CCodeGenerator, c_control_flow.CControlFlowMixin)
    assert c_codegen.CCodeGenerator._codegen_short_circuit_and is (
        c_expression_flow.CExpressionFlowMixin._codegen_short_circuit_and
    )
    assert c_codegen.CCodeGenerator.codegen_If is (
        c_control_flow.CControlFlowMixin.codegen_If
    )
    assert c_codegen.CCodeGenerator.codegen_DoWhile is (
        c_control_flow.CControlFlowMixin.codegen_DoWhile
    )
    assert issubclass(c_codegen.CCodeGenerator, c_switch_flow.CSwitchFlowMixin)
    assert c_codegen.CCodeGenerator.codegen_Switch is (
        c_switch_flow.CSwitchFlowMixin.codegen_Switch
    )
    assert c_codegen.CCodeGenerator.codegen_Case is (
        c_switch_flow.CSwitchFlowMixin.codegen_Case
    )
    assert c_codegen.CCodeGenerator.codegen_Default is (
        c_switch_flow.CSwitchFlowMixin.codegen_Default
    )


def test_c_codegen_inherits_complete_ssa_lowering_seam():
    assert issubclass(c_codegen.CCodeGenerator, c_ssa_lowering.CSSALoweringMixin)
    assert c_codegen.CCodeGenerator._lower_ssa_function is (
        c_ssa_lowering.CSSALoweringMixin._lower_ssa_function
    )
    assert c_codegen.CCodeGenerator._lower_ssa_instruction is (
        c_ssa_lowering.CSSALoweringMixin._lower_ssa_instruction
    )
    assert c_codegen.CCodeGenerator._ssa_convert is (
        c_ssa_lowering.CSSALoweringMixin._ssa_convert
    )


def test_c_codegen_inherits_complete_initializer_lowering_seam():
    assert issubclass(
        c_codegen.CCodeGenerator,
        c_initializer_lowering.CInitializerLoweringMixin,
    )
    assert c_codegen.CCodeGenerator._build_const_init is (
        c_initializer_lowering.CInitializerLoweringMixin._build_const_init
    )
    assert c_codegen.CCodeGenerator._init_runtime_aggregate is (
        c_initializer_lowering.CInitializerLoweringMixin._init_runtime_aggregate
    )


def test_c_codegen_inherits_declaration_lowering_seam():
    assert issubclass(
        c_codegen.CCodeGenerator,
        c_declaration_lowering.CDeclarationLoweringMixin,
    )
    assert c_codegen.CCodeGenerator.codegen_Decl is (
        c_declaration_lowering.CDeclarationLoweringMixin.codegen_Decl
    )


def test_ssa_seam_keeps_signedness_policy_on_the_codegen_facade():
    assert "_usual_arithmetic_conversion" not in c_ssa_lowering.__dict__
    assert "_decide_usual_integer_conversion" not in c_ssa_lowering.__dict__
    assert c_codegen._decide_usual_integer_conversion.__module__ == (
        "pcc.frontends.c.codegen.c_codegen"
    )
