"""Mixin stack for ``L1CodeGen``.

Keep the large inheritance list out of ``layer1.py`` so the public entrypoint
stays small while each lowering concern remains split into its own module.
"""

from __future__ import annotations

from .async_with_lowering import AsyncWithLoweringMixin
from .attr_load_lowering import AttrLoadLoweringMixin
from .attr_store_lowering import AttrStoreLoweringMixin
from .assignment_statement_lowering import AssignmentStatementLoweringMixin
from .assignment_store_lowering import AssignmentStoreLoweringMixin
from .binary_op_lowering import BinaryOpLoweringMixin
from .builtin_type_attr_lowering import BuiltinTypeAttrLoweringMixin
from .call_arg_lowering import CallArgLoweringMixin
from .call_expression_lowering import CallExpressionLoweringMixin
from .call_object_lowering import CallObjectLoweringMixin
from .call_resolution_lowering import CallResolutionLoweringMixin
from .class_alias_lowering import ClassAliasLoweringMixin
from .class_model_lowering import ClassModelLoweringMixin
from .coercion_lowering import CoercionLoweringMixin
from .compare_membership_lowering import CompareMembershipLoweringMixin
from .comprehension_lowering import ComprehensionLoweringMixin
from .control_flow_lowering import ControlFlowLoweringMixin
from .core_helpers import CoreHelperMixin
from .cpy_bridge_lowering import CpyBridgeLoweringMixin
from .cpy_call_lowering import CpyCallLoweringMixin
from .cpy_import_state import CpyImportStateMixin
from .cpy_return_analysis import CpyReturnAnalysisMixin
from .decorator_lowering import DecoratorLoweringMixin
from .delete_lowering import DeleteLoweringMixin
from .dict_lowering import DictLoweringMixin
from .dynamic_type_lowering import DynamicTypeLoweringMixin
from .exact_int_lowering import ExactIntLoweringMixin
from .exception_lowering import ExceptionLoweringMixin
from .expr_dispatch_lowering import ExprDispatchLoweringMixin
from .expr_helper_lowering import ExprHelperLoweringMixin
from .extern_func_info_lowering import ExternFuncInfoLoweringMixin
from .extern_lowering import ExternScaffoldMixin
from .for_loop_lowering import ForLoopLoweringMixin
from .for_normalization_lowering import ForNormalizationLoweringMixin
from .format_lowering import FormatLoweringMixin
from .generation_lowering import GenerationLoweringMixin
from .generator_lowering import GeneratorLoweringMixin
from .import_lowering import ImportLoweringMixin
from .ir_decl_helpers import IrDeclHelperMixin
from .ir_scaffold_lowering import IrScaffoldLoweringMixin
from .isinstance_lowering import IsinstanceLoweringMixin
from .dyn_method_guard import DynMethodGuardMixin
from .iterator_builtin_lowering import IteratorBuiltinLoweringMixin
from .lambda_callback_lowering import LambdaCallbackLoweringMixin
from .lambda_helpers_lowering import LambdaHelperLoweringMixin
from .layer1_init import Layer1InitMixin
from .list_builtin_lowering import ListBuiltinLoweringMixin
from .list_method_lowering import ListMethodLoweringMixin
from .literal_lowering import LiteralLoweringMixin
from .method_call_expression_lowering import MethodCallExpressionLoweringMixin
from .method_call_lowering import MethodCallLoweringMixin
from .module_global_lowering import ModuleGlobalLoweringMixin
from .module_lifecycle_lowering import ModuleLifecycleLoweringMixin
from .module_name_lowering import ModuleNameLoweringMixin
from .name_lowering import NameLoweringMixin
from .native_asyncio import NativeAsyncioLoweringMixin
from .native_dataclasses import NativeDataclassesLoweringMixin
from .native_files import NativeFilesLoweringMixin
from .native_gc import NativeGcLoweringMixin
from .native_math import NativeMathLoweringMixin
from .native_modules import NativeModuleAliasMixin
from .native_os import NativeOsLoweringMixin
from .native_system import NativeSystemLoweringMixin
from .native_text_modules import NativeTextModulesLoweringMixin
from .native_threading import NativeThreadingLoweringMixin
from .native_virtual_thread import NativeVirtualThreadLoweringMixin
from .native_weakref import NativeWeakrefLoweringMixin
from .numeric_builtin_lowering import NumericBuiltinLoweringMixin
from .ownership_lowering import OwnershipLoweringMixin
from .print_lowering import PrintLoweringMixin
from .return_lowering import ReturnLoweringMixin
from .set_lowering import SetLoweringMixin
from .static_test_runner_lowering import StaticTestRunnerLoweringMixin
from .stmt_dispatch_lowering import StmtDispatchLoweringMixin
from .stmt_misc_lowering import StmtMiscLoweringMixin
from .string_globals_lowering import StringGlobalsLoweringMixin
from .string_method_lowering import StringMethodLoweringMixin
from .subscript_lowering import SubscriptLoweringMixin
from .tuple_zip_lowering import TupleZipLoweringMixin
from .type_abi_lowering import TypeAbiLoweringMixin
from .typed_int_abi import TypedIntAbiMixin
from .typing_lowering import TypingProtocolMixin
from .unary_call_lowering import UnaryCallLoweringMixin
from .unsafe_lowering import UnsafeIntrinsicMixin
from .user_function_decl_lowering import UserFunctionDeclLoweringMixin
from .user_function_lowering import UserFunctionLoweringMixin


class L1CodeGenMixinStack(
    TypedIntAbiMixin,
    UnsafeIntrinsicMixin,
    ExternScaffoldMixin,
    TypingProtocolMixin,
    DynamicTypeLoweringMixin,
    LambdaCallbackLoweringMixin,
    AsyncWithLoweringMixin,
    ExceptionLoweringMixin,
    ControlFlowLoweringMixin,
    DeleteLoweringMixin,
    ReturnLoweringMixin,
    LiteralLoweringMixin,
    PrintLoweringMixin,
    ExactIntLoweringMixin,
    CallArgLoweringMixin,
    CallObjectLoweringMixin,
    CallResolutionLoweringMixin,
    CoercionLoweringMixin,
    OwnershipLoweringMixin,
    IrDeclHelperMixin,
    ClassAliasLoweringMixin,
    ModuleNameLoweringMixin,
    CpyBridgeLoweringMixin,
    CpyCallLoweringMixin,
    CpyReturnAnalysisMixin,
    MethodCallLoweringMixin,
    AttrStoreLoweringMixin,
    AssignmentStoreLoweringMixin,
    SubscriptLoweringMixin,
    BuiltinTypeAttrLoweringMixin,
    SetLoweringMixin,
    IteratorBuiltinLoweringMixin,
    NumericBuiltinLoweringMixin,
    ListBuiltinLoweringMixin,
    ListMethodLoweringMixin,
    DictLoweringMixin,
    DynMethodGuardMixin,
    StringMethodLoweringMixin,
    TupleZipLoweringMixin,
    ComprehensionLoweringMixin,
    ForLoopLoweringMixin,
    AssignmentStatementLoweringMixin,
    BinaryOpLoweringMixin,
    CompareMembershipLoweringMixin,
    NameLoweringMixin,
    AttrLoadLoweringMixin,
    CallExpressionLoweringMixin,
    MethodCallExpressionLoweringMixin,
    ClassModelLoweringMixin,
    ModuleGlobalLoweringMixin,
    ModuleLifecycleLoweringMixin,
    GenerationLoweringMixin,
    ExprHelperLoweringMixin,
    ExprDispatchLoweringMixin,
    UnaryCallLoweringMixin,
    LambdaHelperLoweringMixin,
    StmtMiscLoweringMixin,
    StmtDispatchLoweringMixin,
    GeneratorLoweringMixin,
    UserFunctionLoweringMixin,
    FormatLoweringMixin,
    StringGlobalsLoweringMixin,
    TypeAbiLoweringMixin,
    UserFunctionDeclLoweringMixin,
    ExternFuncInfoLoweringMixin,
    StaticTestRunnerLoweringMixin,
    DecoratorLoweringMixin,
    ForNormalizationLoweringMixin,
    CoreHelperMixin,
    IrScaffoldLoweringMixin,
    CpyImportStateMixin,
    ImportLoweringMixin,
    IsinstanceLoweringMixin,
    Layer1InitMixin,
    NativeModuleAliasMixin,
    NativeGcLoweringMixin,
    NativeAsyncioLoweringMixin,
    NativeDataclassesLoweringMixin,
    NativeFilesLoweringMixin,
    NativeOsLoweringMixin,
    NativeMathLoweringMixin,
    NativeTextModulesLoweringMixin,
    NativeSystemLoweringMixin,
    NativeThreadingLoweringMixin,
    NativeVirtualThreadLoweringMixin,
    NativeWeakrefLoweringMixin,
):
    pass


__all__ = ["L1CodeGenMixinStack"]
