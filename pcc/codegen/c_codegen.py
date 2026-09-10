import logging
import math
import re
import struct
from collections import ChainMap
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from itertools import count
# Route C frontend codegen through compat.ir_c. pcc.llvm_capi is the default;
# PCC_USE_LLVMLITE_C=1 selects the legacy llvmlite compatibility path.
from pcc.llvm_capi.compat import ir_c as ir
from pcc.llvm_capi.compat import add_raw_function_attribute
from pcc.c_abi_layout import (
    floating_scalar_layout,
    integer_scalar_layout,
    pointer_scalar_layout,
)
from .c_declaration_state import (
    CodegenError,
    ExternGlobalRef,
    FileScopeFunctionState,
    FileScopeObjectState,
)
from .c_layout import (
    BitFieldRef,
    StructFieldLayout,
    StructStorageSegment,
    ir_type_align as _ir_type_align_static,
    ir_type_size as _ir_type_size_static,
    is_floating_ir_type as _is_floating_ir_type_static,
    is_struct_ir_type as _is_struct_ir_type,
)
from .c_scope_context import (
    NewFunctionContext as _NewFunctionCtx,
    NewScopeContext as _NewScopeCtx,
)
from .c_types import (
    bool_t,
    cstring,
    double_t as _double,
    false_bit,
    false_byte,
    float_t as _float,
    get_ir_type,
    get_ir_type_from_names,
    get_ir_type_from_node,
    int8_t,
    int16_t,
    int32_t,
    int64_t,
    int64ptr_t,
    int128_t,
    names_to_key as _names_to_key,
    resolve_node_type as _resolve_node_type,
    true_bit,
    true_byte,
    void_t as _VOID,
    voidptr_t,
)
from .c_libc_declarations import (
    LIBC_FUNCTIONS,
    _FILE_ptr,
    _LEGACY_LIBC_FUNCTIONS,
    _libc_registry_ir_type,
    _libc_registry_signature_to_codegen,
    _size_t,
    _time_t,
    libc_registry_shadow_names,
    refresh_libc_registry_from_declarative,
)
from .c_expression_flow import CExpressionFlowMixin
from .c_control_flow import CControlFlowMixin
from .c_declaration_lowering import CDeclarationLoweringMixin
from .c_initializer_lowering import CInitializerLoweringMixin
from .c_integer_fold_contract import (
    FOLD_CONSTANT as _C_FOLD_CONSTANT,
    FOLD_POISON as _C_FOLD_POISON,
    fold_c_integer_binary as _fold_c_integer_binary,
    fold_c_integer_unary as _fold_c_integer_unary,
)
from .c_switch_flow import CSwitchFlowMixin
from .c_ssa_lowering import CSSALoweringMixin
IRBuilder = ir.IRBuilder
from .c_varargs import (
    build_report as _build_varargs_report,
    postprocess_varargs_ir as _postprocess_varargs_ir,
)
from ..ast import c_ast as c_ast

_logger = logging.getLogger("pcc.codegen")

struct_types = {}
_aggregate_namespace_counter = count(1)
_LARGE_AGGREGATE_COPY_BYTES = 128


class SemanticError(ValueError):
    pass


@dataclass(frozen=True)
class IntegerConversionDecision:
    """Shared winner for an already-promoted integer operand pair.

    ``target_order`` is a semantic integer rank for AST type keys and a bit
    width for lowered/constant values. ``source`` preserves the concrete type
    object or key selected by the decision.
    """

    target_order: int
    is_unsigned: bool
    source: str


def _decide_usual_integer_conversion(
    lhs_order: int,
    lhs_unsigned: bool,
    rhs_order: int,
    rhs_unsigned: bool,
) -> IntegerConversionDecision:
    """Choose the common integer rank/width and signedness after promotion."""
    if lhs_unsigned == rhs_unsigned:
        source = "lhs" if lhs_order >= rhs_order else "rhs"
        target_order = lhs_order if source == "lhs" else rhs_order
        return IntegerConversionDecision(target_order, lhs_unsigned, source)

    if lhs_unsigned:
        if lhs_order >= rhs_order:
            return IntegerConversionDecision(lhs_order, True, "lhs")
        return IntegerConversionDecision(rhs_order, False, "rhs")

    if rhs_order >= lhs_order:
        return IntegerConversionDecision(rhs_order, True, "rhs")
    return IntegerConversionDecision(lhs_order, False, "lhs")


class ConstIntValue(int):
    def __new__(cls, value, width, is_unsigned):
        obj = int.__new__(cls, value)
        obj.width = width
        obj.is_unsigned = is_unsigned
        return obj

    @property
    def value(self):
        return int(self)

def _is_unsigned_names(names):
    """Check if a type name list represents an unsigned type."""
    return "unsigned" in names


# Known unsigned type names (after typedef resolution)
_UNSIGNED_TYPE_NAMES = frozenset(
    {
        "char unsigned",
        "int unsigned",
        "unsigned",
        "int short unsigned",
        "short unsigned",
        "int long unsigned",
        "long unsigned",
        "long long unsigned",
        "size_t",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
    }
)


_PCC_VAARG_DECL_RE = re.compile(
    r'^declare .+@(?:"__pcc_va_arg_\d+"|__pcc_va_arg_\d+)\(.+\)\n?', re.M
)
_PCC_VAARG_CALL_RE = re.compile(
    r"^(?P<lhs>\s*%\S+)\s*=\s*call\s+"
    r"(?P<rettype>[^()\s]+)\s+(?:\([^)]*\)\s+)?"
    r'@(?:"(?P<qname>__pcc_va_arg_\d+)"|(?P<name>__pcc_va_arg_\d+))\('
    r'(?P<argtype>.+?)\s+(?P<argval>%".+?"|%\S+)\)$',
    re.M,
)


@dataclass(frozen=True)
class IRTextRewrite:
    name: str
    count: int
    reason: str

    def as_dict(self):
        return {
            "name": self.name,
            "count": self.count,
            "reason": self.reason,
        }


def postprocess_ir_text_with_report(text):
    """Apply textual lowering and return a structured rewrite report.

    Delegates to :mod:`pcc.codegen.c_varargs` so the varargs lowering is no
    longer buried in the 10k-line C codegen module (roadmap C1 split).
    Returns ``(new_text, VarargsRewriteReport)``.
    """
    rewrites = []
    new_text = _postprocess_varargs_ir(text, report=rewrites)
    return new_text, _build_varargs_report(rewrites)


def postprocess_ir_text(text):
    """Apply the minimal textual lowering that llvmlite cannot express directly."""
    return _postprocess_varargs_ir(text)


def _no_builtin_enabled() -> bool:
    """True when libc-builtin recognition must be disabled for this unit."""
    import os

    return str(os.environ.get("PCC_NO_BUILTIN", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


_AARCH64_BRANCH_PROTECTION_ATTRS = (
    '"branch-target-enforcement"',
    '"sign-return-address"="non-leaf"',
    '"sign-return-address-key"="a_key"',
)


class LLVMCodeGenerator(
    CExpressionFlowMixin,
    CControlFlowMixin,
    CDeclarationLoweringMixin,
    CInitializerLoweringMixin,
    CSwitchFlowMixin,
    CSSALoweringMixin,
    object,
):

    def __init__(self, translation_unit_name=None, emit_debug=False, pass_ctx=None):
        self.module = ir.Module()
        # Set proper data layout for struct padding/alignment
        import llvmlite.binding as _llvm

        _llvm.initialize_native_target()
        _triple = _llvm.get_default_triple()
        _tm = _llvm.Target.from_default_triple().create_target_machine()
        self.module.triple = _triple
        self.module.data_layout = str(_tm.target_data)
        # Cache a real TargetData handle so `type.get_abi_size(...)`
        # (used by the SSA pointer-difference lowering) can query element
        # sizes without parsing the layout string itself.
        self._target_data = _tm.target_data
        self.emit_debug = emit_debug
        self._di_file = None
        self._di_compile_unit = None
        self._di_scope = None
        self._di_basic_types = {}

        #
        self.builder = None
        self.global_builder: IRBuilder = ir.IRBuilder()
        self.env = ChainMap()
        self.nlabels = 0
        self.function = None
        self._function_display_name = None
        self._frame_address_marker = None
        self.in_global = True
        self._declared_libc = set()
        self._unsigned_bindings = set()  # alloca/global ids with unsigned type
        self._unsigned_pointee_bindings = set()
        self._unsigned_return_bindings = set()
        self._vla_bindings = set()
        self._expr_ir_types = {}
        self._decl_ast_types = ChainMap()
        self._typedef_ast_types = ChainMap()
        self._labels = {}
        self._label_value_tags = {}
        self._vaarg_counter = 0
        self._anon_type_counter = 0
        self._file_scope_object_states = {}
        self._file_scope_function_states = {}
        self._future_file_scope_object_ir_types = {}
        self._future_file_scope_function_ir_types = {}
        self.translation_unit_name = self._sanitize_translation_unit_name(
            translation_unit_name
        )
        self._global_compound_literal_cache = {}
        self._switch_contexts = []
        base_namespace = self.translation_unit_name or "pcc"
        self._aggregate_namespace = (
            f"{base_namespace}_{next(_aggregate_namespace_counter)}"
        )
        self._scope_id_counter = 0
        self._current_scope_id = 0

        # Pass framework context (HighTier analysis results)
        self._pass_ctx = pass_ctx

        # SEC-P1-UBSAN: opt-in `-fsanitize=undefined`-style UB trapping.
        # OFF by default -> `_maybe_ubsan_guard_*` helpers return immediately
        # and lowering is byte-for-byte identical to the un-instrumented path
        # (the property pinned by tests/security/test_c_ubsan_characterization.py).
        # `_ubsan_checks` is the enabled subset of the Clang-style check names;
        # `_ubsan_mode` selects trap vs handler (only "trap" is implemented, it
        # needs no runtime and is the only self-backend-safe mode — §4.2 of
        # docs/design/pcc-ubsan.md).
        self._ubsan_checks: set = set()
        self._ubsan_mode = "trap"

    # --- SEC-P1-UBSAN: opt-in UB-trap instrumentation --------------------
    #
    # The check set follows Clang's `-fsanitize=<group>` naming. `undefined`
    # expands to the individual arithmetic groups in scope for this slice; a
    # caller can also request a single group. See docs/design/pcc-ubsan.md §5.
    _UBSAN_GROUP_EXPANSION = {
        "undefined": (
            "signed-integer-overflow",
            "integer-divide-by-zero",
            "shift",
            "shift-base",
            "shift-exponent",
        ),
        "shift": ("shift", "shift-base", "shift-exponent"),
    }

    def configure_ubsan(self, checks, *, mode="trap"):
        """Enable opt-in UB trapping for the named Clang-style ``checks``.

        ``checks`` is an iterable of Clang ``-fsanitize`` names (or the
        ``undefined`` umbrella). ``mode`` selects ``trap`` (self-contained,
        self-backend-safe) — the only implemented fail-block mode. Passing an
        empty ``checks`` leaves the instrumentation OFF (the default), so the
        un-instrumented lowering is unchanged.
        """
        resolved: set = set()
        for name in checks or ():
            name = str(name).strip()
            if not name:
                continue
            resolved.update(self._UBSAN_GROUP_EXPANSION.get(name, (name,)))
        self._ubsan_checks = resolved
        if mode not in ("trap",):
            raise ValueError(
                f"unsupported UBSan mode {mode!r}; only 'trap' is implemented "
                f"(handler mode pulls in libubsan and is not self-backend-safe "
                f"— see docs/design/pcc-ubsan.md §4.2)"
            )
        self._ubsan_mode = mode

    def _ubsan_enabled(self, check):
        return bool(self._ubsan_checks) and check in self._ubsan_checks

    def _emit_ubsan_trap_branch(self, cond, *, kind):
        """Split the current block: on ``cond`` true, trap; else continue.

        Mirrors Clang's ``-fsanitize-trap=undefined`` fail block. The trap leg
        reuses the exact ``llvm.trap`` + ``unreachable`` idiom of
        ``_codegen_builtin_trap`` so it is self-contained (no ``libubsan``) and
        the self backend can lower it (``brk #1`` / ``ud2``). ``cond`` is the
        "UB detected" predicate (i1); lowering of the real op continues in the
        fall-through ``cont`` block.
        """
        fail_bb = self.function.append_basic_block(name=f"ubsan_{kind}_fail")
        cont_bb = self.function.append_basic_block(name=f"ubsan_{kind}_cont")
        self.builder.cbranch(cond, fail_bb, cont_bb)
        self.builder.position_at_end(fail_bb)
        trap = self._get_or_declare_intrinsic("llvm.trap", ir.VoidType(), [])
        self.builder.call(trap, [])
        self.builder.unreachable()
        self.builder.position_at_end(cont_bb)

    def _maybe_ubsan_guard_div(self, lhs, rhs, *, signed):
        """Guard a ``/`` or ``%`` against divide-by-zero and ``INT_MIN / -1``.

        No-op unless the flag is on. ``signed`` controls whether the
        overflow leg (``lhs == INT_MIN && rhs == -1``) is added — unsigned
        division cannot overflow, only its zero-divisor case is UB.
        """
        want_zero = self._ubsan_enabled("integer-divide-by-zero")
        want_ovf = signed and self._ubsan_enabled("signed-integer-overflow")
        if not (want_zero or want_ovf):
            return
        ty = rhs.type
        if not isinstance(ty, ir.IntType):
            return
        b = self.builder
        cond = None
        if want_zero:
            cond = b.icmp_signed("==", rhs, ir.Constant(ty, 0), "ubsan.divz")
        if want_ovf:
            int_min = -(1 << (ty.width - 1))
            is_min = b.icmp_signed("==", lhs, ir.Constant(ty, int_min), "ubsan.dmin")
            is_neg1 = b.icmp_signed("==", rhs, ir.Constant(ty, -1), "ubsan.dm1")
            ovf = b.and_(is_min, is_neg1, "ubsan.dovf")
            cond = ovf if cond is None else b.or_(cond, ovf, "ubsan.divchk")
        if cond is None:
            return
        self._emit_ubsan_trap_branch(cond, kind="divrem")

    def _maybe_ubsan_guard_shift(self, lhs, rhs):
        """Guard a ``<<`` / ``>>`` against out-of-range / negative amounts.

        UB when the shift amount is negative or ``>=`` the bit width of the
        promoted left operand. This is keyed on width, not signedness (an
        unsigned shift by ``>= width`` is still UB). No-op unless enabled.
        """
        if not self._ubsan_enabled("shift-exponent"):
            return
        ty = rhs.type
        if not isinstance(ty, ir.IntType):
            return
        width = lhs.type.width if isinstance(lhs.type, ir.IntType) else ty.width
        b = self.builder
        # Unsigned compare of the amount against the width catches both the
        # negative case (wraps to a huge unsigned value) and the too-large
        # case in one predicate — this is exactly Clang's check.
        cond = b.icmp_unsigned(
            ">=", rhs, ir.Constant(ty, width), "ubsan.shamt"
        )
        self._emit_ubsan_trap_branch(cond, kind="shift")

    def _maybe_ubsan_guard_arith(self, lhs, rhs, op, *, signed):
        """Guard signed ``+ - *`` against overflow via the checked intrinsic.

        Reuses ``llvm.sadd/ssub/smul.with.overflow`` — the same detection the
        ``__builtin_*_overflow`` path already declares — and branches on the
        overflow flag. Unsigned arithmetic is well-defined modular wrap and is
        never guarded by the ``undefined`` set. No-op unless enabled.
        """
        if not signed or not self._ubsan_enabled("signed-integer-overflow"):
            return
        ty = lhs.type
        if not isinstance(ty, ir.IntType) or not isinstance(rhs.type, ir.IntType):
            return
        prefix = {"+": "sadd", "-": "ssub", "*": "smul"}.get(op)
        if prefix is None:
            return
        struct_ty = ir.LiteralStructType([ty, ir.IntType(1)])
        intrinsic = self._get_or_declare_intrinsic(
            f"llvm.{prefix}.with.overflow.i{ty.width}", struct_ty, [ty, ty]
        )
        agg = self.builder.call(intrinsic, [lhs, rhs], "ubsan.ovf")
        flag = self.builder.extract_value(agg, 1, "ubsan.ovfflag")
        self._emit_ubsan_trap_branch(flag, kind="arith")

    def set_target_machine(self, triple, target_machine):
        self.module.triple = triple
        self.module.data_layout = str(target_machine.target_data)
        self._target_data = target_machine.target_data

    def _apply_aarch64_branch_protection_attributes(self) -> None:
        triple = str(self.module.triple or "")
        if not (triple.startswith("arm64-") or triple.startswith("aarch64-")):
            return
        for function in self.module.functions:
            if not getattr(function, "blocks", None):
                continue
            for attribute in _AARCH64_BRANCH_PROTECTION_ATTRS:
                add_raw_function_attribute(function, attribute)

    def define(self, name, val):
        self.env[name] = val

    def _record_decl_ast_type(self, name, node_type):
        if name:
            self._decl_ast_types[name] = node_type

    def _lookup_decl_ast_type(self, name):
        return self._decl_ast_types.get(name)

    def _record_typedef_ast_type(self, name, node_type):
        if name:
            self._typedef_ast_types[name] = node_type

    def _lookup_typedef_ast_type(self, name):
        return self._typedef_ast_types.get(name)

    @staticmethod
    def _sanitize_translation_unit_name(name):
        if not name:
            return None
        return re.sub(r"\W+", "_", name)

    @staticmethod
    def _tag_type_key(name):
        return f"__struct_{name}"

    @staticmethod
    def _enum_tag_key(name):
        return f"__enum_{name}"

    def _next_anon_struct_name(self, kind):
        self._anon_type_counter += 1
        return (
            f"__pcc_{self._aggregate_namespace}_{kind}_{self._anon_type_counter}"
        )

    def _aggregate_type_name(self, kind, name=None, scope_id=None):
        if name:
            active_scope_id = (
                self._current_scope_id if scope_id is None else scope_id
            )
            return (
                f"__pcc_{self._aggregate_namespace}_{kind}_{active_scope_id}_{name}"
            )
        return self._next_anon_struct_name(kind)

    def _identified_aggregate_type(self, kind, name, body):
        aggregate_type = self.module.context.get_identified_type(
            self._aggregate_type_name(kind, name)
        )
        if aggregate_type.is_opaque:
            from pcc.llvm_capi.compat import set_struct_body
            set_struct_body(aggregate_type, body)
        return aggregate_type

    def _is_file_scope_static(self, storage=None):
        return (
            self.translation_unit_name
            and self.in_global
            and storage
            and "static" in storage
        )

    def _has_internal_inline_linkage(self, storage=None, funcspec=None):
        return (
            self.translation_unit_name
            and self.in_global
            and funcspec
            and "inline" in funcspec
            and not storage
        )

    def _file_scope_symbol_name(self, name, storage=None, funcspec=None, linkage=None):
        if linkage == "internal" or self._is_file_scope_static(storage) or self._has_internal_inline_linkage(storage, funcspec):
            return f"__pcc_internal_{self.translation_unit_name}_{name}"
        return name

    def _static_local_symbol_name(self, name):
        if self.translation_unit_name:
            return f"__static_{self.translation_unit_name}_{self.function.name}_{name}"
        return f"__static_{self.function.name}_{name}"

    def _create_bound_global(
        self, bind_name, ir_type, symbol_name=None, external=False, storage=None
    ):
        actual_name = symbol_name or bind_name
        gv = self.module.globals.get(actual_name)
        if gv is None:
            gv = ir.GlobalVariable(self.module, ir_type, actual_name)
        if self._is_file_scope_static(storage):
            gv.linkage = "internal"
        elif not external and getattr(gv, "initializer", None) is None:
            gv.initializer = ir.Constant(ir_type, None)
        self.define(bind_name, (ir_type, gv))
        return gv

    def _decl_linkage(self, storage=None, funcspec=None, existing_state=None):
        if storage and "static" in storage:
            return "internal"
        if self._has_internal_inline_linkage(storage, funcspec):
            return "internal"
        if existing_state is not None:
            return existing_state.linkage
        return "external"

    def _effective_file_scope_symbol_name(
        self, name, storage=None, funcspec=None, existing_state=None, linkage=None
    ):
        if linkage is None:
            linkage = self._decl_linkage(
                storage, funcspec=funcspec, existing_state=existing_state
            )
        if linkage == "internal":
            if existing_state is not None and existing_state.linkage == "internal":
                return existing_state.symbol_name
            return self._file_scope_symbol_name(
                name, storage=storage, funcspec=funcspec, linkage=linkage
            )
        if existing_state is not None and existing_state.linkage == "internal":
            return existing_state.symbol_name
        return name

    def _file_scope_object_definition_kind(self, storage=None, has_initializer=False):
        if storage and "extern" in storage and not has_initializer:
            return "extern"
        if has_initializer:
            return "definition"
        return "tentative"

    def _is_incomplete_array_ir_type(self, ir_type):
        return isinstance(ir_type, ir.ArrayType) and ir_type.count == 0

    def _are_compatible_object_ir_types(self, existing_ir_type, new_ir_type):
        if str(existing_ir_type) == str(new_ir_type):
            return True
        if not (
            isinstance(existing_ir_type, ir.ArrayType)
            and isinstance(new_ir_type, ir.ArrayType)
        ):
            return False
        if str(existing_ir_type.element) != str(new_ir_type.element):
            return False
        return (
            existing_ir_type.count == 0
            or new_ir_type.count == 0
            or existing_ir_type.count == new_ir_type.count
        )

    def _merge_object_ir_types(self, existing_ir_type, new_ir_type):
        if self._is_incomplete_array_ir_type(existing_ir_type) and not self._is_incomplete_array_ir_type(new_ir_type):
            return new_ir_type
        return existing_ir_type

    def _preferred_file_scope_object_ir_type(self, name, ir_type):
        future_ir_type = self._future_file_scope_object_ir_types.get(name)
        if future_ir_type is None:
            return ir_type
        if not self._are_compatible_object_ir_types(ir_type, future_ir_type):
            return ir_type
        return self._merge_object_ir_types(ir_type, future_ir_type)

    def _preferred_file_scope_function_ir_type(self, name, function_type, has_prototype):
        if has_prototype:
            return function_type
        future_type = self._future_file_scope_function_ir_types.get(name)
        if future_type is None:
            return function_type
        if str(function_type.return_type) != str(future_type.return_type):
            return function_type
        return future_type

    def _prepare_file_scope_object(self, name, ir_type, storage=None, has_initializer=False):
        if not self.in_global or name is None:
            return None, True
        if name in self._file_scope_function_states:
            raise SemanticError(f"'{name}' redeclared as object after function declaration")
        ir_type = self._preferred_file_scope_object_ir_type(name, ir_type)

        type_key = str(ir_type)
        definition_kind = self._file_scope_object_definition_kind(
            storage, has_initializer
        )
        state = self._file_scope_object_states.get(name)
        linkage = self._decl_linkage(storage, existing_state=state)
        symbol_name = self._effective_file_scope_symbol_name(
            name, storage=storage, existing_state=state
        )

        if state is None:
            state = FileScopeObjectState(
                type_key=type_key,
                linkage=linkage,
                definition_kind=definition_kind,
                symbol_name=symbol_name,
                ir_type=ir_type,
            )
            self._file_scope_object_states[name] = state
            if definition_kind == "extern":
                self._record_extern_global(name, ir_type, storage=storage)
                return None, False
            gv = self._create_bound_global(
                name, ir_type, symbol_name=symbol_name, storage=storage
            )
            return gv, True

        if not self._are_compatible_object_ir_types(state.ir_type, ir_type):
            raise SemanticError(f"conflicting types for global '{name}'")
        merged_ir_type = self._merge_object_ir_types(state.ir_type, ir_type)
        state.ir_type = merged_ir_type
        state.type_key = str(merged_ir_type)
        if state.linkage != linkage:
            raise SemanticError(f"conflicting linkage for global '{name}'")
        if state.symbol_name != symbol_name:
            raise SemanticError(f"conflicting symbol binding for global '{name}'")

        existing = self.module.globals.get(symbol_name)
        if definition_kind == "extern":
            if existing is not None:
                self.define(name, (merged_ir_type, existing))
            else:
                self._record_extern_global(name, merged_ir_type, storage=storage)
            return None, False

        if existing is None:
            gv = self._create_bound_global(
                name, ir_type, symbol_name=symbol_name, storage=storage
            )
        else:
            gv = existing
            if definition_kind != "extern":
                try:
                    if self._is_file_scope_static(storage):
                        gv.linkage = "internal"
                    elif getattr(gv, "linkage", "") == "external":
                        gv.linkage = ""
                except Exception:
                    pass
            self.define(name, (ir_type, gv))

        if state.definition_kind == "definition":
            if definition_kind == "definition":
                raise SemanticError(f"redefinition of global '{name}'")
            return gv, False

        if state.definition_kind == "tentative":
            if definition_kind == "definition":
                state.definition_kind = "definition"
                return gv, True
            return gv, False

        state.definition_kind = definition_kind
        return gv, True

    def _register_file_scope_function(
        self, name, function_type, storage=None, funcspec=None, is_definition=False
    ):
        if not self.in_global or name is None:
            return
        if name in self._file_scope_object_states:
            raise SemanticError(f"'{name}' redeclared as function after object declaration")

        type_key = str(function_type)
        state = self._file_scope_function_states.get(name)
        linkage = self._decl_linkage(storage, funcspec=funcspec, existing_state=state)
        symbol_name = self._effective_file_scope_symbol_name(
            name,
            storage=storage,
            funcspec=funcspec,
            existing_state=state,
            linkage=linkage,
        )

        if state is None:
            self._file_scope_function_states[name] = FileScopeFunctionState(
                type_key=type_key,
                function_type=function_type,
                linkage=linkage,
                defined=is_definition,
                symbol_name=symbol_name,
            )
            return symbol_name

        if (
            is_definition
            and self._is_no_prototype_function_ir_type(function_type)
            and isinstance(state.function_type, ir.FunctionType)
            and len(getattr(state.function_type, "args", ())) > 0
        ):
            raise SemanticError(f"conflicting types for function '{name}'")

        if not self._are_compatible_function_ir_types(
            state.function_type, function_type
        ):
            raise SemanticError(f"conflicting types for function '{name}'")
        merged_function_type = self._merge_function_ir_types(
            state.function_type, function_type
        )
        state.function_type = merged_function_type
        state.type_key = str(merged_function_type)
        if state.linkage != linkage:
            # An implicit record is a *use* with no linkage intent -- the
            # ``extern`` above is what C forces an implicit declaration to
            # assume, not something the source said. pcc's own C inliner
            # lowers a wrapper body at an earlier call site, so a `static`
            # helper defined further down the file can be used before its
            # declaration is reached; treating the fabricated `extern` as
            # binding rejected translation units that cc compiles cleanly
            # (py_obj.c's pcc_gc_store_plan_commit_locked_impl, which broke
            # every pcc-C runtime archive build). A real declaration still
            # conflicts, because only implicit records are settled here.
            if state.implicit and not state.defined:
                state.linkage = linkage
                state.symbol_name = symbol_name
                state.implicit = False
            else:
                raise SemanticError(f"conflicting linkage for function '{name}'")
        if state.symbol_name != symbol_name:
            raise SemanticError(f"conflicting symbol binding for function '{name}'")
        if is_definition:
            if state.defined:
                raise SemanticError(f"redefinition of function '{name}'")
            state.defined = True
        return state.symbol_name

    @staticmethod
    def _function_arg_types_match(lhs_args, rhs_args):
        if len(lhs_args) != len(rhs_args):
            return False
        return all(str(lhs) == str(rhs) for lhs, rhs in zip(lhs_args, rhs_args))

    @staticmethod
    def _is_no_prototype_function_ir_type(function_type):
        return (
            isinstance(function_type, ir.FunctionType)
            and bool(getattr(function_type, "var_arg", False))
            and len(getattr(function_type, "args", ())) == 0
        )

    @classmethod
    def _are_compatible_function_ir_types(cls, existing_type, new_type):
        if str(existing_type.return_type) != str(new_type.return_type):
            return False

        existing_no_proto = cls._is_no_prototype_function_ir_type(existing_type)
        new_no_proto = cls._is_no_prototype_function_ir_type(new_type)
        if existing_no_proto and new_no_proto:
            return True
        if existing_no_proto or new_no_proto:
            concrete = new_type if existing_no_proto else existing_type
            return not bool(getattr(concrete, "var_arg", False))

        if bool(getattr(existing_type, "var_arg", False)) != bool(
            getattr(new_type, "var_arg", False)
        ):
            return False
        return cls._function_arg_types_match(existing_type.args, new_type.args)

    @classmethod
    def _merge_function_ir_types(cls, existing_type, new_type):
        if cls._is_no_prototype_function_ir_type(
            existing_type
        ) and not cls._is_no_prototype_function_ir_type(new_type):
            return new_type
        return existing_type

    def external_definitions(self):
        defs = []
        for name, state in self._file_scope_function_states.items():
            if state.linkage == "external" and state.defined:
                defs.append(("function", state.symbol_name, name))
        for name, state in self._file_scope_object_states.items():
            if (
                state.linkage == "external"
                and state.definition_kind in ("tentative", "definition")
            ):
                defs.append(("object", state.symbol_name, name))
        return defs

    def _is_global_extern_decl(self, node):
        return (
            self.in_global
            and node.init is None
            and node.storage
            and "extern" in node.storage
            and not isinstance(node.type, c_ast.FuncDecl)
            and node.name is not None
        )

    def _extern_decl_ir_type(self, name, node_type):
        if isinstance(node_type, c_ast.ArrayDecl):
            ir_type = self._build_array_ir_type(node_type)
        else:
            ir_type = self._resolve_ast_type(node_type)
        return self._preferred_file_scope_object_ir_type(name, ir_type)

    def _static_local_ir_type(self, node_type, init_node=None):
        if isinstance(node_type, c_ast.ArrayDecl):
            return self._build_array_ir_type(node_type, init_node=init_node)
        return self._resolve_ast_type(node_type)

    def _collect_file_scope_object_ir_types(self, ext_nodes):
        merged_types = {}
        for ext in ext_nodes:
            if not (
                isinstance(ext, c_ast.Decl)
                and ext.name is not None
                and not isinstance(ext.type, c_ast.FuncDecl)
            ):
                continue
            try:
                if isinstance(ext.type, c_ast.ArrayDecl):
                    ir_type = self._build_array_ir_type(ext.type, init_node=ext.init)
                else:
                    ir_type = self._resolve_ast_type(ext.type)
            except Exception as exc:
                _logger.debug("skipping object type for %r: %s", ext.name, exc)
                continue
            existing_ir_type = merged_types.get(ext.name)
            if existing_ir_type is None:
                merged_types[ext.name] = ir_type
                continue
            if self._are_compatible_object_ir_types(existing_ir_type, ir_type):
                merged_types[ext.name] = self._merge_object_ir_types(
                    existing_ir_type, ir_type
                )
        self._future_file_scope_object_ir_types = merged_types

    def _collect_file_scope_function_ir_types(self, ext_nodes):
        future_types = {}
        for ext in ext_nodes:
            decl = None
            if isinstance(ext, c_ast.FuncDef):
                decl = ext.decl
            elif isinstance(ext, c_ast.Decl) and isinstance(ext.type, c_ast.FuncDecl):
                decl = ext
            if decl is None or decl.name is None:
                continue
            if getattr(decl.type, "args", None) is None:
                continue
            try:
                if isinstance(ext, c_ast.FuncDef):
                    function_type, _ = self._build_future_funcdef_ir_type(ext)
                else:
                    function_type, _ = self._build_function_ir_type(decl.type)
            except Exception as exc:
                _logger.debug("skipping function type for %r: %s", decl.name, exc)
                continue
            future_types[decl.name] = function_type
        self._future_file_scope_function_ir_types = future_types

    def _record_extern_global(self, name, ir_type, storage=None):
        ir_type = self._preferred_file_scope_object_ir_type(name, ir_type)
        self.define(
            name,
            (
                ir_type,
                ExternGlobalRef(
                    self._file_scope_symbol_name(name, storage), ir_type
                ),
            ),
        )

    def _mark_unsigned(self, binding):
        """Mark a concrete IR binding as having unsigned type."""
        if binding is not None:
            try:
                binding._pcc_unsigned_binding = True
            except (AttributeError, TypeError):
                self._unsigned_bindings.add(binding)

    def _mark_unsigned_pointee(self, binding):
        """Mark a pointer/array binding whose immediate pointee is unsigned."""
        if binding is not None:
            try:
                binding._pcc_unsigned_pointee_binding = True
            except (AttributeError, TypeError):
                self._unsigned_pointee_bindings.add(binding)

    def _mark_unsigned_return(self, binding):
        """Mark a function or function-pointer binding with unsigned return."""
        if binding is not None:
            try:
                binding._pcc_unsigned_return_binding = True
            except (AttributeError, TypeError):
                self._unsigned_return_bindings.add(binding)

    def _is_unsigned_val(self, val):
        """Check if a value should use unsigned operations."""
        # Check if the value was produced by an unsigned operation
        return getattr(val, "_is_unsigned", False)

    def _is_unsigned_binding(self, binding):
        return binding is not None and (
            getattr(binding, "_pcc_unsigned_binding", False)
            or binding in self._unsigned_bindings
        )

    def _is_unsigned_pointee_binding(self, binding):
        return binding is not None and (
            getattr(binding, "_pcc_unsigned_pointee_binding", False)
            or binding in self._unsigned_pointee_bindings
        )

    def _is_unsigned_return_binding(self, binding):
        return binding is not None and (
            getattr(binding, "_pcc_unsigned_return_binding", False)
            or binding in self._unsigned_return_bindings
        )

    def _propagate_binding_tags(self, value, binding):
        """Copy signedness-related metadata from a binding to a produced value."""
        if self._is_unsigned_binding(binding):
            self._tag_unsigned(value)
        if self._is_unsigned_pointee_binding(binding):
            self._tag_unsigned_pointee(value)
        if self._is_unsigned_return_binding(binding):
            self._tag_unsigned_return(value)
        return value

    def _mark_vla_binding(self, binding):
        if binding is not None:
            try:
                binding._pcc_vla_binding = True
            except (AttributeError, TypeError):
                self._vla_bindings.add(binding)

    def _is_vla_binding(self, binding):
        return binding is not None and (
            getattr(binding, "_pcc_vla_binding", False)
            or binding in self._vla_bindings
        )

    def _collect_function_label_names(self, node):
        labels = []

        def visit(current):
            if current is None:
                return
            if isinstance(current, c_ast.Switch):
                visit(current.cond)
                return
            if isinstance(current, c_ast.Label):
                labels.append(current.name)
                visit(current.stmt)
                return
            for _child_name, child in current.children():
                if isinstance(child, list):
                    for item in child:
                        visit(item)
                else:
                    visit(child)

        visit(node)
        ordered = []
        seen = set()
        for name in labels:
            if name in seen:
                continue
            seen.add(name)
            ordered.append(name)
        return ordered

    def _label_address_constant(self, label_name, ptr_type=voidptr_t):
        tag = self._label_value_tags.get(label_name)
        if tag is None:
            raise SemanticError(f"unknown label '{label_name}'")
        return ir.Constant(int64_t, tag).inttoptr(ptr_type)

    def _ensure_label_block(self, label_name):
        block_name = f"label_{label_name}"
        if block_name in self._labels:
            return self._labels[block_name]
        block = self.builder.function.append_basic_block(block_name)
        self._labels[block_name] = block
        return block

    def _tag_unsigned(self, val):
        """Tag an IR value as unsigned."""
        try:
            val._is_unsigned = True
        except (AttributeError, TypeError):
            pass
        return val

    def _clear_unsigned(self, val):
        """Clear unsigned metadata from an IR value."""
        try:
            val._is_unsigned = False
        except (AttributeError, TypeError):
            pass
        return val

    def _tag_unsigned_pointee(self, val):
        try:
            val._pcc_unsigned_pointee = True
        except (AttributeError, TypeError):
            pass
        return val

    def _is_unsigned_pointee(self, val):
        return getattr(val, "_pcc_unsigned_pointee", False)

    def _tag_unsigned_return(self, val):
        try:
            val._pcc_unsigned_return = True
        except (AttributeError, TypeError):
            pass
        return val

    def _is_unsigned_return(self, val):
        return getattr(val, "_pcc_unsigned_return", False)

    def _set_expr_ir_type(self, node, ir_type):
        if node is not None:
            self._expr_ir_types[id(node)] = ir_type

    def _get_expr_ir_type(self, node, default=None):
        if node is None:
            return default
        return self._expr_ir_types.get(id(node), getattr(node, "ir_type", default))

    def _either_unsigned(self, lhs, rhs):
        """Check if either operand is unsigned (C promotion rules)."""
        return self._is_unsigned_val(lhs) or self._is_unsigned_val(rhs)

    def _int_to_float(self, val, target_type):
        if self._is_unsigned_val(val):
            return self.builder.uitofp(val, target_type)
        return self.builder.sitofp(val, target_type)

    def _apply_integer_target_signedness(self, val, target_unsigned):
        if not isinstance(getattr(val, "type", None), ir.IntType):
            return val
        if target_unsigned is True:
            return self._tag_unsigned(val)
        if target_unsigned is False:
            return self._clear_unsigned(val)
        return val

    def _convert_int_value(self, val, target_type, result_unsigned=None):
        if not (
            isinstance(getattr(val, "type", None), ir.IntType)
            and isinstance(target_type, ir.IntType)
        ):
            return self._implicit_convert(val, target_type)

        source_unsigned = self._is_unsigned_val(val)
        if val.type.width < target_type.width:
            if source_unsigned:
                result = self.builder.zext(val, target_type)
            else:
                result = self.builder.sext(val, target_type)
        elif val.type.width > target_type.width:
            result = self.builder.trunc(val, target_type)
        else:
            result = val

        if result_unsigned is None:
            result_unsigned = source_unsigned
        elif result_unsigned != source_unsigned and result is val:
            # Signedness is semantic metadata on an otherwise identical LLVM
            # integer type. Do not retag the original SSA value: it may be
            # reused by a later expression under its declared signedness.
            result = self.builder.or_(
                val,
                ir.Constant(target_type, 0),
                name="int.signcast",
            )
        if result_unsigned:
            return self._tag_unsigned(result)
        return self._clear_unsigned(result)

    def _integer_promotion(self, val):
        if not isinstance(getattr(val, "type", None), ir.IntType):
            return val
        if val.type.width == 1:
            return self._clear_unsigned(self.builder.zext(val, int32_t))
        if val.type.width < int32_t.width:
            return self._convert_int_value(val, int32_t, result_unsigned=False)
        return val

    def _integer_promotion_ir_type(self, ir_type):
        if not isinstance(ir_type, ir.IntType):
            return ir_type
        if ir_type.width < int32_t.width:
            return int32_t
        return ir_type

    def _usual_arithmetic_conversion_ir_type(self, lhs_type, rhs_type):
        lhs_type = self._integer_promotion_ir_type(lhs_type)
        rhs_type = self._integer_promotion_ir_type(rhs_type)

        if self._is_floating_ir_type(lhs_type) or self._is_floating_ir_type(rhs_type):
            if self._is_floating_ir_type(lhs_type) and self._is_floating_ir_type(
                rhs_type
            ):
                return self._common_float_type(lhs_type, rhs_type)
            return lhs_type if self._is_floating_ir_type(lhs_type) else rhs_type

        if isinstance(lhs_type, ir.IntType) and isinstance(rhs_type, ir.IntType):
            decision = _decide_usual_integer_conversion(
                lhs_type.width,
                False,
                rhs_type.width,
                False,
            )
            return lhs_type if decision.source == "lhs" else rhs_type

        return lhs_type

    def _decay_ir_type(self, ir_type):
        if isinstance(ir_type, ir.ArrayType):
            return ir.PointerType(ir_type.element)
        return ir_type

    def _usual_arithmetic_conversion(self, lhs, rhs):
        lhs = self._integer_promotion(lhs)
        rhs = self._integer_promotion(rhs)

        lhs_unsigned = self._is_unsigned_val(lhs)
        rhs_unsigned = self._is_unsigned_val(rhs)
        lhs_width = lhs.type.width
        rhs_width = rhs.type.width

        decision = _decide_usual_integer_conversion(
            lhs_width,
            lhs_unsigned,
            rhs_width,
            rhs_unsigned,
        )
        target_type = lhs.type if decision.source == "lhs" else rhs.type
        result_unsigned = decision.is_unsigned

        lhs = self._convert_int_value(lhs, target_type, result_unsigned)
        rhs = self._convert_int_value(rhs, target_type, result_unsigned)
        return lhs, rhs, result_unsigned

    def _shift_operand_conversion(self, lhs, rhs):
        lhs = self._integer_promotion(lhs)
        rhs = self._integer_promotion(rhs)
        if lhs.type != rhs.type:
            rhs = self._convert_int_value(
                rhs, lhs.type, result_unsigned=self._is_unsigned_val(rhs)
            )
        return lhs, rhs, self._is_unsigned_val(lhs)

    def _is_floating_ir_type(self, ir_type):
        return isinstance(ir_type, (ir.HalfType, ir.FloatType, ir.DoubleType))

    def _common_float_type(self, lhs_type, rhs_type):
        if isinstance(lhs_type, ir.DoubleType) or isinstance(rhs_type, ir.DoubleType):
            return _double
        if isinstance(lhs_type, ir.FloatType) or isinstance(rhs_type, ir.FloatType):
            return _float
        return ir.HalfType()

    def _parse_float_constant(self, raw):
        is_float32 = raw.endswith(("f", "F"))
        value = raw.rstrip("fFlL")
        if value.lower().startswith("0x") and "p" in value.lower():
            parsed = float.fromhex(value)
        else:
            parsed = float(value)
        if is_float32:
            try:
                return struct.unpack("!f", struct.pack("!f", parsed))[0]
            except OverflowError:
                return math.copysign(float("inf"), parsed)
        return parsed

    def _float_literal_ir_type(self, raw):
        if raw.endswith(("f", "F")):
            return _float
        return _double

    def _float_compare(self, op, lhs, rhs, name):
        if op == "!=":
            lhs_nan = self.builder.fcmp_unordered(
                "!=", lhs, lhs, name=f"{name}.lhsnan"
            )
            rhs_nan = self.builder.fcmp_unordered(
                "!=", rhs, rhs, name=f"{name}.rhsnan"
            )
            ordered_neq = self.builder.fcmp_ordered(
                "!=", lhs, rhs, name=f"{name}.orderedneq"
            )
            return self.builder.or_(
                self.builder.or_(lhs_nan, rhs_nan, name=f"{name}.unordered"),
                ordered_neq,
                name=name,
            )
        return self.builder.fcmp_ordered(op, lhs, rhs, name)

    def _build_fp_op(self, opname, lhs, rhs, name=""):
        """Emit floating-point arithmetic with FP contraction enabled.

        Clang enables contraction by default for local fused multiply-add
        opportunities. Preserving that hint lets LLVM form FMADD/FMLA where it
        is legal without switching the whole compiler to unsafe fast-math.
        """
        # Explicit dispatch — closed set. Avoids dynamic getattr for
        # self-host (see scripts/audit_selfhost.py).
        b = self.builder
        if opname == "fadd":   inst = b.fadd(lhs, rhs, name)
        elif opname == "fsub": inst = b.fsub(lhs, rhs, name)
        elif opname == "fmul": inst = b.fmul(lhs, rhs, name)
        elif opname == "fdiv": inst = b.fdiv(lhs, rhs, name)
        elif opname == "frem": inst = b.frem(lhs, rhs, name)
        else:
            raise ValueError(f"unknown fp op {opname!r}")
        if hasattr(inst, "flags") and "contract" not in inst.flags:
            inst.flags = list(inst.flags) + ["contract"]
        return inst

    def _fadd(self, lhs, rhs, name=""):
        return self._build_fp_op("fadd", lhs, rhs, name)

    def _fsub(self, lhs, rhs, name=""):
        return self._build_fp_op("fsub", lhs, rhs, name)

    def _fmul(self, lhs, rhs, name=""):
        return self._build_fp_op("fmul", lhs, rhs, name)

    def _fdiv(self, lhs, rhs, name=""):
        return self._build_fp_op("fdiv", lhs, rhs, name)

    def _frem(self, lhs, rhs, name=""):
        return self._build_fp_op("frem", lhs, rhs, name)

    def _ir_constant_from_value(self, ir_type, value):
        if isinstance(ir_type, ir.IntType):
            return ir.Constant(ir_type, int(value))
        if self._is_floating_ir_type(ir_type):
            value = float(value)
            if isinstance(ir_type, ir.FloatType) and (
                math.isnan(value) or math.isinf(value)
            ):
                bits = struct.unpack(">I", struct.pack(">f", value))[0]
                return ir.Constant(ir.IntType(32), bits).bitcast(ir_type)
            if isinstance(ir_type, ir.DoubleType) and (
                math.isnan(value) or math.isinf(value)
            ):
                bits = struct.unpack(">Q", struct.pack(">d", value))[0]
                return ir.Constant(ir.IntType(64), bits).bitcast(ir_type)
            return ir.Constant(ir_type, value)
        return ir.Constant(ir_type, value)

    def _safe_global_var(self, ir_type, name, external=False):
        """Create or reuse a global variable, avoiding DuplicatedNameError."""
        existing = self.module.globals.get(name)
        if existing:
            if external:
                try:
                    existing.linkage = "external"
                except Exception:
                    pass
                try:
                    existing.initializer = None
                except Exception:
                    pass
            return existing
        try:
            gv = ir.GlobalVariable(self.module, ir_type, name)
            if external:
                try:
                    gv.linkage = "external"
                except Exception:
                    pass
            else:
                gv.initializer = ir.Constant(ir_type, None)
            return gv
        except Exception:
            gv = self.module.globals.get(name) or ir.GlobalVariable(
                self.module, ir_type, self.module.get_unique_name(name)
            )
            if external:
                try:
                    gv.linkage = "external"
                except Exception:
                    pass
                try:
                    gv.initializer = None
                except Exception:
                    pass
            elif getattr(gv, "initializer", None) is None:
                gv.initializer = ir.Constant(ir_type, None)
            return gv

    def _bind_local_extern_object(self, name, ir_type):
        """Bind a block-scope extern object name without mutating file-scope storage.

        A local `extern int x;` inside a function should resolve to the visible
        file-scope object `x`, not retroactively turn a defined global into an
        undefined external declaration (gcc_torture scope-1.c).
        """
        state = self._file_scope_object_states.get(name)
        if state is not None:
            bind_type = self._merge_object_ir_types(state.ir_type, ir_type)
            state.ir_type = bind_type
            state.type_key = str(bind_type)
            symbol_name = state.symbol_name
            existing = self.module.globals.get(symbol_name)
            if existing is None:
                existing = self._safe_global_var(
                    bind_type,
                    symbol_name,
                    external=(state.definition_kind == "extern"),
                )
            self.define(name, (bind_type, existing))
            return

        existing = self.module.globals.get(name)
        if existing is None:
            existing = self._safe_global_var(ir_type, name, external=True)
        self.define(name, (ir_type, existing))

    # External C globals lazily declared on first use.
    _EXTERN_GLOBAL_VARS = {
        "stdout": voidptr_t,
        "stderr": voidptr_t,
        "stdin": voidptr_t,
        "__stdoutp": voidptr_t,
        "__stderrp": voidptr_t,
        "__stdinp": voidptr_t,
        "errno": int32_t,
    }

    def lookup(self, name):
        if not isinstance(name, str):
            name = name.name if hasattr(name, "name") else str(name)
        if name not in self.env:
            if name in LIBC_FUNCTIONS:
                self._declare_libc(name)
            elif name in self._EXTERN_GLOBAL_VARS:
                gv_type = self._EXTERN_GLOBAL_VARS[name]
                gv = self._safe_global_var(gv_type, name, external=True)
                self.define(name, (gv_type, gv))
        try:
            stored = self.env[name]
        except KeyError:
            # Support implicit function declarations (C89/C99):
            # if the name is called as a function, auto-declare it as
            # int name(...) to match traditional C behavior.
            raise SemanticError(f"use of undeclared identifier '{name}'")
        if not (isinstance(stored, tuple) and len(stored) == 2):
            return stored
        valtype, binding = stored
        if isinstance(binding, ExternGlobalRef):
            gv = self._safe_global_var(
                binding.ir_type, binding.symbol_name, external=True
            )
            self.define(name, (binding.ir_type, gv))
            return self.env[name]
        return valtype, binding

    def _declare_libc(self, name):
        """Lazily declare a libc function on first use."""
        existing = self.module.globals.get(name)
        if existing:
            self.define(name, (None, existing))
            self._declared_libc.add(name)
            return
        ret_type, param_types, var_arg = LIBC_FUNCTIONS[name]
        fnty = ir.FunctionType(ret_type, param_types, var_arg=var_arg)
        try:
            func = ir.Function(self.module, fnty, name=name)
        except Exception:
            func = self.module.globals.get(name)
        if isinstance(func, ir.Function):
            try:
                if name in ("setjmp", "_setjmp"):
                    func.attributes.add("returns_twice")
                elif name in ("longjmp", "_longjmp"):
                    func.attributes.add("noreturn")
            except Exception:
                pass
        self.define(name, (fnty, func))
        self._declared_libc.add(name)

    def _implicit_function_ir_type(self, name, call_arg_count=0):
        future_type = self._future_file_scope_function_ir_types.get(name)
        if future_type is not None:
            return future_type, future_type.return_type
        return ir.FunctionType(int32_t, [], var_arg=call_arg_count > 0), int32_t

    def _direct_call_callee(self, callee_func, call_args):
        """Materialize a concrete call ABI for old-style no-prototype calls.

        On arm64 Darwin, calling a declared-but-unprototyped function through an
        ``...``-only IR signature can misplace fixed arguments in registers. A
        call like ``strlen(format)`` inside ``f(int, char*, ...)`` then leaves
        the pointer in ``x1`` instead of the callee-expected ``x0`` and crashes
        in ``strlen``. Bitcasting the callee to a concrete fixed-parameter
        function type derived from the promoted call operands matches clang's
        old-style call ABI lowering.
        """
        if (
            not isinstance(callee_func, ir.Function)
            or not call_args
            or not self._is_no_prototype_function_ir_type(
                getattr(callee_func, "function_type", None)
            )
        ):
            return callee_func

        call_type = ir.FunctionType(
            callee_func.function_type.return_type,
            [arg.type for arg in call_args],
            var_arg=False,
        )
        return self.builder.bitcast(
            callee_func,
            call_type.as_pointer(),
            name=f"{callee_func.name}.callabi",
        )

    # GCC/clang builtins that are plain libm/libc entry points at the ABI
    # level. musl's pow() calls __builtin_fma; emitting that name verbatim
    # left an undefined ___builtin_fma symbol at link time.
    _BUILTIN_SYMBOL_ALIASES = {
        "__builtin_fma": "fma",
        "__builtin_fmaf": "fmaf",
        "__builtin_fabs": "fabs",
        "__builtin_fabsf": "fabsf",
        "__builtin_copysign": "copysign",
        "__builtin_copysignf": "copysignf",
        "__builtin_sqrt": "sqrt",
        "__builtin_sqrtf": "sqrtf",
    }

    def _declare_implicit_function(self, name, call_arg_count=0):
        alias = self._BUILTIN_SYMBOL_ALIASES.get(name)
        if alias is not None:
            name = alias
        function_type, ret_ir = self._implicit_function_ir_type(
            name, call_arg_count=call_arg_count
        )
        state = self._file_scope_function_states.get(name)
        if state is None:
            self._file_scope_function_states[name] = FileScopeFunctionState(
                type_key=str(function_type),
                function_type=function_type,
                linkage="external",
                defined=False,
                symbol_name=name,
                implicit=True,
            )
        existing = self.module.globals.get(name)
        if existing is None:
            func = ir.Function(self.module, function_type, name=name)
        else:
            func = existing
        self.define(name, (ret_ir, func))
        return ret_ir, func

    def new_label(self, name):
        self.nlabels += 1
        return f"label_{name}_{self.nlabels}"

    def new_scope(self):
        return _NewScopeCtx(self)

    def new_function(self):
        return _NewFunctionCtx(self)

    def _init_debug_info(self):
        """Initialize DWARF debug info metadata."""
        if not self.emit_debug:
            return
        filename = self.translation_unit_name or "unknown.c"
        import os
        dirname = os.getcwd()
        self._di_file = self.module.add_debug_info("DIFile", {
            "filename": filename,
            "directory": dirname,
        })
        self._di_compile_unit = self.module.add_debug_info("DICompileUnit", {
            "language": ir.DIToken("DW_LANG_C99"),
            "file": self._di_file,
            "producer": "pcc 0.0.8",
            "isOptimized": False,
            "runtimeVersion": 0,
            "emissionKind": ir.DIToken("FullDebug"),
        }, is_distinct=True)
        self._di_scope = self._di_file
        # Named metadata owns the compile-unit node directly.  Passing a list
        # asks both llvmlite and the native builder to manufacture an extra
        # ``!{!cu}`` tuple; LLVM then rejects the named operand as an invalid
        # compile unit and silently drops all DWARF during object emission.
        self.module.add_named_metadata("llvm.dbg.cu", self._di_compile_unit)
        di_flags = self.module.add_named_metadata("llvm.module.flags")
        i32 = ir.IntType(32)
        # Dwarf Version = 4
        di_flags.add(self.module.add_metadata([
            ir.Constant(i32, 7), ir.MetaDataString(self.module, "Dwarf Version"), ir.Constant(i32, 4),
        ]))
        # Debug Info Version = 3
        di_flags.add(self.module.add_metadata([
            ir.Constant(i32, 1), ir.MetaDataString(self.module, "Debug Info Version"), ir.Constant(i32, 3),
        ]))

    def _di_get_basic_type(self, ir_type):
        """Get or create a DIBasicType for the given IR type."""
        key = str(ir_type)
        if key in self._di_basic_types:
            return self._di_basic_types[key]
        if isinstance(ir_type, ir.IntType):
            name = f"int{ir_type.width}_t"
            size = ir_type.width
            encoding = ir.DIToken("DW_ATE_signed")
        elif isinstance(ir_type, ir.DoubleType):
            name, size, encoding = "double", 64, ir.DIToken("DW_ATE_float")
        elif isinstance(ir_type, ir.HalfType):
            name, size, encoding = "_Float16", 16, ir.DIToken("DW_ATE_float")
        elif isinstance(ir_type, ir.FloatType):
            name, size, encoding = "float", 32, ir.DIToken("DW_ATE_float")
        else:
            name, size, encoding = "void", 0, ir.DIToken("DW_ATE_signed")
        dt = self.module.add_debug_info("DIBasicType", {
            "name": name, "size": size, "encoding": encoding,
        })
        self._di_basic_types[key] = dt
        return dt

    def _di_create_function(self, func, funcname, ret_ir_type, line):
        """Attach DISubprogram metadata to a function."""
        if not self.emit_debug or self._di_file is None:
            return
        di_ret = self._di_get_basic_type(ret_ir_type)
        di_sub_type = self.module.add_debug_info("DISubroutineType", {
            "types": self.module.add_metadata([di_ret]),
        })
        di_sp = self.module.add_debug_info("DISubprogram", {
            "name": funcname,
            "file": self._di_file,
            "line": max(line, 1),
            "type": di_sub_type,
            "isLocal": func.linkage == "internal",
            "unit": self._di_compile_unit,
            "scope": self._di_file,
        }, is_distinct=True)
        func.set_metadata("dbg", di_sp)
        self._di_scope = di_sp

    def _di_set_location(self, inst, node):
        """Set debug location on an instruction from an AST node's coord."""
        if not self.emit_debug or self._di_scope is None:
            return
        coord = getattr(node, "coord", None)
        if coord is None:
            return
        line = getattr(coord, "line", 0) or 0
        col = getattr(coord, "column", 0) or 0
        di_loc = self.module.add_debug_info("DILocation", {
            "line": max(line, 1),
            "column": max(col, 0),
            "scope": self._di_scope,
        })
        if hasattr(inst, "set_metadata"):
            inst.set_metadata("dbg", di_loc)

    def _di_attach_function_call_locations(self, func, node):
        """Give every call in a debug function a valid source location.

        LLVM requires calls in a function carrying ``DISubprogram`` metadata
        to have ``!dbg`` locations.  The C lowerer does not yet retain a source
        node on every emitted IR instruction, so use the enclosing function's
        coordinate as the honest finite fallback instead of letting clang
        discard the entire compile unit.
        """
        if not self.emit_debug or self._di_scope is None:
            return
        for block in func.blocks:
            for inst in block.instructions:
                if getattr(inst, "opname", "") == "call":
                    self._di_set_location(inst, node)

    def generate_code(self, node):
        self._init_debug_info()
        normal = self.codegen(node)

        # for else end have no instruction
        if self.builder:
            if not self.builder.block.is_terminated:
                self.builder.ret(ir.Constant(ir.IntType(64), int(0)))

        self._apply_aarch64_branch_protection_attributes()

        pass  # empty block fixes done in IR post-processing

        return normal

    def create_entry_block_alloca(
        self,
        name,
        type_str,
        size,
        array_list=None,
        point_level=0,
        storage=None,
        symbol_name=None,
    ):

        ir_type = get_ir_type(type_str)

        if array_list is not None:
            reversed_list = reversed(array_list)
            for dim in reversed_list:
                ir_type = self._checked_array_ir_type(ir_type, dim)
            ir_type.dim_array = array_list

        if point_level != 0:
            if isinstance(ir_type, ir.VoidType):
                ir_type = int8_t  # void* -> i8*
            for level in range(point_level):
                ir_type = ir.PointerType(ir_type)

        if not self.in_global:
            ret = self._alloca_in_entry(ir_type, name)
            self.define(name, (ir_type, ret))
        else:
            ret = self._create_bound_global(
                name,
                ir_type,
                symbol_name=symbol_name or self._file_scope_symbol_name(name, storage),
                storage=storage,
            )

        return ret, ir_type

    def _alloca_in_entry(self, ir_type, name):
        if self.function is None:
            return self.builder.alloca(ir_type, size=None, name=name)
        entry_block = self.function.entry_basic_block
        current_block = self.builder.block if self.builder is not None else None
        entry_builder = ir.IRBuilder(entry_block)
        insert_before = None
        for inst in entry_block.instructions:
            if inst.opname not in ("phi", "alloca"):
                insert_before = inst
                break
        if insert_before is not None:
            entry_builder.position_before(insert_before)
        else:
            entry_builder.position_at_end(entry_block)
        ret = entry_builder.alloca(ir_type, size=None, name=name)
        if (
            self.builder is not None
            and current_block is entry_block
            and not current_block.is_terminated
        ):
            self.builder.position_at_end(current_block)
        return ret

    _codegen_dispatch = None

    def codegen(self, node):
        if node is None:
            return None, None
        owner = type(self)
        disp = owner.__dict__.get("_codegen_dispatch")
        if disp is None:
            disp = {}
            for base in owner.__mro__:
                for name, fn in base.__dict__.items():
                    if not name.startswith("codegen_"):
                        continue
                    if not callable(fn):
                        continue
                    disp.setdefault(name[len("codegen_"):], fn)
            owner._codegen_dispatch = disp
        fn = disp.get(type(node).__name__)
        if fn is None:
            return None, None
        return fn(self, node)

    def codegen_FileAST(self, node):
        # Collect names of functions that have definitions (FuncDef)
        funcdef_names = set()
        for ext in node.ext:
            if isinstance(ext, c_ast.FuncDef) and ext.decl:
                funcdef_names.add(ext.decl.name)
        self._funcdef_names = funcdef_names

        # Two-pass: first types/typedefs, then everything else
        pass1 = set()
        for i, ext in enumerate(node.ext):
            is_type_def = False
            if isinstance(ext, c_ast.Decl):
                if ext.name is None and isinstance(
                    ext.type, (c_ast.Struct, c_ast.Union, c_ast.Enum)
                ):
                    is_type_def = True
                elif ext.name is None and isinstance(ext.type, c_ast.TypeDecl) and isinstance(
                    ext.type.type, (c_ast.Struct, c_ast.Union)
                ):
                    is_type_def = True
            elif isinstance(ext, c_ast.Typedef):
                is_type_def = True
            if is_type_def:
                self.codegen(ext)
                pass1.add(i)
        remaining_exts = [ext for i, ext in enumerate(node.ext) if i not in pass1]
        self._collect_file_scope_function_ir_types(remaining_exts)
        self._collect_file_scope_object_ir_types(remaining_exts)
        for i, ext in enumerate(node.ext):
            if i not in pass1:
                self.codegen(ext)

    _escape_map = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        "\\": "\\",
        "0": "\0",
        "'": "'",
        '"': '"',
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "v": "\v",
    }

    def _process_escapes(self, s):
        """Process C escape sequences in a string."""
        result = []
        i = 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s):
                if s[i + 1] == "x":
                    j = i + 2
                    hex_digits = []
                    while j < len(s) and s[j] in "0123456789abcdefABCDEF":
                        hex_digits.append(s[j])
                        j += 1
                    if hex_digits:
                        result.append(chr(int("".join(hex_digits), 16) & 0xFF))
                        i = j
                        continue
                if s[i + 1] in "01234567":
                    j = i + 1
                    oct_digits = []
                    while j < len(s) and len(oct_digits) < 3 and s[j] in "01234567":
                        oct_digits.append(s[j])
                        j += 1
                    result.append(chr(int("".join(oct_digits), 8) & 0xFF))
                    i = j
                    continue
                esc = self._escape_map.get(s[i + 1])
                if esc is not None:
                    result.append(esc)
                    i += 2
                    continue
            result.append(s[i])
            i += 1
        return "".join(result)

    @staticmethod
    def _string_bytes(s):
        return bytearray((ord(ch) & 0xFF) for ch in s)

    @staticmethod
    def _is_string_constant(node):
        return isinstance(node, c_ast.Constant) and getattr(node, "type", None) in (
            "string",
            "wstring",
        )

    @staticmethod
    def _is_wide_string_constant(node):
        return isinstance(node, c_ast.Constant) and (
            getattr(node, "type", None) == "wstring"
            or (
                getattr(node, "type", None) == "string"
                and str(getattr(node, "value", "")).startswith('L"')
            )
        )

    def _string_literal_content(self, raw, *, wide=False):
        if wide or raw.startswith('L"'):
            body = raw[2:-1]
        else:
            body = raw[1:-1]
        return self._process_escapes(body)

    def _string_literal_data(self, node):
        raw = node.value
        wide = self._is_wide_string_constant(node)
        content = self._string_literal_content(raw, wide=wide)
        if wide:
            return [ord(ch) for ch in content] + [0]
        return list(self._string_bytes(content + "\00"))

    def _char_constant_value(self, raw):
        if raw and raw[:2] in {"L'", "u'", "U'"} and raw.endswith("'"):
            raw = raw[1:]
        if not raw or len(raw) < 2 or raw[0] != "'" or raw[-1] != "'":
            return 0
        processed = self._process_escapes(raw[1:-1])
        if not processed:
            return 0
        value = 0
        for ch in processed:
            value = (value << 8) | (ord(ch) & 0xFF)
        return value

    def codegen_Constant(self, node):

        if node.type == "int":
            # Support hex (0xFF), octal (077), and decimal literals
            raw = node.value
            raw_lower = raw.lower()
            has_unsigned_suffix = "u" in raw_lower
            has_long_suffix = "l" in raw_lower
            val_str = raw.rstrip("uUlL")
            if val_str.startswith("0x") or val_str.startswith("0X"):
                int_val = int(val_str, 16)
                is_non_decimal = True
            elif val_str.startswith("0") and len(val_str) > 1 and val_str[1:].isdigit():
                int_val = int(val_str, 8)
                is_non_decimal = True
            else:
                int_val = int(val_str)
                is_non_decimal = False

            if has_long_suffix or int_val > 0xFFFFFFFF:
                ir_type = int64_t
                is_unsigned = has_unsigned_suffix
            elif has_unsigned_suffix:
                ir_type = int32_t
                is_unsigned = True
            elif is_non_decimal and int_val > 0x7FFFFFFF:
                ir_type = int32_t
                is_unsigned = True
            elif int_val > 0x7FFFFFFF:
                ir_type = int64_t
                is_unsigned = False
            else:
                ir_type = int32_t
                is_unsigned = False

            result = ir.values.Constant(ir_type, int_val)
            if is_unsigned:
                self._tag_unsigned(result)
            return result, None
        elif node.type == "char":
            # char constant like 'a' -> i8
            is_wide_char = str(getattr(node, "value", "")).startswith(("L'", "u'", "U'"))
            ir_type = int32_t if is_wide_char else int8_t
            mask = 0xFFFFFFFF if is_wide_char else 0xFF
            return (
                ir.values.Constant(
                    ir_type, self._char_constant_value(node.value) & mask
                ),
                None,
            )
        elif node.type in ("string", "wstring"):
            data = self._string_literal_data(node)
            if self._is_wide_string_constant(node):
                array = ir.ArrayType(int32_t, len(data))
                tmp = ir.values.Constant(
                    array, [ir.Constant(int32_t, cp) for cp in data]
                )
                return tmp, None
            array = ir.ArrayType(ir.IntType(8), len(data))
            tmp = ir.values.Constant(array, data)
            return tmp, None
        elif node.type == "nullptr_t":
            return ir.Constant(ir.PointerType(ir.IntType(8)), None), None
        else:
            ir_type = self._float_literal_ir_type(node.value)
            return (
                ir.values.Constant(ir_type, self._parse_float_constant(node.value)),
                None,
            )

    def codegen_StaticAssert(self, node):
        # Evaluate the condition as a compile-time constant
        try:
            cond_val, _ = self.codegen(node.cond)
            if not isinstance(cond_val, ir.Constant):
                raise SemanticError(
                    "_Static_assert condition is not an integer constant expression"
                )
            val = self._constant_raw_value(cond_val)
            if not isinstance(val, int):
                raise SemanticError(
                    "_Static_assert condition is not an integer constant expression"
                )
            if val == 0:
                msg = node.message
                if hasattr(msg, 'value'):
                    msg = msg.value.strip('"')
                msg = msg or "static assertion failed"
                raise SemanticError(
                    f"_Static_assert failed: {msg}"
                )
        except SemanticError:
            raise
        except Exception as exc:
            raise SemanticError(
                "unable to evaluate _Static_assert condition: " + str(exc)
            ) from exc
        return None, None

    def codegen_Alignas(self, node):
        # Alignment specifier is handled at declaration level; as a standalone
        # expression it's a no-op in codegen.
        return None, None

    def codegen_Assignment(self, node):

        aggregate_copy = self._try_codegen_large_aggregate_assignment(node)
        if aggregate_copy is not None:
            return aggregate_copy

        lv, lv_addr = self.codegen(node.lvalue)
        rv, _ = self.codegen(node.rvalue)
        if lv is None or rv is None:
            return ir.Constant(int64_t, 0), None
        result = None
        is_bitfield = isinstance(lv_addr, BitFieldRef)

        dispatch_type_double = 1
        dispatch_type_int = 0
        dispatch_dict = {
            ("+=", dispatch_type_double): self._fadd,
            ("+=", dispatch_type_int): self.builder.add,
            ("-=", dispatch_type_double): self._fsub,
            ("-=", dispatch_type_int): self.builder.sub,
            ("*=", dispatch_type_double): self._fmul,
            ("*=", dispatch_type_int): self.builder.mul,
            ("/=", dispatch_type_double): self._fdiv,
            ("/=", dispatch_type_int): self.builder.sdiv,
            ("%=", dispatch_type_int): self.builder.srem,
            ("%=", dispatch_type_double): self._frem,
            ("<<=", dispatch_type_int): self.builder.shl,
            (">>=", dispatch_type_int): self.builder.ashr,
            ("&=", dispatch_type_int): self.builder.and_,
            ("|=", dispatch_type_int): self.builder.or_,
            ("^=", dispatch_type_int): self.builder.xor,
        }
        is_unsigned = False
        # Promote mismatched types before compound assignment
        if isinstance(lv.type, ir.IntType) and isinstance(rv.type, ir.IntType):
            if node.op in ("<<=", ">>="):
                lv, rv, is_unsigned = self._shift_operand_conversion(lv, rv)
            else:
                lv, rv, is_unsigned = self._usual_arithmetic_conversion(lv, rv)
            dispatch_type = dispatch_type_int
        elif isinstance(lv.type, ir.IntType) and self._is_floating_ir_type(rv.type):
            lv = self._implicit_convert(lv, rv.type)
            dispatch_type = dispatch_type_double
        elif self._is_floating_ir_type(lv.type) and isinstance(rv.type, ir.IntType):
            rv = self._implicit_convert(rv, lv.type)
            dispatch_type = dispatch_type_double
        elif self._is_floating_ir_type(lv.type) and self._is_floating_ir_type(rv.type):
            if lv.type != rv.type:
                target = self._common_float_type(lv.type, rv.type)
                lv = self._implicit_convert(lv, target)
                rv = self._implicit_convert(rv, target)
            dispatch_type = dispatch_type_double
        else:
            dispatch_type = dispatch_type_double
        dispatch = (node.op, dispatch_type)
        handle = dispatch_dict.get(dispatch)
        # Override to unsigned for /= %= >>= when operands are unsigned
        if dispatch_type == dispatch_type_int and is_unsigned:
            if node.op == "/=":
                handle = self.builder.udiv
            elif node.op == "%=":
                handle = self.builder.urem
            elif node.op == ">>=":
                handle = self.builder.lshr

        if node.op == "=":
            # Type coercion: match rv to the target's pointee type
            if is_bitfield:
                target_type = lv_addr.semantic_ir_type
            elif lv_addr and hasattr(lv_addr.type, "pointee"):
                target_type = lv_addr.type.pointee
            else:
                target_type = lv.type
            if rv.type != target_type:
                target_unsigned = None
                if is_bitfield:
                    target_unsigned = bool(getattr(lv_addr, "is_unsigned", False))
                elif self._is_unsigned_binding(lv_addr):
                    target_unsigned = True
                rv = self._implicit_convert(
                    rv,
                    target_type,
                    target_unsigned=target_unsigned,
                )
            if is_bitfield:
                self._store_bitfield(rv, lv_addr)
                rv = self._load_bitfield(lv_addr)
            else:
                self._safe_store(rv, lv_addr)
            return rv, lv_addr  # return value for chained assignment
        else:
            # Pointer compound assignment: p += n, p -= n
            if isinstance(lv.type, ir.PointerType) and isinstance(rv.type, ir.IntType):
                rv = self._integer_promotion(rv)
                rv = self._convert_int_value(rv, int64_t, result_unsigned=False)
                if node.op == "+=":
                    addresult = self.builder.gep(lv, [rv], name="ptradd")
                elif node.op == "-=":
                    neg = self.builder.neg(rv, "neg")
                    addresult = self.builder.gep(lv, [neg], name="ptrsub")
                else:
                    addresult = handle(lv, rv, "addtmp")
            else:
                # SEC-P1-UBSAN guards for compound assignment (no-op if off).
                # Only the integer-typed forms carry arithmetic UB in scope.
                if dispatch_type == dispatch_type_int:
                    if node.op in ("/=", "%="):
                        self._maybe_ubsan_guard_div(lv, rv, signed=not is_unsigned)
                    elif node.op in ("<<=", ">>="):
                        self._maybe_ubsan_guard_shift(lv, rv)
                    elif node.op in ("+=", "-=", "*="):
                        self._maybe_ubsan_guard_arith(
                            lv, rv, node.op[0], signed=not is_unsigned
                        )
                addresult = handle(lv, rv, "addtmp")
            if dispatch_type == dispatch_type_int and is_unsigned:
                self._tag_unsigned(addresult)
            if is_bitfield:
                self._store_bitfield(addresult, lv_addr)
                return self._load_bitfield(lv_addr), lv_addr
            self._safe_store(addresult, lv_addr)
            return addresult, lv_addr

    @staticmethod
    def _is_direct_addressable_aggregate_expr(node):
        """Whether *node* can yield an aggregate address without side effects.

        Keep this deliberately narrow.  Falling back after evaluating a more
        general pointer expression could evaluate calls/increments twice.
        Direct aggregate objects and ``*pointer_name`` cover the common C
        struct-copy forms while preserving the existing path for everything
        else.
        """
        return isinstance(node, c_ast.ID) or (
            isinstance(node, c_ast.UnaryOp)
            and node.op == "*"
            and isinstance(node.expr, c_ast.ID)
        )

    def _direct_aggregate_address(self, node):
        if isinstance(node, c_ast.ID):
            aggregate_type, binding = self.lookup(node.name)
            if (
                self._is_aggregate_ir_type(aggregate_type)
                and isinstance(getattr(binding, "type", None), ir.PointerType)
                and self._same_ir_type_semantics(
                    binding.type.pointee, aggregate_type
                )
            ):
                return binding, aggregate_type
            return None

        pointer_value, _ = self.codegen(node.expr)
        pointer_type = getattr(pointer_value, "type", None)
        if (
            isinstance(pointer_type, ir.PointerType)
            and self._is_aggregate_ir_type(pointer_type.pointee)
        ):
            return pointer_value, pointer_type.pointee
        return None

    def _try_codegen_large_aggregate_assignment(self, node):
        """Lower large addressable struct/union assignment as one memmove.

        Materializing a value such as ``struct { int x[4096]; }`` turns a
        source-level assignment into thousands of aggregate SSA operations.
        LLVM's SelectionDAG then spends minutes combining MERGE_VALUES nodes.
        A memory copy is the natural C representation and also preserves the
        exact object representation (including padding).  ``memmove`` keeps
        exact-source/destination aliasing and overlapping subobjects safe.
        """
        if node.op != "=":
            return None
        if not self._is_direct_addressable_aggregate_expr(
            node.lvalue
        ) or not self._is_direct_addressable_aggregate_expr(node.rvalue):
            return None

        dest = self._direct_aggregate_address(node.lvalue)
        source = self._direct_aggregate_address(node.rvalue)
        if dest is None or source is None:
            return None
        dest_addr, dest_type = dest
        source_addr, source_type = source
        if not self._same_ir_type_semantics(dest_type, source_type):
            return None

        copy_size = self._ir_type_size(dest_type)
        if copy_size < _LARGE_AGGREGATE_COPY_BYTES:
            return None

        dest_ptr = (
            dest_addr
            if dest_addr.type == voidptr_t
            else self.builder.bitcast(dest_addr, voidptr_t, name="aggcopy.dst")
        )
        source_ptr = (
            source_addr
            if source_addr.type == voidptr_t
            else self.builder.bitcast(source_addr, voidptr_t, name="aggcopy.src")
        )
        memmove = self._get_or_declare_intrinsic(
            "llvm.memmove.p0.p0.i64",
            ir.VoidType(),
            [voidptr_t, voidptr_t, int64_t, bool_t],
        )
        self.builder.call(
            memmove,
            [
                dest_ptr,
                source_ptr,
                ir.Constant(int64_t, copy_size),
                false_bit,
            ],
            name="aggcopy",
        )

        # C assignment expressions yield the assigned value.  In the common
        # statement form this load is dead and LLVM removes it; chained/used
        # assignments still receive the required value semantics.
        return self._safe_load(dest_addr, name="aggcopy.value"), dest_addr

    def codegen_UnaryOp(self, node):

        result = None
        result_ptr = None

        if node.op in ("p++", "p--", "++", "--"):
            lv, lv_addr = self.codegen(node.expr)
            if lv is None:
                return ir.Constant(int64_t, 0), None
            is_post = node.op.startswith("p")
            is_inc = "+" in node.op
            if isinstance(lv.type, ir.PointerType):
                delta = ir.Constant(int64_t, 1 if is_inc else -1)
                new_val = self.builder.gep(lv, [delta], name="ptrincdec")
            elif self._is_floating_ir_type(lv.type):
                one = ir.Constant(lv.type, 1.0)
                new_val = (
                    self._fadd(lv, one, "inc")
                    if is_inc
                    else self._fsub(lv, one, "dec")
                )
            else:
                one = ir.Constant(lv.type, 1)
                new_val = (
                    self.builder.add(lv, one, "inc")
                    if is_inc
                    else self.builder.sub(lv, one, "dec")
                )
                if self._is_unsigned_val(lv):
                    self._tag_unsigned(new_val)
            if isinstance(lv_addr, BitFieldRef):
                self._store_bitfield(new_val, lv_addr)
                result = lv if is_post else self._load_bitfield(lv_addr)
            else:
                self._safe_store(new_val, lv_addr)
                result = lv if is_post else new_val

        elif node.op == "*":
            if (
                isinstance(node.expr, c_ast.Cast)
                and isinstance(node.expr.expr, c_ast.FuncCall)
                and isinstance(node.expr.expr.name, c_ast.ID)
                and node.expr.expr.name.name == "__builtin_va_arg"
            ):
                target_ptr_type = self._resolve_ast_type(node.expr.to_type.type)
                va_args = (
                    node.expr.expr.args.exprs if node.expr.expr.args is not None else []
                )
                if isinstance(target_ptr_type, ir.PointerType) and va_args:
                    ap_addr = self._builtin_va_list_storage(va_args[0])
                    if ap_addr is not None:
                        aggregate_type = target_ptr_type.pointee
                        self._vaarg_counter += 1
                        if self._is_aggregate_ir_type(aggregate_type):
                            result, result_ptr = self._codegen_aggregate_va_arg(
                                ap_addr, aggregate_type
                            )
                            if result is not None:
                                return result, result_ptr
                        name = f"__pcc_va_arg_{self._vaarg_counter}"
                        placeholder = self.module.globals.get(name)
                        if placeholder is None:
                            placeholder = ir.Function(
                                self.module,
                                ir.FunctionType(
                                    aggregate_type, [ap_addr.type]
                                ),
                                name=name,
                            )
                        result = self.builder.call(
                            placeholder,
                            [ap_addr],
                            name=f"vaargtmp.{self._vaarg_counter}",
                        )
                        return result, None
            name_ir, name_ptr = self.codegen(node.expr)
            if name_ptr is None and isinstance(name_ir.type, ir.ArrayType):
                result_ptr = self._decay_array_value_to_pointer(name_ir, "derefarray")
            else:
                result_ptr = name_ir
            if isinstance(getattr(result_ptr, "type", None), ir.PointerType) and isinstance(
                result_ptr.type.pointee, ir.ArrayType
            ):
                self._set_expr_ir_type(node, result_ptr.type.pointee)
                return result_ptr, result_ptr
            result = self._safe_load(result_ptr)
            if self._is_unsigned_pointee(name_ir) or self._is_unsigned_pointee(
                result_ptr
            ):
                self._tag_unsigned(result)

        elif node.op == "&":
            if isinstance(node.expr, c_ast.StructRef) and self._is_offsetof_like_structref(
                node.expr
            ):
                try:
                    offset, field_type = self._eval_offsetof_structref(node.expr)
                except CodegenError:
                    pass
                else:
                    result = self.builder.inttoptr(
                        ir.Constant(int64_t, offset),
                        ir.PointerType(field_type),
                        name="offsetofptr",
                    )
                    self._set_expr_ir_type(node, result.type)
                    return result, None
            name_ir, name_ptr = self.codegen(node.expr)
            if name_ptr is None:
                # Functions are already first-class pointers in LLVM IR.
                # Taking their address should preserve the function symbol,
                # not turn it into a null pointer.
                result = name_ir
                result_ptr = None
            else:
                result_ptr = name_ptr
                result = result_ptr
            if self._is_unsigned_binding(result_ptr):
                self._tag_unsigned_pointee(result)
            if self._is_unsigned_return_binding(result_ptr):
                self._tag_unsigned_return(result)

        elif node.op == "+":
            operand, _ = self.codegen(node.expr)
            if isinstance(operand.type, ir.IntType):
                operand = self._integer_promotion(operand)
            result = operand  # unary plus is a no-op

        elif node.op == "-":
            operand, _ = self.codegen(node.expr)
            if isinstance(operand.type, ir.IntType):
                operand = self._integer_promotion(operand)
                result = self.builder.neg(operand, "negtmp")
                if self._is_unsigned_val(operand):
                    self._tag_unsigned(result)
            else:
                result = self.builder.fneg(operand, "negtmp")

        elif node.op == "!":
            operand, _ = self.codegen(node.expr)
            if isinstance(operand.type, ir.PointerType):
                null = ir.Constant(operand.type, None)
                cmp = self.builder.icmp_unsigned("==", operand, null, "nottmp")
                result = self.builder.zext(cmp, int64_t, "notres")
            elif isinstance(operand.type, ir.IntType):
                cmp = self.builder.icmp_signed(
                    "==", operand, ir.Constant(operand.type, 0), "nottmp"
                )
                result = self.builder.zext(cmp, int64_t, "notres")
            else:
                cmp = self.builder.fcmp_ordered(
                    "==", operand, ir.Constant(operand.type, 0.0), "nottmp"
                )
                result = self.builder.zext(cmp, int64_t, "notres")

        elif node.op == "~":
            operand, _ = self.codegen(node.expr)
            if isinstance(operand.type, ir.IntType):
                operand = self._integer_promotion(operand)
            result = self.builder.not_(operand, "invtmp")
            if self._is_unsigned_val(operand):
                self._tag_unsigned(result)

        elif node.op == "sizeof":
            result = self._codegen_sizeof(node.expr)

        elif node.op in ("_Alignof", "__alignof", "__alignof__"):
            result = self._codegen_alignof(node.expr)

        elif node.op == "&&" and isinstance(node.expr, c_ast.ID):
            result = self._label_address_constant(node.expr.name, voidptr_t)
            self._set_expr_ir_type(node, voidptr_t)

        return result, result_ptr

    def _codegen_sizeof(self, expr):
        """Return sizeof as an i64 constant (always unsigned in C)."""
        if isinstance(expr, c_ast.Typename):
            ir_t = self._resolve_ast_type(expr.type)
            size = self._ir_type_size(ir_t)
        elif self._is_string_constant(expr):
            size = len(self._string_literal_data(expr))
        elif isinstance(expr, c_ast.ID):
            try:
                ir_type, _ = self.lookup(expr.name)
            except SemanticError:
                decl_type = self._lookup_decl_ast_type(expr.name)
                if decl_type is None:
                    raise
                ir_type = self._resolve_ast_type(decl_type)
            size = self._ir_type_size(ir_type)
        else:
            semantic_type = self._infer_sizeof_operand_ir_type(expr)
            size = self._ir_type_size(semantic_type)
        result = ir.Constant(int64_t, size)
        return self._tag_unsigned(result)

    def _codegen_alignof(self, expr):
        """Return alignment as an i64 constant (always unsigned in C)."""
        if isinstance(expr, c_ast.Typename):
            ir_t = self._resolve_ast_type(expr.type)
        elif self._is_string_constant(expr):
            ir_t = self._get_ir_type("int")
        elif isinstance(expr, c_ast.ID):
            try:
                ir_type, _ = self.lookup(expr.name)
            except SemanticError:
                decl_type = self._lookup_decl_ast_type(expr.name)
                if decl_type is None:
                    raise
                ir_type = self._resolve_ast_type(decl_type)
            ir_t = ir_type
        else:
            ir_t = self._infer_sizeof_operand_ir_type(expr)
        result = ir.Constant(int64_t, self._ir_type_align(ir_t))
        return self._tag_unsigned(result)

    def _resolve_type_str(self, type_str, depth=0):
        """Resolve typedef'd type names to their base type string."""
        if depth > 10:
            return type_str  # prevent infinite recursion
        if isinstance(type_str, list):
            type_str = type_str[0] if len(type_str) == 1 else type_str
        if isinstance(type_str, list):
            return type_str  # multi-word type, not a typedef
        key = f"__typedef_{type_str}"
        if key in self.env:
            resolved = self.env[key]
            if isinstance(resolved, str):
                # Could be a __struct_ reference or a base type name
                if resolved.startswith("__struct_"):
                    if resolved in self.env:
                        return self.env[resolved][0]
                    return int8_t  # opaque
                # Recursively resolve further typedefs
                return self._resolve_type_str(resolved, depth + 1)
            if isinstance(resolved, ir.Type):
                return resolved
            # resolved is a list — recursively resolve single-element lists
            if isinstance(resolved, list) and len(resolved) == 1:
                return self._resolve_type_str(resolved[0], depth + 1)
            return resolved
        return type_str

    def _get_ir_type(self, type_str):
        """Get IR type, resolving typedefs."""
        resolved = self._resolve_type_str(type_str)
        if isinstance(resolved, ir.Type):
            return resolved
        return get_ir_type(resolved)

    def _is_unsigned_type_names(self, type_str):
        """Check if a type name list resolves to an unsigned type."""
        if isinstance(type_str, list):
            if _is_unsigned_names(type_str):
                return True
            # Single-element list: check typedef chain
            if len(type_str) == 1:
                return self._is_unsigned_type_names(type_str[0])
            s = " ".join(sorted(type_str))
            return s in _UNSIGNED_TYPE_NAMES
        # String: check typedef chain
        key = f"__typedef_{type_str}"
        if key in self.env:
            resolved = self.env[key]
            if isinstance(resolved, list):
                return self._is_unsigned_type_names(resolved)
            if isinstance(resolved, str):
                return self._is_unsigned_type_names(resolved)
        return type_str in _UNSIGNED_TYPE_NAMES or type_str == "size_t"

    def _is_unsigned_scalar_decl_type(self, node_type):
        if not isinstance(node_type, c_ast.TypeDecl):
            return False
        inner = node_type.type
        if not isinstance(inner, c_ast.IdentifierType):
            return False
        return self._is_unsigned_type_names(inner.names)

    def _enum_value_range(self, enum_node):
        values = getattr(enum_node, "values", None)
        if values is None and getattr(enum_node, "name", None):
            return self.env.get(self._enum_tag_key(enum_node.name))
        if values is None:
            return None
        enumerators = getattr(values, "enumerators", None) or []
        if not enumerators:
            return None

        current = 0
        min_value = None
        max_value = None
        for enumerator in enumerators:
            if enumerator.value is not None:
                current = int(self._eval_const_expr(enumerator.value))
            if min_value is None or current < min_value:
                min_value = current
            if max_value is None or current > max_value:
                max_value = current
            current += 1
        return min_value, max_value

    def _bitfield_decl_is_unsigned(self, node_type, bit_width):
        if self._is_unsigned_scalar_decl_type(node_type):
            return True
        if not isinstance(node_type, c_ast.TypeDecl):
            return False
        inner = node_type.type
        if not isinstance(inner, c_ast.Enum):
            return False
        enum_range = self._enum_value_range(inner)
        if enum_range is None:
            return False
        min_value, max_value = enum_range
        if min_value < 0 or bit_width <= 0:
            return False
        return max_value >= (1 << (bit_width - 1))

    def _has_unsigned_scalar_pointee(self, node_type):
        if isinstance(node_type, (c_ast.ArrayDecl, c_ast.PtrDecl)):
            child = node_type.type
            if self._is_unsigned_scalar_decl_type(child):
                return True
            return self._has_unsigned_scalar_pointee(child)
        return False

    def _func_decl_returns_unsigned(self, node_type):
        return isinstance(
            node_type, c_ast.FuncDecl
        ) and self._is_unsigned_scalar_decl_type(node_type.type)

    def _tag_value_from_decl_type(self, value, decl_type):
        if value is None:
            return value
        if isinstance(getattr(value, "type", None), ir.IntType):
            if self._is_unsigned_scalar_decl_type(decl_type):
                self._tag_unsigned(value)
            elif isinstance(decl_type, c_ast.TypeDecl):
                self._clear_unsigned(value)
        if self._has_unsigned_scalar_pointee(decl_type) and isinstance(
            getattr(value, "type", None), ir.PointerType
        ):
            self._tag_unsigned_pointee(value)
        if (
            isinstance(decl_type, c_ast.PtrDecl)
            and self._func_decl_returns_unsigned(decl_type.type)
            and isinstance(getattr(value, "type", None), ir.PointerType)
        ):
            self._tag_unsigned_return(value)
        return value

    def _checked_array_ir_type(self, element_ir_type, dim):
        """Build an array type while matching clang's oversized-object rejection."""
        if dim < 0:
            raise SemanticError("array size is negative")
        element_size = self._ir_type_size(element_ir_type)
        total_size = element_size * int(dim)
        if total_size >= (1 << 61):
            raise SemanticError(f"array is too large ({int(dim)} elements)")
        return ir.ArrayType(element_ir_type, dim)

    def _resolve_param_type(self, param):
        """Resolve a function parameter type, handling typedefs and pointers."""
        node_type = param.type if hasattr(param, "type") else param
        if isinstance(node_type, c_ast.ArrayDecl):
            inner = node_type.type
            if isinstance(inner, c_ast.ArrayDecl):
                return ir.PointerType(self._build_array_ir_type(inner))
            elem_ir_type = self._resolve_ast_type(inner)
            if isinstance(elem_ir_type, ir.VoidType):
                elem_ir_type = int8_t
            return ir.PointerType(elem_ir_type)
        if isinstance(node_type, c_ast.TypeDecl) and isinstance(
            node_type.type, c_ast.FuncDecl
        ):
            return self._build_func_ptr_type(node_type.type)
        t = self._resolve_ast_type(node_type)
        if isinstance(t, ir.ArrayType):
            return ir.PointerType(t.element)
        if isinstance(t, ir.FunctionType):
            return t.as_pointer()
        if isinstance(t, ir.VoidType):
            return None  # void params mean "no params" in C
        return t

    def _emit_vla_param_bound_side_effects(self, node_type):
        current = node_type
        while isinstance(current, c_ast.ArrayDecl):
            dim = current.dim
            if dim is not None and not isinstance(dim, c_ast.Constant):
                self.codegen(dim)
            current = current.type

    def _resolve_ast_type(self, node_type):
        """Recursively resolve an AST type to IR type, with typedef support."""
        if isinstance(node_type, c_ast.Struct):
            return self.codegen_Struct(node_type)
        elif isinstance(node_type, c_ast.Union):
            return self.codegen_Union(node_type)
        elif isinstance(node_type, c_ast.Enum):
            self.codegen_Enum(node_type)
            return int32_t
        if isinstance(node_type, c_ast.PtrDecl):
            inner = node_type.type
            if isinstance(inner, c_ast.FuncDecl):
                return self._build_func_ptr_type(inner)
            pointee = self._resolve_ast_type(inner)
            if isinstance(pointee, ir.VoidType):
                return voidptr_t
            return ir.PointerType(pointee)
        elif isinstance(node_type, c_ast.TypeDecl):
            if isinstance(node_type.type, c_ast.IdentifierType):
                return self._get_ir_type(node_type.type.names)
            elif isinstance(node_type.type, c_ast.FuncDecl):
                return self._build_function_ir_type(node_type.type)[0]
            elif isinstance(node_type.type, c_ast.Struct):
                return self.codegen_Struct(node_type.type)
            elif isinstance(node_type.type, c_ast.Union):
                return self.codegen_Union(node_type.type)
            elif isinstance(node_type.type, c_ast.Enum):
                self.codegen_Enum(node_type.type)
                return int32_t
            return int64_t
        elif isinstance(node_type, c_ast.ArrayDecl):
            return voidptr_t
        elif isinstance(node_type, c_ast.FuncDecl):
            func_type, _ = self._build_function_ir_type(node_type)
            return func_type
        return int64_t

    def _eval_dim(self, dim_node):
        """Evaluate array dimension (may be a constant or expression)."""
        if dim_node is None:
            return 0
        if isinstance(dim_node, c_ast.Constant):
            v = dim_node.value.rstrip("uUlL")
            return int(v, 0)  # handles hex/octal/decimal
        return self._eval_const_expr(dim_node)

    def _infer_array_count_from_initializer(self, init_node, elem_ir_type=None):
        if init_node is None:
            return None
        if isinstance(init_node, c_ast.InitList):
            exprs = list(getattr(init_node, "exprs", None) or [])
            if any(isinstance(expr, c_ast.NamedInitializer) for expr in exprs):
                cursor = 0
                max_index = 0
                for expr in exprs:
                    if isinstance(expr, c_ast.NamedInitializer):
                        designators = getattr(expr, "name", None) or []
                        if designators:
                            bounds = self._designator_index_bounds(designators[0])
                            if bounds is not None:
                                start, end = bounds
                                if start >= 0:
                                    cursor = start
                                    max_index = max(max_index, end + 1)
                                    cursor = end + 1
                                    continue
                    max_index = max(max_index, cursor + 1)
                    cursor += 1
                return max_index
            if elem_ir_type is not None:
                init_node = self._normalize_initializer_for_type(
                    init_node, ir.ArrayType(elem_ir_type, 0)
                )
            return len(init_node.exprs)
        if (
            self._is_string_constant(init_node)
        ):
            return len(self._string_literal_data(init_node))
        return None

    def _build_func_ptr_type(self, func_decl_node):
        """Build an IR function pointer type from a FuncDecl AST node."""
        func_type, _ = self._build_function_ir_type(func_decl_node)
        return func_type.as_pointer()

    def _build_function_ir_type(self, func_decl_node):
        """Build an IR function type from a FuncDecl AST node."""
        ret_ir, _ = self.codegen(func_decl_node)
        param_types = []
        is_var_arg = func_decl_node.args is None
        if func_decl_node.args:
            for param in func_decl_node.args.params:
                if isinstance(param, c_ast.EllipsisParam):
                    is_var_arg = True
                    continue
                if isinstance(param, (c_ast.Typename, c_ast.Decl)):
                    t = self._resolve_param_type(param)
                    if t is not None:
                        param_types.append(t)
        if isinstance(ret_ir, ir.VoidType):
            ret_ir = ir.VoidType()
        return ir.FunctionType(ret_ir, param_types, var_arg=is_var_arg), ret_ir

    def _build_future_funcdef_ir_type(self, func_def_node):
        """Build the most specific callable type we can infer for a FuncDef."""
        ret_ir, _ = self.codegen(func_def_node.decl.type)
        param_infos, is_var_arg = self._funcdef_param_infos(func_def_node)
        if getattr(func_def_node.decl.type, "args", None) is None:
            is_var_arg = True
        arg_types = [param_type for _name, param_type, _decl in param_infos]
        if isinstance(ret_ir, ir.VoidType):
            ret_ir = ir.VoidType()
        return ir.FunctionType(ret_ir, arg_types, var_arg=is_var_arg), ret_ir

    def _funcdef_param_infos(self, node):
        infos = []
        is_var_arg = False
        if not node.decl.type.args:
            return infos, is_var_arg

        knr_param_decls = {
            decl.name: decl for decl in (getattr(node, "param_decls", None) or [])
        }

        for index, param in enumerate(node.decl.type.args.params):
            if isinstance(param, c_ast.EllipsisParam):
                is_var_arg = True
                continue

            decl = param
            if isinstance(param, c_ast.ID):
                decl = knr_param_decls.get(param.name)
                if decl is None:
                    infos.append((param.name, int32_t, None))
                    continue

            t = self._resolve_param_type(decl)
            if t is None:
                continue

            pname = getattr(decl, "name", None)
            if not isinstance(pname, str):
                pname = f"arg{index}"
            infos.append((pname, t, decl))

        return infos, is_var_arg

    def _safe_load(self, ptr, name=""):
        """Load from ptr, guard against non-pointer types."""
        if not isinstance(ptr.type, ir.PointerType):
            return ptr
        if isinstance(ptr.type.pointee, ir.FunctionType):
            return ptr  # function pointers are first-class as pointers
        try:
            return self.builder.load(ptr, name=name)
        except Exception:
            return ptr

    @staticmethod
    def _constant_raw_value(value):
        raw_value = getattr(value, "value", None)
        if raw_value is None:
            raw_value = getattr(value, "constant", None)
        return raw_value

    def _decay_array_value_to_pointer(self, value, name="arraydecay"):
        """Convert an array value (including string literals) to &value[0]."""
        if not isinstance(value.type, ir.ArrayType):
            return value
        base = value
        if isinstance(value, ir.values.Constant):
            gv = ir.GlobalVariable(
                self.module, value.type, self.module.get_unique_name("strlit")
            )
            gv.initializer = value
            gv.global_constant = True
            gv.linkage = "internal"
            base = gv
        elif not isinstance(getattr(value, "type", None), ir.PointerType):
            base = self._alloca_in_entry(value.type, f"{name}.tmp")
            self._safe_store(value, base)
        idx0 = ir.Constant(int64_t, 0)
        return self.builder.gep(base, [idx0, idx0], name=name)

    def _decay_array_expr_to_pointer(self, expr_node, value, name="arrayexprdecay"):
        """Apply array-to-pointer decay using the expression's semantic type."""
        semantic_type = self._get_expr_ir_type(expr_node)
        if isinstance(semantic_type, ir.ArrayType):
            if isinstance(getattr(value, "type", None), ir.ArrayType):
                return self._decay_array_value_to_pointer(value, name)
            if (
                isinstance(getattr(value, "type", None), ir.PointerType)
                and value.type.pointee == semantic_type
            ):
                idx0 = ir.Constant(int64_t, 0)
                return self.builder.gep(value, [idx0, idx0], name=name)
        return self._decay_array_value_to_pointer(value, name)

    def _safe_store(self, value, ptr):
        """Store value to ptr, auto-converting types if needed."""
        if value is None or ptr is None:
            return
        if isinstance(value.type, ir.VoidType):
            return  # Can't store void
        if not isinstance(ptr.type, ir.PointerType):
            return
        if hasattr(ptr.type, "pointee") and value.type != ptr.type.pointee:
            value = self._implicit_convert(
                value,
                ptr.type.pointee,
                target_unsigned=(
                    True if self._is_unsigned_binding(ptr) else None
                ),
            )
        try:
            self.builder.store(value, ptr)
        except (TypeError, Exception):
            pass

    def _implicit_convert(self, val, target_type, *, target_unsigned=None):
        """Convert val to target_type if needed (implicit C promotion/truncation)."""
        if val is None or isinstance(val.type, ir.VoidType):
            # Can't convert void — return a zero of target type
            if isinstance(target_type, ir.VoidType):
                return val
            return self._zero_initializer(target_type)
        if self._same_ir_type_semantics(val.type, target_type):
            return self._apply_integer_target_signedness(val, target_unsigned)
        if isinstance(val.type, ir.IntType) and self._is_floating_ir_type(target_type):
            return self._int_to_float(val, target_type)
        if self._is_floating_ir_type(val.type) and isinstance(target_type, ir.IntType):
            if target_unsigned is True:
                return self._tag_unsigned(self.builder.fptoui(val, target_type))
            return self._apply_integer_target_signedness(
                self.builder.fptosi(val, target_type), target_unsigned
            )
        if self._is_floating_ir_type(val.type) and self._is_floating_ir_type(
            target_type
        ):
            float_width = (
                16
                if isinstance(val.type, ir.HalfType)
                else 32
                if isinstance(val.type, ir.FloatType)
                else 64
            )
            target_width = (
                16
                if isinstance(target_type, ir.HalfType)
                else 32
                if isinstance(target_type, ir.FloatType)
                else 64
            )
            if float_width < target_width:
                return self.builder.fpext(val, target_type)
            if float_width > target_width:
                return self.builder.fptrunc(val, target_type)
            return val
        # int -> int (wider or narrower)
        if isinstance(val.type, ir.IntType) and isinstance(target_type, ir.IntType):
            if val.type.width < target_type.width:
                if self._is_unsigned_val(val):
                    result = self.builder.zext(val, target_type)
                    return self._apply_integer_target_signedness(
                        result,
                        True if target_unsigned is None else target_unsigned,
                    )
                return self._apply_integer_target_signedness(
                    self.builder.sext(val, target_type), target_unsigned
                )
            elif val.type.width > target_type.width:
                result = self.builder.trunc(val, target_type)
                if target_unsigned is None:
                    target_unsigned = self._is_unsigned_val(val)
                return self._apply_integer_target_signedness(result, target_unsigned)
            return self._apply_integer_target_signedness(val, target_unsigned)
        # int -> pointer (e.g., NULL assignment, p = 0)
        if isinstance(val.type, ir.IntType) and isinstance(target_type, ir.PointerType):
            # inttoptr only works for simple pointer types, not function pointers
            raw_ptr = self.builder.inttoptr(val, voidptr_t)
            if target_type == voidptr_t:
                return raw_ptr
            return self.builder.bitcast(raw_ptr, target_type)
        # pointer -> int
        if isinstance(val.type, ir.PointerType) and isinstance(target_type, ir.IntType):
            return self.builder.ptrtoint(val, target_type)
        # pointer -> different pointer
        if isinstance(val.type, ir.PointerType) and isinstance(
            target_type, ir.PointerType
        ):
            result = self.builder.bitcast(val, target_type)
            if self._is_unsigned_pointee(val):
                self._tag_unsigned_pointee(result)
            if self._is_unsigned_return(val):
                self._tag_unsigned_return(result)
            return result
        # array -> pointer (string literal to char*)
        if isinstance(val.type, ir.ArrayType) and isinstance(
            target_type, ir.PointerType
        ):
            ptr = self._decay_array_value_to_pointer(val)
            if self._same_ir_type_semantics(ptr.type, target_type):
                return ptr
            return self.builder.bitcast(ptr, target_type)
        return val

    def _is_scalar_ir_type(self, ir_type):
        return (
            isinstance(ir_type, (ir.IntType, ir.PointerType))
            or self._is_floating_ir_type(ir_type)
        )

    def _is_aggregate_ir_type(self, ir_type):
        return _is_struct_ir_type(ir_type) or getattr(ir_type, "is_union", False)

    def _same_ir_type_semantics(self, lhs, rhs):
        if lhs is rhs:
            return True
        if type(lhs) is not type(rhs):
            return False
        if isinstance(lhs, ir.PointerType):
            return (
                getattr(lhs, "addrspace", 0) == getattr(rhs, "addrspace", 0)
                and self._same_ir_type_semantics(lhs.pointee, rhs.pointee)
            )
        if isinstance(lhs, ir.ArrayType):
            return lhs.count == rhs.count and self._same_ir_type_semantics(
                lhs.element, rhs.element
            )
        return str(lhs) == str(rhs)

    def _validate_explicit_cast(self, source_type, target_type):
        if isinstance(target_type, ir.VoidType):
            return

        if self._is_aggregate_ir_type(target_type) or isinstance(
            target_type, ir.ArrayType
        ):
            raise SemanticError("invalid cast to non-scalar type")

        if self._is_aggregate_ir_type(source_type):
            raise SemanticError("invalid cast from non-scalar type")

        if isinstance(source_type, ir.ArrayType):
            if not isinstance(target_type, ir.PointerType):
                raise SemanticError("invalid cast from array type")
            return

        if self._is_floating_ir_type(source_type) and isinstance(
            target_type, ir.PointerType
        ):
            raise SemanticError("invalid cast from floating type to pointer type")

        if isinstance(source_type, ir.PointerType) and self._is_floating_ir_type(
            target_type
        ):
            raise SemanticError("invalid cast from pointer type to floating type")

        if self._is_scalar_ir_type(source_type) and self._is_scalar_ir_type(
            target_type
        ):
            return

        raise SemanticError("invalid cast expression")

    def _extend_call_result(self, result, returns_unsigned=False):
        if not isinstance(result.type, ir.IntType):
            return result
        if returns_unsigned:
            self._tag_unsigned(result)
        else:
            self._clear_unsigned(result)
        return result

    def _to_bool(self, val, name="cond"):
        """Convert any value to an i1 boolean (!=0)."""
        if isinstance(val.type, ir.IntType):
            if val.type.width == 1:
                return val
            return self.builder.icmp_signed("!=", val, ir.Constant(val.type, 0), name)
        elif isinstance(val.type, ir.PointerType):
            null = ir.Constant(val.type, None)
            return self.builder.icmp_unsigned("!=", val, null, name)
        else:
            return self.builder.fcmp_unordered(
                "!=", val, ir.Constant(val.type, 0.0), name
            )

    def _ir_type_align(self, ir_type):
        """Return natural alignment of an IR type in bytes."""
        custom_align = getattr(ir_type, "custom_align", None)
        if custom_align is not None:
            return custom_align
        if isinstance(ir_type, ir.VoidType):
            return 1
        if isinstance(ir_type, ir.IntType):
            return integer_scalar_layout(ir_type.width).alignment
        elif isinstance(ir_type, ir.HalfType):
            return floating_scalar_layout(16).alignment
        elif isinstance(ir_type, ir.FloatType):
            return floating_scalar_layout(32).alignment
        elif isinstance(ir_type, ir.DoubleType):
            return floating_scalar_layout(64).alignment
        elif isinstance(ir_type, ir.PointerType):
            return pointer_scalar_layout().alignment
        elif isinstance(ir_type, ir.ArrayType):
            return self._ir_type_align(ir_type.element)
        elif _is_struct_ir_type(ir_type):
            if not ir_type.elements:
                return 1
            return max(self._ir_type_align(e) for e in ir_type.elements)
        return 8

    def _ir_type_size(self, ir_type):
        """Compute byte size of an IR type with proper alignment/padding."""
        custom_size = getattr(ir_type, "custom_size", None)
        if custom_size is not None:
            return custom_size
        if isinstance(ir_type, ir.IntType):
            return integer_scalar_layout(ir_type.width).size
        elif isinstance(ir_type, ir.HalfType):
            return floating_scalar_layout(16).size
        elif isinstance(ir_type, ir.FloatType):
            return floating_scalar_layout(32).size
        elif isinstance(ir_type, ir.DoubleType):
            return floating_scalar_layout(64).size
        elif isinstance(ir_type, ir.PointerType):
            return pointer_scalar_layout().size
        elif isinstance(ir_type, ir.ArrayType):
            return int(ir_type.count) * self._ir_type_size(ir_type.element)
        elif _is_struct_ir_type(ir_type):
            offset = 0
            for elem in ir_type.elements:
                align = self._ir_type_align(elem)
                offset = (offset + align - 1) & ~(align - 1)  # align up
                offset += self._ir_type_size(elem)
            # Tail padding: align to struct's overall alignment
            struct_align = self._ir_type_align(ir_type)
            offset = (offset + struct_align - 1) & ~(struct_align - 1)
            return offset
        return 8

    @staticmethod
    def _align_up(value, align):
        if align <= 1:
            return value
        return (value + align - 1) & ~(align - 1)

    def _resolve_struct_member_ir_type(self, decl):
        if isinstance(decl.type, c_ast.Struct):
            return self.codegen_Struct(decl.type)
        if isinstance(decl.type, c_ast.Union):
            return self.codegen_Union(decl.type)
        if isinstance(decl.type, c_ast.TypeDecl) and isinstance(
            decl.type.type, c_ast.Struct
        ):
            return self.codegen_Struct(decl.type.type)
        if isinstance(decl.type, c_ast.TypeDecl) and isinstance(
            decl.type.type, c_ast.Union
        ):
            return self.codegen_Union(decl.type.type)
        if isinstance(decl.type, c_ast.ArrayDecl):
            def _build_array_type(arr_node):
                dim = self._eval_dim(arr_node.dim) if arr_node.dim else 0
                if isinstance(arr_node.type, c_ast.ArrayDecl):
                    inner = _build_array_type(arr_node.type)
                else:
                    inner = self._resolve_ast_type(arr_node.type)
                return self._checked_array_ir_type(inner, dim)

            return _build_array_type(decl.type)
        if isinstance(decl.type, c_ast.PtrDecl):
            return self._resolve_ast_type(decl.type)
        if isinstance(decl.type, c_ast.TypeDecl):
            return self._resolve_ast_type(decl.type)
        return int64_t

    def _aggregate_member_names(self, aggregate_type):
        return list(getattr(aggregate_type, "members", ()) or [])

    def _aggregate_member_ir_type(self, aggregate_type, field_index):
        if getattr(aggregate_type, "is_union", False):
            member_types_by_index = getattr(
                aggregate_type, "member_types_by_index", None
            )
            if member_types_by_index is not None:
                return member_types_by_index[field_index]
            member_names = self._aggregate_member_names(aggregate_type)
            return aggregate_type.member_types[member_names[field_index]]
        return aggregate_type.elements[field_index]

    def _aggregate_member_decl_type(self, aggregate_type, field_index):
        member_decl_types_by_index = getattr(
            aggregate_type, "member_decl_types_by_index", None
        )
        if member_decl_types_by_index is not None:
            if field_index < len(member_decl_types_by_index):
                return member_decl_types_by_index[field_index]
            return None

        member_decl_types = getattr(aggregate_type, "member_decl_types", None)
        if isinstance(member_decl_types, dict):
            member_names = self._aggregate_member_names(aggregate_type)
            if field_index < len(member_names):
                return member_decl_types.get(member_names[field_index])
            return None
        if member_decl_types is not None and field_index < len(member_decl_types):
            return member_decl_types[field_index]
        return None

    def _aggregate_visible_field_paths(self, aggregate_type):
        visible_paths = getattr(aggregate_type, "visible_field_paths", None)
        if isinstance(visible_paths, dict):
            return visible_paths

        visible_paths = {}
        member_names = self._aggregate_member_names(aggregate_type)
        for field_index, member_name in enumerate(member_names):
            if member_name is not None:
                visible_paths.setdefault(member_name, (field_index,))
        return visible_paths

    def _compute_visible_field_paths(self, member_names, member_types):
        visible_paths = {}
        for field_index, (member_name, member_type) in enumerate(
            zip(member_names, member_types)
        ):
            if member_name is not None:
                visible_paths.setdefault(member_name, (field_index,))
                continue
            if not self._is_aggregate_ir_type(member_type):
                continue
            nested_paths = self._aggregate_visible_field_paths(member_type)
            for nested_name, nested_path in nested_paths.items():
                visible_paths.setdefault(
                    nested_name, (field_index,) + tuple(nested_path)
                )
        return visible_paths

    def _aggregate_field_path(self, aggregate_type, field_name):
        return self._aggregate_visible_field_paths(aggregate_type).get(field_name)

    def _aggregate_direct_member_index(self, aggregate_type, field_name):
        member_names = self._aggregate_member_names(aggregate_type)
        for field_index, member_name in enumerate(member_names):
            if member_name == field_name:
                return field_index
        named_member_indices = getattr(aggregate_type, "named_member_indices", None)
        if isinstance(named_member_indices, dict):
            return named_member_indices.get(field_name)
        return None

    def _aggregate_layout_by_index(self, aggregate_type, field_index):
        field_layouts_by_index = getattr(aggregate_type, "field_layouts_by_index", None)
        if field_layouts_by_index is None or field_index >= len(field_layouts_by_index):
            return None
        return field_layouts_by_index[field_index]

    def _bitfield_storage_ir_type(self, decl):
        storage_ir_type = self._resolve_ast_type(decl.type)
        if not isinstance(storage_ir_type, ir.IntType):
            return int32_t
        return storage_ir_type

    def _raw_layout_struct_type(
        self, size_bytes, align_bytes, type_name=None, existing_type=None, body=None
    ):
        align_map = {8: int64_t, 4: int32_t, 2: int16_t, 1: int8_t}
        identified_name = type_name or self._aggregate_type_name("layout")
        if body is None:
            if size_bytes <= 0:
                body = []
            elif align_bytes <= 1:
                body = [ir.ArrayType(int8_t, size_bytes)]
            else:
                align_type = align_map.get(align_bytes)
                if align_type is None or size_bytes < align_bytes:
                    body = [ir.ArrayType(int8_t, size_bytes)]
                else:
                    pad_size = size_bytes - align_bytes
                    if pad_size > 0:
                        body = [align_type, ir.ArrayType(int8_t, pad_size)]
                    else:
                        body = [align_type]
        if existing_type is not None:
            storage_type = existing_type
        else:
            storage_type = self.module.context.get_identified_type(identified_name)
        if storage_type.is_opaque:
            from pcc.llvm_capi.compat import set_struct_body
            set_struct_body(storage_type, body)
        storage_type.custom_size = size_bytes
        storage_type.custom_align = align_bytes
        storage_type.has_custom_layout = True
        return storage_type

    def _custom_layout_storage_segments(self, field_layouts_by_index, size_bytes):
        segments = []
        cursor = 0
        storage_segment_by_offset = {}

        for field_index, layout in enumerate(field_layouts_by_index):
            if layout.is_bitfield:
                start = layout.storage_byte_offset
                segment = storage_segment_by_offset.get(start)
                if segment is None:
                    storage_size = self._ir_type_size(layout.storage_ir_type)
                    if start < cursor:
                        return None
                    if start > cursor:
                        segments.append(
                            StructStorageSegment(
                                kind="padding",
                                byte_offset=cursor,
                                ir_type=ir.ArrayType(int8_t, start - cursor),
                            )
                        )
                    segment = StructStorageSegment(
                        kind="bitfield_storage",
                        byte_offset=start,
                        ir_type=layout.storage_ir_type,
                        bitfield_indices=(field_index,),
                    )
                    storage_segment_by_offset[start] = segment
                    segments.append(segment)
                    cursor = max(cursor, start + storage_size)
                else:
                    segment.bitfield_indices = segment.bitfield_indices + (field_index,)
                continue

            start = layout.byte_offset
            if start < cursor:
                return None
            if start > cursor:
                segments.append(
                    StructStorageSegment(
                        kind="padding",
                        byte_offset=cursor,
                        ir_type=ir.ArrayType(int8_t, start - cursor),
                    )
                )
            segments.append(
                StructStorageSegment(
                    kind="field",
                    byte_offset=start,
                    ir_type=layout.semantic_ir_type,
                    field_index=field_index,
                )
            )
            cursor = start + self._ir_type_size(layout.semantic_ir_type)

        if cursor < size_bytes:
            segments.append(
                StructStorageSegment(
                    kind="padding",
                    byte_offset=cursor,
                    ir_type=ir.ArrayType(int8_t, size_bytes - cursor),
                )
            )

        return segments

    def _build_layout_backed_struct(self, node):
        current_bit = 0
        max_align = 1
        member_names = []
        member_decl_types = []
        member_types = []
        field_layouts = {}
        field_layouts_by_index = []

        for decl in node.decls:
            if decl.bitsize is not None:
                storage_ir_type = self._bitfield_storage_ir_type(decl)
                storage_bits = storage_ir_type.width
                bit_width = self._eval_const_expr(decl.bitsize)
                max_align = max(max_align, self._ir_type_align(storage_ir_type))
                if bit_width == 0:
                    current_bit = self._align_up(current_bit, storage_bits)
                    continue
                unit_start = (current_bit // storage_bits) * storage_bits
                if current_bit + bit_width > unit_start + storage_bits:
                    current_bit = self._align_up(current_bit, storage_bits)
                    unit_start = current_bit
                layout = StructFieldLayout(
                    name=decl.name,
                    byte_offset=current_bit // 8,
                    semantic_ir_type=storage_ir_type,
                    decl_type=decl.type,
                    is_bitfield=True,
                    storage_byte_offset=unit_start // 8,
                    storage_ir_type=storage_ir_type,
                    bit_offset=current_bit - unit_start,
                    bit_width=bit_width,
                    is_unsigned=self._bitfield_decl_is_unsigned(
                        decl.type, bit_width
                    ),
                )
                if decl.name is not None:
                    field_layouts[decl.name] = layout
                field_layouts_by_index.append(layout)
                member_names.append(decl.name)
                member_decl_types.append(decl.type)
                member_types.append(storage_ir_type)
                current_bit += bit_width
                continue

            semantic_ir_type = self._resolve_struct_member_ir_type(decl)
            align_bits = self._ir_type_align(semantic_ir_type) * 8
            current_bit = self._align_up(current_bit, align_bits)
            max_align = max(max_align, self._ir_type_align(semantic_ir_type))
            layout = StructFieldLayout(
                name=decl.name,
                byte_offset=current_bit // 8,
                semantic_ir_type=semantic_ir_type,
                decl_type=decl.type,
            )
            if decl.name is not None:
                field_layouts[decl.name] = layout
            field_layouts_by_index.append(layout)
            member_names.append(decl.name)
            member_decl_types.append(decl.type)
            member_types.append(semantic_ir_type)
            current_bit += self._ir_type_size(semantic_ir_type) * 8

        size_bits = self._align_up(current_bit, max_align * 8)
        size_bytes = max(1, size_bits // 8)
        type_name = None
        existing_type = None
        if node.name:
            tag_key = self._tag_type_key(node.name)
            if tag_key in self.env:
                existing_type = self.env[tag_key][0]
            type_name = self._aggregate_type_name("struct", node.name)
        storage_segments = self._custom_layout_storage_segments(
            field_layouts_by_index, size_bytes
        )
        struct_type = self._raw_layout_struct_type(
            size_bytes,
            max_align,
            type_name,
            existing_type=existing_type,
            body=(
                [segment.ir_type for segment in storage_segments]
                if storage_segments is not None
                else None
            ),
        )
        struct_type.members = member_names
        struct_type.member_decl_types = member_decl_types
        struct_type.field_layouts = field_layouts
        struct_type.field_layouts_by_index = field_layouts_by_index
        if storage_segments is not None:
            struct_type.storage_segments = storage_segments
        struct_type.named_member_indices = {
            name: index
            for index, name in enumerate(member_names)
            if name is not None
        }
        struct_type.visible_field_paths = self._compute_visible_field_paths(
            member_names, member_types
        )
        if node.name:
            self.define(self._tag_type_key(node.name), (struct_type, None))
        return struct_type

    def _byte_offset_ptr(self, base_ptr, byte_offset, target_ptr_type, name="fieldptr"):
        byte_ptr = self.builder.bitcast(base_ptr, voidptr_t, name=f"{name}.base")
        if byte_offset:
            byte_ptr = self.builder.gep(
                byte_ptr,
                [ir.Constant(int64_t, byte_offset)],
                name=f"{name}.offs",
            )
        byte_pointee = getattr(byte_ptr.type, "pointee", None)
        target_pointee = getattr(target_ptr_type, "pointee", None)
        if byte_pointee != target_pointee:
            return self.builder.bitcast(byte_ptr, target_ptr_type, name=name)
        return byte_ptr

    @staticmethod
    def _bitfield_mask(bit_width):
        if bit_width <= 0:
            return 0
        return (1 << bit_width) - 1

    def _load_bitfield(self, ref):
        align = max(1, self._ir_type_align(ref.storage_ir_type))
        container_ptr = ref.container_ptr
        storage_ptr_ty = ref.storage_ir_type.as_pointer()
        pointee = getattr(container_ptr.type, "pointee", None)
        if pointee != ref.storage_ir_type:
            container_ptr = self.builder.bitcast(
                container_ptr, storage_ptr_ty, "bitfieldptr.base"
            )
        raw = self.builder.load(container_ptr, align=align)
        if ref.bit_offset:
            raw = self.builder.lshr(
                raw, ir.Constant(ref.storage_ir_type, ref.bit_offset), "bitshift"
            )

        semantic_width = ref.semantic_ir_type.width
        if ref.is_unsigned:
            if ref.bit_width < ref.storage_ir_type.width:
                raw = self.builder.and_(
                    raw,
                    ir.Constant(ref.storage_ir_type, self._bitfield_mask(ref.bit_width)),
                    "bitmask",
                )
            if raw.type.width > semantic_width:
                raw = self.builder.trunc(raw, ref.semantic_ir_type, "bittrunc")
            elif raw.type.width < semantic_width:
                raw = self.builder.zext(raw, ref.semantic_ir_type, "bitzext")
            self._tag_unsigned(raw)
            return raw

        if ref.bit_width < ref.storage_ir_type.width:
            narrow_type = ir.IntType(ref.bit_width)
            raw = self.builder.trunc(raw, narrow_type, "bitsigned.trunc")
            return self.builder.sext(raw, ref.semantic_ir_type, "bitsigned.sext")
        if raw.type.width > semantic_width:
            return self.builder.trunc(raw, ref.semantic_ir_type, "bittrunc")
        if raw.type.width < semantic_width:
            return self.builder.sext(raw, ref.semantic_ir_type, "bitsext")
        return raw

    def _store_bitfield(self, value, ref):
        if value is None:
            return
        align = max(1, self._ir_type_align(ref.storage_ir_type))
        container_ptr = ref.container_ptr
        storage_ptr_ty = ref.storage_ir_type.as_pointer()
        pointee = getattr(container_ptr.type, "pointee", None)
        if pointee != ref.storage_ir_type:
            container_ptr = self.builder.bitcast(
                container_ptr, storage_ptr_ty, "bitfieldptr.base"
            )
        storage_value = self.builder.load(container_ptr, align=align)
        if value.type != ref.semantic_ir_type:
            value = self._implicit_convert(value, ref.semantic_ir_type)
        value = self._convert_int_value(
            value, ref.storage_ir_type, result_unsigned=ref.is_unsigned
        )
        if value.type != ref.storage_ir_type:
            if isinstance(value.type, ir.IntType) and isinstance(
                ref.storage_ir_type, ir.IntType
            ):
                if value.type.width > ref.storage_ir_type.width:
                    value = self.builder.trunc(
                        value, ref.storage_ir_type, "bitstore.cast"
                    )
                elif value.type.width < ref.storage_ir_type.width:
                    ext = self.builder.zext if ref.is_unsigned else self.builder.sext
                    value = ext(value, ref.storage_ir_type, "bitstore.cast")
            else:
                value = self._implicit_convert(value, ref.storage_ir_type)
        field_mask = self._bitfield_mask(ref.bit_width)
        field_mask_const = ir.Constant(ref.storage_ir_type, field_mask)
        if ref.bit_width < ref.storage_ir_type.width:
            value = self.builder.and_(value, field_mask_const, "bitstore.mask")
            if ref.bit_offset:
                value = self.builder.shl(
                    value,
                    ir.Constant(ref.storage_ir_type, ref.bit_offset),
                    "bitstore.shift",
                )
            clear_mask = ((1 << ref.storage_ir_type.width) - 1) ^ (
                field_mask << ref.bit_offset
            )
            storage_value = self.builder.and_(
                storage_value,
                ir.Constant(ref.storage_ir_type, clear_mask),
                "bitstore.clear",
            )
            value = self.builder.or_(storage_value, value, "bitstore.merge")
        self.builder.store(value, container_ptr, align=align)

    def _refine_member_ir_type(self, aggregate_type, member_key, field_type):
        """Prefer semantic member types over storage types when available."""
        semantic_field_type = field_type
        member_decl_types = getattr(aggregate_type, "member_decl_types", None)
        decl_type = None

        if isinstance(member_decl_types, dict):
            decl_type = member_decl_types.get(member_key)
        elif (
            isinstance(member_key, int)
            and member_decl_types is not None
            and member_key < len(member_decl_types)
        ):
            decl_type = member_decl_types[member_key]

        if decl_type is None:
            return semantic_field_type

        if self._is_aggregate_ir_type(field_type):
            return semantic_field_type

        try:
            resolved = self._resolve_ast_type(decl_type)
            if isinstance(field_type, ir.ArrayType) and isinstance(
                resolved, ir.PointerType
            ):
                return semantic_field_type
            if isinstance(resolved, (ir.ArrayType, ir.PointerType)) or _is_struct_ir_type(
                resolved
            ):
                return resolved
        except Exception:
            pass

        return semantic_field_type

    def _get_aggregate_field_info_by_path(self, aggregate_type, field_path):
        field_index = field_path[0]

        if getattr(aggregate_type, "is_union", False):
            field_type = self._aggregate_member_ir_type(aggregate_type, field_index)
            semantic_field_type = self._refine_member_ir_type(
                aggregate_type, field_index, field_type
            )
            if len(field_path) == 1:
                return 0, semantic_field_type
            if not self._is_aggregate_ir_type(semantic_field_type):
                raise CodegenError("field path descends into non-aggregate union member")
            nested_offset, nested_type = self._get_aggregate_field_info_by_path(
                semantic_field_type, field_path[1:]
            )
            return nested_offset, nested_type

        if getattr(aggregate_type, "has_custom_layout", False):
            layout = self._aggregate_layout_by_index(aggregate_type, field_index)
            if layout is None:
                raise CodegenError(f"Field index {field_index} not found in aggregate")
            if len(field_path) == 1:
                if layout.is_bitfield:
                    raise CodegenError("offsetof on bit-field is not supported")
                return layout.byte_offset, layout.semantic_ir_type
            if layout.is_bitfield or not self._is_aggregate_ir_type(
                layout.semantic_ir_type
            ):
                raise CodegenError("field path descends into non-aggregate member")
            nested_offset, nested_type = self._get_aggregate_field_info_by_path(
                layout.semantic_ir_type, field_path[1:]
            )
            return layout.byte_offset + nested_offset, nested_type

        if not hasattr(aggregate_type, "members"):
            raise CodegenError(f"Aggregate has no named fields: {aggregate_type}")

        offset = 0
        field_type = None
        for i, member_type in enumerate(aggregate_type.elements):
            align = self._ir_type_align(member_type)
            offset = self._align_up(offset, align)
            if i == field_index:
                field_type = member_type
                break
            offset += self._ir_type_size(member_type)

        if field_type is None:
            raise CodegenError(f"Field index {field_index} not found in aggregate")

        semantic_field_type = self._refine_member_ir_type(
            aggregate_type, field_index, field_type
        )
        if len(field_path) == 1:
            return offset, semantic_field_type
        if not self._is_aggregate_ir_type(semantic_field_type):
            raise CodegenError("field path descends into non-aggregate member")
        nested_offset, nested_type = self._get_aggregate_field_info_by_path(
            semantic_field_type, field_path[1:]
        )
        return offset + nested_offset, nested_type

    def _get_aggregate_field_info(self, aggregate_type, field_name):
        """Return byte offset and semantic IR type for a struct/union field."""
        field_path = self._aggregate_field_path(aggregate_type, field_name)
        if field_path is None:
            raise CodegenError(f"Field '{field_name}' not found in aggregate")
        return self._get_aggregate_field_info_by_path(aggregate_type, field_path)

    def _eval_offsetof_structref(self, node):
        """Evaluate offsetof-like expressions expanded as &((T*)0)->field."""
        if isinstance(node, c_ast.StructRef):
            base_offset, base_type = self._eval_offsetof_structref(node.name)
            aggregate_type = base_type
            if node.type == "->" and isinstance(aggregate_type, ir.PointerType):
                aggregate_type = aggregate_type.pointee
            field_offset, field_type = self._get_aggregate_field_info(
                aggregate_type, node.field.name
            )
            return base_offset + field_offset, field_type

        if isinstance(node, c_ast.Cast):
            target_type = self._resolve_ast_type(node.to_type.type)
            return 0, target_type

        raise CodegenError(f"Not an offsetof base: {type(node).__name__}")

    def _is_offsetof_like_structref(self, node):
        if isinstance(node, c_ast.StructRef):
            return self._is_offsetof_like_structref(node.name)
        if not isinstance(node, c_ast.Cast):
            return False
        try:
            return int(self._eval_const_expr(node.expr)) == 0
        except Exception:
            return False

    def _infer_sizeof_operand_ir_type(self, node):
        """Infer the operand type for sizeof without emitting runtime IR."""
        cached = self._get_expr_ir_type(node)
        if cached is not None:
            return cached

        if isinstance(node, c_ast.Constant):
            if node.type == "int":
                raw = node.value
                lower = raw.lower()
                val_str = raw.rstrip("uUlL")
                if val_str.startswith("0x") or val_str.startswith("0X"):
                    value = int(val_str, 16)
                elif val_str.startswith("0") and len(val_str) > 1 and val_str[1:].isdigit():
                    value = int(val_str, 8)
                else:
                    value = int(val_str)
                if "l" in lower or value > 0x7FFFFFFF:
                    return int64_t
                return int32_t
            if node.type == "char":
                return int32_t
            if node.type in ("string", "wstring"):
                data = self._string_literal_data(node)
                elem_type = int32_t if self._is_wide_string_constant(node) else int8_t
                return ir.ArrayType(elem_type, len(data))
            return self._float_literal_ir_type(node.value)

        if isinstance(node, c_ast.ID):
            try:
                ir_type, _ = self.lookup(node.name)
            except SemanticError:
                decl_type = self._lookup_decl_ast_type(node.name)
                if decl_type is None:
                    raise
                ir_type = self._resolve_ast_type(decl_type)
            return ir_type

        if isinstance(node, c_ast.CompoundLiteral):
            return self._compound_literal_ir_type(node.type.type, init_node=node.init)

        if isinstance(node, c_ast.StructRef):
            base_type = self._infer_sizeof_operand_ir_type(node.name)
            aggregate_type = base_type
            if node.type == "->":
                if isinstance(base_type, ir.ArrayType):
                    aggregate_type = base_type.element
                elif not isinstance(base_type, ir.PointerType):
                    raise CodegenError(
                        f"sizeof operand is not a pointer for '->': {base_type}"
                    )
                else:
                    aggregate_type = base_type.pointee
            _, field_type = self._get_aggregate_field_info(
                aggregate_type, node.field.name
            )
            return field_type

        if isinstance(node, c_ast.ArrayRef):
            base_type = self._infer_sizeof_operand_ir_type(node.name)
            if isinstance(base_type, ir.ArrayType):
                return base_type.element
            if isinstance(base_type, ir.PointerType):
                return base_type.pointee
            raise CodegenError(
                f"sizeof operand is not indexable: {type(base_type).__name__}"
            )

        if isinstance(node, c_ast.Cast):
            return self._resolve_ast_type(node.to_type.type)

        if isinstance(node, c_ast.UnaryOp):
            if node.op == "&":
                return ir.PointerType(self._infer_sizeof_operand_ir_type(node.expr))
            if node.op == "*":
                base_type = self._infer_sizeof_operand_ir_type(node.expr)
                if isinstance(base_type, ir.ArrayType):
                    return base_type.element
                if not isinstance(base_type, ir.PointerType):
                    raise CodegenError(
                        f"sizeof operand is not a pointer for '*': {base_type}"
                    )
                return base_type.pointee
            if node.op in ("+", "-", "~"):
                base_type = self._infer_sizeof_operand_ir_type(node.expr)
                if isinstance(base_type, ir.IntType):
                    return self._integer_promotion_ir_type(base_type)
                return base_type
            if node.op == "!":
                return int32_t
            if node.op in ("p++", "p--", "++", "--"):
                return self._infer_sizeof_operand_ir_type(node.expr)
            if node.op == "sizeof":
                return int64_t

        if isinstance(node, c_ast.BinaryOp):
            lhs_type = self._decay_ir_type(
                self._infer_sizeof_operand_ir_type(node.left)
            )
            rhs_type = self._decay_ir_type(
                self._infer_sizeof_operand_ir_type(node.right)
            )

            if node.op in ("&&", "||", "==", "!=", "<", "<=", ">", ">="):
                return int32_t

            if (
                node.op in ("+", "-")
                and isinstance(lhs_type, ir.PointerType)
                and isinstance(rhs_type, ir.IntType)
            ):
                return lhs_type
            if (
                node.op == "+"
                and isinstance(rhs_type, ir.PointerType)
                and isinstance(lhs_type, ir.IntType)
            ):
                return rhs_type
            if (
                node.op == "-"
                and isinstance(lhs_type, ir.PointerType)
                and isinstance(rhs_type, ir.PointerType)
            ):
                return int64_t

            if node.op in ("<<", ">>") and isinstance(lhs_type, ir.IntType):
                return self._integer_promotion_ir_type(lhs_type)

            return self._usual_arithmetic_conversion_ir_type(lhs_type, rhs_type)

        if isinstance(node, c_ast.TernaryOp):
            true_type = self._infer_sizeof_operand_ir_type(node.iftrue)
            false_type = self._infer_sizeof_operand_ir_type(node.iffalse)
            if true_type == false_type:
                return true_type
            if (
                isinstance(
                    true_type,
                    (ir.IntType, ir.HalfType, ir.FloatType, ir.DoubleType),
                )
                and isinstance(
                    false_type,
                    (ir.IntType, ir.HalfType, ir.FloatType, ir.DoubleType),
                )
            ):
                return self._usual_arithmetic_conversion_ir_type(
                    true_type, false_type
                )
            if isinstance(true_type, ir.PointerType) and isinstance(
                false_type, ir.PointerType
            ):
                return true_type
            return true_type

        if isinstance(node, c_ast.ExprList) and node.exprs:
            return self._infer_sizeof_operand_ir_type(node.exprs[-1])

        if isinstance(node, c_ast.StmtExpr) and getattr(node.stmt, "block_items", None):
            for item in reversed(node.stmt.block_items):
                if self._is_expression_node(item):
                    return self._infer_sizeof_operand_ir_type(item)

        if isinstance(node, c_ast.GenericSelection):
            selected = self._select_generic_association(node)
            if selected is not None:
                return self._infer_sizeof_operand_ir_type(selected)

        if isinstance(node, c_ast.FuncCall):
            if isinstance(node.name, c_ast.ID):
                ret_type = getattr(self, "func_return_types", {}).get(node.name.name)
                if ret_type is not None:
                    return ret_type

        raise CodegenError(f"Cannot infer sizeof operand type: {type(node).__name__}")

    @staticmethod
    def _make_identifier_type(names, quals=None, declname=None):
        return c_ast.TypeDecl(
            declname,
            list(quals or []),
            c_ast.IdentifierType(list(names)),
        )

    @staticmethod
    def _is_expression_node(node):
        return isinstance(
            node,
            (
                c_ast.ArrayRef,
                c_ast.Assignment,
                c_ast.BinaryOp,
                c_ast.Cast,
                c_ast.CompoundLiteral,
                c_ast.Constant,
                c_ast.ExprList,
                c_ast.FuncCall,
                c_ast.GenericSelection,
                c_ast.ID,
                c_ast.InitList,
                c_ast.StmtExpr,
                c_ast.StructRef,
                c_ast.TernaryOp,
                c_ast.UnaryOp,
            ),
        )

    @staticmethod
    def _canonical_identifier_names(names):
        canonical = list(names)
        if "signed" in canonical and "char" not in canonical:
            canonical = [name for name in canonical if name != "signed"]
        if "int" in canonical and any(name in canonical for name in ("short", "long")):
            canonical = [name for name in canonical if name != "int"]
        return tuple(sorted(canonical))

    def _generic_type_key_from_type(
        self,
        node_type,
        *,
        inherited_quals=(),
        top_level=True,
        strip_top_level_quals=False,
        decay_top_level=False,
    ):
        if node_type is None:
            return None

        if isinstance(node_type, c_ast.Typename):
            return self._generic_type_key_from_type(
                node_type.type,
                inherited_quals=inherited_quals,
                top_level=top_level,
                strip_top_level_quals=strip_top_level_quals,
                decay_top_level=decay_top_level,
            )

        if isinstance(node_type, c_ast.TypeDecl):
            merged_quals = tuple(sorted(inherited_quals + tuple(node_type.quals or ())))
            inner = node_type.type
            if isinstance(inner, c_ast.IdentifierType) and len(inner.names) == 1:
                resolved = self._lookup_typedef_ast_type(inner.names[0])
                if resolved is not None:
                    return self._generic_type_key_from_type(
                        resolved,
                        inherited_quals=merged_quals,
                        top_level=top_level,
                        strip_top_level_quals=strip_top_level_quals,
                        decay_top_level=decay_top_level,
                    )
            effective_quals = (
                ()
                if top_level and strip_top_level_quals
                else merged_quals
            )
            if isinstance(inner, c_ast.IdentifierType):
                return (
                    "base",
                    effective_quals,
                    self._canonical_identifier_names(inner.names),
                )
            if isinstance(inner, c_ast.Struct):
                return ("struct", effective_quals, inner.name or f"anon:{id(inner)}")
            if isinstance(inner, c_ast.Union):
                return ("union", effective_quals, inner.name or f"anon:{id(inner)}")
            if isinstance(inner, c_ast.Enum):
                return ("enum", effective_quals, inner.name or f"anon:{id(inner)}")
            return self._generic_type_key_from_type(
                inner,
                inherited_quals=effective_quals,
                top_level=top_level,
                strip_top_level_quals=False,
                decay_top_level=decay_top_level,
            )

        if isinstance(node_type, c_ast.PtrDecl):
            quals = tuple(
                sorted(
                    ()
                    if top_level and strip_top_level_quals
                    else inherited_quals + tuple(node_type.quals or ())
                )
            )
            return (
                "ptr",
                quals,
                self._generic_type_key_from_type(
                    node_type.type,
                    top_level=False,
                    strip_top_level_quals=False,
                    decay_top_level=False,
                ),
            )

        if isinstance(node_type, c_ast.ArrayDecl):
            if top_level and decay_top_level:
                return (
                    "ptr",
                    (),
                    self._generic_type_key_from_type(
                        node_type.type,
                        top_level=False,
                        strip_top_level_quals=False,
                        decay_top_level=False,
                    ),
                )
            dim = None
            if node_type.dim is not None:
                try:
                    dim = int(self._eval_const_expr(node_type.dim))
                except Exception:
                    dim = None
            return (
                "array",
                dim,
                self._generic_type_key_from_type(
                    node_type.type,
                    top_level=False,
                    strip_top_level_quals=False,
                    decay_top_level=False,
                ),
            )

        if isinstance(node_type, c_ast.FuncDecl):
            params = []
            is_var_arg = False
            if node_type.args:
                for param in node_type.args.params:
                    if isinstance(param, c_ast.EllipsisParam):
                        is_var_arg = True
                        continue
                    param_type = param.type if hasattr(param, "type") else param
                    params.append(
                        self._generic_type_key_from_type(
                            param_type,
                            top_level=False,
                            strip_top_level_quals=False,
                            decay_top_level=False,
                        )
                    )
            func_key = (
                "func",
                self._generic_type_key_from_type(
                    node_type.type,
                    top_level=False,
                    strip_top_level_quals=False,
                    decay_top_level=False,
                ),
                tuple(params),
                is_var_arg,
            )
            if top_level and decay_top_level:
                return ("ptr", (), func_key)
            return func_key

        return None

    def _generic_integer_literal_type(self, raw):
        lower = raw.lower()
        val_str = raw.rstrip("uUlL")
        names = []
        if "u" in lower:
            names.append("unsigned")
        if "ll" in lower:
            names.extend(["long", "long"])
        elif "l" in lower:
            names.append("long")
        else:
            names.append("int")
        return self._make_identifier_type(names)

    def _generic_base_rank(self, names):
        if "long" in names and names.count("long") > 1:
            return 4
        if "long" in names:
            return 3
        if "int" in names:
            return 2
        if "short" in names:
            return 1
        if "char" in names:
            return 0
        return -1

    @staticmethod
    def _generic_is_base_key(key):
        return isinstance(key, tuple) and len(key) == 3 and key[0] == "base"

    def _generic_integer_promotion_key(self, key):
        if not self._generic_is_base_key(key):
            return key
        _kind, quals, names = key
        if "float" in names or "double" in names:
            return key
        if self._generic_base_rank(names) < self._generic_base_rank(("int",)):
            return ("base", quals, ("int",))
        return key

    def _generic_usual_arithmetic_conversion_key(self, lhs_key, rhs_key):
        lhs = self._generic_integer_promotion_key(lhs_key)
        rhs = self._generic_integer_promotion_key(rhs_key)
        if not (self._generic_is_base_key(lhs) and self._generic_is_base_key(rhs)):
            return lhs

        lhs_names = lhs[2]
        rhs_names = rhs[2]

        if "double" in lhs_names or "double" in rhs_names:
            return ("base", (), ("double",))
        if "float" in lhs_names or "float" in rhs_names:
            return ("base", (), ("float",))

        lhs_rank = self._generic_base_rank(lhs_names)
        rhs_rank = self._generic_base_rank(rhs_names)
        decision = _decide_usual_integer_conversion(
            lhs_rank,
            "unsigned" in lhs_names,
            rhs_rank,
            "unsigned" in rhs_names,
        )
        result_names = lhs_names if decision.source == "lhs" else rhs_names
        return ("base", (), result_names)

    def _generic_expr_type_key(self, node):
        if node is None:
            return None

        if isinstance(node, c_ast.ID):
            decl_type = self._lookup_decl_ast_type(node.name)
            if decl_type is not None:
                return self._generic_type_key_from_type(
                    decl_type,
                    strip_top_level_quals=True,
                    decay_top_level=True,
                )
            return None

        if isinstance(node, c_ast.Constant):
            if node.type == "int":
                return self._generic_type_key_from_type(
                    self._generic_integer_literal_type(node.value)
                )
            if node.type == "char":
                return self._generic_type_key_from_type(
                    self._make_identifier_type(["int"])
                )
            if node.type == "float":
                raw = node.value.lower()
                names = ["float"] if raw.endswith("f") else ["double"]
                return self._generic_type_key_from_type(
                    self._make_identifier_type(names)
                )
            if node.type == "wstring":
                return (
                    "ptr",
                    (),
                    self._generic_type_key_from_type(
                        self._make_identifier_type(["wchar_t"])
                    ),
                )
            if node.type == "string":
                return (
                    "ptr",
                    (),
                    self._generic_type_key_from_type(
                        self._make_identifier_type(["char"])
                    ),
                )
            return None

        if isinstance(node, c_ast.Cast):
            return self._generic_type_key_from_type(node.to_type.type)

        if isinstance(node, c_ast.UnaryOp):
            if node.op == "&":
                expr_key = self._generic_expr_type_key(node.expr)
                return ("ptr", (), expr_key) if expr_key is not None else None
            if node.op == "*":
                expr_key = self._generic_expr_type_key(node.expr)
                if isinstance(expr_key, tuple) and expr_key[0] == "ptr":
                    return expr_key[2]
                return None
            if node.op == "!":
                return self._generic_type_key_from_type(
                    self._make_identifier_type(["int"])
                )
            if node.op in ("+", "-", "~", "p++", "p--", "++", "--"):
                expr_key = self._generic_expr_type_key(node.expr)
                return self._generic_integer_promotion_key(expr_key)
            if node.op == "sizeof":
                return self._generic_type_key_from_type(
                    self._make_identifier_type(["unsigned", "long"])
                )

        if isinstance(node, c_ast.BinaryOp):
            if node.op in ("&&", "||", "==", "!=", "<", "<=", ">", ">="):
                return self._generic_type_key_from_type(
                    self._make_identifier_type(["int"])
                )
            lhs_key = self._generic_expr_type_key(node.left)
            rhs_key = self._generic_expr_type_key(node.right)
            if lhs_key is None or rhs_key is None:
                return lhs_key or rhs_key
            if node.op in ("+", "-", "*", "/", "%", "<<", ">>", "&", "|", "^"):
                return self._generic_usual_arithmetic_conversion_key(lhs_key, rhs_key)
            return lhs_key

        if isinstance(node, c_ast.TernaryOp):
            true_key = self._generic_expr_type_key(node.iftrue)
            false_key = self._generic_expr_type_key(node.iffalse)
            if true_key == false_key:
                return true_key
            if true_key is None or false_key is None:
                return true_key or false_key
            if self._generic_is_base_key(true_key) and self._generic_is_base_key(false_key):
                return self._generic_usual_arithmetic_conversion_key(
                    true_key, false_key
                )
            return true_key

        if isinstance(node, c_ast.ExprList) and node.exprs:
            return self._generic_expr_type_key(node.exprs[-1])

        if isinstance(node, c_ast.FuncCall):
            callee_key = self._generic_expr_type_key(node.name)
            if (
                isinstance(callee_key, tuple)
                and callee_key[0] == "ptr"
                and isinstance(callee_key[2], tuple)
                and callee_key[2][0] == "func"
            ):
                return callee_key[2][1]
            return None

        if isinstance(node, c_ast.StmtExpr) and getattr(node.stmt, "block_items", None):
            for item in reversed(node.stmt.block_items):
                if self._is_expression_node(item):
                    return self._generic_expr_type_key(item)
            return None

        if isinstance(node, c_ast.GenericSelection):
            selected = self._select_generic_association(node)
            if selected is not None:
                return self._generic_expr_type_key(selected)

        return None

    def _select_generic_association(self, node):
        controlling_key = self._generic_expr_type_key(node.expr)
        default_expr = None
        for assoc in node.associations or []:
            if assoc.type is None:
                default_expr = assoc.expr
                continue
            assoc_key = self._generic_type_key_from_type(assoc.type)
            if assoc_key == controlling_key:
                return assoc.expr
        return default_expr

    def codegen_Typename(self, node):
        # Used inside sizeof(type) — not directly code-generated
        return None, None

    def codegen_BinaryOp(self, node):
        # Short-circuit && and || before evaluating both sides
        if node.op == "&&":
            return self._codegen_short_circuit_and(node)
        elif node.op == "||":
            return self._codegen_short_circuit_or(node)

        lhs, _ = self.codegen(node.left)
        rhs, _ = self.codegen(node.right)
        if lhs is None or rhs is None:
            return ir.Constant(int64_t, 0), None
        lhs = self._decay_array_expr_to_pointer(node.left, lhs, "lhsarraydecay")
        rhs = self._decay_array_expr_to_pointer(node.right, rhs, "rhsarraydecay")

        # Pointer arithmetic: ptr + int or ptr - int
        if (
            node.op in ("+", "-")
            and isinstance(lhs.type, ir.PointerType)
            and isinstance(rhs.type, ir.IntType)
        ):
            rhs = self._integer_promotion(rhs)
            rhs = self._convert_int_value(rhs, int64_t, result_unsigned=False)
            if node.op == "-":
                rhs = self.builder.neg(rhs, "negidx")
            return self.builder.gep(lhs, [rhs], name="ptradd"), None
        if (
            node.op == "+"
            and isinstance(rhs.type, ir.PointerType)
            and isinstance(lhs.type, ir.IntType)
        ):
            lhs = self._integer_promotion(lhs)
            lhs = self._convert_int_value(lhs, int64_t, result_unsigned=False)
            return self.builder.gep(rhs, [lhs], name="ptradd"), None

        # Pointer subtraction: ptr - ptr -> int (element count)
        if (
            node.op == "-"
            and isinstance(lhs.type, ir.PointerType)
            and isinstance(rhs.type, ir.PointerType)
        ):
            lhs_int = self.builder.ptrtoint(lhs, int64_t)
            rhs_int = self.builder.ptrtoint(rhs, int64_t)
            diff = self.builder.sub(lhs_int, rhs_int, "ptrdiff")
            elem_size = self._ir_type_size(lhs.type.pointee)
            return (
                self.builder.sdiv(
                    diff, ir.Constant(int64_t, elem_size), "ptrdiff_elems"
                ),
                None,
            )

        # Promote int/pointer mix
        if isinstance(lhs.type, ir.PointerType) and isinstance(rhs.type, ir.IntType):
            rhs = self._implicit_convert(rhs, lhs.type)
        elif isinstance(rhs.type, ir.PointerType) and isinstance(lhs.type, ir.IntType):
            lhs = self._implicit_convert(lhs, rhs.type)

        # Promotion above can turn int/pointer into ptr/ptr; handle subtraction
        if (
            node.op == "-"
            and isinstance(lhs.type, ir.PointerType)
            and isinstance(rhs.type, ir.PointerType)
        ):
            lhs_int = self.builder.ptrtoint(lhs, int64_t)
            rhs_int = self.builder.ptrtoint(rhs, int64_t)
            diff = self.builder.sub(lhs_int, rhs_int, "ptrdiff")
            elem_size = self._ir_type_size(lhs.type.pointee)
            return (
                self.builder.sdiv(
                    diff, ir.Constant(int64_t, elem_size), "ptrdiff_elems"
                ),
                None,
            )

        is_unsigned = False
        if isinstance(lhs.type, ir.IntType) and self._is_floating_ir_type(rhs.type):
            lhs = self._implicit_convert(lhs, rhs.type)
        elif self._is_floating_ir_type(lhs.type) and isinstance(rhs.type, ir.IntType):
            rhs = self._implicit_convert(rhs, lhs.type)
        elif self._is_floating_ir_type(lhs.type) and self._is_floating_ir_type(
            rhs.type
        ):
            if lhs.type != rhs.type:
                target = self._common_float_type(lhs.type, rhs.type)
                lhs = self._implicit_convert(lhs, target)
                rhs = self._implicit_convert(rhs, target)
        elif isinstance(lhs.type, ir.IntType) and isinstance(rhs.type, ir.IntType):
            if node.op in ("<<", ">>"):
                lhs, rhs, is_unsigned = self._shift_operand_conversion(lhs, rhs)
            else:
                lhs, rhs, is_unsigned = self._usual_arithmetic_conversion(lhs, rhs)

        dispatch_type_double = 1
        dispatch_type_int = 0

        if isinstance(lhs.type, ir.IntType) and isinstance(rhs.type, ir.IntType):
            dispatch_type = dispatch_type_int
        else:
            dispatch_type = dispatch_type_double

        if node.op in ["+", "-", "*", "/", "%"]:
            if dispatch_type == dispatch_type_double:
                ops = {
                    "+": self._fadd,
                    "-": self._fsub,
                    "*": self._fmul,
                    "/": self._fdiv,
                    "%": self._frem,
                }
                return ops[node.op](lhs, rhs, "tmp"), None
            else:
                # SEC-P1-UBSAN guards (no-op unless the flag is enabled).
                if node.op in ("/", "%"):
                    self._maybe_ubsan_guard_div(lhs, rhs, signed=not is_unsigned)
                elif node.op in ("+", "-", "*"):
                    self._maybe_ubsan_guard_arith(
                        lhs, rhs, node.op, signed=not is_unsigned
                    )
                if node.op in ("/", "%") and is_unsigned:
                    op = self.builder.udiv if node.op == "/" else self.builder.urem
                else:
                    ops = {
                        "+": self.builder.add,
                        "-": self.builder.sub,
                        "*": self.builder.mul,
                        "/": self.builder.sdiv,
                        "%": self.builder.srem,
                    }
                    op = ops[node.op]
                result = op(lhs, rhs, "tmp")
                # Do not attach no-wrap flags by default. Even for signed
                # arithmetic, frontends need a proof before adding `nsw`;
                # otherwise LLVM is free to miscompile wrap-sensitive code.
                # Later passes can annotate proven-safe operations.
                if is_unsigned:
                    self._tag_unsigned(result)
                return result, None
        elif node.op in [">", "<", ">=", "<=", "!=", "=="]:
            if isinstance(lhs.type, ir.PointerType) and isinstance(
                rhs.type, ir.PointerType
            ):
                lhs_i = self.builder.ptrtoint(lhs, int64_t)
                rhs_i = self.builder.ptrtoint(rhs, int64_t)
                cmp = self.builder.icmp_unsigned(node.op, lhs_i, rhs_i, "ptrcmp")
            elif dispatch_type == dispatch_type_int:
                if is_unsigned:
                    cmp = self.builder.icmp_unsigned(node.op, lhs, rhs, "cmptmp")
                else:
                    cmp = self.builder.icmp_signed(node.op, lhs, rhs, "cmptmp")
            else:
                cmp = self._float_compare(node.op, lhs, rhs, "cmptmp")
            # clang CodeGen: comparison results are i32 (C int), not i64.
            # zext i1→i32 is cheaper and _to_bool handles i32 directly.
            return self.builder.zext(cmp, int32_t, "booltmp"), None
        elif node.op == "&":
            result = self.builder.and_(lhs, rhs, "andtmp")
            if is_unsigned:
                self._tag_unsigned(result)
            return result, None
        elif node.op == "|":
            result = self.builder.or_(lhs, rhs, "ortmp")
            if is_unsigned:
                self._tag_unsigned(result)
            return result, None
        elif node.op == "^":
            result = self.builder.xor(lhs, rhs, "xortmp")
            if is_unsigned:
                self._tag_unsigned(result)
            return result, None
        elif node.op == "<<":
            self._maybe_ubsan_guard_shift(lhs, rhs)  # SEC-P1-UBSAN (no-op if off)
            result = self.builder.shl(lhs, rhs, "shltmp")
            if is_unsigned:
                self._tag_unsigned(result)
            return result, None
        elif node.op == ">>":
            self._maybe_ubsan_guard_shift(lhs, rhs)  # SEC-P1-UBSAN (no-op if off)
            if is_unsigned:
                result = self.builder.lshr(lhs, rhs, "shrtmp")
                self._tag_unsigned(result)
                return result, None
            return self.builder.ashr(lhs, rhs, "shrtmp"), None
        else:
            func = self.module.globals.get("binary{0}".format(node.op))
            return self.builder.call(func, [lhs, rhs], "binop"), None

    def codegen_NoneType(self, node):
        return None, None


    def codegen_TernaryOp(self, node):
        try:
            cond_const = self._eval_const_expr(node.cond)
        except Exception:
            cond_const = None

        if cond_const is not None:
            if cond_const:
                chosen = node.iftrue if node.iftrue is not None else node.cond
            else:
                chosen = node.iffalse
            result = self.codegen(chosen)
            result_val, _ = result
            if result_val is not None:
                semantic_type = self._get_expr_ir_type(
                    chosen, getattr(result_val, "type", None)
                )
                if semantic_type is not None:
                    self._set_expr_ir_type(node, semantic_type)
            return result

        cond_val, _ = self.codegen(node.cond)
        cmp = self._to_bool(cond_val)

        then_bb = self.builder.function.append_basic_block("ternary_true")
        else_bb = self.builder.function.append_basic_block("ternary_false")
        merge_bb = self.builder.function.append_basic_block("ternary_end")

        self.builder.cbranch(cmp, then_bb, else_bb)

        self.builder.position_at_end(then_bb)
        if node.iftrue is None:
            true_val = cond_val
        else:
            true_val, _ = self.codegen(node.iftrue)
        true_bb_end = self.builder.block

        self.builder.position_at_end(else_bb)
        false_val, _ = self.codegen(node.iffalse)
        false_bb_end = self.builder.block

        def zero_value(target_type):
            return self._zero_initializer(target_type)

        def pick_target_type(lhs, rhs):
            if lhs is None and rhs is None:
                return int64_t
            if lhs is None:
                return rhs.type
            if rhs is None:
                return lhs.type
            if isinstance(lhs.type, ir.ArrayType) or isinstance(rhs.type, ir.ArrayType):
                if isinstance(lhs.type, ir.PointerType):
                    return lhs.type
                if isinstance(rhs.type, ir.PointerType):
                    return rhs.type
                if isinstance(lhs.type, ir.ArrayType):
                    return ir.PointerType(lhs.type.element)
                return ir.PointerType(rhs.type.element)
            if lhs.type == rhs.type:
                return lhs.type
            if isinstance(lhs.type, ir.PointerType) and isinstance(
                rhs.type, ir.PointerType
            ):
                if lhs.type == rhs.type:
                    return lhs.type
                return voidptr_t
            if isinstance(lhs.type, ir.PointerType) and isinstance(
                rhs.type, ir.IntType
            ):
                return lhs.type
            if isinstance(rhs.type, ir.PointerType) and isinstance(
                lhs.type, ir.IntType
            ):
                return rhs.type
            if self._is_floating_ir_type(lhs.type) or self._is_floating_ir_type(
                rhs.type
            ):
                return self._common_float_type(lhs.type, rhs.type)
            if isinstance(lhs.type, ir.IntType) and isinstance(rhs.type, ir.IntType):
                return lhs.type if lhs.type.width >= rhs.type.width else rhs.type
            return lhs.type

        integer_decision = None
        if (
            true_val is not None
            and false_val is not None
            and isinstance(true_val.type, ir.IntType)
            and isinstance(false_val.type, ir.IntType)
        ):
            true_width = max(true_val.type.width, int32_t.width)
            false_width = max(false_val.type.width, int32_t.width)
            true_unsigned = (
                self._is_unsigned_val(true_val)
                if true_val.type.width >= int32_t.width
                else False
            )
            false_unsigned = (
                self._is_unsigned_val(false_val)
                if false_val.type.width >= int32_t.width
                else False
            )
            integer_decision = _decide_usual_integer_conversion(
                true_width,
                true_unsigned,
                false_width,
                false_unsigned,
            )
            if true_val.type.width == integer_decision.target_order:
                target = true_val.type
            elif false_val.type.width == integer_decision.target_order:
                target = false_val.type
            else:
                target = ir.IntType(integer_decision.target_order)
        else:
            target = pick_target_type(true_val, false_val)
        incoming = []
        for branch_end, branch_val in (
            (true_bb_end, true_val),
            (false_bb_end, false_val),
        ):
            if branch_end.is_terminated:
                continue
            self.builder.position_at_end(branch_end)
            value = branch_val if branch_val is not None else zero_value(target)
            if integer_decision is not None and isinstance(value.type, ir.IntType):
                value = self._integer_promotion(value)
                value = self._convert_int_value(
                    value,
                    target,
                    result_unsigned=integer_decision.is_unsigned,
                )
            elif value.type != target or isinstance(value.type, ir.ArrayType):
                value = self._implicit_convert(value, target)
            incoming.append((self.builder.block, value))
            self.builder.branch(merge_bb)

        self.builder.position_at_end(merge_bb)
        if not incoming:
            return zero_value(target), None
        result_is_unsigned = isinstance(target, ir.IntType) and (
            integer_decision.is_unsigned
            if integer_decision is not None
            else any(self._is_unsigned_val(value) for _pred, value in incoming)
        )
        result_has_unsigned_pointee = isinstance(target, ir.PointerType) and any(
            self._is_unsigned_pointee(value) for _pred, value in incoming
        )
        result_has_unsigned_return = isinstance(target, ir.PointerType) and any(
            self._is_unsigned_return(value) for _pred, value in incoming
        )
        if len(incoming) == 1:
            result = incoming[0][1]
            if result_is_unsigned:
                self._tag_unsigned(result)
            if result_has_unsigned_pointee:
                self._tag_unsigned_pointee(result)
            if result_has_unsigned_return:
                self._tag_unsigned_return(result)
            return result, None

        phi = self.builder.phi(target, "ternary")
        for pred, value in incoming:
            phi.add_incoming(value, pred)
        if result_is_unsigned:
            self._tag_unsigned(phi)
        if result_has_unsigned_pointee:
            self._tag_unsigned_pointee(phi)
        if result_has_unsigned_return:
            self._tag_unsigned_return(phi)
        return phi, None

    def codegen_Cast(self, node):
        dest_ir_type = self._resolve_ast_type(node.to_type.type)
        if isinstance(node.expr, c_ast.InitList) and (
            isinstance(dest_ir_type, ir.ArrayType)
            or _is_struct_ir_type(dest_ir_type)
            or getattr(dest_ir_type, "is_union", False)
        ):
            return self._materialize_compound_literal(node.to_type.type, node.expr)

        expr, ptr = self.codegen(node.expr)

        if (
            expr is not None
            and expr.type == dest_ir_type
            and (
                self._is_aggregate_ir_type(dest_ir_type)
                or isinstance(dest_ir_type, ir.ArrayType)
            )
        ):
            self._set_expr_ir_type(node, dest_ir_type)
            return expr, ptr

        self._validate_explicit_cast(expr.type, dest_ir_type)
        # Check if casting to unsigned type
        is_unsigned = False
        if isinstance(node.to_type.type, c_ast.TypeDecl) and isinstance(
            node.to_type.type.type, c_ast.IdentifierType
        ):
            is_unsigned = self._is_unsigned_type_names(node.to_type.type.type.names)
        if self._is_floating_ir_type(expr.type) and isinstance(
            dest_ir_type, ir.IntType
        ):
            if is_unsigned:
                result = self.builder.fptoui(expr, dest_ir_type)
                self._tag_value_from_decl_type(result, node.to_type.type)
                return result, None
            result = self.builder.fptosi(expr, dest_ir_type)
            self._clear_unsigned(result)
            self._tag_value_from_decl_type(result, node.to_type.type)
            return result, None
        if self._same_ir_type_semantics(expr.type, dest_ir_type):
            if isinstance(dest_ir_type, ir.IntType):
                if is_unsigned:
                    if self._is_unsigned_val(expr):
                        self._tag_value_from_decl_type(expr, node.to_type.type)
                        return expr, None
                    result = self.builder.add(
                        expr, ir.Constant(dest_ir_type, 0), "casttmp"
                    )
                    self._tag_unsigned(result)
                    self._tag_value_from_decl_type(result, node.to_type.type)
                    return result, None
                if self._is_unsigned_val(expr):
                    result = self.builder.add(
                        expr, ir.Constant(dest_ir_type, 0), "casttmp"
                    )
                    self._tag_value_from_decl_type(result, node.to_type.type)
                    return result, None
                self._clear_unsigned(expr)
            if is_unsigned:
                self._tag_unsigned(expr)
            self._tag_value_from_decl_type(expr, node.to_type.type)
            return expr, ptr
        result = self._implicit_convert(expr, dest_ir_type)
        if is_unsigned:
            self._tag_unsigned(result)
        elif isinstance(dest_ir_type, ir.IntType):
            self._clear_unsigned(result)
        self._tag_value_from_decl_type(result, node.to_type.type)
        return result, None

    def codegen_CompoundLiteral(self, node):
        return self._materialize_compound_literal(node.type.type, node.init)

    def codegen_FuncCall(self, node):
        if isinstance(node.name, c_ast.ID):
            _alias = self._BUILTIN_SYMBOL_ALIASES.get(node.name.name)
            if _alias is not None and node.name.name not in self.env:
                node.name = c_ast.ID(_alias, coord=node.name.coord)

        callee = None
        if isinstance(node.name, c_ast.ID):
            callee = node.name.name
            if callee == "__builtin_va_start":
                return self._codegen_builtin_va_start(node)
            if callee == "__builtin_va_end":
                return self._codegen_builtin_va_end(node)
            if callee == "__builtin_va_copy":
                return self._codegen_builtin_va_copy(node)
            if callee == "__builtin_alloca":
                return self._codegen_builtin_alloca(node)
            if callee == "alloca":
                return self._codegen_builtin_alloca(node)
            if callee == "__builtin_va_arg":
                return ir.Constant(voidptr_t, None), None
            if callee == "__builtin_expect":
                return self._codegen_builtin_expect(node)
            if callee == "__builtin_trap":
                return self._codegen_builtin_trap(node)
            if callee == "__builtin_assume":
                return ir.Constant(int64_t, 0), None
            if callee == "__builtin_prefetch":
                return ir.Constant(int64_t, 0), None
            if callee == "__builtin_unreachable":
                return self._codegen_builtin_unreachable(node)
            if callee == "__builtin_classify_type":
                return self._codegen_builtin_classify_type(node)
            if callee == "__builtin_add_overflow":
                return self._codegen_builtin_overflow(node, "add")
            if callee == "__builtin_sub_overflow":
                return self._codegen_builtin_overflow(node, "sub")
            if callee == "__builtin_mul_overflow":
                return self._codegen_builtin_overflow(node, "mul")
            if callee in {"abs", "__builtin_abs"}:
                return self._codegen_builtin_abs(node, int32_t)
            if callee in {
                "labs",
                "llabs",
                "imaxabs",
                "__builtin_labs",
                "__builtin_llabs",
                "__builtin_imaxabs",
            }:
                return self._codegen_builtin_abs(node, int64_t)
            if callee == "__builtin_bswap16":
                return self._codegen_builtin_bswap(node, 16)
            if callee == "__builtin_bswap32":
                return self._codegen_builtin_bswap(node, 32)
            if callee == "__builtin_bswap64":
                return self._codegen_builtin_bswap(node, 64)
            if callee == "__builtin_rotateleft32":
                return self._codegen_builtin_rotate(node, 32, "left")
            if callee == "__builtin_rotateleft64":
                return self._codegen_builtin_rotate(node, 64, "left")
            if callee == "__builtin_rotateright32":
                return self._codegen_builtin_rotate(node, 32, "right")
            if callee == "__builtin_rotateright64":
                return self._codegen_builtin_rotate(node, 64, "right")
            if callee == "__builtin_clz":
                return self._codegen_builtin_bitcount(node, 32, "ctlz")
            if callee == "__builtin_clzll":
                return self._codegen_builtin_bitcount(node, 64, "ctlz")
            if callee == "__builtin_ctz":
                return self._codegen_builtin_bitcount(node, 32, "cttz")
            if callee == "__builtin_ctzll":
                return self._codegen_builtin_bitcount(node, 64, "cttz")
            if callee == "__builtin_ffs":
                return self._codegen_builtin_ffs(node, 32)
            if callee == "__builtin_ffsll":
                return self._codegen_builtin_ffs(node, 64)
            if callee == "__builtin_frame_address":
                return self._codegen_builtin_frame_address(node)
            if callee == "__builtin_memcmp":
                callee = "memcmp"
            if callee == "__builtin_memchr":
                callee = "memchr"
            if callee == "__builtin_strcmp":
                callee = "strcmp"
            if callee == "__builtin_strcpy":
                callee = "strcpy"
            if callee == "__builtin_sprintf":
                callee = "sprintf"
            if callee == "__builtin_snprintf":
                callee = "snprintf"
            if callee == "__builtin_inf":
                return self._ir_constant_from_value(_double, float("inf")), None
            if callee == "__builtin_inff":
                return self._ir_constant_from_value(_float, float("inf")), None
            if callee == "__builtin_infl":
                return self._ir_constant_from_value(_double, float("inf")), None
            if callee == "__builtin_nan":
                return self._ir_constant_from_value(_double, float("nan")), None
            if callee == "__builtin_nanf":
                return self._ir_constant_from_value(_float, float("nan")), None
            if callee == "__builtin_nanl":
                return self._ir_constant_from_value(_double, float("nan")), None
            if callee in ("__builtin_isnan", "__builtin_isnanf", "__builtin_isnanl"):
                return self._codegen_builtin_isnan(node)
            if callee in ("__builtin_isfinite", "__builtin_finite"):
                return self._codegen_builtin_isfinite(node)
            if callee in ("__builtin_isinf", "__builtin_isinff", "__builtin_isinfl"):
                return self._codegen_builtin_isinf(node)
            if callee == "__builtin_signbit":
                return self._codegen_builtin_signbit(node)
            if callee == "__builtin_isunordered":
                return self._codegen_builtin_isunordered(node)
            if callee == "__builtin_isless":
                return self._codegen_builtin_ordered_compare(node, "<", "isless")
            if callee == "__builtin_islessequal":
                return self._codegen_builtin_ordered_compare(
                    node, "<=", "islessequal"
                )
            if callee == "__builtin_isgreater":
                return self._codegen_builtin_ordered_compare(node, ">", "isgreater")
            if callee == "__builtin_isgreaterequal":
                return self._codegen_builtin_ordered_compare(
                    node, ">=", "isgreaterequal"
                )
            if callee == "__builtin_islessgreater":
                return self._codegen_builtin_islessgreater(node)
            if callee == "__builtin_copysign":
                return self._codegen_builtin_copysign(node, _double)
            if callee == "__builtin_copysignf":
                return self._codegen_builtin_copysign(node, _float)
            if callee == "__builtin_copysignl":
                return self._codegen_builtin_copysign(node, _double)
            if callee == "__sync_synchronize":
                return self._codegen_builtin_sync_synchronize(node)
            if callee == "__sync_fetch_and_add":
                return self._codegen_builtin_sync_fetch_and_add(node)
            if callee == "__sync_bool_compare_and_swap":
                return self._codegen_builtin_sync_bool_compare_and_swap(node)
            if callee == "__atomic_load_n":
                return self._codegen_builtin_atomic_load(node)
            if callee == "__atomic_store_n":
                return self._codegen_builtin_atomic_store(node)
            if callee == "__atomic_add_fetch":
                return self._codegen_builtin_atomic_fetch_op(node, "add")
            if callee == "__atomic_sub_fetch":
                return self._codegen_builtin_atomic_fetch_op(node, "sub")
            if callee == "__atomic_or_fetch":
                return self._codegen_builtin_atomic_fetch_op(node, "or")
            if callee == "__atomic_and_fetch":
                return self._codegen_builtin_atomic_fetch_op(node, "and")
            if callee == "__atomic_xor_fetch":
                return self._codegen_builtin_atomic_fetch_op(node, "xor")
            if callee == "__atomic_fetch_add":
                return self._codegen_builtin_atomic_fetch_op(node, "add", return_new=False)
            if callee == "__atomic_fetch_sub":
                return self._codegen_builtin_atomic_fetch_op(node, "sub", return_new=False)
            if callee == "__atomic_fetch_or":
                return self._codegen_builtin_atomic_fetch_op(node, "or", return_new=False)
            if callee == "__atomic_fetch_and":
                return self._codegen_builtin_atomic_fetch_op(node, "and", return_new=False)
            if callee == "__atomic_fetch_xor":
                return self._codegen_builtin_atomic_fetch_op(node, "xor", return_new=False)
            if callee == "__atomic_exchange_n":
                return self._codegen_builtin_atomic_fetch_op(
                    node, "xchg", return_new=False
                )
            if callee == "__atomic_compare_exchange_n":
                return self._codegen_builtin_atomic_compare_exchange(node)
            if callee == "__atomic_test_and_set":
                return self._codegen_builtin_atomic_test_and_set(node)
            if callee == "__atomic_clear":
                return self._codegen_builtin_atomic_clear(node)
            if callee == "__atomic_thread_fence":
                return self._codegen_builtin_atomic_thread_fence(node)
        else:
            # Calling function pointer in struct: s.fn(args)
            call_args = []
            arg_nodes = []
            if node.args:
                arg_nodes = list(node.args.exprs)
                call_args = [self.codegen(arg)[0] for arg in arg_nodes]
            fp_val, _ = self.codegen(node.name)
            if isinstance(fp_val.type, ir.PointerType) and isinstance(
                fp_val.type.pointee, ir.FunctionType
            ):
                # Coerce args to match function pointer param types
                ftype = fp_val.type.pointee
                coerced = []
                for j, a in enumerate(call_args):
                    arg_node = arg_nodes[j] if j < len(arg_nodes) else None
                    if j < len(ftype.args):
                        coerced.append(
                            self._coerce_arg(a, ftype.args[j], arg_node=arg_node)
                        )
                    else:
                        coerced.append(
                            self._default_arg_promotion(a, arg_node=arg_node)
                        )
                call_args = coerced
                ret_type = ftype.return_type
                if isinstance(ret_type, ir.VoidType):
                    self.builder.call(fp_val, call_args)
                    return ir.Constant(int64_t, 0), None
                result = self.builder.call(fp_val, call_args, "fpcall")
                return (
                    self._extend_call_result(
                        result, returns_unsigned=self._is_unsigned_return(fp_val)
                    ),
                    None,
                )
            # Not a function pointer — can't call, return dummy
            return ir.Constant(int64_t, 0), None

        try:
            _, callee_func = self.lookup(callee)
        except (KeyError, SemanticError):
            _, callee_func = self._declare_implicit_function(
                callee,
                call_arg_count=len(node.args.exprs) if node.args else 0,
            )

        call_args = []
        arg_nodes = []
        if node.args:
            arg_nodes = list(node.args.exprs)
            call_args = [self.codegen(arg)[0] for arg in arg_nodes]

        # Function pointer: load the pointer and call through it
        if not isinstance(callee_func, ir.Function):
            if hasattr(callee_func, "type") and isinstance(
                callee_func.type, ir.PointerType
            ):
                loaded = self._safe_load(callee_func, name="fptr")
                if self._is_unsigned_return_binding(callee_func):
                    self._tag_unsigned_return(loaded)
                # loaded could be a function pointer (ptr to FunctionType)
                # or the alloca's pointee could be a function ptr
                func_val = loaded
                if isinstance(func_val.type, ir.PointerType) and isinstance(
                    func_val.type.pointee, ir.FunctionType
                ):
                    ftype = func_val.type.pointee
                    coerced = [
                        self._coerce_arg(
                            a,
                            ftype.args[j],
                            arg_node=arg_nodes[j] if j < len(arg_nodes) else None,
                        )
                        if j < len(ftype.args)
                        else self._default_arg_promotion(
                            a,
                            arg_node=arg_nodes[j] if j < len(arg_nodes) else None,
                        )
                        for j, a in enumerate(call_args)
                    ]
                    ret_type = ftype.return_type
                    is_void = isinstance(ret_type, ir.VoidType)
                    if is_void:
                        self.builder.call(func_val, coerced)
                        return ir.Constant(int64_t, 0), None
                    result = self.builder.call(func_val, coerced, "fpcall")
                    return (
                        self._extend_call_result(
                            result, returns_unsigned=self._is_unsigned_return(func_val)
                        ),
                        None,
                    )
            return ir.Constant(int64_t, 0), None  # unknown function — return dummy

        if callee_func is None or not isinstance(callee_func, (ir.Function,)):
            return ir.Constant(int64_t, 0), None

        # Convert arguments to match function parameter types
        converted = self._convert_call_args(
            call_args, callee_func, arg_nodes=arg_nodes
        )
        secure_memset_result = self._maybe_codegen_secure_clear(
            callee, converted
        )
        if secure_memset_result is not None:
            return secure_memset_result

        # Call and handle return type
        call_target = self._direct_call_callee(callee_func, converted)

        try:
            is_void = isinstance(callee_func.return_value.type, ir.VoidType)
        except Exception:
            is_void = False
        try:
            if is_void:
                self.builder.call(call_target, converted)
                return ir.Constant(int64_t, 0), None
            result = self.builder.call(call_target, converted, "calltmp")
        except (TypeError, IndexError):
            # Arg count/type mismatch — return dummy value
            return ir.Constant(int64_t, 0), None

        # Widen small int returns (e.g., i32 from strcmp) to i64
        return (
            self._extend_call_result(
                result, returns_unsigned=self._is_unsigned_return_binding(callee_func)
            ),
            None,
        )

    def _is_integer_zero_constant(self, value):
        if not isinstance(getattr(value, "type", None), ir.IntType):
            return False
        raw = getattr(value, "value", None)
        if raw is None:
            raw = getattr(value, "constant", None)
        try:
            return int(raw) == 0
        except (TypeError, ValueError):
            return False

    def _maybe_codegen_secure_clear(self, callee, converted):
        """Lower secret-clearing calls so dead-store elimination cannot erase
        the zero-fill of a buffer that dies (CWE-14 / CWE-226).

        Three call shapes are recognized:

          * ``memset(dst, 0, n)``          -> volatile ``llvm.memset``
          * ``explicit_bzero(dst, n)``     -> volatile ``llvm.memset`` (0)
          * ``memset_s(dst, smax, ch, n)`` -> bounded volatile ``llvm.memset`` (ch)

        The clear is a *volatile* ``llvm.memset`` (``isvolatile == true``). A
        volatile store is an observable side effect the optimizer is not allowed
        to remove, so the zero-fill survives ``-O2`` DSE without any extra
        optimization barrier. (An earlier revision also emitted glibc's inline
        ``asm sideeffect "" ... ~{memory}`` barrier for defense in depth, but it
        was redundant given the volatile store and, more importantly, the
        LLVM-free ``self`` backend cannot parse an inline-asm call — it broke
        real programs such as lz4 that memset internally. The volatile lowering
        is the mechanism the task explicitly lists as sufficient and it is
        backend-agnostic.) ``explicit_bzero`` returns ``void`` (modeled as 0);
        ``memset_s`` caps the write at ``smax`` and returns non-zero when
        ``n > smax`` rather than turning a bounded clear into an out-of-bounds
        volatile write."""
        if len(converted) < 2:
            return None
        # A translation unit that DEFINES a libc primitive must not have its
        # memset-zero calls turned into llvm.memset: LLVM's Darwin lowering
        # rewrites llvm.memset(p, 0, n) back into a `bzero` call, so libc's
        # own bzero would tail-branch to itself and recurse forever
        # (BUG-P1-SELF-MEM-INTRINSIC-LIBCALL-SELF-BRANCH). PCC_NO_BUILTIN=1 is
        # pcc's -fno-builtin: keep the plain call the source wrote.
        if _no_builtin_enabled():
            return None

        if callee == "memset":
            if len(converted) < 3:
                return None
            dst, fill, size = converted[:3]
            if not self._is_integer_zero_constant(fill):
                return None
            fill_byte = ir.Constant(int8_t, 0)
            result = None  # memset returns dst
        elif callee == "explicit_bzero":
            if len(converted) < 2:
                return None
            dst, size = converted[0], converted[1]
            fill_byte = ir.Constant(int8_t, 0)
            result = ir.Constant(int64_t, 0)  # void return, modeled as 0
        elif callee == "memset_s":
            # memset_s(void *dst, rsize_t smax, int ch, rsize_t n)
            if len(converted) < 4:
                return None
            dst, smax, ch, size = converted[:4]
            if isinstance(getattr(ch, "type", None), ir.IntType):
                fill_byte = self._convert_int_value(ch, int8_t)
            else:
                fill_byte = ir.Constant(int8_t, 0)
            result = None  # Filled after width-normalizing smax/size.
        else:
            return None

        if not isinstance(getattr(dst, "type", None), ir.PointerType):
            return None
        if dst.type == voidptr_t:
            dst_ptr = dst
        else:
            dst_ptr = self.builder.bitcast(dst, voidptr_t, name="secure.memset.dst")
        if getattr(size, "type", None) != int64_t:
            size = self._implicit_convert(size, int64_t)
        if callee == "memset_s":
            if getattr(smax, "type", None) != int64_t:
                smax = self._implicit_convert(smax, int64_t)
            in_bounds = self.builder.icmp_unsigned(
                "<=", size, smax, name="memset_s.in_bounds"
            )
            size = self.builder.select(
                in_bounds, size, smax, name="memset_s.size"
            )
            result = self.builder.select(
                in_bounds,
                ir.Constant(int64_t, 0),
                ir.Constant(int64_t, 22),
                name="memset_s.errno",
            )
        memset_intrinsic = self._get_or_declare_intrinsic(
            "llvm.memset.p0.i64",
            ir.VoidType(),
            [voidptr_t, int8_t, int64_t, bool_t],
        )
        self.builder.call(
            memset_intrinsic,
            [dst_ptr, fill_byte, size, true_bit],
            name="secure.memset",
        )
        return dst_ptr if result is None else result, None

    def _get_or_declare_intrinsic(self, name, ret_type, arg_types):
        existing = self.module.globals.get(name)
        if existing is not None:
            return existing
        return ir.Function(self.module, ir.FunctionType(ret_type, arg_types), name=name)

    def _builtin_va_list_storage(self, expr):
        value, addr = self.codegen(expr)
        storage = addr if addr is not None else value
        if not isinstance(getattr(storage, "type", None), ir.PointerType):
            return None
        return storage

    def _codegen_builtin_va_start(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int64_t, 0), None
        ap_addr = self._builtin_va_list_storage(node.args.exprs[0])
        if ap_addr is None:
            return ir.Constant(int64_t, 0), None
        intrinsic = self._get_or_declare_intrinsic(
            "llvm.va_start", ir.VoidType(), [voidptr_t]
        )
        arg = ap_addr
        if arg.type != voidptr_t:
            arg = self.builder.bitcast(arg, voidptr_t, name="vastartarg")
        self.builder.call(intrinsic, [arg])
        return ir.Constant(int64_t, 0), None

    def _codegen_builtin_va_end(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int64_t, 0), None
        ap_addr = self._builtin_va_list_storage(node.args.exprs[0])
        if ap_addr is None:
            return ir.Constant(int64_t, 0), None
        intrinsic = self._get_or_declare_intrinsic(
            "llvm.va_end", ir.VoidType(), [voidptr_t]
        )
        arg = ap_addr
        if arg.type != voidptr_t:
            arg = self.builder.bitcast(arg, voidptr_t, name="vaendarg")
        self.builder.call(intrinsic, [arg])
        return ir.Constant(int64_t, 0), None

    def _codegen_builtin_va_copy(self, node):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int64_t, 0), None
        dst_addr = self._builtin_va_list_storage(node.args.exprs[0])
        src_addr = self._builtin_va_list_storage(node.args.exprs[1])
        if dst_addr is None:
            return ir.Constant(int64_t, 0), None
        if src_addr is None:
            return ir.Constant(int64_t, 0), None
        src_val = self._safe_load(src_addr)
        dst_pointee = dst_addr.type.pointee
        if src_val.type != dst_pointee:
            src_val = self._implicit_convert(src_val, dst_pointee)
        self._safe_store(src_val, dst_addr)
        return ir.Constant(int64_t, 0), None

    def _codegen_aggregate_va_arg(self, ap_addr, aggregate_type):
        if ap_addr is None or not isinstance(getattr(ap_addr, "type", None), ir.PointerType):
            return None, None

        current_ap = self._safe_load(ap_addr)
        if not isinstance(getattr(current_ap, "type", None), ir.PointerType):
            return None, None

        src_ptr = current_ap if current_ap.type == voidptr_t else self.builder.bitcast(
            current_ap, voidptr_t, name="vaargsrc"
        )

        value_size = self._ir_type_size(aggregate_type)
        slot_size = self._align_up(value_size, 8)
        aggregate_align = max(1, self._ir_type_align(aggregate_type))

        temp = self._alloca_in_entry(
            aggregate_type, f"vaarg_agg_{self._vaarg_counter}"
        )
        try:
            temp.align = aggregate_align
        except Exception:
            pass
        dst_ptr = self.builder.bitcast(temp, voidptr_t, name="vaargdst")

        memcpy = self._get_or_declare_intrinsic(
            "llvm.memcpy.p0.p0.i64",
            ir.VoidType(),
            [voidptr_t, voidptr_t, int64_t, ir.IntType(1)],
        )
        self.builder.call(
            memcpy,
            [
                dst_ptr,
                src_ptr,
                ir.Constant(int64_t, value_size),
                ir.Constant(ir.IntType(1), 0),
            ],
            name=f"vaargcpy.{self._vaarg_counter}",
        )

        next_ptr = self.builder.gep(
            src_ptr,
            [ir.Constant(int64_t, slot_size)],
            inbounds=True,
            name=f"vaargnext.{self._vaarg_counter}",
        )
        stored_next_ptr = next_ptr
        if ap_addr.type.pointee != next_ptr.type:
            stored_next_ptr = self.builder.bitcast(
                next_ptr,
                ap_addr.type.pointee,
                name=f"vaargnextcast.{self._vaarg_counter}",
            )
        self._safe_store(stored_next_ptr, ap_addr)

        return self._safe_load(temp), temp

    def _flatten_homogeneous_floating_members(self, ir_type):
        if self._is_floating_ir_type(ir_type):
            return [ir_type]

        if isinstance(ir_type, ir.ArrayType):
            nested = self._flatten_homogeneous_floating_members(ir_type.element)
            if nested is None:
                return None
            return nested * ir_type.count

        if not _is_struct_ir_type(ir_type):
            return None

        flattened = []
        for member_type in self._aggregate_member_ir_types(ir_type):
            nested = self._flatten_homogeneous_floating_members(member_type)
            if nested is None:
                return None
            flattened.extend(nested)

        if not flattened:
            return None

        first = flattened[0]
        if not all(str(member_type) == str(first) for member_type in flattened):
            return None
        return flattened

    def _coerce_variadic_aggregate_arg(self, arg):
        if not self._is_aggregate_ir_type(arg.type):
            return arg

        source_type = arg.type
        source_size = self._ir_type_size(source_type)
        source_align = max(1, self._ir_type_align(source_type))

        source_tmp = self._alloca_in_entry(source_type, "varargagg.src")
        try:
            source_tmp.align = source_align
        except Exception:
            pass
        self._safe_store(arg, source_tmp)
        source_ptr = self.builder.bitcast(source_tmp, voidptr_t, name="varargaggsrc")

        hfa_members = self._flatten_homogeneous_floating_members(source_type)
        if hfa_members and 1 <= len(hfa_members) <= 4:
            packed_type = ir.ArrayType(hfa_members[0], len(hfa_members))
            if self._ir_type_size(packed_type) == source_size:
                packed_align = max(1, self._ir_type_align(packed_type))
                packed_tmp = self._alloca_in_entry(packed_type, "varargagg.hfa")
                try:
                    packed_tmp.align = packed_align
                except Exception:
                    pass
                packed_ptr = self.builder.bitcast(
                    packed_tmp, voidptr_t, name="varargagghfaptr"
                )
                memcpy = self._get_or_declare_intrinsic(
                    "llvm.memcpy.p0.p0.i64",
                    ir.VoidType(),
                    [voidptr_t, voidptr_t, int64_t, ir.IntType(1)],
                )
                self.builder.call(
                    memcpy,
                    [
                        packed_ptr,
                        source_ptr,
                        ir.Constant(int64_t, source_size),
                        ir.Constant(ir.IntType(1), 0),
                    ],
                    name="varargagg.hfacpy",
                )
                return self._safe_load(packed_tmp)

        chunk_count = max(1, self._align_up(source_size, 8) // 8)
        packed_type = ir.ArrayType(int64_t, chunk_count)
        packed_tmp = self._alloca_in_entry(packed_type, "varargagg.i64")
        try:
            packed_tmp.align = 8
        except Exception:
            pass
        self._safe_store(ir.Constant(packed_type, None), packed_tmp)
        packed_ptr = self.builder.bitcast(packed_tmp, voidptr_t, name="varargaggi64ptr")
        memcpy = self._get_or_declare_intrinsic(
            "llvm.memcpy.p0.p0.i64",
            ir.VoidType(),
            [voidptr_t, voidptr_t, int64_t, ir.IntType(1)],
        )
        self.builder.call(
            memcpy,
            [
                packed_ptr,
                source_ptr,
                ir.Constant(int64_t, source_size),
                ir.Constant(ir.IntType(1), 0),
            ],
            name="varargagg.i64cpy",
        )
        return self._safe_load(packed_tmp)

    def _codegen_builtin_expect(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int64_t, 0), None
        value, _ = self.codegen(node.args.exprs[0])
        return value, None

    def _codegen_builtin_trap(self, node):
        intrinsic = self._get_or_declare_intrinsic("llvm.trap", ir.VoidType(), [])
        self.builder.call(intrinsic, [])
        if self.builder is not None and not self.builder.block.is_terminated:
            self.builder.unreachable()
            dead_bb = self.function.append_basic_block(name="after_trap")
            self.builder.position_at_end(dead_bb)
        return ir.Constant(int64_t, 0), None

    def _codegen_builtin_classify_type(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int32_t, 1), None
        arg_node = node.args.exprs[0]
        expr_key = self._generic_expr_type_key(arg_node)
        if isinstance(expr_key, tuple) and expr_key and expr_key[0] == "base":
            names = expr_key[2]
            if "float" in names or "double" in names:
                return ir.Constant(int32_t, 8), None
            return ir.Constant(int32_t, 1), None
        value, _ = self.codegen(arg_node)
        if self._is_floating_ir_type(getattr(value, "type", None)):
            return ir.Constant(int32_t, 8), None
        return ir.Constant(int32_t, 1), None

    def _codegen_builtin_unreachable(self, node):
        if self.builder is not None and not self.builder.block.is_terminated:
            self.builder.unreachable()
            dead_bb = self.function.append_basic_block(name="after_unreachable")
            self.builder.position_at_end(dead_bb)
        return ir.Constant(int64_t, 0), None

    def _codegen_builtin_alloca(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(voidptr_t, None), None

        size_val, _ = self.codegen(node.args.exprs[0])
        if not isinstance(getattr(size_val, "type", None), ir.IntType):
            size_val = self._implicit_convert(size_val, int64_t)

        return self.builder.alloca(int8_t, size=size_val, name="builtinalloca"), None

    def _codegen_builtin_frame_address(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(voidptr_t, None), None
        try:
            level = int(self._eval_const_expr(node.args.exprs[0]))
        except Exception:
            level = 0
        if level != 0 or self.builder is None:
            return ir.Constant(voidptr_t, None), None
        if self._frame_address_marker is None:
            self._frame_address_marker = self._alloca_in_entry(
                int8_t, "__builtin_frame_address"
            )
        if self._frame_address_marker.type == voidptr_t:
            return self._frame_address_marker, None
        return self.builder.bitcast(
            self._frame_address_marker, voidptr_t, name="frameaddrcast"
        ), None

    def _codegen_builtin_ffs(self, node, width):
        if not node.args or not node.args.exprs:
            return ir.Constant(int32_t, 0), None

        arg, _ = self.codegen(node.args.exprs[0])
        arg_type = ir.IntType(width)
        if not isinstance(getattr(arg, "type", None), ir.IntType) or arg.type != arg_type:
            arg = self._implicit_convert(arg, arg_type)

        zero = ir.Constant(arg_type, 0)
        is_zero = self.builder.icmp_unsigned("==", arg, zero, name="ffsiszero")
        intrinsic = self._get_or_declare_intrinsic(
            f"llvm.cttz.i{width}",
            arg_type,
            [arg_type, ir.IntType(1)],
        )
        cttz = self.builder.call(
            intrinsic,
            [arg, ir.Constant(ir.IntType(1), 0)],
            name="ffstmp",
        )
        if cttz.type != int32_t:
            cttz = self.builder.trunc(cttz, int32_t, name="ffsi32")
        plus_one = self.builder.add(cttz, ir.Constant(int32_t, 1), name="ffsplusone")
        result = self.builder.select(is_zero, ir.Constant(int32_t, 0), plus_one)
        self._clear_unsigned(result)
        return result, None

    def _codegen_builtin_abs(self, node, target_type):
        if not node.args or not node.args.exprs:
            return ir.Constant(target_type, 0), None
        value, _ = self.codegen(node.args.exprs[0])
        if not isinstance(getattr(value, "type", None), ir.IntType):
            value = self._implicit_convert(value, target_type)
        if value.type != target_type:
            value = self._implicit_convert(value, target_type)
        zero = ir.Constant(target_type, 0)
        is_negative = self.builder.icmp_signed("<", value, zero, name="absneg")
        negated = self.builder.neg(value, name="abstmp")
        result = self.builder.select(is_negative, negated, value, name="abs")
        self._clear_unsigned(result)
        return result, None

    def _coerce_builtin_float_arg(self, expr, target_type=None):
        value, _ = self.codegen(expr)
        if target_type is None:
            if isinstance(getattr(value, "type", None), ir.HalfType):
                target_type = ir.HalfType()
            elif isinstance(getattr(value, "type", None), ir.FloatType):
                target_type = _float
            else:
                target_type = _double
        if not self._is_floating_ir_type(getattr(value, "type", None)):
            value = self._implicit_convert(value, target_type)
        elif value.type != target_type:
            value = self._implicit_convert(value, target_type)
        return value

    def _materialize_compound_literal(self, ast_type, init_node):
        dest_ir_type = self._compound_literal_ir_type(ast_type, init_node)
        if self.builder is None:
            return self._build_const_init(init_node, dest_ir_type), None
        tmp_ptr = self._alloca_in_entry(dest_ir_type, "compoundlit")
        self._safe_store(self._zero_initializer(dest_ir_type), tmp_ptr)
        self._init_runtime_value(tmp_ptr, dest_ir_type, init_node)
        value = self._safe_load(tmp_ptr, name="compoundlitval")
        self._tag_value_from_decl_type(value, ast_type)
        return value, tmp_ptr

    def _builtin_bool_to_i32(self, value, name):
        return self.builder.zext(value, int32_t, name=name), None

    def _codegen_builtin_isnan(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int32_t, 0), None
        value = self._coerce_builtin_float_arg(node.args.exprs[0])
        result = self.builder.fcmp_unordered("!=", value, value, name="isnan")
        return self._builtin_bool_to_i32(result, "isnani32")

    def _codegen_builtin_isinf(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int32_t, 0), None
        value = self._coerce_builtin_float_arg(node.args.exprs[0])
        pos_inf = ir.Constant(value.type, float("inf"))
        neg_inf = ir.Constant(value.type, float("-inf"))
        is_pos = self.builder.fcmp_ordered("==", value, pos_inf, name="isinfpos")
        is_neg = self.builder.fcmp_ordered("==", value, neg_inf, name="isinfneg")
        return self._builtin_bool_to_i32(
            self.builder.or_(is_pos, is_neg, name="isinf"),
            "isinfi32",
        )

    def _codegen_builtin_isfinite(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int32_t, 0), None
        value = self._coerce_builtin_float_arg(node.args.exprs[0])
        not_nan = self.builder.fcmp_ordered("==", value, value, name="isfinitenotnan")
        pos_inf = self.builder.fcmp_ordered(
            "==", value, ir.Constant(value.type, float("inf")), name="isfiniteposinf"
        )
        neg_inf = self.builder.fcmp_ordered(
            "==", value, ir.Constant(value.type, float("-inf")), name="isfiniteneginf"
        )
        is_inf = self.builder.or_(pos_inf, neg_inf, name="isfiniteisinf")
        result = self.builder.and_(
            not_nan,
            self.builder.not_(is_inf, name="isfinite_not_inf"),
            name="isfinite",
        )
        return self._builtin_bool_to_i32(result, "isfinitei32")

    def _codegen_builtin_signbit(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int32_t, 0), None
        value = self._coerce_builtin_float_arg(node.args.exprs[0])
        if isinstance(value.type, ir.HalfType):
            int_type = ir.IntType(16)
            sign_mask = ir.Constant(int_type, 0x8000)
        elif isinstance(value.type, ir.FloatType):
            int_type = ir.IntType(32)
            sign_mask = ir.Constant(int_type, 0x80000000)
        else:
            int_type = ir.IntType(64)
            sign_mask = ir.Constant(int_type, 0x8000000000000000)
        bits = self.builder.bitcast(value, int_type, name="signbitbits")
        masked = self.builder.and_(bits, sign_mask, name="signbitmask")
        result = self.builder.icmp_unsigned(
            "!=", masked, ir.Constant(int_type, 0), name="signbit"
        )
        return self._builtin_bool_to_i32(result, "signbiti32")

    def _codegen_builtin_isunordered(self, node):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int32_t, 0), None
        lhs = self._coerce_builtin_float_arg(node.args.exprs[0])
        rhs = self._coerce_builtin_float_arg(node.args.exprs[1], lhs.type)
        lhs_nan = self.builder.fcmp_unordered("!=", lhs, lhs, name="lhsnan")
        rhs_nan = self.builder.fcmp_unordered("!=", rhs, rhs, name="rhsnan")
        result = self.builder.or_(lhs_nan, rhs_nan, name="isunord")
        return self._builtin_bool_to_i32(result, "isunordi32")

    def _codegen_builtin_ordered_compare(self, node, op, name):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int32_t, 0), None
        lhs = self._coerce_builtin_float_arg(node.args.exprs[0])
        rhs = self._coerce_builtin_float_arg(node.args.exprs[1], lhs.type)
        result = self.builder.fcmp_ordered(op, lhs, rhs, name=name)
        return self._builtin_bool_to_i32(result, f"{name}i32")

    def _codegen_builtin_islessgreater(self, node):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int32_t, 0), None
        lhs = self._coerce_builtin_float_arg(node.args.exprs[0])
        rhs = self._coerce_builtin_float_arg(node.args.exprs[1], lhs.type)
        result = self.builder.fcmp_ordered("!=", lhs, rhs, name="islessgreater")
        return self._builtin_bool_to_i32(result, "islessgreateri32")

    def _codegen_builtin_copysign(self, node, target_type):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(target_type, 0.0), None
        magnitude = self._coerce_builtin_float_arg(node.args.exprs[0], target_type)
        sign = self._coerce_builtin_float_arg(node.args.exprs[1], target_type)

        if isinstance(target_type, ir.HalfType):
            int_type = ir.IntType(16)
            sign_mask = ir.Constant(int_type, 0x8000)
            value_mask = ir.Constant(int_type, 0x7FFF)
        elif isinstance(target_type, ir.FloatType):
            int_type = ir.IntType(32)
            sign_mask = ir.Constant(int_type, 0x80000000)
            value_mask = ir.Constant(int_type, 0x7FFFFFFF)
        else:
            int_type = ir.IntType(64)
            sign_mask = ir.Constant(int_type, 0x8000000000000000)
            value_mask = ir.Constant(int_type, 0x7FFFFFFFFFFFFFFF)

        magnitude_bits = self.builder.bitcast(
            magnitude, int_type, name="copysignmagbits"
        )
        sign_bits = self.builder.bitcast(sign, int_type, name="copysignsignbits")
        magnitude_bits = self.builder.and_(
            magnitude_bits, value_mask, name="copysignmag"
        )
        sign_bits = self.builder.and_(sign_bits, sign_mask, name="copysignsign")
        result_bits = self.builder.or_(
            magnitude_bits, sign_bits, name="copysignbits"
        )
        return self.builder.bitcast(result_bits, target_type, name="copysigntmp"), None

    def _codegen_builtin_overflow(self, node, operation):
        if not node.args or len(node.args.exprs) < 3:
            return ir.Constant(int32_t, 0), None

        lhs, _ = self.codegen(node.args.exprs[0])
        rhs, _ = self.codegen(node.args.exprs[1])
        out_ptr, _ = self.codegen(node.args.exprs[2])

        if not isinstance(getattr(out_ptr, "type", None), ir.PointerType):
            return ir.Constant(int32_t, 0), None

        result_type = out_ptr.type.pointee
        if not isinstance(result_type, ir.IntType):
            return ir.Constant(int32_t, 0), None

        lhs = self._implicit_convert(lhs, result_type)
        rhs = self._implicit_convert(rhs, result_type)

        is_unsigned = self._is_unsigned_val(lhs) or self._is_unsigned_val(rhs)
        if is_unsigned:
            self._tag_unsigned(lhs)
            self._tag_unsigned(rhs)

        intrinsic_prefix = {
            ("add", False): "sadd",
            ("add", True): "uadd",
            ("sub", False): "ssub",
            ("sub", True): "usub",
            ("mul", False): "smul",
            ("mul", True): "umul",
        }[(operation, is_unsigned)]
        pair_type = ir.LiteralStructType([result_type, ir.IntType(1)])
        intrinsic = self._get_or_declare_intrinsic(
            f"llvm.{intrinsic_prefix}.with.overflow.i{result_type.width}",
            pair_type,
            [result_type, result_type],
        )
        pair = self.builder.call(intrinsic, [lhs, rhs], name=f"{operation}ovtmp")
        result = self.builder.extract_value(pair, 0, name=f"{operation}ovval")
        overflow = self.builder.extract_value(pair, 1, name=f"{operation}ovflag")
        if is_unsigned:
            self._tag_unsigned(result)
        self._safe_store(result, out_ptr)

        overflow_i32 = self.builder.zext(overflow, int32_t, name=f"{operation}ovi32")
        self._clear_unsigned(overflow_i32)
        return overflow_i32, None

    def _codegen_builtin_sync_synchronize(self, node):
        self.builder.fence("seq_cst")
        return ir.Constant(int64_t, 0), None

    def _codegen_builtin_sync_fetch_and_add(self, node):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int64_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        value, _ = self.codegen(node.args.exprs[1])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int64_t, 0), None
        pointee_type = ptr.type.pointee
        if not isinstance(pointee_type, ir.IntType):
            return ir.Constant(int64_t, 0), None
        if value.type != pointee_type:
            value = self._implicit_convert(value, pointee_type)
        result = self.builder.atomic_rmw(
            "add", ptr, value, "seq_cst", name="sync.fetch_add"
        )
        if self._is_unsigned_pointee(ptr):
            self._tag_unsigned(result)
        return result, ptr

    def _codegen_builtin_sync_bool_compare_and_swap(self, node):
        if not node.args or len(node.args.exprs) < 3:
            return ir.Constant(int32_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        expected, _ = self.codegen(node.args.exprs[1])
        desired, _ = self.codegen(node.args.exprs[2])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int32_t, 0), None
        pointee_type = ptr.type.pointee
        if expected.type != pointee_type:
            expected = self._implicit_convert(expected, pointee_type)
        if desired.type != pointee_type:
            desired = self._implicit_convert(desired, pointee_type)
        pair = self.builder.cmpxchg(
            ptr,
            expected,
            desired,
            "seq_cst",
            "seq_cst",
            name="sync.cmpxchg",
        )
        success = self.builder.extract_value(pair, 1, name="sync.cas.success")
        result = self.builder.zext(success, int32_t, name="sync.cas.i32")
        self._clear_unsigned(result)
        return result, None

    def _codegen_builtin_bswap(self, node, width):
        if not node.args or not node.args.exprs:
            return ir.Constant(ir.IntType(width), 0), None

        arg, _ = self.codegen(node.args.exprs[0])
        arg_type = ir.IntType(width)
        returns_unsigned = self._is_unsigned_val(arg)

        if not isinstance(getattr(arg, "type", None), ir.IntType) or arg.type != arg_type:
            arg = self._implicit_convert(arg, arg_type)

        mask = ir.Constant(arg_type, 0xFF)
        result = ir.Constant(arg_type, 0)
        byte_count = width // 8

        for index in range(byte_count):
            piece = arg
            if index:
                piece = self.builder.lshr(
                    piece,
                    ir.Constant(arg_type, index * 8),
                    name=f"bswapshr{index}",
                )
            piece = self.builder.and_(piece, mask, name=f"bswapmask{index}")
            shift = (byte_count - 1 - index) * 8
            if shift:
                piece = self.builder.shl(
                    piece,
                    ir.Constant(arg_type, shift),
                    name=f"bswapshl{index}",
                )
            result = self.builder.or_(result, piece, name=f"bswapor{index}")

        return self._extend_call_result(result, returns_unsigned=returns_unsigned), None

    def _codegen_builtin_rotate(self, node, width, direction):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(ir.IntType(width), 0), None

        value, _ = self.codegen(node.args.exprs[0])
        amount, _ = self.codegen(node.args.exprs[1])
        value_type = ir.IntType(width)
        returns_unsigned = self._is_unsigned_val(value)

        if (
            not isinstance(getattr(value, "type", None), ir.IntType)
            or value.type != value_type
        ):
            value = self._implicit_convert(value, value_type)
        if (
            not isinstance(getattr(amount, "type", None), ir.IntType)
            or amount.type != value_type
        ):
            amount = self._implicit_convert(amount, value_type)

        mask = ir.Constant(value_type, width - 1)
        amount = self.builder.and_(amount, mask, name=f"rot{direction}amt")
        inverse = self.builder.sub(
            ir.Constant(value_type, 0), amount, name=f"rot{direction}invtmp"
        )
        inverse = self.builder.and_(inverse, mask, name=f"rot{direction}inv")

        if direction == "left":
            lhs = self.builder.shl(value, amount, name=f"rot{direction}lhs")
            rhs = self.builder.lshr(value, inverse, name=f"rot{direction}rhs")
        else:
            lhs = self.builder.lshr(value, amount, name=f"rot{direction}lhs")
            rhs = self.builder.shl(value, inverse, name=f"rot{direction}rhs")

        result = self.builder.or_(lhs, rhs, name=f"rot{direction}")
        return self._extend_call_result(result, returns_unsigned=returns_unsigned), None

    def _codegen_builtin_bitcount(self, node, width, intrinsic_base):
        if not node.args or not node.args.exprs:
            return ir.Constant(int32_t, 0), None

        arg, _ = self.codegen(node.args.exprs[0])
        arg_type = ir.IntType(width)
        if not isinstance(getattr(arg, "type", None), ir.IntType) or arg.type != arg_type:
            arg = self._implicit_convert(arg, arg_type)

        intrinsic = self._get_or_declare_intrinsic(
            f"llvm.{intrinsic_base}.i{width}",
            arg_type,
            [arg_type, ir.IntType(1)],
        )
        result = self.builder.call(
            intrinsic,
            [arg, ir.Constant(ir.IntType(1), 0)],
            name=f"{intrinsic_base}tmp",
        )
        if result.type != int32_t:
            result = self.builder.trunc(result, int32_t, name=f"{intrinsic_base}i32")
        self._clear_unsigned(result)
        return result, None

    def _atomic_order_value(self, node):
        try:
            # GCC reserves the upper bits for target-specific modifiers such
            # as __ATOMIC_HLE_ACQUIRE.  The base memory model is always the
            # low 16 bits.  A runtime order cannot be represented directly in
            # LLVM IR, so conservatively use seq_cst rather than silently
            # weakening it to monotonic.
            return int(self._eval_const_expr(node)) & 0xFFFF
        except Exception:
            return None

    def _atomic_ordering(self, node, is_store):
        value = self._atomic_order_value(node)
        if value is None:
            return "seq_cst"
        if is_store:
            return {
                0: "monotonic",  # __ATOMIC_RELAXED
                3: "release",    # __ATOMIC_RELEASE
                4: "release",    # __ATOMIC_ACQ_REL
                5: "seq_cst",    # __ATOMIC_SEQ_CST
            }.get(value, "seq_cst")
        return {
            0: "monotonic",  # __ATOMIC_RELAXED
            1: "acquire",    # __ATOMIC_CONSUME
            2: "acquire",    # __ATOMIC_ACQUIRE
            5: "seq_cst",    # __ATOMIC_SEQ_CST
        }.get(value, "seq_cst")

    def _atomic_rmw_ordering(self, node):
        value = self._atomic_order_value(node)
        if value is None:
            return "seq_cst"
        return {
            0: "monotonic",  # __ATOMIC_RELAXED
            1: "acquire",    # __ATOMIC_CONSUME
            2: "acquire",    # __ATOMIC_ACQUIRE
            3: "release",    # __ATOMIC_RELEASE
            4: "acq_rel",    # __ATOMIC_ACQ_REL
            5: "seq_cst",    # __ATOMIC_SEQ_CST
        }.get(value, "seq_cst")

    def _codegen_builtin_atomic_thread_fence(self, node):
        if not node.args or not node.args.exprs:
            return ir.Constant(int64_t, 0), None
        order_value = self._atomic_order_value(node.args.exprs[0])
        if order_value is None:
            order_value = 5
        ordering = {
            0: None,       # __ATOMIC_RELAXED: no inter-thread ordering edge
            1: "acquire",  # __ATOMIC_CONSUME is implemented as acquire
            2: "acquire",
            3: "release",
            4: "acq_rel",
            5: "seq_cst",
        }.get(order_value, "seq_cst")
        if ordering is not None:
            self.builder.fence(ordering)
        return ir.Constant(int64_t, 0), None

    def _codegen_builtin_atomic_load(self, node):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int64_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int64_t, 0), None
        pointee_type = ptr.type.pointee
        align = max(1, self._ir_type_align(pointee_type))
        ordering = self._atomic_ordering(node.args.exprs[1], is_store=False)
        result = self.builder.load_atomic(ptr, ordering, align)
        if self._is_unsigned_pointee(ptr):
            self._tag_unsigned(result)
        return result, ptr

    def _codegen_builtin_atomic_store(self, node):
        if not node.args or len(node.args.exprs) < 3:
            return ir.Constant(int64_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        value, _ = self.codegen(node.args.exprs[1])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int64_t, 0), None
        pointee_type = ptr.type.pointee
        if value.type != pointee_type:
            value = self._implicit_convert(value, pointee_type)
        align = max(1, self._ir_type_align(pointee_type))
        ordering = self._atomic_ordering(node.args.exprs[2], is_store=True)
        self.builder.store_atomic(value, ptr, ordering, align)
        return ir.Constant(int64_t, 0), None

    def _codegen_builtin_atomic_fetch_op(self, node, op, *, return_new=True):
        # return_new=True  -> __atomic_<op>_fetch (returns the NEW value)
        # return_new=False -> __atomic_fetch_<op> (returns the OLD value,
        #                     which is exactly what atomicrmw yields). Without
        #                     this the fetch_* family fell through to an
        #                     undefined `__atomic_fetch_<op>` libcall.
        if not node.args or len(node.args.exprs) < 3:
            return ir.Constant(int64_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        value, _ = self.codegen(node.args.exprs[1])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int64_t, 0), None
        pointee_type = ptr.type.pointee
        if not isinstance(pointee_type, ir.IntType) and not (
            op == "xchg" and isinstance(pointee_type, ir.PointerType)
        ):
            return ir.Constant(int64_t, 0), None
        if value.type != pointee_type:
            value = self._implicit_convert(value, pointee_type)
        ordering = self._atomic_rmw_ordering(node.args.exprs[2])
        old = self.builder.atomic_rmw(
            op, ptr, value, ordering, name=f"atomic.{op}.old"
        )
        if not return_new:
            result = old
        elif op == "add":
            result = self.builder.add(old, value, name="atomic.add.new")
        elif op == "sub":
            result = self.builder.sub(old, value, name="atomic.sub.new")
        elif op == "or":
            result = self.builder.or_(old, value, name="atomic.or.new")
        elif op == "and":
            result = self.builder.and_(old, value, name="atomic.and.new")
        elif op == "xor":
            result = self.builder.xor(old, value, name="atomic.xor.new")
        else:
            result = old
        if self._is_unsigned_pointee(ptr):
            self._tag_unsigned(result)
        return result, None

    def _codegen_builtin_atomic_compare_exchange(self, node):
        if not node.args or len(node.args.exprs) < 6:
            return ir.Constant(int32_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        expected_ptr, _ = self.codegen(node.args.exprs[1])
        desired, _ = self.codegen(node.args.exprs[2])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int32_t, 0), None
        if not isinstance(getattr(expected_ptr, "type", None), ir.PointerType):
            return ir.Constant(int32_t, 0), None
        pointee_type = ptr.type.pointee
        if not isinstance(pointee_type, ir.IntType):
            return ir.Constant(int32_t, 0), None
        expected = self._safe_load(expected_ptr)
        if expected.type != pointee_type:
            expected = self._implicit_convert(expected, pointee_type)
        if desired.type != pointee_type:
            desired = self._implicit_convert(desired, pointee_type)
        success_order = self._atomic_rmw_ordering(node.args.exprs[4])
        failure_order = self._atomic_ordering(node.args.exprs[5], is_store=False)
        pair = self.builder.cmpxchg(
            ptr,
            expected,
            desired,
            success_order,
            failure_order,
            name="atomic.cmpxchg",
        )
        old = self.builder.extract_value(pair, 0, name="atomic.cmpxchg.old")
        success = self.builder.extract_value(pair, 1, name="atomic.cmpxchg.ok")
        stored_expected = old
        if stored_expected.type != expected_ptr.type.pointee:
            stored_expected = self._implicit_convert(
                stored_expected, expected_ptr.type.pointee
            )
        self._safe_store(stored_expected, expected_ptr)
        result = self.builder.zext(success, int32_t, name="atomic.cmpxchg.i32")
        self._clear_unsigned(result)
        return result, None

    def _codegen_builtin_atomic_test_and_set(self, node):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int32_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int32_t, 0), None
        pointee_type = ptr.type.pointee
        if not isinstance(pointee_type, ir.IntType):
            return ir.Constant(int32_t, 0), None
        one = ir.Constant(pointee_type, 1)
        ordering = self._atomic_rmw_ordering(node.args.exprs[1])
        old = self.builder.atomic_rmw(
            "xchg", ptr, one, ordering, name="atomic.test_and_set.old"
        )
        is_set = self.builder.icmp_unsigned(
            "!=", old, ir.Constant(pointee_type, 0), name="atomic.test_and_set.bool"
        )
        result = self.builder.zext(is_set, int32_t, name="atomic.test_and_set.i32")
        self._clear_unsigned(result)
        return result, None

    def _codegen_builtin_atomic_clear(self, node):
        if not node.args or len(node.args.exprs) < 2:
            return ir.Constant(int64_t, 0), None
        ptr, _ = self.codegen(node.args.exprs[0])
        if not isinstance(getattr(ptr, "type", None), ir.PointerType):
            return ir.Constant(int64_t, 0), None
        pointee_type = ptr.type.pointee
        if not isinstance(pointee_type, ir.IntType):
            return ir.Constant(int64_t, 0), None
        ordering = self._atomic_ordering(node.args.exprs[1], is_store=True)
        align = max(1, self._ir_type_align(pointee_type))
        self.builder.store_atomic(
            ir.Constant(pointee_type, 0), ptr, ordering, align
        )
        return ir.Constant(int64_t, 0), None

    def _convert_call_args(self, call_args, callee_func, arg_nodes=None):
        """Convert call arguments to match function parameter types."""
        converted = []
        param_types = [p.type for p in callee_func.args]

        for i, arg in enumerate(call_args):
            arg_node = arg_nodes[i] if arg_nodes and i < len(arg_nodes) else None
            if i < len(param_types):
                expected = param_types[i]
                arg = self._coerce_arg(arg, expected, arg_node=arg_node)
            else:
                arg = self._default_arg_promotion(arg, arg_node=arg_node)
            converted.append(arg)
        return converted

    def _default_arg_promotion(self, arg, arg_node=None):
        """Apply C default argument promotions for variadic calls."""
        if arg is None or isinstance(getattr(arg, "type", None), ir.VoidType):
            return ir.Constant(int64_t, 0)
        arg = self._decay_array_expr_to_pointer(arg_node, arg, "varargarraydecay")
        if isinstance(arg.type, ir.ArrayType):
            return self._implicit_convert(arg, ir.PointerType(arg.type.element))
        if self._is_aggregate_ir_type(arg.type):
            return self._coerce_variadic_aggregate_arg(arg)
        if isinstance(arg.type, (ir.HalfType, ir.FloatType)):
            return self.builder.fpext(arg, ir.DoubleType())
        if isinstance(arg.type, ir.IntType) and arg.type.width < int32_t.width:
            return self._integer_promotion(arg)
        return arg

    def _coerce_arg(self, arg, expected, arg_node=None):
        """Coerce a single argument to the expected type."""
        if arg is None or isinstance(getattr(arg, "type", None), ir.VoidType):
            return (
                ir.Constant(expected, None)
                if isinstance(expected, ir.PointerType)
                else ir.Constant(int64_t, 0)
            )
        arg = self._decay_array_expr_to_pointer(arg_node, arg, "argarraydecay")
        if arg.type == expected:
            return arg
        # Array values decay to pointers at the call site; do not try to
        # synthesize globals from function-local SSA array values here.
        if isinstance(arg.type, ir.ArrayType) and isinstance(expected, ir.PointerType):
            return self._implicit_convert(arg, expected)
        # Pointer -> different pointer: bitcast
        if isinstance(arg.type, ir.PointerType) and isinstance(
            expected, ir.PointerType
        ):
            return self.builder.bitcast(arg, expected)
        # Numeric conversions
        return self._implicit_convert(arg, expected)

    def codegen_ID(self, node):
        if node.name in {"__func__", "__FUNCTION__", "__PRETTY_FUNCTION__"}:
            func_name = self._function_display_name or (
                self.function.name if self.function is not None else node.name
            )
            gv = self._make_global_string_constant(func_name, name_hint="funcname")
            ptr = self._const_pointer_to_first_elem(gv, cstring)
            # `__func__` behaves like an implicitly-declared local array object,
            # not a `char *`. Keep the expression's semantic type as the array
            # so downstream array-ref lowering emits a byte load instead of
            # decaying the whole expression into a pointer comparison.
            node.ir_type = gv.type.pointee
            return ptr, gv

        valtype, var = self.lookup(node.name)
        node.ir_type = valtype
        # Enum constants are stored as ir.Constant, not alloca'd
        if isinstance(var, ir.values.Constant):
            return self._propagate_binding_tags(var, var), None
        # Function reference: return function pointer directly
        if isinstance(var, ir.Function):
            return self._propagate_binding_tags(var, var), None
        if self._is_vla_binding(var):
            return self._propagate_binding_tags(var, var), var
        # Array types: decay to pointer to first element
        if isinstance(valtype, ir.ArrayType):
            ptr = self.builder.gep(
                var,
                [ir.Constant(int64_t, 0), ir.Constant(int64_t, 0)],
                name="arraydecay",
            )
            return self._propagate_binding_tags(ptr, var), var
        # Guard: only load from pointer types
        if not isinstance(var.type, ir.PointerType):
            return self._propagate_binding_tags(var, var), None
        result = self._safe_load(var)
        return self._propagate_binding_tags(result, var), var

    def codegen_ArrayRef(self, node):

        name = node.name
        subscript = node.subscript
        name_ir, name_ptr = self.codegen(name)
        if name_ir is None:
            return ir.Constant(int64_t, 0), None
        if (
            name_ptr is None
            and isinstance(name_ir, ir.values.Constant)
            and isinstance(name_ir.type, ir.ArrayType)
        ):
            gv = ir.GlobalVariable(
                self.module, name_ir.type, self.module.get_unique_name("strlit")
            )
            gv.initializer = name_ir
            gv.global_constant = True
            gv.linkage = "internal"
            name_ptr = gv
        subscript_ir, subscript_ptr = self.codegen(subscript)
        if subscript_ir is None:
            return ir.Constant(int64_t, 0), None

        if isinstance(subscript_ir.type, ir.IntType):
            subscript_ir = self._implicit_convert(subscript_ir, ir.IntType(64))
        else:
            subscript_ir = self.builder.fptoui(subscript_ir, ir.IntType(64))

        # Pointer subscript: p[i] -> *(p + i)
        name_type = self._get_expr_ir_type(name) or name_ir.type
        if isinstance(name_type, ir.PointerType) and isinstance(
            name_ir.type, ir.PointerType
        ):
            value_ir_type = name_type.pointee
            elem_ptr = self.builder.gep(name_ir, [subscript_ir], name="ptridx")
            # If GEP result points to an array, return pointer (array decay)
            if isinstance(elem_ptr.type, ir.PointerType) and isinstance(
                elem_ptr.type.pointee, ir.ArrayType
            ):
                if self._is_unsigned_pointee(name_ir) or self._is_unsigned_pointee(
                    name_ptr
                ):
                    self._tag_unsigned_pointee(elem_ptr)
                node.ir_type = elem_ptr.type.pointee
                return elem_ptr, elem_ptr
            value_result = self._safe_load(elem_ptr)
            if self._is_unsigned_pointee(name_ir) or self._is_unsigned_pointee(
                name_ptr
            ):
                self._tag_unsigned(value_result)
            node.ir_type = value_ir_type
            return value_result, elem_ptr

        # Non-array type (opaque struct etc): treat as pointer subscript
        if not isinstance(name_type, ir.ArrayType):
            ptr = (
                self._implicit_convert(name_ir, ir.PointerType(int8_t))
                if not isinstance(name_ir.type, ir.PointerType)
                else name_ir
            )
            elem_ptr = self.builder.gep(ptr, [subscript_ir], name="ptridx")
            value_result = self._safe_load(elem_ptr)
            if self._is_unsigned_pointee(name_ir) or self._is_unsigned_pointee(ptr):
                self._tag_unsigned(value_result)
            node.ir_type = (
                elem_ptr.type.pointee
                if isinstance(elem_ptr.type, ir.PointerType)
                else name_type
            )
            return value_result, elem_ptr

        # Array subscript: a[i] using GEP for correct stride calculation
        value_ir_type = name_type.element

        # If no address pointer, use name_ir as base
        if name_ptr is None:
            name_ptr = name_ir
        if name_ptr is None:
            return ir.Constant(int64_t, 0), None

        # GEP requires a pointer base; if name_ptr is a pointer to array, use GEP
        if isinstance(name_ptr.type, ir.PointerType):
            zero = ir.Constant(int64_t, 0)
            if isinstance(subscript_ir.type, ir.IntType):
                if subscript_ir.type.width < 64:
                    idx = self.builder.sext(subscript_ir, int64_t)
                elif subscript_ir.type.width > 64:
                    idx = self.builder.trunc(subscript_ir, int64_t)
                else:
                    idx = subscript_ir
            else:
                idx = subscript_ir
            elem_ptr = self.builder.gep(name_ptr, [zero, idx], name="arridx")

            # If element is sub-array, return pointer (array decay)
            if isinstance(value_ir_type, ir.ArrayType):
                if self._is_unsigned_pointee(name_ir) or self._is_unsigned_pointee(
                    name_ptr
                ):
                    self._tag_unsigned_pointee(elem_ptr)
                node.ir_type = value_ir_type
                return elem_ptr, elem_ptr
            else:
                value_result = self._safe_load(elem_ptr)
                if self._is_unsigned_pointee(name_ir) or self._is_unsigned_pointee(
                    name_ptr
                ):
                    self._tag_unsigned(value_result)
                node.ir_type = value_ir_type
                return value_result, elem_ptr

        # Fallback: byte offset arithmetic (for non-pointer base)
        elem_size = self._ir_type_size(value_ir_type)
        stride = ir.Constant(ir.IntType(64), elem_size)
        offset = self.builder.mul(stride, subscript_ir, "array_add")
        base_int = (
            self.builder.ptrtoint(name_ptr, ir.IntType(64))
            if isinstance(name_ptr.type, ir.PointerType)
            else (
                name_ptr
                if isinstance(name_ptr.type, ir.IntType)
                else self.builder.ptrtoint(name_ptr, ir.IntType(64))
            )
        )
        addr = self.builder.add(offset, base_int, "addtmp")
        value_ptr = self.builder.inttoptr(addr, ir.PointerType(value_ir_type))
        if isinstance(value_ir_type, ir.ArrayType):
            node.ir_type = value_ir_type
            return value_ptr, value_ptr
        else:
            value_result = self._safe_load(value_ptr)
            if self._is_unsigned_pointee(name_ir) or self._is_unsigned_pointee(
                name_ptr
            ):
                self._tag_unsigned(value_result)
            node.ir_type = value_ir_type
            return value_result, value_ptr

    def codegen_Return(self, node):

        if node.expr is None:
            self.builder.ret_void()
        else:
            retval, _ = self.codegen(node.expr)
            # Implicit convert to function return type
            func_ret_type = self.function.return_value.type
            if isinstance(func_ret_type, ir.VoidType):
                self.builder.ret_void()
                return None, None
            if retval.type != func_ret_type:
                retval = self._implicit_convert(
                    retval,
                    func_ret_type,
                    target_unsigned=(
                        True if self._is_unsigned_return_binding(self.function) else None
                    ),
                )
            self.builder.ret(retval)
        return None, None

    def codegen_Compound(self, node):
        return self._codegen_compound_items(node, use_new_scope=True)

    def codegen_StmtExpr(self, node):
        result = ir.Constant(int64_t, 0)
        result_ptr = None
        with self.new_scope():
            items = list(getattr(node.stmt, "block_items", None) or [])
            for stmt in items:
                if self.builder and self.builder.block.is_terminated:
                    if self._switch_contexts and (
                        isinstance(stmt, (c_ast.Case, c_ast.Default))
                        or self._stmt_contains_switch_label(stmt)
                    ):
                        current = self.codegen(stmt)
                    elif isinstance(stmt, c_ast.Label):
                        current = self.codegen(stmt)
                    elif isinstance(stmt, c_ast.Compound) and self._stmt_contains_label(
                        stmt
                    ):
                        current = self._codegen_compound_with_forward_labels(stmt)
                    else:
                        continue
                else:
                    current = self.codegen(stmt)

                if self._is_expression_node(stmt):
                    current_val, current_ptr = current
                    if current_val is not None:
                        result = current_val
                        result_ptr = current_ptr
                        semantic_type = self._get_expr_ir_type(
                            stmt, getattr(current_val, "type", None)
                        )
                        if semantic_type is not None:
                            self._set_expr_ir_type(node, semantic_type)
        return result, result_ptr

    def _stmt_contains_label(self, node):
        if node is None:
            return False
        if isinstance(node, c_ast.Label):
            return True
        for _name, child in node.children():
            if isinstance(child, list):
                if any(self._stmt_contains_label(item) for item in child):
                    return True
                continue
            if self._stmt_contains_label(child):
                return True
        return False

    def _stmt_contains_switch_label(self, node):
        if node is None:
            return False
        if isinstance(node, (c_ast.Case, c_ast.Default)):
            return True
        if isinstance(node, c_ast.Switch):
            return False
        for _name, child in node.children():
            if isinstance(child, list):
                if any(self._stmt_contains_switch_label(item) for item in child):
                    return True
                continue
            if self._stmt_contains_switch_label(child):
                return True
        return False

    def _codegen_compound_with_forward_labels(self, node):
        with self.new_scope():
            seen_label = False
            for stmt in node.block_items or []:
                if seen_label:
                    self.codegen(stmt)
                    continue

                if isinstance(stmt, c_ast.Label):
                    seen_label = True
                    self.codegen(stmt)
                    continue

                if isinstance(stmt, c_ast.Compound) and self._stmt_contains_label(stmt):
                    seen_label = True
                    self._codegen_compound_with_forward_labels(stmt)
                    continue

                if isinstance(stmt, c_ast.Decl):
                    if stmt.init is not None:
                        raise CodegenError(
                            "goto into block skips declaration with initializer is not supported"
                        )
                    self._codegen_decl_before_forward_label(stmt)
                    continue

                if isinstance(stmt, (c_ast.Typedef, c_ast.EmptyStatement)):
                    self.codegen(stmt)
                    continue

    def _codegen_decl_before_forward_label(self, node):
        if (
            self.builder is None
            or self.function is None
            or self.builder.block is None
            or not self.builder.block.is_terminated
        ):
            self.codegen(node)
            return

        saved_block = self.builder.block
        entry_block = self.function.entry_basic_block
        entry_builder = ir.IRBuilder(entry_block)
        insert_before = None
        for inst in entry_block.instructions:
            if inst.opname not in ("phi", "alloca"):
                insert_before = inst
                break
        if insert_before is not None:
            entry_builder.position_before(insert_before)
        else:
            entry_builder.position_at_end(entry_block)

        saved_builder = self.builder
        self.builder = entry_builder
        try:
            self.codegen(node)
        finally:
            self.builder = saved_builder
            self.builder.position_at_end(saved_block)

    def _codegen_compound_items(self, node, use_new_scope):
        scope = self.new_scope() if use_new_scope else nullcontext()

        with scope:
            if node.block_items:
                for stmt in node.block_items:
                    if self.builder and self.builder.block.is_terminated:
                        # After a terminator (goto/break/continue/return),
                        # only process reachable label paths — skip other unreachable code
                        if self._switch_contexts and (
                            isinstance(stmt, (c_ast.Case, c_ast.Default))
                            or self._stmt_contains_switch_label(stmt)
                        ):
                            self.codegen(stmt)
                            continue
                        if isinstance(stmt, c_ast.Label):
                            self.codegen(stmt)
                        elif isinstance(stmt, c_ast.Compound) and self._stmt_contains_label(
                            stmt
                        ):
                            self._codegen_compound_with_forward_labels(stmt)
                        continue
                    self.codegen(stmt)
        return None, None

    def codegen_FuncDecl(self, node):
        ir_type = self._resolve_ast_type(node.type)
        return ir_type, None

    def codegen_FuncDef(self, node):

        # deep level func have deep level
        # we don't want funcdecl in codegen_decl too
        ir_type, _ = self.codegen(node.decl.type)
        funcname = node.decl.name
        self._record_decl_ast_type(funcname, node.decl.type)

        self.return_type = ir_type  # for call in C
        if not hasattr(self, "func_return_types"):
            self.func_return_types = {}
        self.func_return_types[funcname] = ir_type

        param_infos, is_var_arg = self._funcdef_param_infos(node)
        if getattr(node.decl.type, "args", None) is None:
            is_var_arg = True
        arg_types = [param_type for _name, param_type, _decl in param_infos]

        function_type = ir.FunctionType(ir_type, arg_types, var_arg=is_var_arg)
        prior_state = self._file_scope_function_states.get(funcname)
        if node.param_decls and prior_state is not None:
            prior_decl = self.module.globals.get(prior_state.symbol_name)
            if isinstance(prior_decl, ir.Function):
                prior_type = prior_decl.function_type
                if self._function_arg_types_match(prior_type.args, arg_types):
                    function_type = prior_type
        symbol_name = self._register_file_scope_function(
            funcname,
            function_type,
            storage=node.decl.storage,
            funcspec=node.decl.funcspec,
            is_definition=True,
        )
        function_state = self._file_scope_function_states.get(funcname)
        needs_internal_linkage = (
            function_state is not None and function_state.linkage == "internal"
        )

        with self.new_function():
            self._function_display_name = funcname
            self._label_value_tags = {
                label_name: index + 1
                for index, label_name in enumerate(
                    self._collect_function_label_names(node.body)
                )
            }

            existing = self.module.globals.get(symbol_name)
            if existing and isinstance(existing, ir.Function):
                if existing.is_declaration:
                    self.function = existing
                    if needs_internal_linkage:
                        self.function.linkage = "internal"
                else:
                    raise SemanticError(f"redefinition of function '{funcname}'")
            else:
                try:
                    self.function = ir.Function(
                        self.module,
                        function_type,
                        name=symbol_name,
                    )
                    if needs_internal_linkage:
                        self.function.linkage = "internal"
                except Exception:
                    raise SemanticError(f"failed to define function '{funcname}'")
            if self._func_decl_returns_unsigned(node.decl.type):
                self._mark_unsigned_return(self.function)
            # Add stack protector attribute for security hardening
            self.function.attributes.add("sspstrong")
            # PCC_NO_BUILTIN=1 is the -fno-builtin equivalent, required when a
            # translation unit DEFINES a libc primitive: without it LLVM turns
            # the body's memset-zero back into a `bzero` call (Darwin TLI),
            # so libc's own bzero tail-branches to itself and recurses
            # forever (BUG-P1-SELF-MEM-INTRINSIC-LIBCALL-SELF-BRANCH).
            if _no_builtin_enabled():
                # clang's -fno-builtin emits the function-level string attr
                # "no-builtins"; the bare `nobuiltin` keyword is a call-site
                # attribute and does not stop InstCombine from recognizing
                # libc calls inside the body.
                self.function.attributes.add('"no-builtins"')
            # Attach DWARF debug info
            func_line = getattr(getattr(node, "coord", None), "line", 1) or 1
            self._di_create_function(self.function, funcname, ir_type, func_line)
            self.block = self.function.append_basic_block()
            self.builder = ir.IRBuilder(self.block)
            if len(self.env.maps) > 1:
                self.env.maps[1][funcname] = (ir_type, self.function)
            self.define(funcname, (ir_type, self.function))
            for param_idx, (pname, arg_type, p) in enumerate(param_infos):
                if param_idx >= len(arg_types):
                    break
                var = self._alloca_in_entry(arg_type, pname)
                self.define(pname, (arg_type, var))
                if isinstance(p, c_ast.Decl):
                    self._record_decl_ast_type(pname, p.type)
                self._safe_store(self.function.args[param_idx], var)
                # Track unsigned params
                if isinstance(p, c_ast.Decl) and isinstance(
                    getattr(p, "type", None), c_ast.TypeDecl
                ):
                    if isinstance(p.type.type, c_ast.IdentifierType):
                        if self._is_unsigned_type_names(p.type.type.names):
                            self._mark_unsigned(var)
                if isinstance(p, c_ast.Decl):
                    if self._has_unsigned_scalar_pointee(p.type):
                        self._mark_unsigned_pointee(var)
                    if isinstance(p.type, c_ast.PtrDecl) and self._func_decl_returns_unsigned(
                        p.type.type
                    ):
                        self._mark_unsigned_return(var)

            for _pname, _arg_type, p in param_infos:
                if isinstance(p, c_ast.Decl):
                    self._emit_vla_param_bound_side_effects(p.type)

            # Phase 2: try SSA → LLVM lowering for eligible functions.
            # Falls back to AST codegen if SSA IR is unavailable or
            # the function uses constructs not yet in the SSA subset.
            _used_ssa_lowering = False
            if self._has_ssa_function(funcname):
                import os
                # Snapshot the current block count so we can roll back any
                # orphan blocks created by _lower_ssa_function when SSA
                # lowering raises partway through.
                _ssa_prev_block_count = len(self.function.basic_blocks)
                try:
                    _used_ssa_lowering = self._lower_ssa_function(
                        funcname, ir_type,
                    )
                except Exception as _ssa_err:
                    if os.environ.get("PCC_DEBUG_SSA_LOWER_FAIL"):
                        import traceback
                        print(f"[ssa-lower] {funcname} fell back: {_ssa_err}", flush=True)
                        traceback.print_exc()
                    _used_ssa_lowering = False
                if not _used_ssa_lowering:
                    # Remove any partial blocks created while trying SSA.
                    while len(self.function.basic_blocks) > _ssa_prev_block_count:
                        self.function.basic_blocks.pop()
                    # Reset the builder to the entry block so AST codegen
                    # continues there rather than at the middle of a
                    # half-built SSA block.
                    self.builder.position_at_end(self.function.basic_blocks[_ssa_prev_block_count - 1])

            if not _used_ssa_lowering:
                self._codegen_compound_items(node.body, use_new_scope=False)

            if not self.builder.block.is_terminated:
                if isinstance(ir_type, ir.VoidType):
                    self.builder.ret_void()
                else:
                    self.builder.ret(self._zero_initializer(ir_type))

            self._di_attach_function_call_locations(self.function, node)

            return None, None

    def codegen_Struct(self, node):
        # Generate LLVM types for struct members

        # If this is a reference to a named struct without decls, look it up
        if node.name and node.decls is None:
            tag_key = self._tag_type_key(node.name)
            if tag_key in self.env:
                return self.env[tag_key][0]
            opaque = self.module.context.get_identified_type(
                self._aggregate_type_name("struct", node.name)
            )
            self.define(tag_key, (opaque, None))
            return opaque

        if any(decl.bitsize is not None for decl in node.decls):
            return self._build_layout_backed_struct(node)

        member_types = []
        member_names = []
        member_decl_types = []
        for decl in node.decls:
            member_types.append(self._resolve_struct_member_ir_type(decl))
            member_names.append(decl.name)
            member_decl_types.append(decl.type)
        # Create the struct type
        struct_type = self._identified_aggregate_type("struct", node.name, member_types)
        struct_type.members = member_names
        struct_type.member_decl_types = member_decl_types
        struct_type.visible_field_paths = self._compute_visible_field_paths(
            member_names, member_types
        )

        # Register named structs for later reuse
        if node.name:
            self.define(self._tag_type_key(node.name), (struct_type, None))

        return struct_type

    def codegen_Union(self, node):
        """Model union as a struct with alignment-preserving storage."""
        if node.name and node.decls is None:
            tag_key = self._tag_type_key(node.name)
            if tag_key in self.env:
                return self.env[tag_key][0]
            opaque = self.module.context.get_identified_type(
                self._aggregate_type_name("union", node.name)
            )
            self.define(tag_key, (opaque, None))
            return opaque

        if node.decls is not None and len(node.decls) == 0:
            union_type = self._identified_aggregate_type("union", node.name, [])
            union_type.members = []
            union_type.member_types = {}
            union_type.member_decl_types = {}
            union_type.member_types_by_index = []
            union_type.member_decl_types_by_index = []
            union_type.named_member_indices = {}
            union_type.visible_field_paths = {}
            union_type.is_union = True
            if node.name:
                self.define(self._tag_type_key(node.name), (union_type, None))
            return union_type

        member_names = []
        member_types = {}
        member_decl_types = {}
        member_types_by_index = []
        member_decl_types_by_index = []
        named_member_indices = {}
        max_size = 0
        max_align = 1
        for field_index, decl in enumerate(node.decls):
            if isinstance(decl.type, c_ast.ArrayDecl):
                ir_t = self._build_array_ir_type(decl.type)
            else:
                ir_t = self._resolve_ast_type(decl.type)
            member_names.append(decl.name)
            member_types_by_index.append(ir_t)
            member_decl_types_by_index.append(decl.type)
            if decl.name is not None:
                member_types[decl.name] = ir_t
                member_decl_types[decl.name] = decl.type
                named_member_indices[decl.name] = field_index
            sz = self._ir_type_size(ir_t)
            al = self._ir_type_align(ir_t)
            if sz > max_size:
                max_size = sz
            if al > max_align:
                max_align = al

        # Use a struct {align_type, [padding x i8]} to preserve alignment
        # Pick an alignment element: i64 for 8, i32 for 4, i16 for 2, i8 for 1
        align_map = {8: int64_t, 4: int32_t, 2: int16_t, 1: int8_t}
        align_type = align_map.get(max_align, int64_t)
        align_size = max_align
        pad_size = max_size - align_size
        if pad_size > 0:
            union_body = [align_type, ir.ArrayType(int8_t, pad_size)]
        else:
            union_body = [align_type]
        union_type = self._identified_aggregate_type("union", node.name, union_body)
        union_type.members = member_names
        union_type.member_types = member_types
        union_type.member_decl_types = member_decl_types
        union_type.member_types_by_index = member_types_by_index
        union_type.member_decl_types_by_index = member_decl_types_by_index
        union_type.named_member_indices = named_member_indices
        union_type.visible_field_paths = self._compute_visible_field_paths(
            member_names, member_types_by_index
        )
        union_type.is_union = True

        if node.name:
            self.define(self._tag_type_key(node.name), (union_type, None))

        return union_type

    def _finalize_aggregate_field_access(
        self, node, typed_field_addr, semantic_field_type, decl_type=None
    ):
        if isinstance(semantic_field_type, ir.ArrayType):
            elem_ptr = self.builder.gep(
                typed_field_addr,
                [ir.Constant(int64_t, 0), ir.Constant(int64_t, 0)],
                name="arraydecay",
            )
            if decl_type is not None:
                self._tag_value_from_decl_type(elem_ptr, decl_type)
            self._set_expr_ir_type(node, semantic_field_type)
            return elem_ptr, typed_field_addr

        field_value = self._safe_load(typed_field_addr)
        if decl_type is not None:
            self._tag_value_from_decl_type(field_value, decl_type)
        self._set_expr_ir_type(node, semantic_field_type)
        return field_value, typed_field_addr

    def _codegen_aggregate_path_access(
        self, node, aggregate_addr, aggregate_type, field_path
    ):
        field_index = field_path[0]
        is_last = len(field_path) == 1

        if getattr(aggregate_type, "has_custom_layout", False):
            layout = self._aggregate_layout_by_index(aggregate_type, field_index)
            if layout is None:
                raise RuntimeError(
                    f"Field '{node.field.name}' not found in struct"
                )

            if layout.is_bitfield:
                if not is_last:
                    raise RuntimeError(
                        f"Field '{node.field.name}' not found in struct"
                    )
                container_ptr = self._byte_offset_ptr(
                    aggregate_addr,
                    layout.storage_byte_offset,
                    ir.PointerType(layout.storage_ir_type),
                    name="bitfieldptr",
                )
                ref = BitFieldRef(
                    container_ptr=container_ptr,
                    storage_ir_type=layout.storage_ir_type,
                    bit_offset=layout.bit_offset,
                    bit_width=layout.bit_width,
                    semantic_ir_type=layout.semantic_ir_type,
                    is_unsigned=layout.is_unsigned,
                )
                val = self._load_bitfield(ref)
                if layout.decl_type is not None:
                    self._tag_value_from_decl_type(val, layout.decl_type)
                if layout.is_unsigned:
                    self._tag_unsigned(val)
                self._set_expr_ir_type(node, layout.semantic_ir_type)
                return val, ref

            typed_field_addr = self._byte_offset_ptr(
                aggregate_addr,
                layout.byte_offset,
                ir.PointerType(layout.semantic_ir_type),
                name="fieldptr",
            )
            if is_last:
                return self._finalize_aggregate_field_access(
                    node,
                    typed_field_addr,
                    layout.semantic_ir_type,
                    layout.decl_type,
                )
            if not self._is_aggregate_ir_type(layout.semantic_ir_type):
                raise RuntimeError(f"Field '{node.field.name}' not found in struct")
            return self._codegen_aggregate_path_access(
                node,
                typed_field_addr,
                layout.semantic_ir_type,
                field_path[1:],
            )

        if getattr(aggregate_type, "is_union", False):
            member_ir_type = self._aggregate_member_ir_type(aggregate_type, field_index)
            decl_type = self._aggregate_member_decl_type(aggregate_type, field_index)
            semantic_field_type = self._refine_member_ir_type(
                aggregate_type, field_index, member_ir_type
            )
            typed_field_addr = self.builder.bitcast(
                aggregate_addr,
                ir.PointerType(semantic_field_type),
            )
            if is_last:
                return self._finalize_aggregate_field_access(
                    node,
                    typed_field_addr,
                    semantic_field_type,
                    decl_type,
                )
            if not self._is_aggregate_ir_type(semantic_field_type):
                raise RuntimeError(f"Field '{node.field.name}' not found in struct")
            return self._codegen_aggregate_path_access(
                node,
                typed_field_addr,
                semantic_field_type,
                field_path[1:],
            )

        if not hasattr(aggregate_type, "members"):
            raise SemanticError(
                f"field '{node.field.name}' accessed on incomplete struct"
            )

        if field_index >= len(aggregate_type.elements):
            raise RuntimeError(f"Field '{node.field.name}' not found in struct")

        field_addr = self.builder.gep(
            aggregate_addr,
            [ir.Constant(int64_t, 0), ir.Constant(ir.IntType(32), field_index)],
            inbounds=True,
        )

        field_type = self._aggregate_member_ir_type(aggregate_type, field_index)
        decl_type = self._aggregate_member_decl_type(aggregate_type, field_index)
        semantic_field_type = self._refine_member_ir_type(
            aggregate_type, field_index, field_type
        )

        typed_field_addr = field_addr
        target_ptr_type = ir.PointerType(semantic_field_type)
        field_pointee = getattr(field_addr.type, "pointee", None)
        target_pointee = getattr(target_ptr_type, "pointee", None)
        if field_pointee != target_pointee:
            try:
                typed_field_addr = self.builder.bitcast(
                    field_addr, target_ptr_type
                )
            except Exception:
                typed_field_addr = field_addr

        if is_last:
            return self._finalize_aggregate_field_access(
                node,
                typed_field_addr,
                semantic_field_type,
                decl_type,
            )
        if not self._is_aggregate_ir_type(semantic_field_type):
            raise RuntimeError(f"Field '{node.field.name}' not found in struct")
        return self._codegen_aggregate_path_access(
            node,
            typed_field_addr,
            semantic_field_type,
            field_path[1:],
        )

    def _codegen_aggregate_field_access(
        self, node, aggregate_addr, aggregate_type, field_name
    ):
        direct_field_index = self._aggregate_direct_member_index(
            aggregate_type, field_name
        )
        if direct_field_index is not None:
            return self._codegen_direct_aggregate_field_access(
                node,
                aggregate_addr,
                aggregate_type,
                direct_field_index,
            )

        field_path = self._aggregate_field_path(aggregate_type, field_name)
        if field_path is None:
            if not hasattr(aggregate_type, "members") and not getattr(
                aggregate_type, "is_union", False
            ):
                raise SemanticError(
                    f"field '{field_name}' accessed on incomplete struct"
                )
            raise RuntimeError(f"Field '{field_name}' not found in struct")
        return self._codegen_aggregate_path_access(
            node,
            aggregate_addr,
            aggregate_type,
            field_path,
        )

    def _codegen_direct_aggregate_field_access(
        self, node, aggregate_addr, aggregate_type, field_index
    ):
        if getattr(aggregate_type, "has_custom_layout", False):
            layout = self._aggregate_layout_by_index(aggregate_type, field_index)
            if layout is None:
                raise RuntimeError(f"Field '{node.field.name}' not found in struct")

            if layout.is_bitfield:
                container_ptr = self._byte_offset_ptr(
                    aggregate_addr,
                    layout.storage_byte_offset,
                    ir.PointerType(layout.storage_ir_type),
                    name="bitfieldptr",
                )
                ref = BitFieldRef(
                    container_ptr=container_ptr,
                    storage_ir_type=layout.storage_ir_type,
                    bit_offset=layout.bit_offset,
                    bit_width=layout.bit_width,
                    semantic_ir_type=layout.semantic_ir_type,
                    is_unsigned=layout.is_unsigned,
                )
                val = self._load_bitfield(ref)
                if layout.decl_type is not None:
                    self._tag_value_from_decl_type(val, layout.decl_type)
                if layout.is_unsigned:
                    self._tag_unsigned(val)
                self._set_expr_ir_type(node, layout.semantic_ir_type)
                return val, ref

            typed_field_addr = self._byte_offset_ptr(
                aggregate_addr,
                layout.byte_offset,
                ir.PointerType(layout.semantic_ir_type),
                name="fieldptr",
            )
            return self._finalize_aggregate_field_access(
                node,
                typed_field_addr,
                layout.semantic_ir_type,
                layout.decl_type,
            )

        if getattr(aggregate_type, "is_union", False):
            member_ir_type = self._aggregate_member_ir_type(aggregate_type, field_index)
            decl_type = self._aggregate_member_decl_type(aggregate_type, field_index)
            semantic_field_type = self._refine_member_ir_type(
                aggregate_type, field_index, member_ir_type
            )
            typed_field_addr = self.builder.bitcast(
                aggregate_addr,
                ir.PointerType(semantic_field_type),
            )
            return self._finalize_aggregate_field_access(
                node,
                typed_field_addr,
                semantic_field_type,
                decl_type,
            )

        if not hasattr(aggregate_type, "members"):
            raise SemanticError(
                f"field '{node.field.name}' accessed on incomplete struct"
            )

        if field_index >= len(aggregate_type.elements):
            raise RuntimeError(f"Field '{node.field.name}' not found in struct")

        field_addr = self.builder.gep(
            aggregate_addr,
            [ir.Constant(int64_t, 0), ir.Constant(ir.IntType(32), field_index)],
            inbounds=True,
        )
        field_type = self._aggregate_member_ir_type(aggregate_type, field_index)
        decl_type = self._aggregate_member_decl_type(aggregate_type, field_index)
        semantic_field_type = self._refine_member_ir_type(
            aggregate_type, field_index, field_type
        )

        typed_field_addr = field_addr
        target_ptr_type = ir.PointerType(semantic_field_type)
        field_pointee = getattr(field_addr.type, "pointee", None)
        target_pointee = getattr(target_ptr_type, "pointee", None)
        if field_pointee != target_pointee:
            try:
                typed_field_addr = self.builder.bitcast(field_addr, target_ptr_type)
            except Exception:
                typed_field_addr = field_addr

        return self._finalize_aggregate_field_access(
            node,
            typed_field_addr,
            semantic_field_type,
            decl_type,
        )

    def codegen_StructRef(self, node):

        if isinstance(node.name, c_ast.StructRef):
            inner_val, inner_addr = self.codegen_StructRef(node.name)
            if node.type == "->":
                # Chain: (a->b)->c — need to use the VALUE of a->b as pointer base
                # inner_val is the loaded field value (a pointer to next struct)
                base = inner_val
                semantic_base_type = self._get_expr_ir_type(node.name)
                if (
                    isinstance(semantic_base_type, ir.PointerType)
                    and base.type != semantic_base_type
                ):
                    try:
                        base = self.builder.bitcast(base, semantic_base_type)
                    except Exception:
                        pass
                struct_type = (
                    base.type.pointee if hasattr(base.type, "pointee") else int8_t
                )
                struct_addr = base
            else:
                # Chain: (a->b).c — use the ADDRESS of a->b as struct base
                semantic_base_type = self._get_expr_ir_type(node.name)
                if semantic_base_type is not None:
                    expected_addr_type = ir.PointerType(semantic_base_type)
                    if inner_addr.type != expected_addr_type:
                        try:
                            inner_addr = self.builder.bitcast(
                                inner_addr, expected_addr_type
                            )
                        except Exception:
                            pass
                struct_type = (
                    inner_addr.type.pointee
                    if hasattr(inner_addr.type, "pointee")
                    else int8_t
                )
                struct_addr = inner_addr
        elif isinstance(node.name, c_ast.ID):
            _, struct_instance_addr = self.lookup(node.name.name)
            if not isinstance(struct_instance_addr.type, ir.PointerType):
                raise Exception("Invalid struct reference")

            if node.type == "->":
                if isinstance(struct_instance_addr.type.pointee, ir.ArrayType):
                    ptr_val = self.builder.gep(
                        struct_instance_addr,
                        [ir.Constant(int64_t, 0), ir.Constant(int64_t, 0)],
                        name="structrefarraydecay",
                    )
                else:
                    ptr_val = self._safe_load(struct_instance_addr)
                struct_type = (
                    ptr_val.type.pointee if hasattr(ptr_val.type, "pointee") else int8_t
                )
                struct_addr = ptr_val
            else:
                struct_type = (
                    struct_instance_addr.type.pointee
                    if hasattr(struct_instance_addr.type, "pointee")
                    else int8_t
                )
                struct_addr = struct_instance_addr
        else:
            # Cast/UnaryOp/other expression as struct base: ((Type*)ptr)->field
            val, addr = self.codegen(node.name)
            semantic_base_type = self._get_expr_ir_type(node.name)
            if node.type == "->":
                struct_addr = val
                if (
                    isinstance(semantic_base_type, ir.PointerType)
                    and struct_addr.type != semantic_base_type
                ):
                    try:
                        struct_addr = self.builder.bitcast(
                            struct_addr, semantic_base_type
                        )
                    except Exception:
                        pass
                struct_type = (
                    struct_addr.type.pointee
                    if hasattr(struct_addr.type, "pointee")
                    else int8_t
                )
            else:
                struct_addr = addr if addr else val
                if addr is not None and semantic_base_type is not None:
                    expected_addr_type = ir.PointerType(semantic_base_type)
                    if struct_addr.type != expected_addr_type:
                        try:
                            struct_addr = self.builder.bitcast(
                                struct_addr, expected_addr_type
                            )
                        except Exception:
                            pass
                    struct_type = (
                        struct_addr.type.pointee
                        if hasattr(struct_addr.type, "pointee")
                        else int8_t
                    )
                else:
                    struct_type = (
                        semantic_base_type
                        if semantic_base_type is not None
                        else (val.type if hasattr(val.type, "members") else int8_t)
                    )
                if (
                    addr is None
                    and not isinstance(getattr(struct_addr, "type", None), ir.PointerType)
                    and (
                        getattr(struct_type, "has_custom_layout", False)
                        or _is_struct_ir_type(struct_type)
                    )
                ):
                    materialized = self._alloca_in_entry(val.type, "structrval")
                    self._safe_store(val, materialized)
                    struct_addr = materialized

        return self._codegen_aggregate_field_access(
            node,
            struct_addr,
            struct_type,
            node.field.name,
        )

    def codegen_EmptyStatement(self, node):
        return None, None

    def codegen_ExprList(self, node):
        # Comma operator: evaluate all, return last
        result = None
        result_ptr = None
        last_expr = None
        for expr in node.exprs:
            last_expr = expr
            result, result_ptr = self.codegen(expr)
        if last_expr is not None:
            semantic_result_type = self._get_expr_ir_type(
                last_expr, getattr(result, "type", None)
            )
            if semantic_result_type is not None:
                self._set_expr_ir_type(node, semantic_result_type)
        return result, result_ptr

    def codegen_GenericSelection(self, node):
        selected = self._select_generic_association(node)
        if selected is None:
            raise SemanticError("no matching association in _Generic selection")
        result = self.codegen(selected)
        result_val, _ = result
        if result_val is not None:
            semantic_type = self._get_expr_ir_type(
                selected, getattr(result_val, "type", None)
            )
            if semantic_type is not None:
                self._set_expr_ir_type(node, semantic_type)
        return result

    def codegen_Label(self, node):
        label_bb = self._ensure_label_block(node.name)
        if not self.builder.block.is_terminated:
            self.builder.branch(label_bb)
        self.builder.position_at_end(label_bb)
        if node.stmt:
            self.codegen(node.stmt)
        return None, None


    def codegen_Goto(self, node):
        target_bb = self._ensure_label_block(node.name)
        self.builder.branch(target_bb)
        return None, None

    def codegen_ComputedGoto(self, node):
        target_val, _ = self.codegen(node.expr)
        if target_val is None:
            raise SemanticError("computed goto requires a target expression")

        if isinstance(target_val.type, ir.PointerType):
            target_tag = self.builder.ptrtoint(target_val, int64_t)
        elif isinstance(target_val.type, ir.IntType):
            target_tag = self._implicit_convert(target_val, int64_t)
        else:
            raise SemanticError("computed goto target must be an integer or pointer")

        current_bb = self.builder.block
        default_bb = self.builder.function.append_basic_block("computed_goto_default")
        switch_inst = self.builder.switch(target_tag, default_bb)
        for label_name, tag in self._label_value_tags.items():
            switch_inst.add_case(
                ir.Constant(int64_t, tag),
                self._ensure_label_block(label_name),
            )

        default_builder = ir.IRBuilder(default_bb)
        default_builder.unreachable()
        self.builder.position_at_end(current_bb)
        return None, None

    def codegen_Enum(self, node):
        # Define each enumerator as a constant in the environment
        enum_range = None
        if node.values:
            current_val = 0
            min_value = None
            max_value = None
            for enumerator in node.values.enumerators:
                if enumerator.value:
                    current_val = self._eval_const_expr(enumerator.value)
                self.define(
                    enumerator.name, (int32_t, ir.Constant(int32_t, current_val))
                )
                if min_value is None or current_val < min_value:
                    min_value = current_val
                if max_value is None or current_val > max_value:
                    max_value = current_val
                current_val += 1
            enum_range = (min_value, max_value)
        if getattr(node, "name", None) and enum_range is not None:
            self.env[self._enum_tag_key(node.name)] = enum_range
        return None, None

    def _eval_const_expr(self, node):
        """Evaluate a constant expression at compile time (for enum values)."""
        def is_float_value(value):
            return isinstance(value, float)

        def is_int_value(value):
            return isinstance(value, ConstIntValue)

        def cast_int_value(value, width, is_unsigned):
            mask = (1 << width) - 1
            value = int(value) & mask
            if is_unsigned:
                return value
            sign_bit = 1 << (width - 1)
            if value & sign_bit:
                value -= 1 << width
            return value

        def make_int(value, width=32, is_unsigned=False):
            return ConstIntValue(
                cast_int_value(value, width, is_unsigned),
                width,
                is_unsigned,
            )

        def coerce_int_value(value):
            if is_int_value(value):
                return value
            return make_int(value)

        def numeric_value(value):
            if is_int_value(value):
                return value.value
            return value

        def integer_promotion(value):
            value = coerce_int_value(value)
            if value.width == 1 or value.width < 32:
                return make_int(value.value, 32, False)
            return value

        def convert_int_value(value, width, is_unsigned):
            return make_int(numeric_value(value), width, is_unsigned)

        def usual_arithmetic_conversion(lhs, rhs):
            lhs = integer_promotion(lhs)
            rhs = integer_promotion(rhs)

            lhs_unsigned = lhs.is_unsigned
            rhs_unsigned = rhs.is_unsigned
            lhs_width = lhs.width
            rhs_width = rhs.width

            decision = _decide_usual_integer_conversion(
                lhs_width,
                lhs_unsigned,
                rhs_width,
                rhs_unsigned,
            )
            target_width = decision.target_order
            result_unsigned = decision.is_unsigned

            lhs = convert_int_value(lhs, target_width, result_unsigned)
            rhs = convert_int_value(rhs, target_width, result_unsigned)
            return lhs, rhs, result_unsigned

        def parse_int_constant(raw):
            raw_lower = raw.lower()
            has_unsigned_suffix = "u" in raw_lower
            has_long_suffix = "l" in raw_lower
            val_str = raw.rstrip("uUlL")
            if val_str.startswith(("0x", "0X")):
                int_val = int(val_str, 16)
                is_non_decimal = True
            elif val_str.startswith("0") and len(val_str) > 1 and val_str[1:].isdigit():
                int_val = int(val_str, 8)
                is_non_decimal = True
            else:
                int_val = int(val_str)
                is_non_decimal = False

            if has_long_suffix or int_val > 0xFFFFFFFF:
                return make_int(int_val, 64, has_unsigned_suffix)
            if has_unsigned_suffix:
                return make_int(int_val, 32, True)
            if is_non_decimal and int_val > 0x7FFFFFFF:
                return make_int(int_val, 32, True)
            if int_val > 0x7FFFFFFF:
                return make_int(int_val, 64, False)
            return make_int(int_val, 32, False)

        def cast_const_value(value, target_decl_type):
            target_ir_type = self._resolve_ast_type(target_decl_type)
            if isinstance(target_ir_type, ir.IntType):
                return make_int(
                    numeric_value(value),
                    target_ir_type.width,
                    self._is_unsigned_scalar_decl_type(target_decl_type),
                )
            if self._is_floating_ir_type(target_ir_type):
                return float(numeric_value(value))
            return value

        def c_float_div(lhs, rhs):
            if rhs == 0.0:
                if lhs == 0.0:
                    return float("nan")
                sign = math.copysign(1.0, lhs) * math.copysign(1.0, rhs)
                return math.copysign(float("inf"), sign)
            return lhs / rhs

        if isinstance(node, c_ast.Constant):
            if node.type in ("string", "wstring"):
                return 0  # string constants can't be int-evaluated
            if node.type in ("float", "double"):
                return self._parse_float_constant(node.value)
            v = node.value.rstrip("uUlL")
            if v.startswith("'"):
                return make_int(self._char_constant_value(v))
            try:
                return parse_int_constant(node.value)
            except ValueError:
                return make_int(0)
        elif isinstance(node, c_ast.UnaryOp):
            if node.op == "sizeof":
                if isinstance(node.expr, c_ast.Typename):
                    ir_t = self._resolve_ast_type(node.expr.type)
                    return make_int(self._ir_type_size(ir_t), 64, True)
                if self._is_string_constant(node.expr):
                    return make_int(len(self._string_literal_data(node.expr)), 64, True)
                ir_t = self._infer_sizeof_operand_ir_type(node.expr)
                return make_int(self._ir_type_size(ir_t), 64, True)
            if (
                node.op == "&"
                and isinstance(node.expr, c_ast.StructRef)
                and self._is_offsetof_like_structref(node.expr)
            ):
                offset, _ = self._eval_offsetof_structref(node.expr)
                return make_int(offset, 64, True)
            val = self._eval_const_expr(node.expr)
            if node.op in ("-", "+", "~", "!") and is_int_value(val):
                promoted = integer_promotion(val)
                status, folded = _fold_c_integer_unary(
                    node.op,
                    promoted.width,
                    promoted.is_unsigned,
                    numeric_value(promoted),
                )
                if status == _C_FOLD_POISON:
                    raise CodegenError(
                        f"undefined integer constant expression: {node.op}"
                    )
                if status == _C_FOLD_CONSTANT:
                    if node.op == "!":
                        return make_int(folded)
                    return make_int(
                        folded,
                        promoted.width,
                        promoted.is_unsigned,
                    )
            if node.op == "-":
                return -val
            if node.op == "+":
                return val
            if node.op == "!":
                return make_int(0 if numeric_value(val) else 1)
            if node.op == "~":
                raise CodegenError("bitwise complement requires an integer operand")
        elif isinstance(node, c_ast.BinaryOp):
            l = self._eval_const_expr(node.left)
            if node.op == "&&" and not numeric_value(l):
                return make_int(0)
            if node.op == "||" and numeric_value(l):
                return make_int(1)
            r = self._eval_const_expr(node.right)
            if node.op == "&&":
                return make_int(1 if numeric_value(r) else 0)
            if node.op == "||":
                return make_int(1 if numeric_value(r) else 0)
            use_float = is_float_value(l) or is_float_value(r)

            if use_float:
                lhs = float(numeric_value(l))
                rhs = float(numeric_value(r))
                if node.op == "+":
                    return lhs + rhs
                if node.op == "-":
                    return lhs - rhs
                if node.op == "*":
                    return lhs * rhs
                if node.op == "/":
                    return c_float_div(lhs, rhs)
                if node.op in ("==", "!=", "<", "<=", ">", ">="):
                    if node.op == "==":
                        result = lhs == rhs
                    elif node.op == "!=":
                        result = lhs != rhs
                    elif node.op == "<":
                        result = lhs < rhs
                    elif node.op == "<=":
                        result = lhs <= rhs
                    elif node.op == ">":
                        result = lhs > rhs
                    else:
                        result = lhs >= rhs
                    return make_int(1 if result else 0)
                raise CodegenError(
                    f"invalid floating constant-expression operator: {node.op}"
                )

            if node.op in ("<<", ">>"):
                lhs = integer_promotion(coerce_int_value(l))
                rhs = integer_promotion(coerce_int_value(r))
                result_width = lhs.width
                result_unsigned = lhs.is_unsigned
            else:
                lhs, rhs, result_unsigned = usual_arithmetic_conversion(l, r)
                result_width = lhs.width
            status, folded = _fold_c_integer_binary(
                node.op,
                result_width,
                result_unsigned,
                numeric_value(lhs),
                numeric_value(rhs),
            )
            if status == _C_FOLD_POISON:
                raise CodegenError(
                    f"undefined integer constant expression: {node.op}"
                )
            if status != _C_FOLD_CONSTANT:
                raise CodegenError(
                    f"unsupported integer constant-expression operator: {node.op}"
                )
            if node.op in ("==", "!=", "<", "<=", ">", ">="):
                return make_int(folded)
            return make_int(folded, result_width, result_unsigned)
        elif isinstance(node, c_ast.TernaryOp):
            cond = self._eval_const_expr(node.cond)
            if numeric_value(cond):
                return self._eval_const_expr(node.iftrue)
            return self._eval_const_expr(node.iffalse)
        elif isinstance(node, c_ast.ID):
            # Only true integer constant bindings (for example enum values)
            # participate in constant-expression evaluation. Ordinary locals
            # and globals must not silently fold to zero.
            if node.name in self.env:
                _, val = self.env[node.name]
                if isinstance(val, ir.values.Constant) and isinstance(
                    val.type, ir.IntType
                ):
                    raw_value = self._constant_raw_value(val)
                    return make_int(
                        int(raw_value),
                        val.type.width,
                        self._is_unsigned_val(val),
                    )
            raise CodegenError(f"Not a constant expression: identifier '{node.name}'")
        elif isinstance(node, c_ast.Cast):
            value = self._eval_const_expr(node.expr)
            return cast_const_value(value, node.to_type.type)
        elif isinstance(node, c_ast.FuncCall):
            if isinstance(node.name, c_ast.ID):
                callee = node.name.name
                if callee in ("__builtin_inf", "__builtin_inff", "__builtin_infl"):
                    return float("inf")
                if callee in ("__builtin_nan", "__builtin_nanf", "__builtin_nanl"):
                    return float("nan")
            raise CodegenError(f"Not a constant expression: {type(node).__name__}")
        elif isinstance(node, c_ast.Typename):
            return 0
        elif isinstance(node, c_ast.ID):
            try:
                _, binding = self.lookup(node.name)
            except Exception:
                raise CodegenError(
                    f"Not a constant expression: {type(node).__name__} {node.name!r}"
                )
            if isinstance(binding, ir.Constant):
                width = getattr(binding.type, "width", 32)
                is_unsigned = bool(getattr(binding.type, "is_unsigned", False))
                return make_int(int(binding.value), width, is_unsigned)
            raise CodegenError(
                f"Not a constant expression: {type(node).__name__} {node.name!r}"
            )
        raise CodegenError(f"Not a constant expression: {type(node).__name__}")

    def codegen_InitList(self, node):
        # InitList as expression — return first element or zero
        if node.exprs:
            return self.codegen(node.exprs[0])
        return ir.Constant(int64_t, 0), None

    def codegen_DeclList(self, node):
        for decl in node.decls:
            self.codegen(decl)
        return None, None

    def codegen_Typedef(self, node):
        # typedef int myint; / typedef int* intptr; / typedef struct{...} Name;
        self._record_typedef_ast_type(node.name, node.type)
        if isinstance(node.type, c_ast.TypeDecl):
            if isinstance(node.type.type, c_ast.IdentifierType):
                base_type = node.type.type.names
                self.define(f"__typedef_{node.name}", base_type)
            elif isinstance(node.type.type, c_ast.Struct):
                if node.type.type.name:
                    # Named struct: store reference to struct name for lazy resolution
                    self.codegen_Struct(node.type.type)  # ensure it's registered
                    self.define(
                        f"__typedef_{node.name}", f"__struct_{node.type.type.name}"
                    )
                else:
                    struct_type = self.codegen_Struct(node.type.type)
                    self.define(f"__typedef_{node.name}", struct_type)
            elif isinstance(node.type.type, c_ast.Union):
                if node.type.type.name:
                    self.codegen_Union(node.type.type)
                    self.define(
                        f"__typedef_{node.name}", f"__struct_{node.type.type.name}"
                    )
                else:
                    union_type = self.codegen_Union(node.type.type)
                    self.define(f"__typedef_{node.name}", union_type)
            elif isinstance(node.type.type, c_ast.Enum):
                # typedef enum { A, B, C } MyEnum;
                self.codegen_Enum(node.type.type)
                self.define(f"__typedef_{node.name}", int32_t)
        elif isinstance(node.type, c_ast.ArrayDecl):
            self.define(f"__typedef_{node.name}", self._build_array_ir_type(node.type))
        elif isinstance(node.type, c_ast.PtrDecl):
            inner = node.type.type
            if isinstance(inner, c_ast.FuncDecl):
                fp_type = self._build_func_ptr_type(inner)
                self.define(f"__typedef_{node.name}", fp_type)
            elif isinstance(inner, c_ast.TypeDecl):
                if isinstance(inner.type, c_ast.IdentifierType):
                    base_ir = self._get_ir_type(inner.type.names)
                elif isinstance(inner.type, c_ast.Struct):
                    base_ir = self.codegen_Struct(inner.type)
                elif isinstance(inner.type, c_ast.Union):
                    base_ir = self.codegen_Union(inner.type)
                else:
                    base_ir = get_ir_type(
                        inner.type.names if hasattr(inner.type, "names") else ["int"]
                    )
                if isinstance(base_ir, ir.VoidType):
                    ptr_type = voidptr_t
                else:
                    ptr_type = ir.PointerType(base_ir)
                self.define(f"__typedef_{node.name}", ptr_type)
        elif isinstance(node.type, c_ast.FuncDecl):
            func_type, _ = self._build_function_ir_type(node.type)
            self.define(f"__typedef_{node.name}", func_type)
        return None, None
