"""Mixin stack for ``L1CodeGen``.

Keep the large inheritance list out of ``layer1.py`` so the public entrypoint
stays small while each lowering concern remains split into its own module.
"""

from __future__ import annotations

from pcc.frontends.python.codegen.async_with_lowering import AsyncWithLoweringMixin
from pcc.frontends.python.codegen.attr_load_lowering import AttrLoadLoweringMixin
from pcc.frontends.python.codegen.attr_store_lowering import AttrStoreLoweringMixin
from pcc.frontends.python.codegen.assignment_statement_lowering import AssignmentStatementLoweringMixin
from pcc.frontends.python.codegen.assignment_store_lowering import AssignmentStoreLoweringMixin
from pcc.frontends.python.codegen.binary_op_lowering import BinaryOpLoweringMixin
from pcc.frontends.python.codegen.builtin_type_attr_lowering import BuiltinTypeAttrLoweringMixin
from pcc.frontends.python.codegen.call_arg_lowering import CallArgLoweringMixin
from pcc.frontends.python.codegen.call_expression_lowering import CallExpressionLoweringMixin
from pcc.frontends.python.codegen.call_object_lowering import CallObjectLoweringMixin
from pcc.frontends.python.codegen.call_resolution_lowering import CallResolutionLoweringMixin
from pcc.frontends.python.codegen.class_alias_lowering import ClassAliasLoweringMixin
from pcc.frontends.python.codegen.class_model_lowering import ClassModelLoweringMixin
from pcc.frontends.python.codegen.coercion_lowering import CoercionLoweringMixin
from pcc.frontends.python.codegen.compare_membership_lowering import CompareMembershipLoweringMixin
from pcc.frontends.python.codegen.comprehension_lowering import ComprehensionLoweringMixin
from pcc.frontends.python.codegen.control_flow_lowering import ControlFlowLoweringMixin
from pcc.frontends.python.codegen.core_helpers import CoreHelperMixin
from pcc.frontends.python.codegen.cpy_bridge_lowering import CpyBridgeLoweringMixin
from pcc.frontends.python.codegen.cpy_call_lowering import CpyCallLoweringMixin
from pcc.frontends.python.codegen.cpy_import_state import CpyImportStateMixin
from pcc.frontends.python.codegen.cpy_return_analysis import CpyReturnAnalysisMixin
from pcc.frontends.python.codegen.decorator_lowering import DecoratorLoweringMixin
from pcc.frontends.python.codegen.delete_lowering import DeleteLoweringMixin
from pcc.frontends.python.codegen.dict_lowering import DictLoweringMixin
from pcc.frontends.python.codegen.dynamic_type_lowering import DynamicTypeLoweringMixin
from pcc.frontends.python.codegen.exact_int_lowering import ExactIntLoweringMixin
from pcc.frontends.python.codegen.exception_lowering import ExceptionLoweringMixin
from pcc.frontends.python.codegen.expr_dispatch_lowering import ExprDispatchLoweringMixin
from pcc.frontends.python.codegen.expr_helper_lowering import ExprHelperLoweringMixin
from pcc.frontends.python.codegen.extern_func_info_lowering import ExternFuncInfoLoweringMixin
from pcc.frontends.python.codegen.extern_lowering import ExternScaffoldMixin
from pcc.frontends.python.codegen.for_loop_lowering import ForLoopLoweringMixin
from pcc.frontends.python.codegen.for_normalization_lowering import ForNormalizationLoweringMixin
from pcc.frontends.python.codegen.format_lowering import FormatLoweringMixin
from pcc.frontends.python.codegen.generation_lowering import GenerationLoweringMixin
from pcc.frontends.python.codegen.generator_lowering import GeneratorLoweringMixin
from pcc.frontends.python.codegen.import_lowering import ImportLoweringMixin
from pcc.frontends.python.codegen.ir_decl_helpers import IrDeclHelperMixin
from pcc.frontends.python.codegen.ir_scaffold_lowering import IrScaffoldLoweringMixin
from pcc.frontends.python.codegen.isinstance_lowering import IsinstanceLoweringMixin
from pcc.frontends.python.codegen.dyn_method_guard import DynMethodGuardMixin
from pcc.frontends.python.codegen.iterator_builtin_lowering import IteratorBuiltinLoweringMixin
from pcc.frontends.python.codegen.lambda_callback_lowering import LambdaCallbackLoweringMixin
from pcc.frontends.python.codegen.lambda_helpers_lowering import LambdaHelperLoweringMixin
from pcc.frontends.python.codegen.layer1_init import Layer1InitMixin
from pcc.frontends.python.codegen.list_builtin_lowering import ListBuiltinLoweringMixin
from pcc.frontends.python.codegen.list_method_lowering import ListMethodLoweringMixin
from pcc.frontends.python.codegen.literal_lowering import LiteralLoweringMixin
from pcc.frontends.python.codegen.method_call_expression_lowering import MethodCallExpressionLoweringMixin
from pcc.frontends.python.codegen.method_call_lowering import MethodCallLoweringMixin
from pcc.frontends.python.codegen.module_global_lowering import ModuleGlobalLoweringMixin
from pcc.frontends.python.codegen.module_lifecycle_lowering import ModuleLifecycleLoweringMixin
from pcc.frontends.python.codegen.module_name_lowering import ModuleNameLoweringMixin
from pcc.frontends.python.codegen.name_lowering import NameLoweringMixin
from pcc.frontends.python.codegen.native_asyncio import NativeAsyncioLoweringMixin
from pcc.frontends.python.codegen.native_dataclasses import NativeDataclassesLoweringMixin
from pcc.frontends.python.codegen.native_files import NativeFilesLoweringMixin
from pcc.frontends.python.codegen.native_gc import NativeGcLoweringMixin
from pcc.frontends.python.codegen.native_math import NativeMathLoweringMixin
from pcc.frontends.python.codegen.native_modules import NativeModuleAliasMixin
from pcc.frontends.python.codegen.native_os import NativeOsLoweringMixin
from pcc.frontends.python.codegen.native_system import NativeSystemLoweringMixin
from pcc.frontends.python.codegen.native_text_modules import NativeTextModulesLoweringMixin
from pcc.frontends.python.codegen.native_threading import NativeThreadingLoweringMixin
from pcc.frontends.python.codegen.native_virtual_thread import NativeVirtualThreadLoweringMixin
from pcc.frontends.python.codegen.native_weakref import NativeWeakrefLoweringMixin
from pcc.frontends.python.codegen.numeric_builtin_lowering import NumericBuiltinLoweringMixin
from pcc.frontends.python.codegen.ownership_lowering import OwnershipLoweringMixin
from pcc.frontends.python.codegen.print_lowering import PrintLoweringMixin
from pcc.frontends.python.codegen.return_lowering import ReturnLoweringMixin
from pcc.frontends.python.codegen.set_lowering import SetLoweringMixin
from pcc.frontends.python.codegen.static_test_runner_lowering import StaticTestRunnerLoweringMixin
from pcc.frontends.python.codegen.stmt_dispatch_lowering import StmtDispatchLoweringMixin
from pcc.frontends.python.codegen.stmt_misc_lowering import StmtMiscLoweringMixin
from pcc.frontends.python.codegen.string_globals_lowering import StringGlobalsLoweringMixin
from pcc.frontends.python.codegen.string_method_lowering import StringMethodLoweringMixin
from pcc.frontends.python.codegen.subscript_lowering import SubscriptLoweringMixin
from pcc.frontends.python.codegen.tuple_zip_lowering import TupleZipLoweringMixin
from pcc.frontends.python.codegen.type_abi_lowering import TypeAbiLoweringMixin
from pcc.frontends.python.codegen.typed_int_abi import TypedIntAbiMixin
from pcc.frontends.python.codegen.typing_lowering import TypingProtocolMixin
from pcc.frontends.python.codegen.unary_call_lowering import UnaryCallLoweringMixin
from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin
from pcc.frontends.python.codegen.user_function_decl_lowering import UserFunctionDeclLoweringMixin
from pcc.frontends.python.codegen.user_function_lowering import UserFunctionLoweringMixin


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
