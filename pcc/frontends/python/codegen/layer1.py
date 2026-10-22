"""Public ``L1CodeGen`` facade.

Implementation lives in the split ``*_lowering.py`` mixins plus
``layer1_entrypoints.py`` / ``layer1_init.py``.
"""
from __future__ import annotations

from pcc.frontends.python.codegen.class_gen import ClassLowering
from pcc.frontends.python.codegen.layer1_entrypoints import L1CodeGenEntrypointMixin
from pcc.frontends.python.codegen.layer1_mixins import L1CodeGenMixinStack
from pcc.frontends.python.codegen.layer1_constants import ANNOTATION_ONLY_IMPORT_MODULES, COMPILE_TIME_ONLY_IMPORT_FROMS, COMPILE_TIME_ONLY_MODULES, EXTERN_SCAFFOLD_MODULES, IR_RUNTIME_COMPAT_MODULE, TEST_FACADE_IMPORT_MODULES, UNSAFE_SCAFFOLD_MODULES
from pcc.frontends.python.codegen.user_function_lowering import _low_ir_emit_function_to_llvm, _low_ir_lower_typed_int_function
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.debug_info_lowering import DebugInfoLoweringMixin


class L1CodeGen(L1CodeGenEntrypointMixin, DebugInfoLoweringMixin, L1CodeGenMixinStack):
    # Class-local copies are required for the self-hosted stage compiler:
    # several host orchestration paths in layer1.py read these attrs directly,
    # and pcc1 does not yet reliably resolve class attrs through mixin bases.
    _EXTERN_SCAFFOLD_MODULES = EXTERN_SCAFFOLD_MODULES
    _IR_RUNTIME_COMPAT_MODULE = IR_RUNTIME_COMPAT_MODULE
    _UNSAFE_SCAFFOLD_MODULES = UNSAFE_SCAFFOLD_MODULES
    _COMPILE_TIME_ONLY_MODULES = COMPILE_TIME_ONLY_MODULES
    _COMPILE_TIME_ONLY_IMPORT_FROMS = COMPILE_TIME_ONLY_IMPORT_FROMS
    _TEST_FACADE_IMPORT_MODULES = TEST_FACADE_IMPORT_MODULES
    _ANNOTATION_ONLY_IMPORT_MODULES = ANNOTATION_ONLY_IMPORT_MODULES

    class_lowering: ClassLowering
    _inspect_signature_aliases: dict = {}
    _inspect_fullargspec_aliases: dict = {}


__all__ = ["L1CodeGen", "L1CodegenError"]
