"""pcc LLVM C-API binding (P6C.2 scaffold).

Per ``docs/plans/python-frontend-plan.md`` §Phase 6C.2 — the chosen
Strategy C bootstrap requires replacing ``llvmlite`` with a
hand-written, pcc-compilable LLVM-C-API binding so the self-hosted
binary has no Python-C-extension dependency at runtime.

This module is the scaffold: the public types (``LLVMContextRef``,
``LLVMModuleRef``, ``LLVMBuilderRef``, ...) are opaque ``c_ptr``
aliases, and each LLVM-C function is declared via the
:mod:`pcc.extern` FFI package. With the P6C.1 codegen lowering in
place these become direct LLVM ``call`` sites with no Python
trampoline.

Implementation status (2026-04-20): **declarations only**. The
adapter layer that lets ``pcc.codegen.c_codegen`` / ``pcc.ir_passes``
swap ``llvmlite`` for this binding is P6C.2's remaining deliverable.
"""
from __future__ import annotations

from pcc.extern import (
    ExternFn, extern,
    c_int, c_int32, c_int64, c_void, c_ptr, c_str, c_bool,
    c_double, c_rawptr,
)

# ---------------------------------------------------------------------------
# Opaque ref types. All of them lower to ``ptr`` at the IR level; the
# per-ref name is for documentation and (eventually) type-inference
# bucketing on the pcc side.
# ---------------------------------------------------------------------------

LLVMContextRef = c_ptr
LLVMModuleRef = c_ptr
LLVMValueRef = c_ptr
LLVMTypeRef = c_ptr
LLVMBuilderRef = c_ptr
LLVMBasicBlockRef = c_ptr
LLVMMemoryBufferRef = c_ptr
LLVMPassManagerRef = c_ptr
LLVMExecutionEngineRef = c_ptr
LLVMGenericValueRef = c_ptr
LLVMTargetMachineRef = c_ptr
LLVMTargetDataRef = c_ptr
LLVMTargetRef = c_ptr
LLVMErrorRef = c_ptr


# ---------------------------------------------------------------------------
# Core: context + module
# ---------------------------------------------------------------------------

LLVMContextCreate: ExternFn = extern(
    "LLVMContextCreate", argtypes=(), restype=LLVMContextRef,
)
LLVMContextDispose: ExternFn = extern(
    "LLVMContextDispose", argtypes=(LLVMContextRef,), restype=c_void,
)

LLVMModuleCreateWithNameInContext: ExternFn = extern(
    "LLVMModuleCreateWithNameInContext",
    argtypes=(c_str, LLVMContextRef),
    restype=LLVMModuleRef,
)
LLVMDisposeModule: ExternFn = extern(
    "LLVMDisposeModule", argtypes=(LLVMModuleRef,), restype=c_void,
)
LLVMPrintModuleToString: ExternFn = extern(
    # ``char *`` owned by LLVM and released with ``LLVMDisposeMessage``: a raw
    # address, not a Python object, so ``c_rawptr`` rather than the ambiguous
    # ``c_str``.
    "LLVMPrintModuleToString", argtypes=(LLVMModuleRef,), restype=c_rawptr,
)
LLVMParseIRInContext: ExternFn = extern(
    "LLVMParseIRInContext",
    argtypes=(LLVMContextRef, LLVMMemoryBufferRef, c_ptr, c_ptr),
    restype=c_bool,
)
LLVMVerifyModule: ExternFn = extern(
    "LLVMVerifyModule", argtypes=(LLVMModuleRef, c_int, c_ptr),
    restype=c_bool,
)

# Types
LLVMVoidTypeInContext: ExternFn = extern(
    "LLVMVoidTypeInContext", argtypes=(LLVMContextRef,),
    restype=LLVMTypeRef,
)
LLVMInt1TypeInContext: ExternFn = extern(
    "LLVMInt1TypeInContext", argtypes=(LLVMContextRef,),
    restype=LLVMTypeRef,
)
LLVMInt8TypeInContext: ExternFn = extern(
    "LLVMInt8TypeInContext", argtypes=(LLVMContextRef,),
    restype=LLVMTypeRef,
)
LLVMInt32TypeInContext: ExternFn = extern(
    "LLVMInt32TypeInContext", argtypes=(LLVMContextRef,),
    restype=LLVMTypeRef,
)
LLVMInt64TypeInContext: ExternFn = extern(
    "LLVMInt64TypeInContext", argtypes=(LLVMContextRef,),
    restype=LLVMTypeRef,
)
LLVMDoubleTypeInContext: ExternFn = extern(
    "LLVMDoubleTypeInContext", argtypes=(LLVMContextRef,),
    restype=LLVMTypeRef,
)
LLVMPointerTypeInContext: ExternFn = extern(
    "LLVMPointerTypeInContext",
    argtypes=(LLVMContextRef, c_int),
    restype=LLVMTypeRef,
)
LLVMFunctionType: ExternFn = extern(
    "LLVMFunctionType",
    argtypes=(LLVMTypeRef, c_ptr, c_int, c_int),
    restype=LLVMTypeRef,
)

# Values + functions
LLVMAddFunction: ExternFn = extern(
    "LLVMAddFunction",
    argtypes=(LLVMModuleRef, c_str, LLVMTypeRef),
    restype=LLVMValueRef,
)
LLVMGetNamedFunction: ExternFn = extern(
    "LLVMGetNamedFunction",
    argtypes=(LLVMModuleRef, c_str),
    restype=LLVMValueRef,
)
LLVMAppendBasicBlockInContext: ExternFn = extern(
    "LLVMAppendBasicBlockInContext",
    argtypes=(LLVMContextRef, LLVMValueRef, c_str),
    restype=LLVMBasicBlockRef,
)

# Builder
LLVMCreateBuilderInContext: ExternFn = extern(
    "LLVMCreateBuilderInContext",
    argtypes=(LLVMContextRef,),
    restype=LLVMBuilderRef,
)
LLVMDisposeBuilder: ExternFn = extern(
    "LLVMDisposeBuilder", argtypes=(LLVMBuilderRef,), restype=c_void,
)
LLVMPositionBuilderAtEnd: ExternFn = extern(
    "LLVMPositionBuilderAtEnd",
    argtypes=(LLVMBuilderRef, LLVMBasicBlockRef),
    restype=c_void,
)
LLVMBuildRet: ExternFn = extern(
    "LLVMBuildRet",
    argtypes=(LLVMBuilderRef, LLVMValueRef),
    restype=LLVMValueRef,
)
LLVMBuildRetVoid: ExternFn = extern(
    "LLVMBuildRetVoid", argtypes=(LLVMBuilderRef,),
    restype=LLVMValueRef,
)
LLVMBuildAdd: ExternFn = extern(
    "LLVMBuildAdd",
    argtypes=(LLVMBuilderRef, LLVMValueRef, LLVMValueRef, c_str),
    restype=LLVMValueRef,
)
LLVMBuildCall2: ExternFn = extern(
    "LLVMBuildCall2",
    argtypes=(
        LLVMBuilderRef, LLVMTypeRef, LLVMValueRef, c_ptr, c_int, c_str,
    ),
    restype=LLVMValueRef,
)
LLVMBuildLoad2: ExternFn = extern(
    "LLVMBuildLoad2",
    argtypes=(LLVMBuilderRef, LLVMTypeRef, LLVMValueRef, c_str),
    restype=LLVMValueRef,
)
LLVMBuildStore: ExternFn = extern(
    "LLVMBuildStore",
    argtypes=(LLVMBuilderRef, LLVMValueRef, LLVMValueRef),
    restype=LLVMValueRef,
)
LLVMBuildAlloca: ExternFn = extern(
    "LLVMBuildAlloca",
    argtypes=(LLVMBuilderRef, LLVMTypeRef, c_str),
    restype=LLVMValueRef,
)
LLVMConstInt: ExternFn = extern(
    "LLVMConstInt",
    argtypes=(LLVMTypeRef, c_int64, c_int),
    restype=LLVMValueRef,
)
LLVMConstReal: ExternFn = extern(
    "LLVMConstReal",
    argtypes=(LLVMTypeRef, c_double),
    restype=LLVMValueRef,
)

# ---------------------------------------------------------------------------
# Analysis / JIT (subset)
# ---------------------------------------------------------------------------

LLVMInitializeNativeTarget: ExternFn = extern(
    "LLVMInitializeNativeTarget", argtypes=(), restype=c_bool,
)
LLVMInitializeNativeAsmPrinter: ExternFn = extern(
    "LLVMInitializeNativeAsmPrinter", argtypes=(), restype=c_bool,
)

__all__ = [
    "LLVMContextRef", "LLVMModuleRef", "LLVMValueRef", "LLVMTypeRef",
    "LLVMBuilderRef", "LLVMBasicBlockRef", "LLVMMemoryBufferRef",
    "LLVMPassManagerRef", "LLVMExecutionEngineRef",
    "LLVMGenericValueRef", "LLVMTargetMachineRef", "LLVMTargetDataRef",
    "LLVMTargetRef", "LLVMErrorRef",
    "LLVMContextCreate", "LLVMContextDispose",
    "LLVMModuleCreateWithNameInContext", "LLVMDisposeModule",
    "LLVMPrintModuleToString", "LLVMParseIRInContext", "LLVMVerifyModule",
    "LLVMVoidTypeInContext", "LLVMInt1TypeInContext",
    "LLVMInt8TypeInContext", "LLVMInt32TypeInContext",
    "LLVMInt64TypeInContext", "LLVMDoubleTypeInContext",
    "LLVMPointerTypeInContext", "LLVMFunctionType",
    "LLVMAddFunction", "LLVMGetNamedFunction",
    "LLVMAppendBasicBlockInContext",
    "LLVMCreateBuilderInContext", "LLVMDisposeBuilder",
    "LLVMPositionBuilderAtEnd", "LLVMBuildRet", "LLVMBuildRetVoid",
    "LLVMBuildAdd", "LLVMBuildCall2", "LLVMBuildLoad2",
    "LLVMBuildStore", "LLVMBuildAlloca",
    "LLVMConstInt", "LLVMConstReal",
    "LLVMInitializeNativeTarget", "LLVMInitializeNativeAsmPrinter",
]
