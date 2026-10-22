"""Restricted AST -> SSA builder for structured scalar functions."""

from __future__ import annotations

from dataclasses import dataclass, field

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.c_character_literals import decode_c_character_constant
from pcc.frontends.c.c_abi_layout import builtin_integer_is_unsigned, builtin_scalar_layout, c_ptrdiff_type_name, c_size_type_name, c_target_triple, integer_literal_type_name, integer_literal_value, pointer_scalar_layout
from pcc.frontends.c.ssa.ir import SSABinaryOp, SSABlock, SSABranch, SSACast, SSACall, SSAConstant, SSAFieldAddr, SSAFieldExtract, SSAFunction, SSAGlobalRef, SSABinding, SSAJump, SSALoad, SSAParam, SSAPhi, SSAReturn, SSAStackAlloc, SSAStringConstant, SSAStore, SSASwitch, SSAUnaryOp, SSAUndef, SSAValue


class SSAConstructionError(ValueError):
    """Raised when the current SSA builder cannot represent a construct yet."""


def _debug_phi_types(phi, site_label: str) -> None:
    """Diagnostic: log phi nodes whose incomings disagree with the phi's
    declared `type_name`. Enabled by setting `PCC_DEBUG_PHI_TYPES=<path>`
    to a log file (supports parallel builds; each process appends).
    No-op when the env var is unset. Left in the codebase because
    debugging these mismatches is non-trivial (subprocess compilation,
    MCJIT IR parse errors with minimal context).
    """
    import os as _os
    log_path = _os.environ.get("PCC_DEBUG_PHI_TYPES")
    if not log_path:
        return
    pty = getattr(phi, "type_name", "?")
    mismatched = []
    for pred, val in getattr(phi, "incomings", ()):
        vty = getattr(val, "type_name", "?")
        if vty != pty:
            mismatched.append((pred, vty, getattr(val, "name", "?")))
    if mismatched:
        try:
            with open(log_path, "a") as _f:
                _f.write(
                    f"[phi_type_mismatch] {site_label} phi={phi.name!r} "
                    f"type={pty!r} mismatches={mismatched}\n"
                )
        except Exception:
            pass


# Builtins the AST codegen handles via dedicated lowering (alloca, varargs,
# overflow intrinsics, ...). The SSA path must decline these so codegen
# doesn't emit a plain `call alloca` that the linker fails to resolve.
_SSA_FALLBACK_BUILTINS = frozenset({
    "alloca",
    "__builtin_alloca",
    "__builtin_va_start",
    "__builtin_va_end",
    "__builtin_va_arg",
    "__builtin_va_copy",
    "__builtin_add_overflow",
    "__builtin_sub_overflow",
    "__builtin_mul_overflow",
    "__builtin_classify_type",
    "__builtin_prefetch",
    "__builtin_assume",
    "__builtin_unreachable",
    "__builtin_trap",
    "__builtin_frame_address",
    "__builtin_return_address",
    "__builtin_expect",
    "__builtin_expect_with_probability",
    # Handled later in the pass pipeline by LowerConstantIntrinsicsPass /
    # AlignmentFromAssumptionsPass but those run after ssa-bootstrap, so
    # any function calling them must fall back to AST codegen.
    "__builtin_constant_p",
    "__builtin_assume_aligned",
    # Bit/byte intrinsics, math builtins, string builtins, and float
    # classification — all have dedicated AST codegen lowerings that the
    # SSA path would skip, producing unresolved `call @__builtin_X`.
    "__builtin_bswap16",
    "__builtin_bswap32",
    "__builtin_bswap64",
    "__builtin_rotateleft32",
    "__builtin_rotateleft64",
    "__builtin_rotateright32",
    "__builtin_rotateright64",
    "__builtin_clz",
    "__builtin_clzll",
    "__builtin_ctz",
    "__builtin_ctzll",
    "__builtin_ffs",
    "__builtin_ffsll",
    "__builtin_memcmp",
    "__builtin_memchr",
    "__builtin_strcmp",
    "__builtin_strcpy",
    "__builtin_sprintf",
    "__builtin_snprintf",
    "__builtin_inf",
    "__builtin_inff",
    "__builtin_infl",
    "__builtin_nan",
    "__builtin_nanf",
    "__builtin_nanl",
    "__builtin_signbit",
    "__builtin_copysign",
    "__builtin_copysignf",
    "__builtin_copysignl",
    "__builtin_isgreater",
    "__builtin_isgreaterequal",
    "__builtin_isless",
    "__builtin_islessequal",
    "__builtin_islessgreater",
    "__builtin_isunordered",
    "__builtin_isnan",
    "__builtin_isnanf",
    "__builtin_isnanl",
    "__builtin_isfinite",
    "__builtin_finite",
    "__builtin_isinf",
    "__builtin_isinff",
    "__builtin_isinfl",
})


@dataclass(slots=True)
class _LoopFrame:
    exit_block: SSABlock
    continue_target: SSABlock
    breaks: list[tuple[SSABlock, dict[str, SSAValue]]] = field(default_factory=list)
    continues: list[tuple[SSABlock, dict[str, SSAValue]]] = field(default_factory=list)


@dataclass(slots=True)
class _BuilderState:
    function_name: str
    blocks: list[SSABlock] = field(default_factory=list)
    bindings: list[SSABinding] = field(default_factory=list)
    block_counter: int = 0
    value_counter: int = 0
    variable_versions: dict[str, int] = field(default_factory=dict)
    loop_stack: list[_LoopFrame] = field(default_factory=list)
    # Phase 4 MVP: names of locals that were alloca'd as struct/union
    # value slots. `s.field` on these works (resolves via
    # `.`-on-pointer), but whole-value uses (`helper(s)`, `s2 = s1`,
    # `return s`, `&s`) must be rejected because we model them as a
    # pointer-to-struct in the SSA IR, not an SSA struct value.
    struct_alloca_locals: set[str] = field(default_factory=set)
    # Phase 4 MVP: names of 2D array locals, mapped to the inner-dim size
    # so `mat[i][j]` can be lowered as `mat + i*inner_dim + j` with a
    # flat alloca of `count=outer*inner` scalars.
    multi_dim_arrays: dict[str, int] = field(default_factory=dict)
    # `sizeof(arr)` on a local array must return `count * sizeof(elem)`,
    # but inside a loop `env[arr]` may be rewritten to an SSAPhi by the
    # loop-header phi machinery (the phi has `type_name="T*"`, so the
    # generic sizeof fallback would return `sizeof(pointer)`). Persist
    # the original alloca's `(count, elem_type_name)` keyed by decl
    # name so `_sizeof_expr` can recover the right answer — see
    # gcc_torture 20030105-1.c (`sizeof(a) / sizeof(*a)` in for-cond).
    array_alloca_sizes: dict[str, tuple[int, str]] = field(default_factory=dict)


class SSABuilder:
    """Build a minimal SSA graph from a restricted pycparser function AST."""

    _UNARY_OPS = frozenset({"+", "-", "!", "~"})
    _KNOWN_EXTERN_GLOBAL_TYPES = {
        "errno": "int",
    }
    _ESCAPE_MAP = {
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

    def __init__(self, target_triple=None):
        self._state: _BuilderState | None = None
        self._function_return_types: dict[str, str] = {}
        self._typedef_types: dict[str, c_ast.Node] = {}
        self._struct_types: dict[str, c_ast.Struct] = {}
        self._union_types: dict[str, c_ast.Union] = {}
        self._enum_types: dict[str, c_ast.Enum] = {}
        self._enum_values: dict[str, int] = {}
        self._file_scope_decl_types: dict[str, c_ast.Node] = {}
        self.target_triple: str = c_target_triple(target_triple)

    def index_file_scope(self, ast: c_ast.FileAST) -> None:
        if not isinstance(ast, c_ast.FileAST):
            raise SSAConstructionError("expected FileAST")
        self._function_return_types = self._collect_function_return_types(ast)
        (
            self._typedef_types,
            self._struct_types,
            self._union_types,
            self._enum_types,
        ) = self._collect_type_info(ast)
        self._enum_values = self._collect_enum_values(ast)
        self._file_scope_decl_types = self._collect_file_scope_decl_types(ast)

    def build_file(self, ast: c_ast.FileAST) -> dict[str, SSAFunction]:
        if not isinstance(ast, c_ast.FileAST):
            raise SSAConstructionError("expected FileAST")

        self.index_file_scope(ast)
        functions: dict[str, SSAFunction] = {}
        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                functions[ext.decl.name] = self.build_function(ext)
        return functions

    def build_function(self, funcdef: c_ast.FuncDef) -> SSAFunction:
        if not isinstance(funcdef, c_ast.FuncDef):
            raise SSAConstructionError("expected FuncDef")

        func_name = funcdef.decl.name
        self._function_return_types.setdefault(
            func_name,
            self._function_return_type_name(funcdef.decl.type),
        )
        self._state = _BuilderState(function_name=func_name)
        try:
            entry = self._new_block("entry")
            env: dict[str, SSAValue] = {}
            params = self._build_params(funcdef, env)

            if not isinstance(funcdef.body, c_ast.Compound):
                raise SSAConstructionError(f"{func_name}: expected compound function body")

            exit_block, _ = self._lower_compound(funcdef.body, entry, env)
            if exit_block is not None and exit_block.terminator is None:
                if self._function_return_types.get(func_name, "int") == "void":
                    exit_block.terminator = SSAReturn(value=None)
                else:
                    raise SSAConstructionError(f"{func_name}: function ended without an explicit return")

            function = SSAFunction(
                name=func_name,
                params=params,
                blocks=list(self._state.blocks),
                entry_block=entry.name,
                bindings=list(self._state.bindings),
            )
            function.recompute_dominators()
            return function
        finally:
            self._state = None

    def _build_params(
        self,
        funcdef: c_ast.FuncDef,
        env: dict[str, SSAValue],
    ) -> list[SSAParam]:
        params: list[SSAParam] = []
        func_type = funcdef.decl.type
        if not isinstance(func_type, c_ast.FuncDecl):
            raise SSAConstructionError(f"{funcdef.decl.name}: expected FuncDecl")

        raw_params = func_type.args.params if func_type.args else []
        if self._is_void_parameter_list(raw_params):
            return params
        for param in raw_params or []:
            if isinstance(param, c_ast.EllipsisParam):
                raise SSAConstructionError(f"{funcdef.decl.name}: variadic functions are unsupported")
            if not isinstance(param, c_ast.Decl) or not param.name:
                raise SSAConstructionError(f"{funcdef.decl.name}: unsupported parameter {type(param).__name__}")
            type_name = self._decl_type_name(param.type)
            # Struct/union pass-by-value has platform-specific ABI lowering
            # (register splitting on arm64 HFA, byval on x86_64, etc.) that
            # only the AST codegen implements. Struct *pointers* are fine —
            # those are ordinary scalar pointers. Reject only by-value
            # aggregate params (see c-testsuite 00204/00215/00216.c).
            if (
                (type_name.startswith("struct ") or type_name.startswith("union "))
                and not type_name.endswith("*")
            ):
                raise SSAConstructionError(
                    f"{funcdef.decl.name}: unsupported aggregate parameter {param.name!r}"
                )
            value = SSAParam(
                name=self._new_version(param.name),
                type_name=type_name,
                source_name=param.name,
            )
            env[param.name] = value
            params.append(value)
        return params

    def _lower_compound(
        self,
        compound: c_ast.Compound,
        block: SSABlock | None,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        if block is None:
            return None, dict(env)

        current_block = block
        current_env = dict(env)
        declared_here: list[str] = []

        for stmt in compound.block_items or []:
            if current_block is None:
                break
            if isinstance(stmt, c_ast.Decl):
                current_block, current_env = self._lower_decl(
                    stmt,
                    current_block,
                    current_env,
                )
                declared_here.append(stmt.name)
                continue
            current_block, current_env = self._lower_stmt(
                stmt,
                current_block,
                current_env,
            )

        for name in declared_here:
            current_env.pop(name, None)
        return current_block, current_env

    def _lower_decl(
        self,
        decl: c_ast.Decl,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock, dict[str, SSAValue]]:
        storage = set(getattr(decl, "storage", None) or ())
        if "static" in storage or "extern" in storage:
            storage_list = ", ".join(sorted(storage))
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported local storage class {storage_list}"
            )
        if not decl.name:
            raise SSAConstructionError("unnamed local declarations are unsupported")
        if decl.name in env:
            raise SSAConstructionError(f"{self._state.function_name}: shadowing {decl.name!r} is unsupported")

        resolved_decl = self._resolve_type_node(decl.type)
        if isinstance(resolved_decl, c_ast.ArrayDecl):
            # Phase 4 MVP: allow positional scalar-only InitList for
            # local arrays (e.g. `int a[3] = {1, 2, 3};`) and string-
            # literal init for char arrays (`char s[] = "hi";`). Still
            # reject nested InitList and aggregate element types — fall
            # back to AST codegen for those.
            inner_array = None
            inner_resolved = self._resolve_type_node(resolved_decl.type)
            if isinstance(inner_resolved, c_ast.ArrayDecl):
                # 2D array `int mat[OUTER][INNER]`. Flatten to count=OUTER*INNER
                # of the inner scalar element type. `mat[i][j]` is lowered via
                # the `ArrayRef(ArrayRef(...))` fast path in `_lower_expr` and
                # `_lower_assignment_expr`.
                inner_elem_resolved = self._resolve_type_node(inner_resolved.type)
                if isinstance(inner_elem_resolved, (c_ast.ArrayDecl, c_ast.Struct, c_ast.Union)):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported aggregate element in 2D array"
                    )
                if getattr(inner_resolved, "dim", None) is None:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported unsized inner dim in 2D array"
                    )
                inner_array = inner_resolved
                elem_type_name = self._decl_type_name(inner_resolved.type)
            else:
                elem_type_name = self._decl_type_name(resolved_decl.type)
            # Unsized outer dim (`int a[]` / `char s[]`) — count resolved
            # from the init. `_array_type_count` raises for dim=None so
            # guard with getattr() before calling.
            if getattr(resolved_decl, "dim", None) is None:
                count = 0
            else:
                count = self._array_type_count(resolved_decl)
            # For 2D: total = outer * inner, and record inner dim for
            # subsequent index math.
            inner_dim = 0
            if inner_array is not None:
                inner_dim = self._array_type_count(inner_array)
                if count > 0:
                    count = count * inner_dim
                # 2D init not supported yet — reject until the nested
                # InitList form is wired in.
                if decl.init is not None:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported initializer for 2D array"
                    )
            # String-literal init for a char array becomes an equivalent
            # positional InitList of int Constants plus the NUL terminator.
            is_char_elem = elem_type_name in {
                "char", "signed char", "unsigned char", "int8", "int8_t", "uint8_t",
            }
            if (
                decl.init is not None
                and isinstance(decl.init, c_ast.Constant)
                and decl.init.type == "string"
                and is_char_elem
            ):
                decoded = self._decode_string_literal_bytes(decl.init.value)
                # `char s[]` → include trailing NUL; `char s[N]` → only
                # include the first N bytes (no NUL if string is exact).
                synth_exprs: list[c_ast.Node] = []
                coord = getattr(decl.init, "coord", None)
                if count <= 0:
                    bytes_to_emit = list(decoded) + [0]
                    count = len(bytes_to_emit)
                else:
                    if len(decoded) > count:
                        raise SSAConstructionError(
                            f"{self._state.function_name}: string initializer too long for char[{count}]"
                        )
                    bytes_to_emit = list(decoded)
                    if len(bytes_to_emit) < count:
                        bytes_to_emit.append(0)
                for b in bytes_to_emit:
                    synth_exprs.append(
                        c_ast.Constant(type="int", value=str(b), coord=coord)
                    )
                # Replace the init with a synthetic InitList and fall
                # through to the shared InitList path below.
                decl = c_ast.Decl(
                    name=decl.name,
                    quals=list(getattr(decl, "quals", None) or ()),
                    storage=list(getattr(decl, "storage", None) or ()),
                    funcspec=list(getattr(decl, "funcspec", None) or ()),
                    type=decl.type,
                    init=c_ast.InitList(exprs=synth_exprs, coord=coord),
                    bitsize=getattr(decl, "bitsize", None),
                    coord=getattr(decl, "coord", None),
                )
            if decl.init is not None and not isinstance(decl.init, c_ast.InitList):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported local array initializer"
                )
            if decl.init is not None:
                resolved_elem = self._resolve_type_node(resolved_decl.type)
                if isinstance(resolved_elem, (c_ast.ArrayDecl, c_ast.Struct, c_ast.Union)):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported aggregate element in array init"
                    )
                init_exprs = list(decl.init.exprs or ())
                # Classify: positional vs designated-by-index (`[N] = v`).
                # Collect index→expr for designators; error on mixed ordering.
                has_array_designator = False
                max_designated_index = -1
                for expr in init_exprs:
                    if isinstance(expr, c_ast.InitList):
                        raise SSAConstructionError(
                            f"{self._state.function_name}: unsupported nested array initializer"
                        )
                    if isinstance(expr, c_ast.NamedInitializer):
                        has_array_designator = True
                        if (
                            len(expr.name or ()) != 1
                            or not isinstance(expr.name[0], c_ast.Constant)
                        ):
                            raise SSAConstructionError(
                                f"{self._state.function_name}: unsupported array designator shape"
                            )
                        try:
                            idx = self._eval_const_int(expr.name[0])
                        except Exception:
                            raise SSAConstructionError(
                                f"{self._state.function_name}: non-constant array designator"
                            )
                        if idx < 0:
                            raise SSAConstructionError(
                                f"{self._state.function_name}: negative array designator"
                            )
                        max_designated_index = max(max_designated_index, idx)
                # When no size is declared (`int a[] = {...}`), size from init.
                if count <= 0:
                    if has_array_designator:
                        count = max_designated_index + 1
                    else:
                        count = len(init_exprs) or 1
                if not has_array_designator and len(init_exprs) > count:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: too many array initializers"
                    )
                if has_array_designator and max_designated_index >= count:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: array designator out of bounds"
                    )
            alloc = SSAStackAlloc(
                name=self._new_version(decl.name),
                type_name=f"{elem_type_name}*",
                elem_type_name=elem_type_name,
                count=count,
                source_coord=self._coord_key(getattr(decl, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(alloc)
            next_env = dict(env)
            next_env[decl.name] = alloc
            if inner_array is not None and inner_dim > 0:
                self._state.multi_dim_arrays[decl.name] = inner_dim
            if count > 0:
                self._state.array_alloca_sizes[decl.name] = (
                    count, elem_type_name,
                )
            if decl.init is not None:
                coord = self._coord_key(getattr(decl, "coord", None))
                init_exprs = list(decl.init.exprs or ())
                # Build index → expr map supporting mixed positional + designated.
                # Positional exprs fill the next slot; designated exprs jump.
                slot_exprs: list[c_ast.Node | None] = [None] * count
                cursor = 0
                for expr in init_exprs:
                    if isinstance(expr, c_ast.NamedInitializer):
                        idx = self._eval_const_int(expr.name[0])
                        slot_exprs[idx] = expr.expr
                        cursor = idx + 1
                    else:
                        if cursor >= count:
                            raise SSAConstructionError(
                                f"{self._state.function_name}: too many positional array initializers"
                            )
                        slot_exprs[cursor] = expr
                        cursor += 1
                for i in range(count):
                    index_const = SSAConstant.from_int(
                        i, type_name="int", is_safe=True,
                    )
                    if slot_exprs[i] is not None:
                        _, value = self._lower_value_expr(slot_exprs[i], block, next_env)
                        value = self._coerce_value_to_type(
                            block, next_env, value, elem_type_name, coord,
                        )
                    else:
                        value = SSAConstant.from_int(
                            0, type_name=elem_type_name, is_safe=True,
                        )
                    # elem_addr = alloc + i
                    elem_addr = SSABinaryOp(
                        name=self._new_temp(),
                        type_name=f"{elem_type_name}*",
                        op="+",
                        left=alloc,
                        right=index_const,
                        source_coord=coord,
                        available_bindings=self._binding_snapshot(next_env),
                    )
                    block.append(elem_addr)
                    store = SSAStore(
                        name=self._new_temp(),
                        type_name="",
                        addr=elem_addr,
                        value=value,
                        source_coord=coord,
                        available_bindings=self._binding_snapshot(next_env),
                    )
                    block.append(store)
            return block, next_env

        # Phase 4 MVP: local struct/union value declarations are modeled
        # as stack allocations so `s.field` can resolve through the
        # existing `.`-on-pointer path. This unlocks the "direct
        # aggregate value field access" rejection for zlib-style code.
        # Scope:
        #   - no initializer, OR a positional scalar-only InitList
        #     (`struct S s = {1, 2};`),
        #   - struct/union type (not a typedef to scalar/pointer/array),
        #   - `s.field` access and `&s`; whole-value uses (`helper(s)`,
        #     `s2 = s1`, `return s`) are rejected at their use site via
        #     `_state.struct_alloca_locals`.
        if self._is_aggregate_value_type(resolved_decl):
            # Unwrap `(struct T){...}` compound literal into its inner
            # InitList so it flows through the same element-wise store path.
            init_expr = decl.init
            if (
                isinstance(init_expr, c_ast.CompoundLiteral)
                and isinstance(init_expr.init, c_ast.InitList)
            ):
                init_expr = init_expr.init
            if init_expr is not None and not isinstance(init_expr, c_ast.InitList):
                # Other init shapes (like `struct S s = other_s;` copy)
                # need memcpy, not yet supported.
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported struct initializer"
                )
            struct_type_name = self._decl_type_name(decl.type)
            alloc = SSAStackAlloc(
                name=self._new_version(decl.name),
                type_name=f"{struct_type_name}*",
                elem_type_name=struct_type_name,
                count=1,
                source_coord=self._coord_key(getattr(decl, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(alloc)
            next_env = dict(env)
            next_env[decl.name] = alloc
            self._state.struct_alloca_locals.add(decl.name)

            if isinstance(init_expr, c_ast.InitList):
                # Positional scalar-only InitList.
                self._lower_struct_init_list(
                    alloc, resolved_decl, init_expr, block, next_env, decl,
                )
            return block, next_env

        type_name = self._decl_type_name(decl.type)
        if decl.init is not None:
            block, value = self._lower_value_expr(decl.init, block, env)
            # Snapshot a scalar initializer that is a bare SSAGlobalRef
            # into an explicit load, so subsequent reads of the local use
            # this captured value instead of re-loading the global each
            # time (which would also observe any intervening stores to
            # the global through function calls). See gcc_torture
            # nestfunc-4.c for the divergent-recursion symptom.
            if isinstance(value, SSAGlobalRef) and not type_name.endswith("*"):
                load = SSALoad(
                    name=self._new_temp(),
                    type_name=value.type_name,
                    base=value,
                    source_coord=self._coord_key(getattr(decl, "coord", None)),
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(load)
                value = load
            value = self._coerce_value_to_type(
                block,
                env,
                value,
                type_name,
                self._coord_key(getattr(decl, "coord", None)),
            )
        else:
            value = SSAUndef(name=f"undef.{decl.name}", type_name=type_name, source_name=decl.name)

        next_env = dict(env)
        next_env[decl.name] = value
        if decl.init is not None:
            self._record_binding(
                kind="decl_init",
                block=block,
                target_name=decl.name,
                value=value,
                type_name=type_name,
                source_coord=self._coord_key(getattr(decl, "coord", None)),
            )
        return block, next_env

    def _lower_stmt(
        self,
        stmt: c_ast.Node,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        if isinstance(stmt, c_ast.Compound):
            return self._lower_compound(stmt, block, env)

        if isinstance(stmt, c_ast.Assignment):
            next_env = dict(env)
            block, _ = self._lower_assignment_expr(stmt, block, next_env)
            return block, next_env

        if isinstance(stmt, c_ast.Return):
            if stmt.expr is not None:
                # Phase 4 MVP: `return s` on a struct-alloca local
                # should copy the struct value. We can't represent that
                # in the SSA IR yet, so fall back to AST codegen.
                if (
                    isinstance(stmt.expr, c_ast.ID)
                    and stmt.expr.name in self._state.struct_alloca_locals
                ):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported struct-value return {stmt.expr.name!r}"
                    )
                block, value = self._lower_value_expr(stmt.expr, block, env)
            else:
                value = None
            block.terminator = SSAReturn(
                value=value,
                source_coord=self._coord_key(getattr(stmt, "coord", None)),
            )
            return None, dict(env)

        if isinstance(stmt, c_ast.If):
            return self._lower_if(stmt, block, env)

        if isinstance(stmt, c_ast.While):
            return self._lower_while(stmt, block, env)

        if isinstance(stmt, c_ast.DoWhile):
            return self._lower_dowhile(stmt, block, env)

        if isinstance(stmt, c_ast.For):
            return self._lower_for(stmt, block, env)

        if isinstance(stmt, c_ast.Switch):
            return self._lower_switch(stmt, block, env)

        if isinstance(stmt, c_ast.Break):
            if not self._state.loop_stack:
                raise SSAConstructionError(
                    f"{self._state.function_name}: break outside of loop/switch"
                )
            frame = self._state.loop_stack[-1]
            block.terminator = SSAJump(target=frame.exit_block.name)
            frame.breaks.append((block, dict(env)))
            return None, dict(env)

        if isinstance(stmt, c_ast.Continue):
            if not self._state.loop_stack:
                raise SSAConstructionError(
                    f"{self._state.function_name}: continue outside of loop"
                )
            frame = self._state.loop_stack[-1]
            block.terminator = SSAJump(target=frame.continue_target.name)
            frame.continues.append((block, dict(env)))
            return None, dict(env)

        if isinstance(stmt, c_ast.ExprList):
            # Comma expression in statement position: evaluate each in order.
            next_env = dict(env)
            current_block = block
            for sub in stmt.exprs or []:
                if isinstance(sub, c_ast.Assignment):
                    current_block, _ = self._lower_assignment_expr(
                        sub, current_block, next_env,
                    )
                elif isinstance(sub, c_ast.UnaryOp) and sub.op in {"p++", "p--", "++", "--"}:
                    current_block, next_env = self._lower_incdec_stmt(
                        sub, current_block, next_env,
                    )
                else:
                    current_block, _ = self._lower_value_expr(
                        sub, current_block, next_env,
                    )
            return current_block, next_env

        if isinstance(stmt, c_ast.UnaryOp) and stmt.op in {"p++", "p--", "++", "--"}:
            return self._lower_incdec_stmt(stmt, block, env)

        if isinstance(stmt, c_ast.FuncCall):
            next_env = dict(env)
            block, _ = self._lower_value_expr(stmt, block, next_env)
            return block, next_env

        if isinstance(stmt, c_ast.Cast):
            next_env = dict(env)
            block, _ = self._lower_value_expr(stmt, block, next_env)
            return block, next_env

        if isinstance(stmt, c_ast.EmptyStatement):
            return block, dict(env)

        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported statement {type(stmt).__name__}"
        )

    def _lower_if(
        self,
        stmt: c_ast.If,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        then_block = self._new_block("if.then")
        else_block = self._new_block("if.else")
        self._lower_condition_branch(
            stmt.cond,
            block,
            env,
            true_target=then_block.name,
            false_target=else_block.name,
            source_coord=self._coord_key(stmt.coord),
        )

        then_exit, then_env = self._lower_stmt_or_compound(stmt.iftrue, then_block, dict(env))
        else_exit, else_env = self._lower_stmt_or_compound(stmt.iffalse, else_block, dict(env))

        incoming: list[tuple[SSABlock, dict[str, SSAValue]]] = []
        if then_exit is not None:
            incoming.append((then_exit, then_env))
        if else_exit is not None:
            incoming.append((else_exit, else_env))

        if not incoming:
            return None, dict(env)

        join = self._new_block("if.end")
        for pred_block, _ in incoming:
            if pred_block.terminator is None:
                pred_block.terminator = SSAJump(target=join.name)

        merged_env = self._merge_envs(join, incoming)
        return join, merged_env

    def _lower_while(
        self,
        stmt: c_ast.While,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        header = self._new_block("while.header")
        body_block = self._new_block("while.body")
        exit_block = self._new_block("while.end")

        # Jump from predecessor into loop header.
        block.terminator = SSAJump(target=header.name)

        # Create placeholder phi nodes at the header for all live variables.
        # The pre-header incoming is known; the back-edge incoming will be
        # patched after we lower the body.
        header_env, header_phis = self._create_loop_header_phis(
            header, block, env,
        )

        # Lower the condition in the header block, tracking which blocks
        # end up branching to exit_block via short-circuit decomposition
        # and their per-block env snapshots (for side-effecting conds).
        cond_mark = len(self._state.blocks)
        cond_exit_envs: dict[str, dict[str, SSAValue]] = {}
        self._lower_condition_branch(
            stmt.cond,
            header,
            header_env,
            true_target=body_block.name,
            false_target=exit_block.name,
            source_coord=self._coord_key(getattr(stmt, "coord", None)),
            exit_envs=cond_exit_envs,
        )
        cond_chain = [header] + self._state.blocks[cond_mark:]

        # Lower the body with a loop frame on the stack so break/continue
        # can redirect to the exit block / the header.
        frame = _LoopFrame(exit_block=exit_block, continue_target=header)
        self._state.loop_stack.append(frame)
        try:
            body_exit, body_env = self._lower_stmt_or_compound(
                stmt.stmt, body_block, dict(header_env),
            )
        finally:
            self._state.loop_stack.pop()

        # Patch back-edges: normal fall-through plus every continue site.
        if body_exit is not None and body_exit.terminator is None:
            body_exit.terminator = SSAJump(target=header.name)
            self._patch_loop_back_edge_phis(header_phis, body_exit, body_env)
        for cont_block, cont_env in frame.continues:
            self._patch_loop_back_edge_phis(header_phis, cont_block, cont_env)

        # Exit block predecessors: every cond-chain block that branches
        # to exit_block (with its own env snapshot) plus all break sites.
        cond_preds = self._blocks_targeting(cond_chain, exit_block.name)
        if frame.breaks:
            incoming: list[tuple[SSABlock, dict[str, SSAValue]]] = [
                (pred, cond_exit_envs.get(pred.name, dict(header_env)))
                for pred in cond_preds
            ]
            incoming.extend(frame.breaks)
            exit_env = self._merge_envs(exit_block, incoming)
            return exit_block, exit_env

        if len(cond_preds) > 1:
            incoming = [
                (pred, cond_exit_envs.get(pred.name, dict(header_env)))
                for pred in cond_preds
            ]
            exit_env = self._merge_envs(exit_block, incoming)
            return exit_block, exit_env

        if len(cond_preds) == 1:
            return exit_block, cond_exit_envs.get(
                cond_preds[0].name, dict(header_env)
            )

        # No cond-chain block reaches exit (e.g. always-true condition in
        # source without break): exit block is unreachable.
        return exit_block, dict(header_env)

    def _lower_dowhile(
        self,
        stmt: c_ast.DoWhile,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        body_block = self._new_block("dowhile.body")
        latch_block = self._new_block("dowhile.latch")
        exit_block = self._new_block("dowhile.end")

        # Jump from predecessor into body.
        block.terminator = SSAJump(target=body_block.name)

        # Create placeholder phis at body entry for loop-carried values.
        body_env, body_phis = self._create_loop_header_phis(
            body_block, block, env,
        )

        # Lower body with loop frame so break/continue can redirect.
        frame = _LoopFrame(exit_block=exit_block, continue_target=latch_block)
        self._state.loop_stack.append(frame)
        try:
            body_exit, body_exit_env = self._lower_stmt_or_compound(
                stmt.stmt, body_block, dict(body_env),
            )
        finally:
            self._state.loop_stack.pop()

        # Collect latch predecessors: body fall-through (if any) + continues.
        latch_incoming: list[tuple[SSABlock, dict[str, SSAValue]]] = []
        if body_exit is not None and body_exit.terminator is None:
            body_exit.terminator = SSAJump(target=latch_block.name)
            latch_incoming.append((body_exit, body_exit_env))
        latch_incoming.extend(frame.continues)

        if latch_incoming:
            if len(latch_incoming) > 1:
                latch_env = self._merge_envs(latch_block, latch_incoming)
            else:
                latch_env = dict(latch_incoming[0][1])

            cond_mark = len(self._state.blocks)
            cond_exit_envs: dict[str, dict[str, SSAValue]] = {}
            self._lower_condition_branch(
                stmt.cond,
                latch_block,
                latch_env,
                true_target=body_block.name,
                false_target=exit_block.name,
                source_coord=self._coord_key(getattr(stmt, "coord", None)),
                exit_envs=cond_exit_envs,
            )
            cond_chain = [latch_block] + self._state.blocks[cond_mark:]

            # Patch body-phi back-edges for every cond-chain block whose
            # branch reaches body_block. Each predecessor uses its own
            # env snapshot (gzread.c gz_fetch regression + side-effect conds).
            for back in self._blocks_targeting(cond_chain, body_block.name):
                self._patch_loop_back_edge_phis(
                    body_phis, back,
                    cond_exit_envs.get(back.name, latch_env),
                )

            cond_preds = self._blocks_targeting(cond_chain, exit_block.name)
            if frame.breaks:
                incoming: list[tuple[SSABlock, dict[str, SSAValue]]] = [
                    (pred, cond_exit_envs.get(pred.name, dict(latch_env)))
                    for pred in cond_preds
                ]
                incoming.extend(frame.breaks)
                exit_env = self._merge_envs(exit_block, incoming)
                return exit_block, exit_env

            if len(cond_preds) > 1:
                incoming = [
                    (pred, cond_exit_envs.get(pred.name, dict(latch_env)))
                    for pred in cond_preds
                ]
                exit_env = self._merge_envs(exit_block, incoming)
                return exit_block, exit_env

            if len(cond_preds) == 1:
                return exit_block, cond_exit_envs.get(
                    cond_preds[0].name, dict(latch_env)
                )

            return exit_block, dict(latch_env)

        # Body never exits normally (no fall-through to latch AND no
        # continues reached latch). If break sites jumped to exit, merge them.
        if frame.breaks:
            exit_env = self._merge_envs(exit_block, list(frame.breaks))
            return exit_block, exit_env

        # Body never exits normally (e.g. always returns) — loop is dead after.
        return exit_block, dict(body_env)

    def _lower_for(
        self,
        stmt: c_ast.For,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        # Lower the init part in the current block.
        current_block = block
        current_env = dict(env)
        declared_names: list[str] = []

        if stmt.init is not None:
            if isinstance(stmt.init, c_ast.DeclList):
                for decl in stmt.init.decls:
                    current_block, current_env = self._lower_decl(
                        decl, current_block, current_env,
                    )
                    declared_names.append(decl.name)
            elif isinstance(stmt.init, (c_ast.Assignment, c_ast.ExprList)):
                current_block, current_env = self._lower_stmt(
                    stmt.init, current_block, current_env,
                )
            else:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported for-init {type(stmt.init).__name__}"
                )

        header = self._new_block("for.header")
        body_block = self._new_block("for.body")
        exit_block = self._new_block("for.end")

        current_block.terminator = SSAJump(target=header.name)

        header_env, header_phis = self._create_loop_header_phis(
            header, current_block, current_env,
        )

        # Lower the condition (if present) in the header.
        cond_exit_envs: dict[str, dict[str, SSAValue]] = {}
        if stmt.cond is not None:
            cond_mark = len(self._state.blocks)
            self._lower_condition_branch(
                stmt.cond,
                header,
                header_env,
                true_target=body_block.name,
                false_target=exit_block.name,
                source_coord=self._coord_key(getattr(stmt, "coord", None)),
                exit_envs=cond_exit_envs,
            )
            cond_chain = [header] + self._state.blocks[cond_mark:]
        else:
            # Infinite loop: for(;;)
            header.terminator = SSAJump(target=body_block.name)
            cond_chain = []

        # `continue` in a for-loop runs the `next` expression before
        # jumping back to the header, so route continues through a
        # dedicated block that evaluates `next` for any predecessor.
        continue_block = self._new_block("for.continue")

        # Lower the body with a loop frame for break/continue.
        frame = _LoopFrame(exit_block=exit_block, continue_target=continue_block)
        self._state.loop_stack.append(frame)
        try:
            body_exit, body_env = self._lower_stmt_or_compound(
                stmt.stmt, body_block, dict(header_env),
            )
        finally:
            self._state.loop_stack.pop()

        # Collect predecessors of continue_block: fall-through + continues.
        continue_incoming: list[tuple[SSABlock, dict[str, SSAValue]]] = []
        if body_exit is not None and body_exit.terminator is None:
            body_exit.terminator = SSAJump(target=continue_block.name)
            continue_incoming.append((body_exit, body_env))
        continue_incoming.extend(frame.continues)

        if continue_incoming:
            if len(continue_incoming) > 1:
                cont_env = self._merge_envs(continue_block, continue_incoming)
            else:
                cont_env = dict(continue_incoming[0][1])

            cont_block_out = continue_block
            if stmt.next is not None:
                if isinstance(stmt.next, c_ast.Assignment):
                    cont_block_out, cont_env = self._lower_stmt(
                        stmt.next, cont_block_out, cont_env,
                    )
                elif isinstance(stmt.next, c_ast.UnaryOp) and stmt.next.op in {"p++", "p--", "++", "--"}:
                    cont_block_out, cont_env = self._lower_incdec_stmt(
                        stmt.next, cont_block_out, cont_env,
                    )
                elif isinstance(stmt.next, c_ast.FuncCall):
                    cont_block_out, _ = self._lower_value_expr(stmt.next, cont_block_out, cont_env)
                elif isinstance(stmt.next, c_ast.ExprList):
                    cont_block_out, cont_env = self._lower_stmt(
                        stmt.next, cont_block_out, cont_env,
                    )
                else:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported for-next {type(stmt.next).__name__}"
                    )

            # Back-edge continue_block (after next) to header.
            if cont_block_out.terminator is None:
                cont_block_out.terminator = SSAJump(target=header.name)
                self._patch_loop_back_edge_phis(header_phis, cont_block_out, cont_env)

        # If the loop has no cond and no break, exit is unreachable.
        if stmt.cond is None:
            if not frame.breaks:
                return None, dict(env)
            exit_env = self._merge_envs(exit_block, list(frame.breaks))
            for name in declared_names:
                exit_env.pop(name, None)
            return exit_block, exit_env

        # Exit predecessors: cond-chain blocks + break sites.
        cond_preds = self._blocks_targeting(cond_chain, exit_block.name)
        if frame.breaks:
            incoming: list[tuple[SSABlock, dict[str, SSAValue]]] = [
                (pred, cond_exit_envs.get(pred.name, dict(header_env)))
                for pred in cond_preds
            ]
            incoming.extend(frame.breaks)
            exit_env = self._merge_envs(exit_block, incoming)
        elif len(cond_preds) > 1:
            incoming = [
                (pred, cond_exit_envs.get(pred.name, dict(header_env)))
                for pred in cond_preds
            ]
            exit_env = self._merge_envs(exit_block, incoming)
        elif len(cond_preds) == 1:
            exit_env = cond_exit_envs.get(cond_preds[0].name, dict(header_env))
        else:
            exit_env = dict(header_env)

        for name in declared_names:
            exit_env.pop(name, None)

        return exit_block, exit_env

    def _lower_incdec_stmt(
        self,
        expr: c_ast.UnaryOp,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock, dict[str, SSAValue]]:
        """Lower ++x, --x, x++, x-- as statements (value discarded)."""
        next_env = dict(env)
        self._lower_incdec_expr(expr, block, next_env)
        return block, next_env

    def _lower_switch(
        self,
        stmt: c_ast.Switch,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        """Lower a C switch into SSASwitch + labelled case blocks.

        LLVM reference: SwitchInst dispatches on an integer to one of
        several (const → block) targets, with a default. We walk the
        switch body's Compound top-level, one pass to allocate blocks
        per Case/Default, then a second pass to lower each group's
        statements. Fall-through is honored by jumping to the next
        group's block when a case body does not terminate itself.
        """
        if not isinstance(stmt.stmt, c_ast.Compound):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported switch body shape"
            )
        items = list(stmt.stmt.block_items or ())
        if not items:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported empty switch body"
            )
        # Reject anything that's not a Case or Default at the top level.
        for item in items:
            if not isinstance(item, (c_ast.Case, c_ast.Default)):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported non-case statement in switch body"
                )

        # MVP restriction: every case/default body must end in an explicit
        # terminator (break, return, etc.) — no fall-through. Supporting
        # fall-through correctly requires per-case env merging because a
        # case that falls into the next one may have modified env; the
        # next case's block would then need a phi node for every local
        # that differs, including the two distinct entry paths (direct
        # switch dispatch vs fall-through). Reject for now.
        def _last_stmt_ends_flow(stmts: tuple) -> bool:
            if not stmts:
                return False
            last = stmts[-1]
            if isinstance(last, (c_ast.Break, c_ast.Return, c_ast.Continue)):
                return True
            if isinstance(last, c_ast.Compound):
                return _last_stmt_ends_flow(tuple(last.block_items or ()))
            return False

        for item in items:
            stmts = tuple(item.stmts or ())
            if not _last_stmt_ends_flow(stmts):
                # The LAST case/default without a terminator is okay — it
                # naturally exits the switch. But any non-last case that
                # falls through to the next is not supported yet.
                if item is not items[-1]:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported switch fall-through"
                    )

        block, switch_value = self._lower_value_expr(stmt.cond, block, env)
        exit_block = self._new_block("switch.end")

        # Pre-allocate one block per group.
        group_blocks: list[SSABlock] = []
        case_pairs: list[tuple[int, str]] = []
        default_target: str | None = None
        for item in items:
            if isinstance(item, c_ast.Case):
                b = self._new_block("switch.case")
                try:
                    case_value = self._eval_const_int(item.expr)
                except SSAConstructionError:
                    raise
                case_pairs.append((case_value, b.name))
                group_blocks.append(b)
            else:  # Default
                b = self._new_block("switch.default")
                if default_target is not None:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: multiple default labels"
                    )
                default_target = b.name
                group_blocks.append(b)

        block.terminator = SSASwitch(
            value=switch_value,
            default_target=default_target or exit_block.name,
            cases=tuple(case_pairs),
            source_coord=self._coord_key(getattr(stmt, "coord", None)),
        )

        # Push a loop frame so `break` inside lands at exit_block. Inherit
        # outer continue target if any (a switch itself is not a
        # continue target; `continue` inside a switch refers to the
        # enclosing loop).
        outer_continue = (
            self._state.loop_stack[-1].continue_target
            if self._state.loop_stack
            else exit_block
        )
        frame = _LoopFrame(
            exit_block=exit_block, continue_target=outer_continue,
        )
        self._state.loop_stack.append(frame)
        try:
            break_incoming: list[tuple[SSABlock, dict[str, SSAValue]]] = []
            for idx, item in enumerate(items):
                cur_block: SSABlock | None = group_blocks[idx]
                cur_env = dict(env)
                stmts = item.stmts or ()
                for sub in stmts:
                    if cur_block is None:
                        break
                    cur_block, cur_env = self._lower_stmt_or_compound(
                        sub, cur_block, cur_env,
                    )
                # Fall through to next group's block if body didn't end.
                if cur_block is not None and cur_block.terminator is None:
                    next_target = (
                        group_blocks[idx + 1]
                        if idx + 1 < len(group_blocks)
                        else exit_block
                    )
                    cur_block.terminator = SSAJump(target=next_target.name)
                    # Fall-through into next group's env isn't modeled;
                    # each case re-reads from env snapshot. Record the
                    # reaching env as a "break_incoming" only when
                    # falling off the last group directly to exit.
                    if next_target is exit_block:
                        break_incoming.append((cur_block, cur_env))
        finally:
            self._state.loop_stack.pop()

        # Collect exit predecessors: break sites + natural fall-throughs.
        exit_incoming = list(frame.breaks) + break_incoming
        # If no Default label was specified the `switch` itself may target
        # exit_block; in that case block (the switch header) also reaches
        # exit with its own env.
        if default_target is None:
            exit_incoming.append((block, dict(env)))

        if not exit_incoming:
            # Every case terminates (e.g. `return`); exit is unreachable.
            return None, dict(env)

        if len(exit_incoming) == 1:
            return exit_block, dict(exit_incoming[0][1])

        exit_env = self._merge_envs(exit_block, exit_incoming)
        return exit_block, exit_env

    def _eval_const_int(self, expr: c_ast.Node) -> int:
        """Best-effort compile-time integer evaluation for switch case."""
        if isinstance(expr, c_ast.Constant) and expr.type in ("int", "char"):
            if expr.type == "char":
                return self._parse_char_constant(expr.value)
            return self._parse_int_constant(expr.value)
        if isinstance(expr, c_ast.UnaryOp) and expr.op == "-":
            return -self._eval_const_int(expr.expr)
        if isinstance(expr, c_ast.UnaryOp) and expr.op == "+":
            return self._eval_const_int(expr.expr)
        if isinstance(expr, c_ast.Cast):
            return self._eval_const_int(expr.expr)
        if isinstance(expr, c_ast.ID) and expr.name in self._enum_values:
            return self._enum_values[expr.name]
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported non-constant case label"
        )

    def _create_loop_header_phis(
        self,
        header: SSABlock,
        pre_header: SSABlock,
        pre_header_env: dict[str, SSAValue],
    ) -> tuple[dict[str, SSAValue], dict[str, SSAPhi]]:
        """Create phi nodes at the loop header for all live variables.

        Returns the header env (mapping each variable to its phi) and the
        phi map (for patching the back-edge incoming later).
        """
        header_env: dict[str, SSAValue] = {}
        header_phis: dict[str, SSAPhi] = {}

        for name in sorted(pre_header_env):
            pre_value = pre_header_env[name]
            phi = SSAPhi(
                name=self._new_version(name),
                type_name=getattr(pre_value, "type_name", "int"),
                variable_name=name,
                incomings=[(pre_header.name, pre_value)],
            )
            header.append(phi)
            header_env[name] = phi
            header_phis[name] = phi
            _debug_phi_types(phi, f"loop-header@{self._state.function_name}")

        return header_env, header_phis

    def _patch_loop_back_edge_phis(
        self,
        header_phis: dict[str, SSAPhi],
        back_edge_block: SSABlock,
        back_edge_env: dict[str, SSAValue],
    ) -> None:
        """Fill in the back-edge incoming for loop header phis."""
        for name, phi in header_phis.items():
            back_value = back_edge_env.get(name)
            if back_value is None:
                back_value = SSAUndef(
                    name=f"undef.{name}",
                    type_name=getattr(phi, "type_name", "int"),
                    source_name=name,
                )
            phi.incomings.append((back_edge_block.name, back_value))

    def _blocks_targeting(
        self, blocks: list[SSABlock], target_name: str,
    ) -> list[SSABlock]:
        """Return blocks whose terminator branches to `target_name`."""
        result: list[SSABlock] = []
        for b in blocks:
            term = b.terminator
            if isinstance(term, SSAJump) and term.target == target_name:
                result.append(b)
            elif isinstance(term, SSABranch) and (
                term.true_target == target_name or term.false_target == target_name
            ):
                result.append(b)
        return result

    def _lower_condition_branch(
        self,
        expr: c_ast.Node,
        block: SSABlock,
        env: dict[str, SSAValue],
        *,
        true_target: str,
        false_target: str,
        source_coord: str | None,
        exit_envs: dict[str, dict[str, SSAValue]] | None = None,
    ) -> None:
        """Lower a condition expression into a branching terminator.

        If `exit_envs` is provided, each block whose terminator is a
        branch is snapshotted there. Needed for loop cond chains with
        side-effecting subexpressions (`++i`) where each cond-chain
        predecessor has a distinct env.
        """
        if isinstance(expr, c_ast.BinaryOp) and expr.op in {"&&", "||"}:
            rhs_block = self._new_block("if.cond.rhs")
            if expr.op == "&&":
                self._lower_condition_branch(
                    expr.left,
                    block,
                    env,
                    true_target=rhs_block.name,
                    false_target=false_target,
                    source_coord=None,
                    exit_envs=exit_envs,
                )
                self._lower_condition_branch(
                    expr.right,
                    rhs_block,
                    env,
                    true_target=true_target,
                    false_target=false_target,
                    source_coord=None,
                    exit_envs=exit_envs,
                )
                return
            self._lower_condition_branch(
                expr.left,
                block,
                env,
                true_target=true_target,
                false_target=rhs_block.name,
                source_coord=None,
                exit_envs=exit_envs,
            )
            self._lower_condition_branch(
                expr.right,
                rhs_block,
                env,
                true_target=true_target,
                false_target=false_target,
                source_coord=None,
                exit_envs=exit_envs,
            )
            return

        condition = self._lower_expr(expr, block, env)
        block.terminator = SSABranch(
            condition=condition,
            true_target=true_target,
            false_target=false_target,
            source_coord=source_coord,
        )
        if exit_envs is not None:
            exit_envs[block.name] = dict(env)

    def _lower_value_expr(
        self,
        expr: c_ast.Node,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock, SSAValue]:
        if isinstance(expr, c_ast.BinaryOp) and expr.op in {"&&", "||"}:
            return self._lower_short_circuit_value(expr, block, env)
        if isinstance(expr, c_ast.TernaryOp):
            return self._lower_ternary_value(expr, block, env)
        if isinstance(expr, c_ast.ExprList):
            if not expr.exprs:
                raise SSAConstructionError(
                    f"{self._state.function_name}: empty ExprList"
                )
            for sub in expr.exprs[:-1]:
                block, _ = self._lower_value_expr(sub, block, env)
            return self._lower_value_expr(expr.exprs[-1], block, env)
        return block, self._lower_expr(expr, block, env)

    def _lower_short_circuit_value(
        self,
        expr: c_ast.BinaryOp,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock, SSAValue]:
        true_block = self._new_block("logic.true")
        false_block = self._new_block("logic.false")
        self._lower_condition_branch(
            expr,
            block,
            env,
            true_target=true_block.name,
            false_target=false_block.name,
            source_coord=self._coord_key(getattr(expr, "coord", None)),
        )

        join = self._new_block("logic.end")
        true_block.terminator = SSAJump(target=join.name)
        false_block.terminator = SSAJump(target=join.name)

        phi = SSAPhi(
            name=self._new_temp(),
            type_name="int",
            incomings=[
                (true_block.name, SSAConstant.from_int(1, type_name="int", is_safe=True)),
                (false_block.name, SSAConstant.from_int(0, type_name="int", is_safe=True)),
            ],
            source_coord=self._coord_key(getattr(expr, "coord", None)),
        )
        join.append(phi)
        return join, phi

    def _lower_ternary_value(
        self,
        expr: c_ast.TernaryOp,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock, SSAValue]:
        before_env = dict(env)
        cond_env = dict(env)
        true_block = self._new_block("ternary.true")
        false_block = self._new_block("ternary.false")
        self._lower_condition_branch(
            expr.cond,
            block,
            cond_env,
            true_target=true_block.name,
            false_target=false_block.name,
            source_coord=self._coord_key(getattr(expr, "coord", None)),
        )
        if cond_env != before_env:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported ternary with side-effecting condition"
            )

        true_env = dict(cond_env)
        true_before = dict(true_env)
        true_exit, true_value = self._lower_value_expr(expr.iftrue, true_block, true_env)
        true_after = dict(true_env)
        if true_after != true_before:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported ternary with side-effecting arm"
            )

        false_env = dict(cond_env)
        false_before = dict(false_env)
        false_exit, false_value = self._lower_value_expr(expr.iffalse, false_block, false_env)
        false_after = dict(false_env)
        if false_after != false_before:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported ternary with side-effecting arm"
            )

        phi_type_name = self._merge_value_type_name(true_value, false_value)
        true_value = self._coerce_value_to_type(
            true_exit,
            true_env,
            true_value,
            phi_type_name,
            self._coord_key(getattr(expr.iftrue, "coord", None)),
        )
        false_value = self._coerce_value_to_type(
            false_exit,
            false_env,
            false_value,
            phi_type_name,
            self._coord_key(getattr(expr.iffalse, "coord", None)),
        )

        join = self._new_block("ternary.end")
        if true_exit.terminator is None:
            true_exit.terminator = SSAJump(target=join.name)
        if false_exit.terminator is None:
            false_exit.terminator = SSAJump(target=join.name)

        phi = SSAPhi(
            name=self._new_temp(),
            type_name=phi_type_name,
            incomings=[
                (true_exit.name, true_value),
                (false_exit.name, false_value),
            ],
            source_coord=self._coord_key(getattr(expr, "coord", None)),
        )
        join.append(phi)
        # Sanity: both incomings must match the phi's declared type by
        # this point. If a coercion was missed upstream, debug logs
        # write the shape so we can trace it — see test_separate_tus
        # lua regression where one arm is long and phi was typed int.
        _debug_phi_types(phi, f"ternary@{getattr(expr,'coord', None)}")
        return join, phi

    def _lower_stmt_or_compound(
        self,
        stmt: c_ast.Node | None,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock | None, dict[str, SSAValue]]:
        if stmt is None:
            return block, dict(env)
        if isinstance(stmt, c_ast.Compound):
            return self._lower_compound(stmt, block, env)
        return self._lower_stmt(stmt, block, env)

    def _merge_envs(
        self,
        block: SSABlock,
        incoming: list[tuple[SSABlock, dict[str, SSAValue]]],
    ) -> dict[str, SSAValue]:
        merged: dict[str, SSAValue] = {}
        names = set()
        for _, env in incoming:
            names.update(env)

        for name in sorted(names):
            # Determine the authoritative type from the first predecessor
            # where `name` is bound. Using the phi's first-incoming type
            # would be wrong when that incoming is an SSAUndef placeholder
            # we inject below (the placeholder defaults to "int", but the
            # real value on other edges may be a pointer — see
            # gcc_torture pr103255.c).
            phi_type_name: str | None = None
            for _pred_block, pred_env in incoming:
                value = pred_env.get(name)
                if value is not None:
                    phi_type_name = getattr(value, "type_name", None)
                    if phi_type_name:
                        break
            if phi_type_name is None:
                phi_type_name = "int"

            values = []
            for pred_block, pred_env in incoming:
                value = pred_env.get(name)
                if value is None:
                    value = SSAUndef(
                        name=f"undef.{name}",
                        type_name=phi_type_name,
                        source_name=name,
                    )
                values.append((pred_block.name, value))

            first_value = values[0][1]
            if all(value == first_value for _, value in values[1:]):
                merged[name] = first_value
                continue

            phi = SSAPhi(
                name=self._new_version(name),
                type_name=phi_type_name,
                variable_name=name,
                incomings=values,
            )
            block.append(phi)
            merged[name] = phi
            _debug_phi_types(phi, f"merge_envs@{self._state.function_name}")
        return merged

    def _lower_expr(
        self,
        expr: c_ast.Node,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> SSAValue:
        if isinstance(expr, c_ast.ID):
            if expr.name in env:
                return env[expr.name]
            if expr.name in self._enum_values:
                return SSAConstant.from_int(
                    self._enum_values[expr.name],
                    type_name="int",
                    is_safe=True,
                )
            if expr.name in self._file_scope_decl_types:
                return SSAGlobalRef(
                    name=expr.name,
                    symbol_name=expr.name,
                    type_name=self._file_scope_value_type_name(
                        self._file_scope_decl_types[expr.name]
                    ),
                )
            if expr.name in self._KNOWN_EXTERN_GLOBAL_TYPES:
                return SSAGlobalRef(
                    name=expr.name,
                    symbol_name=expr.name,
                    type_name=self._KNOWN_EXTERN_GLOBAL_TYPES[expr.name],
                )
            raise SSAConstructionError(
                f"{self._state.function_name}: use of undeclared SSA value {expr.name!r}"
            )

        if isinstance(expr, c_ast.Constant):
            if expr.type == "int":
                return SSAConstant.from_int(
                    self._parse_int_constant(expr.value),
                    type_name=self._int_literal_type_name(expr.value, self.target_triple),
                    is_safe=self._is_safe_int_literal(expr.value),
                )
            if expr.type == "char":
                return SSAConstant.from_int(
                    self._parse_char_constant(expr.value),
                    type_name=self._char_constant_type_name(expr.value),
                    is_safe=True,
                )
            if expr.type in {"string", "wstring"}:
                return SSAStringConstant(
                    name=expr.value,
                    type_name="wchar_t*" if expr.type == "wstring" else "char*",
                    value=expr.value,
                    literal_kind=expr.type,
                )
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported constant kind {expr.type!r}"
            )

        if isinstance(expr, c_ast.BinaryOp):
            if expr.op in {"&&", "||"}:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported short-circuit expression {expr.op!r} in nested value position"
                )
            left = self._lower_expr(expr.left, block, env)
            right = self._lower_expr(expr.right, block, env)
            inst = SSABinaryOp(
                name=self._new_temp(),
                type_name=self._binary_result_type(expr.op, left, right),
                op=expr.op,
                left=left,
                right=right,
                source_coord=self._coord_key(getattr(expr, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(inst)
            return inst

        if isinstance(expr, c_ast.UnaryOp):
            if expr.op in {"p++", "p--", "++", "--"}:
                return self._lower_incdec_expr(expr, block, env)
            if expr.op == "sizeof":
                return SSAConstant.from_int(
                    self._sizeof_expr(expr.expr, env),
                    type_name=c_size_type_name(self.target_triple),
                    is_safe=True,
                )
            if expr.op == "&":
                if isinstance(expr.expr, c_ast.StructRef):
                    return self._lower_struct_ref_addr(expr.expr, block, env)
                # Phase 4 MVP: `&s` on a struct-alloca local is just
                # the alloca value itself (already a `struct S*`).
                # Common pattern: `helper(&s)` where helper takes
                # `struct S *`.
                if (
                    isinstance(expr.expr, c_ast.ID)
                    and expr.expr.name in self._state.struct_alloca_locals
                ):
                    return env[expr.expr.name]
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported unary op {expr.op!r}"
                )
            if expr.op == "*":
                base = self._lower_expr(expr.expr, block, env)
                inst = SSALoad(
                    name=self._new_temp(),
                    type_name=self._deref_type_name(getattr(base, "type_name", "int")),
                    base=base,
                    source_coord=self._coord_key(getattr(expr, "coord", None)),
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(inst)
                return inst
            if expr.op not in self._UNARY_OPS:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported unary op {expr.op!r}"
                )
            operand = self._lower_expr(expr.expr, block, env)
            inst = SSAUnaryOp(
                name=self._new_temp(),
                type_name=self._unary_result_type(expr.op, operand),
                op=expr.op,
                operand=operand,
                source_coord=self._coord_key(getattr(expr, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(inst)
            return inst

        if isinstance(expr, c_ast.ArrayRef):
            # Phase 4 MVP: `mat[i][j]` on a declared 2D local. The outer
            # ArrayRef has `.name` = inner ArrayRef, and the inner base
            # is an ID of a local in `multi_dim_arrays`. Compute flat
            # index `i*inner_dim + j` and load from `alloca + flat_index`.
            flat_load = self._lower_2d_array_ref_load(expr, block, env)
            if flat_load is not None:
                return flat_load
            base = self._lower_expr(expr.name, block, env)
            index = self._lower_expr(expr.subscript, block, env)
            inst = SSALoad(
                name=self._new_temp(),
                type_name=self._deref_type_name(getattr(base, "type_name", "int")),
                base=base,
                index=index,
                source_coord=self._coord_key(getattr(expr, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(inst)
            return inst

        if isinstance(expr, c_ast.StructRef):
            return self._lower_struct_ref_expr(expr, block, env)

        if isinstance(expr, c_ast.Assignment):
            # Assignment used in value position is safe only when the rhs
            # does not create new CFG blocks (short-circuit / ternary).
            # Those would leave `block` behind as the caller continues to
            # emit into the original block while the real control flow
            # drifted — see gcc_torture 20030401-1.c where
            # `!(error = ((x == 0) || bar()))` produced a use of a value
            # defined in a later block than the use.
            if isinstance(expr.rvalue, (c_ast.BinaryOp,)) and getattr(
                expr.rvalue, "op", ""
            ) in {"&&", "||"}:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported short-circuit rvalue in inline assignment"
                )
            if isinstance(expr.rvalue, c_ast.TernaryOp):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported ternary rvalue in inline assignment"
                )
            _, value = self._lower_assignment_expr(expr, block, env)
            return value

        if isinstance(expr, c_ast.Cast):
            # C11 6.5.4p2: a cast type-name must be a scalar type (or void),
            # and the operand must also have scalar type. Reject aggregate
            # forms here so the AST codegen path can raise `invalid cast`
            # via `_validate_explicit_cast`.
            to_inner = getattr(expr.to_type, "type", None)
            inner_kind = getattr(to_inner, "type", None) if to_inner is not None else None
            if isinstance(inner_kind, (c_ast.Struct, c_ast.Union)):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported cast to aggregate type"
                )
            operand = self._lower_expr(expr.expr, block, env)
            operand_type = getattr(operand, "type_name", "")
            if operand_type.startswith("struct ") or operand_type.startswith("union "):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported cast from aggregate type"
                )
            inst = SSACast(
                name=self._new_temp(),
                type_name=self._decl_type_name(expr.to_type.type),
                operand=operand,
                source_coord=self._coord_key(getattr(expr, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(inst)
            return inst

        if isinstance(expr, c_ast.FuncCall):
            callee_name = ""
            callee = None
            if isinstance(expr.name, c_ast.ID):
                callee_name = expr.name.name
                # Known builtins that the AST codegen recognizes via special
                # paths (alloca, va_arg, overflow intrinsics, ...). They must
                # NOT reach here as a plain SSACall or the linker will fail
                # to resolve `alloca`/etc. Fall back to AST codegen.
                if callee_name in _SSA_FALLBACK_BUILTINS:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported builtin call {callee_name!r}"
                    )
                # If the ID refers to a local/parameter function-pointer
                # in env, this is an indirect call, not a reference to a
                # same-named global function. Resolve through env so the
                # codegen emits `call <value>`, not `call @fp` (which the
                # linker would try to resolve as an extern `fp`).
                if callee_name in env:
                    callee = env[callee_name]
                    callee_name = ""
                    result_type_name = self._call_result_type_name(
                        getattr(callee, "type_name", "int")
                    )
                else:
                    result_type_name = self._function_return_types.get(callee_name, "int")
            elif isinstance(expr.name, c_ast.UnaryOp) and expr.name.op == "*":
                callee = self._lower_expr(expr.name.expr, block, env)
                result_type_name = self._call_result_type_name(
                    getattr(callee, "type_name", "int")
                )
            else:
                callee = self._lower_expr(expr.name, block, env)
                result_type_name = self._call_result_type_name(
                    getattr(callee, "type_name", "int")
                )
            args = getattr(expr.args, "exprs", None) or ()
            # Pre-check: passing aggregate values to calls needs
            # platform-specific ABI lowering (HFA split, byval, etc.)
            # that only AST codegen implements correctly. Reject so
            # the enclosing function falls back — see c-testsuite
            # 00204.c (stdarg + HFA callers).
            for arg in args:
                # Phase 4 MVP: reject passing a struct-alloca local as
                # a whole-value argument. The env binding is a pointer
                # (alloca), but C semantics require a value copy.
                if (
                    isinstance(arg, c_ast.ID)
                    and arg.name in self._state.struct_alloca_locals
                ):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported struct-value argument {arg.name!r}"
                    )
                arg_tn = self._expr_type_name_safe(arg, env)
                if (
                    arg_tn is not None
                    and (arg_tn.startswith("struct ") or arg_tn.startswith("union "))
                    and not arg_tn.endswith("*")
                ):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported aggregate call argument"
                    )
            inst = SSACall(
                name=self._new_temp(),
                type_name=result_type_name,
                callee_name=callee_name,
                callee=callee,
                args=tuple(self._lower_expr(arg, block, env) for arg in args),
                source_coord=self._coord_key(getattr(expr, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(inst)
            return inst

        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported expression {type(expr).__name__}"
        )

    def _decl_type_name(self, node: c_ast.Node) -> str:
        if isinstance(node, c_ast.TypeDecl):
            return self._decl_type_name(node.type)
        if isinstance(node, c_ast.IdentifierType):
            return " ".join(node.names)
        if isinstance(node, c_ast.PtrDecl):
            return f"{self._decl_type_name(node.type)}*"
        if isinstance(node, c_ast.Struct):
            if node.name:
                return f"struct {node.name}"
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported anonymous struct type"
            )
        if isinstance(node, c_ast.Union):
            if node.name:
                return f"union {node.name}"
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported anonymous union type"
            )
        if isinstance(node, c_ast.Enum):
            if node.name:
                return f"enum {node.name}"
            return "int"
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported declaration type {type(node).__name__}"
        )

    def _function_return_type_name(self, node: c_ast.Node) -> str:
        if isinstance(node, c_ast.Decl):
            return self._function_return_type_name(node.type)
        if isinstance(node, c_ast.FuncDecl):
            return self._decl_type_name(node.type)
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported function declaration type {type(node).__name__}"
        )

    def _collect_function_return_types(self, ast: c_ast.FileAST) -> dict[str, str]:
        function_return_types: dict[str, str] = {}
        for ext in ast.ext or []:
            decl = None
            if isinstance(ext, c_ast.FuncDef):
                decl = ext.decl
            elif isinstance(ext, c_ast.Decl):
                decl = ext
            if decl is None or not getattr(decl, "name", None):
                continue
            if not isinstance(getattr(decl, "type", None), c_ast.FuncDecl):
                continue
            function_return_types[decl.name] = self._function_return_type_name(decl)
        return function_return_types

    def _collect_type_info(
        self,
        ast: c_ast.FileAST,
    ) -> tuple[
        dict[str, c_ast.Node],
        dict[str, c_ast.Struct],
        dict[str, c_ast.Union],
        dict[str, c_ast.Enum],
    ]:
        typedef_types: dict[str, c_ast.Node] = {}
        struct_types: dict[str, c_ast.Struct] = {}
        union_types: dict[str, c_ast.Union] = {}
        enum_types: dict[str, c_ast.Enum] = {}

        class _TypeCollector(c_ast.NodeVisitor):
            def visit_Typedef(self, node):
                typedef_types[node.name] = node.type
                self.generic_visit(node)

            def visit_Struct(self, node):
                if node.name and node.decls is not None:
                    struct_types[node.name] = node
                self.generic_visit(node)

            def visit_Union(self, node):
                if node.name and node.decls is not None:
                    union_types[node.name] = node
                self.generic_visit(node)

            def visit_Enum(self, node):
                if node.name and node.values is not None:
                    enum_types[node.name] = node
                self.generic_visit(node)

        collector = _TypeCollector()
        collector.visit(ast)
        return typedef_types, struct_types, union_types, enum_types

    def _collect_file_scope_decl_types(self, ast: c_ast.FileAST) -> dict[str, c_ast.Node]:
        decl_types: dict[str, c_ast.Node] = {}
        for ext in ast.ext or []:
            if isinstance(ext, c_ast.Decl) and getattr(ext, "name", None):
                decl_types[ext.name] = ext.type
            elif isinstance(ext, c_ast.FuncDef):
                decl = getattr(ext, "decl", None)
                if decl is not None and getattr(decl, "name", None):
                    decl_types[decl.name] = decl.type
        return decl_types

    def _collect_enum_values(self, ast: c_ast.FileAST) -> dict[str, int]:
        enum_values: dict[str, int] = {}

        class _EnumCollector(c_ast.NodeVisitor):
            def __init__(self, outer: "SSABuilder"):
                self.outer = outer

            def visit_Enum(self, node):
                current = -1
                for enumerator in getattr(getattr(node, "values", None), "enumerators", []) or []:
                    value_node = getattr(enumerator, "value", None)
                    if value_node is None:
                        current += 1
                    else:
                        current = self.outer._eval_enum_constant(value_node, enum_values)
                    enum_values[enumerator.name] = current
                self.generic_visit(node)

        _EnumCollector(self).visit(ast)
        return enum_values

    def _file_scope_value_type_name(self, node: c_ast.Node) -> str:
        resolved = self._resolve_type_node(node)
        if isinstance(resolved, c_ast.ArrayDecl):
            return f"{self._decl_type_name(resolved.type)}*"
        if isinstance(resolved, c_ast.FuncDecl):
            return "void*"
        return self._decl_type_name(resolved)

    def _call_result_type_name(self, callee_type_name: str) -> str:
        resolved = self._resolve_type_name(callee_type_name)
        if isinstance(resolved, c_ast.PtrDecl):
            resolved = self._resolve_type_node(resolved.type)
        if isinstance(resolved, c_ast.FuncDecl):
            return self._function_return_type_name(resolved)
        return "int"

    def _coerce_value_to_type(
        self,
        block: SSABlock,
        env: dict[str, SSAValue],
        value: SSAValue,
        target_type_name: str,
        coord: str | None,
    ) -> SSAValue:
        if not target_type_name or getattr(value, "type_name", "") == target_type_name:
            return value
        cast = SSACast(
            name=self._new_temp(),
            type_name=target_type_name,
            operand=value,
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(cast)
        return cast

    def _merge_value_type_name(self, left: SSAValue, right: SSAValue) -> str:
        left_type = getattr(left, "type_name", "int")
        right_type = getattr(right, "type_name", "int")
        if left_type == right_type:
            return left_type
        if left_type.endswith("*"):
            return left_type
        if right_type.endswith("*"):
            return right_type
        try:
            left_size = self._type_name_size(left_type)
            right_size = self._type_name_size(right_type)
        except SSAConstructionError:
            return left_type
        return self._binary_result_type("+", left, right)

    def _type_name_is_unsigned(self, type_name: str) -> bool:
        if not type_name or type_name.endswith("*"):
            return False
        resolved = self._resolve_type_name(type_name)
        if isinstance(resolved, c_ast.TypeDecl):
            inner = resolved.type
            if isinstance(inner, c_ast.IdentifierType):
                return builtin_integer_is_unsigned(inner.names, self.target_triple)
        if isinstance(resolved, c_ast.IdentifierType):
            return builtin_integer_is_unsigned(resolved.names, self.target_triple)
        return builtin_integer_is_unsigned(type_name.split(), self.target_triple)

    def _is_void_parameter_list(self, raw_params) -> bool:
        if not raw_params or len(raw_params) != 1:
            return False
        param = raw_params[0]
        if not isinstance(param, c_ast.Typename):
            return False
        return self._decl_type_name(param.type) == "void"

    def _lower_incdec_expr(
        self,
        expr: c_ast.UnaryOp,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> SSAValue:
        coord = self._coord_key(getattr(expr, "coord", None))
        op = "+" if expr.op in {"p++", "++"} else "-"
        step = SSAConstant.from_int(1, is_safe=True)

        if isinstance(expr.expr, c_ast.ID):
            name = expr.expr.name
            if name not in env:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported non-local inc/dec {name!r}"
                )
            prior = env[name]
            result = SSABinaryOp(
                name=self._new_temp(),
                type_name=getattr(prior, "type_name", "int"),
                op=op,
                left=prior,
                right=step,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(result)
            env[name] = result
            self._record_binding(
                kind="assign",
                block=block,
                target_name=name,
                value=result,
                type_name=getattr(prior, "type_name", ""),
                source_coord=coord,
            )
            return prior if expr.op.startswith("p") else result

        if isinstance(expr.expr, c_ast.StructRef):
            field_decl, addr = self._lower_struct_ref_addr_with_decl(expr.expr, block, env)
            prior = SSALoad(
                name=self._new_temp(),
                type_name=self._decl_type_name(field_decl),
                base=addr,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(prior)
            result = SSABinaryOp(
                name=self._new_temp(),
                type_name=getattr(prior, "type_name", "int"),
                op=op,
                left=prior,
                right=step,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(result)
            block.append(
                SSAStore(
                    name=self._new_temp(),
                    type_name=getattr(addr, "type_name", "void"),
                    addr=addr,
                    value=result,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
            )
            return prior if expr.op.startswith("p") else result

        if isinstance(expr.expr, c_ast.UnaryOp) and expr.expr.op == "*":
            addr = self._lower_expr(expr.expr.expr, block, env)
            pointee_type = self._deref_type_name(getattr(addr, "type_name", "int"))
            prior = SSALoad(
                name=self._new_temp(),
                type_name=pointee_type,
                base=addr,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(prior)
            result = SSABinaryOp(
                name=self._new_temp(),
                type_name=pointee_type,
                op=op,
                left=prior,
                right=step,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(result)
            block.append(
                SSAStore(
                    name=self._new_temp(),
                    type_name=getattr(addr, "type_name", "void"),
                    addr=addr,
                    value=result,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
            )
            return prior if expr.op.startswith("p") else result

        if isinstance(expr.expr, c_ast.ArrayRef):
            base = self._lower_expr(expr.expr.name, block, env)
            index = self._lower_expr(expr.expr.subscript, block, env)
            elem_type = self._deref_type_name(getattr(base, "type_name", "int"))
            prior = SSALoad(
                name=self._new_temp(),
                type_name=elem_type,
                base=base,
                index=index,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(prior)
            result = SSABinaryOp(
                name=self._new_temp(),
                type_name=elem_type,
                op=op,
                left=prior,
                right=step,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(result)
            addr = SSABinaryOp(
                name=self._new_temp(),
                type_name=getattr(base, "type_name", "int"),
                op="+",
                left=base,
                right=index,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(addr)
            block.append(
                SSAStore(
                    name=self._new_temp(),
                    type_name=getattr(addr, "type_name", "void"),
                    addr=addr,
                    value=result,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
            )
            return prior if expr.op.startswith("p") else result

        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported inc/dec target"
        )

    def _lower_2d_array_ref_load(
        self,
        expr: c_ast.ArrayRef,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> SSAValue | None:
        """If `expr` is `mat[i][j]` on a declared 2D local, emit a load
        at `alloca + i*inner_dim + j` and return the SSALoad. Else None."""
        addr_info = self._compute_2d_array_addr(expr, block, env)
        if addr_info is None:
            return None
        addr, elem_type_name = addr_info
        load = SSALoad(
            name=self._new_temp(),
            type_name=elem_type_name,
            base=addr,
            source_coord=self._coord_key(getattr(expr, "coord", None)),
            available_bindings=self._binding_snapshot(env),
        )
        block.append(load)
        return load

    def _compute_2d_array_addr(
        self,
        expr: c_ast.ArrayRef,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSAValue, str] | None:
        """Compute flat address for `mat[i][j]` on a declared 2D local.

        Returns `(addr, elem_type_name)` or None if the shape doesn't
        match. The addr is `alloca + i * inner_dim + j`.
        """
        inner_ref = expr.name
        if not isinstance(inner_ref, c_ast.ArrayRef):
            return None
        if not isinstance(inner_ref.name, c_ast.ID):
            return None
        name = inner_ref.name.name
        inner_dim = self._state.multi_dim_arrays.get(name)
        if inner_dim is None:
            return None
        alloca = env.get(name)
        if alloca is None:
            return None
        # `alloca.type_name` should end with `*`; element type is the
        # prefix (e.g. `int*` → `int`).
        alloca_type = getattr(alloca, "type_name", "")
        if not alloca_type.endswith("*"):
            return None
        elem_type_name = alloca_type[:-1].strip()

        coord = self._coord_key(getattr(expr, "coord", None))
        outer_idx = self._lower_expr(inner_ref.subscript, block, env)
        inner_idx = self._lower_expr(expr.subscript, block, env)
        dim_const = SSAConstant.from_int(
            inner_dim, type_name="int", is_safe=True,
        )
        row_offset = SSABinaryOp(
            name=self._new_temp(),
            type_name=getattr(outer_idx, "type_name", "int"),
            op="*",
            left=outer_idx,
            right=dim_const,
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(row_offset)
        flat_offset = SSABinaryOp(
            name=self._new_temp(),
            type_name=getattr(inner_idx, "type_name", "int"),
            op="+",
            left=row_offset,
            right=inner_idx,
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(flat_offset)
        addr = SSABinaryOp(
            name=self._new_temp(),
            type_name=alloca_type,
            op="+",
            left=alloca,
            right=flat_offset,
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(addr)
        return addr, elem_type_name

    def _lower_struct_copy_assignment(
        self,
        expr: c_ast.Assignment,
        block: SSABlock,
        env: dict[str, SSAValue],
        coord: str | None,
    ) -> tuple[SSABlock, SSAValue]:
        """Phase 4 MVP: `s2 = s1` where both are local struct-alloca locals.

        Supported: both sides are struct-alloca locals of the same struct type.
        Each field is copied:
          - scalar: single load+store,
          - array field: unrolled indexed load+store for each element,
          - nested struct: recursive field-by-field copy through `->` chain.
        Rejected: mismatched types, unions.
        """
        dst = env[expr.lvalue.name]
        src = env[expr.rvalue.name]
        dst_type = getattr(dst, "type_name", "")
        src_type = getattr(src, "type_name", "")
        if dst_type != src_type or not dst_type.endswith("*"):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported struct copy across mismatched types"
            )
        elem_type_name = dst_type[:-1].strip()
        self._copy_struct_fields(dst, src, elem_type_name, block, env, coord)
        return block, dst

    def _copy_struct_fields(
        self,
        dst: SSAValue,
        src: SSAValue,
        struct_type_name: str,
        block: SSABlock,
        env: dict[str, SSAValue],
        coord: str | None,
    ) -> None:
        """Emit load/store chain copying every field of a struct from src to dst."""
        resolved = self._resolve_type_name(struct_type_name)
        if isinstance(resolved, c_ast.TypeDecl):
            resolved = resolved.type
        if isinstance(resolved, c_ast.Struct) and resolved.decls is None and resolved.name:
            resolved = self._struct_types.get(resolved.name, resolved)
        if not isinstance(resolved, c_ast.Struct):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported struct copy target shape"
            )
        field_decls = list(resolved.decls or ())
        if any(getattr(f, "bitsize", None) is not None for f in field_decls):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported bitfield in struct copy"
            )

        for field_decl in field_decls:
            resolved_field = self._resolve_type_node(field_decl.type)
            if isinstance(resolved_field, c_ast.Union):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported union field in struct copy"
                )
            if isinstance(resolved_field, c_ast.ArrayDecl):
                self._copy_struct_array_field(
                    dst, src, field_decl, resolved_field, block, env, coord,
                )
                continue
            if isinstance(resolved_field, c_ast.Struct) or (
                isinstance(resolved_field, c_ast.TypeDecl)
                and isinstance(resolved_field.type, c_ast.Struct)
            ):
                inner = resolved_field
                if isinstance(inner, c_ast.TypeDecl):
                    inner = inner.type
                if inner.decls is None and inner.name:
                    inner = self._struct_types.get(inner.name, inner)
                # Compute inner struct addresses on both sides.
                inner_type_name = f"struct {inner.name}" if inner.name else "struct"
                addr_type_name = f"{inner_type_name}*"
                src_field_addr = SSAFieldAddr(
                    name=self._new_temp(),
                    type_name=addr_type_name,
                    base=src,
                    field_name=field_decl.name,
                    access_kind=".",
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(src_field_addr)
                dst_field_addr = SSAFieldAddr(
                    name=self._new_temp(),
                    type_name=addr_type_name,
                    base=dst,
                    field_name=field_decl.name,
                    access_kind=".",
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(dst_field_addr)
                self._copy_struct_fields(
                    dst_field_addr, src_field_addr, inner_type_name,
                    block, env, coord,
                )
                continue

            # Scalar field: single load + store.
            field_type_name = self._decl_type_name(field_decl.type)
            addr_type_name = self._field_address_type_name(
                field_decl.type, field_type_name
            )
            src_field_addr = SSAFieldAddr(
                name=self._new_temp(),
                type_name=addr_type_name,
                base=src,
                field_name=field_decl.name,
                access_kind=".",
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(src_field_addr)
            load = SSALoad(
                name=self._new_temp(),
                type_name=field_type_name,
                base=src_field_addr,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(load)
            dst_field_addr = SSAFieldAddr(
                name=self._new_temp(),
                type_name=addr_type_name,
                base=dst,
                field_name=field_decl.name,
                access_kind=".",
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(dst_field_addr)
            store = SSAStore(
                name=self._new_temp(),
                type_name="",
                addr=dst_field_addr,
                value=load,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)

    def _copy_struct_array_field(
        self,
        dst: SSAValue,
        src: SSAValue,
        field_decl: c_ast.Node,
        resolved_array: c_ast.ArrayDecl,
        block: SSABlock,
        env: dict[str, SSAValue],
        coord: str | None,
    ) -> None:
        """Copy an array field between two structs via unrolled index stores."""
        elem_resolved = self._resolve_type_node(resolved_array.type)
        if isinstance(elem_resolved, (c_ast.ArrayDecl, c_ast.Struct, c_ast.Union)):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported aggregate array element in struct copy"
            )
        if getattr(resolved_array, "dim", None) is None:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported unsized array field in struct copy"
            )
        count = self._array_type_count(resolved_array)
        elem_type_name = self._decl_type_name(resolved_array.type)
        addr_type_name = f"{elem_type_name}*"

        src_field_addr = SSAFieldAddr(
            name=self._new_temp(),
            type_name=addr_type_name,
            base=src,
            field_name=field_decl.name,
            access_kind=".",
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(src_field_addr)
        dst_field_addr = SSAFieldAddr(
            name=self._new_temp(),
            type_name=addr_type_name,
            base=dst,
            field_name=field_decl.name,
            access_kind=".",
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(dst_field_addr)

        for i in range(count):
            index_const = SSAConstant.from_int(i, type_name="int", is_safe=True)
            src_elem = SSABinaryOp(
                name=self._new_temp(),
                type_name=addr_type_name,
                op="+",
                left=src_field_addr,
                right=index_const,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(src_elem)
            load = SSALoad(
                name=self._new_temp(),
                type_name=elem_type_name,
                base=src_elem,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(load)
            dst_elem = SSABinaryOp(
                name=self._new_temp(),
                type_name=addr_type_name,
                op="+",
                left=dst_field_addr,
                right=index_const,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(dst_elem)
            store = SSAStore(
                name=self._new_temp(),
                type_name="",
                addr=dst_elem,
                value=load,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)

    def _lower_assignment_expr(
        self,
        expr: c_ast.Assignment,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[SSABlock, SSAValue]:
        coord = self._coord_key(getattr(expr, "coord", None))
        # Phase 4 MVP: struct-to-struct value assignment (`s2 = s1`).
        # Both sides are struct-alloca locals; emit a field-by-field
        # load/store copy for scalar-only structs. For more complex
        # cases (aggregate fields, mismatched types), fall back to AST.
        if expr.op == "=" and (
            isinstance(expr.lvalue, c_ast.ID)
            and expr.lvalue.name in self._state.struct_alloca_locals
            and isinstance(expr.rvalue, c_ast.ID)
            and expr.rvalue.name in self._state.struct_alloca_locals
        ):
            return self._lower_struct_copy_assignment(
                expr, block, env, coord,
            )
        if (
            isinstance(expr.lvalue, c_ast.ID)
            and expr.lvalue.name in self._state.struct_alloca_locals
        ) or (
            isinstance(expr.rvalue, c_ast.ID)
            and expr.rvalue.name in self._state.struct_alloca_locals
        ):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported struct-value assignment"
            )
        if isinstance(expr.lvalue, c_ast.ID):
            if expr.lvalue.name not in env:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported non-local assignment {expr.lvalue.name!r}"
                )
            block, rhs_value = self._lower_value_expr(expr.rvalue, block, env)
            if expr.op != "=":
                base_op = expr.op[:-1]
                lhs_value = env[expr.lvalue.name]
                # Compound assignments (`a op= b`) are defined as
                # `a = (type(a))((promoted a) op (promoted b))` in C11
                # 6.5.16.2p3 — the op itself runs in the usual-arithmetic
                # common type, then the result is converted back to the
                # lvalue type. Using the lvalue type directly makes
                # `u8 |= 3` become `or i8` and loses the high bits of a
                # subsequent `u8 + u16`-style promotion (pr69447.c).
                value = SSABinaryOp(
                    name=self._new_temp(),
                    type_name=self._binary_result_type(base_op, lhs_value, rhs_value),
                    op=base_op,
                    left=lhs_value,
                    right=rhs_value,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(value)
            else:
                value = rhs_value
            prior = env.get(expr.lvalue.name)
            value = self._coerce_value_to_type(
                block,
                env,
                value,
                getattr(prior, "type_name", getattr(value, "type_name", "int")),
                coord,
            )
            env[expr.lvalue.name] = value
            self._record_binding(
                kind="assign",
                block=block,
                target_name=expr.lvalue.name,
                value=value,
                type_name=getattr(prior, "type_name", ""),
                source_coord=coord,
            )
            return block, value

        if isinstance(expr.lvalue, c_ast.StructRef):
            # Phase 4 MVP: reject aggregate field assignment like
            # `x.d = x.c` where both sides are struct-typed fields — it
            # needs memcpy semantics that the ID=ID struct-copy path
            # doesn't cover (gcc_torture 20001024-1.c). Fall back to AST
            # codegen which handles the memcpy correctly.
            lhs_field_decl = self._struct_field_decl_type(
                self._struct_ref_base_type_name(expr.lvalue, env),
                expr.lvalue.field.name,
                access_kind=expr.lvalue.type,
            )
            if self._field_expr_returns_address(lhs_field_decl):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported aggregate field assignment"
                )
            block, rhs_value = self._lower_value_expr(expr.rvalue, block, env)
            field_decl, addr = self._lower_struct_ref_addr_with_decl(expr.lvalue, block, env)
            if expr.op != "=":
                current = SSALoad(
                    name=self._new_temp(),
                    type_name=self._decl_type_name(field_decl),
                    base=addr,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(current)
                value = SSABinaryOp(
                    name=self._new_temp(),
                    type_name=getattr(current, "type_name", "int"),
                    op=expr.op[:-1],
                    left=current,
                    right=rhs_value,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(value)
            else:
                value = rhs_value
            store = SSAStore(
                name=self._new_temp(),
                type_name=getattr(addr, "type_name", "void"),
                addr=addr,
                value=value,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)
            return block, value

        if isinstance(expr.lvalue, c_ast.UnaryOp) and expr.lvalue.op == "*":
            block, rhs_value = self._lower_value_expr(expr.rvalue, block, env)
            addr = self._lower_expr(expr.lvalue.expr, block, env)
            pointee_type = self._deref_type_name(getattr(addr, "type_name", "int"))
            if expr.op != "=":
                current = SSALoad(
                    name=self._new_temp(),
                    type_name=pointee_type,
                    base=addr,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(current)
                value = SSABinaryOp(
                    name=self._new_temp(),
                    type_name=pointee_type,
                    op=expr.op[:-1],
                    left=current,
                    right=rhs_value,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(value)
            else:
                value = rhs_value
            store = SSAStore(
                name=self._new_temp(),
                type_name=getattr(addr, "type_name", "void"),
                addr=addr,
                value=value,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)
            return block, value

        if isinstance(expr.lvalue, c_ast.ArrayRef):
            # Phase 4 MVP: `mat[i][j] = rhs` on a declared 2D local. Use
            # the same flat-address helper as the load path.
            two_d_addr = self._compute_2d_array_addr(expr.lvalue, block, env)
            if two_d_addr is not None:
                block, rhs_value = self._lower_value_expr(expr.rvalue, block, env)
                addr, elem_type = two_d_addr
                if expr.op != "=":
                    current = SSALoad(
                        name=self._new_temp(),
                        type_name=elem_type,
                        base=addr,
                        source_coord=coord,
                        available_bindings=self._binding_snapshot(env),
                    )
                    block.append(current)
                    value = SSABinaryOp(
                        name=self._new_temp(),
                        type_name=elem_type,
                        op=expr.op[:-1],
                        left=current,
                        right=rhs_value,
                        source_coord=coord,
                        available_bindings=self._binding_snapshot(env),
                    )
                    block.append(value)
                else:
                    value = rhs_value
                store = SSAStore(
                    name=self._new_temp(),
                    type_name=getattr(addr, "type_name", "void"),
                    addr=addr,
                    value=value,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(store)
                return block, value

            block, rhs_value = self._lower_value_expr(expr.rvalue, block, env)
            base = self._lower_expr(expr.lvalue.name, block, env)
            index = self._lower_expr(expr.lvalue.subscript, block, env)
            elem_type = self._deref_type_name(getattr(base, "type_name", "int"))
            if expr.op != "=":
                current = SSALoad(
                    name=self._new_temp(),
                    type_name=elem_type,
                    base=base,
                    index=index,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(current)
                value = SSABinaryOp(
                    name=self._new_temp(),
                    type_name=elem_type,
                    op=expr.op[:-1],
                    left=current,
                    right=rhs_value,
                    source_coord=coord,
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(value)
            else:
                value = rhs_value
            addr = SSABinaryOp(
                name=self._new_temp(),
                type_name=getattr(base, "type_name", "int"),
                op="+",
                left=base,
                right=index,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(addr)
            store = SSAStore(
                name=self._new_temp(),
                type_name=getattr(addr, "type_name", "void"),
                addr=addr,
                value=value,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)
            return block, value

        raise SSAConstructionError(f"{self._state.function_name}: unsupported assignment form")

    def _deref_type_name(self, type_name: str) -> str:
        if not type_name:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported non-pointer load source {type_name!r}"
            )
        resolved = self._resolve_type_name(type_name)
        if isinstance(resolved, c_ast.ArrayDecl):
            return self._decl_type_name(resolved.type)
        if isinstance(resolved, c_ast.PtrDecl):
            return self._decl_type_name(resolved.type)
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported non-pointer load source {type_name!r}"
        )

    def _lower_struct_ref_expr(
        self,
        expr: c_ast.StructRef,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> SSAValue:
        base = self._lower_expr(expr.name, block, env)
        if expr.type == "." and not getattr(base, "type_name", "").endswith("*"):
            field_decl = self._struct_field_decl_type(
                getattr(base, "type_name", "int"),
                expr.field.name,
                access_kind=expr.type,
            )
            if self._field_expr_returns_address(field_decl):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported direct aggregate value field access"
                )
            extract = SSAFieldExtract(
                name=self._new_temp(),
                type_name=self._decl_type_name(field_decl),
                base=base,
                field_name=expr.field.name,
                source_coord=self._coord_key(getattr(expr, "coord", None)),
                available_bindings=self._binding_snapshot(env),
            )
            block.append(extract)
            return extract
        field_decl, field_addr = self._lower_struct_ref_addr_with_decl_from_base(
            expr, base, block, env,
        )
        if self._field_expr_returns_address(field_decl):
            return field_addr
        field_type_name = self._decl_type_name(field_decl)
        load = SSALoad(
            name=self._new_temp(),
            type_name=field_type_name,
            base=field_addr,
            source_coord=self._coord_key(getattr(expr, "coord", None)),
            available_bindings=self._binding_snapshot(env),
        )
        block.append(load)
        return load

    def _lower_struct_ref_addr(
        self,
        expr: c_ast.StructRef,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> SSAValue:
        base = self._lower_expr(expr.name, block, env)
        _, field_addr = self._lower_struct_ref_addr_with_decl_from_base(
            expr, base, block, env,
        )
        return field_addr

    def _lower_struct_ref_addr_with_decl(
        self,
        expr: c_ast.StructRef,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[c_ast.Node, SSAFieldAddr]:
        # Phase 4 MVP: when the base is `arr[i]` and `arr` is an array
        # of struct (alloca'd), compute the element address instead of
        # loading the whole struct value. Subsequent `.field` works via
        # the `.`-on-pointer path. See `struct entry table[3]; table[i].key`.
        if (
            isinstance(expr.name, c_ast.ArrayRef)
            and expr.type == "."
        ):
            array_base = self._lower_expr(expr.name.name, block, env)
            elem_type_name = self._deref_type_name(
                getattr(array_base, "type_name", "int")
            )
            if elem_type_name.startswith("struct ") or elem_type_name.startswith("union "):
                index = self._lower_expr(expr.name.subscript, block, env)
                elem_addr = SSABinaryOp(
                    name=self._new_temp(),
                    type_name=getattr(array_base, "type_name", "int"),
                    op="+",
                    left=array_base,
                    right=index,
                    source_coord=self._coord_key(getattr(expr.name, "coord", None)),
                    available_bindings=self._binding_snapshot(env),
                )
                block.append(elem_addr)
                # The element-address points to the struct; re-dispatch
                # the field lookup with access_kind="->" since the base
                # is now a pointer, matching `(&arr[i])->field`.
                synthetic = c_ast.StructRef(
                    name=expr.name,
                    type="->",
                    field=expr.field,
                    coord=getattr(expr, "coord", None),
                )
                return self._lower_struct_ref_addr_with_decl_from_base(
                    synthetic, elem_addr, block, env,
                )
        base = self._lower_expr(expr.name, block, env)
        return self._lower_struct_ref_addr_with_decl_from_base(expr, base, block, env)

    def _lower_struct_ref_addr_with_decl_from_base(
        self,
        expr: c_ast.StructRef,
        base: SSAValue,
        block: SSABlock,
        env: dict[str, SSAValue],
    ) -> tuple[c_ast.Node, SSAFieldAddr]:
        if expr.type == "." and not getattr(base, "type_name", "").endswith("*"):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported direct aggregate value field access"
            )
        field_decl = self._struct_field_decl_type(
            getattr(base, "type_name", "int"),
            expr.field.name,
            access_kind=expr.type,
        )
        resolved_field = self._resolve_type_node(field_decl)
        if isinstance(resolved_field, c_ast.ArrayDecl):
            field_type_name = self._decl_type_name(resolved_field.type)
        else:
            field_type_name = self._decl_type_name(field_decl)
        addr_type_name = self._field_address_type_name(field_decl, field_type_name)
        field_addr = SSAFieldAddr(
            name=self._new_temp(),
            type_name=addr_type_name,
            base=base,
            field_name=expr.field.name,
            access_kind=expr.type,
            source_coord=self._coord_key(getattr(expr, "coord", None)),
            available_bindings=self._binding_snapshot(env),
        )
        block.append(field_addr)
        return field_decl, field_addr

    def _struct_field_decl_type(
        self,
        base_type_name: str,
        field_name: str,
        *,
        access_kind: str,
    ) -> c_ast.Node:
        aggregate = self._aggregate_type_node(base_type_name, access_kind=access_kind)
        if isinstance(aggregate, c_ast.TypeDecl):
            aggregate = aggregate.type
        if isinstance(aggregate, c_ast.ArrayDecl):
            aggregate = self._resolve_type_node(aggregate.type)
            if isinstance(aggregate, c_ast.TypeDecl):
                aggregate = aggregate.type
        if not isinstance(aggregate, (c_ast.Struct, c_ast.Union)):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported non-aggregate field access {base_type_name!r}"
            )
        # Bitfield members need packed/shift-mask lowering that AST codegen
        # already handles via BitFieldRef. The SSA path extracts raw aggregate
        # slots and would emit a wrong-sized value for a 1-bit or sub-byte
        # field — see gcc_torture bitfld-10.c / 20040709-1.c. Reject so the
        # enclosing function falls back to AST codegen.
        for decl in aggregate.decls or ():
            if getattr(decl, "bitsize", None) is not None:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported bitfield struct access"
                )
        for decl in aggregate.decls or ():
            if decl.name == field_name:
                return decl.type
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported missing field {field_name!r}"
        )

    def _aggregate_type_node(self, type_name: str, *, access_kind: str) -> c_ast.Node:
        resolved = self._resolve_type_name(type_name)
        if access_kind == "->":
            if isinstance(resolved, c_ast.ArrayDecl):
                resolved = self._resolve_type_node(resolved.type)
            if not isinstance(resolved, c_ast.PtrDecl):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported non-pointer base for '->'"
                )
            resolved = self._resolve_type_node(resolved.type)
        elif access_kind == "." and isinstance(resolved, c_ast.PtrDecl):
            # Aggregate-valued fields are represented as field addresses inside
            # the restricted SSA subset, so chained ".field" reads consume the
            # pointed-to aggregate directly.
            resolved = self._resolve_type_node(resolved.type)
        return resolved

    def _field_expr_returns_address(self, field_decl: c_ast.Node) -> bool:
        resolved = self._resolve_type_node(field_decl)
        if isinstance(resolved, c_ast.ArrayDecl):
            return True
        if isinstance(resolved, c_ast.TypeDecl):
            return isinstance(resolved.type, (c_ast.Struct, c_ast.Union))
        return isinstance(resolved, (c_ast.Struct, c_ast.Union))

    def _field_address_type_name(self, field_decl: c_ast.Node, field_type_name: str) -> str:
        resolved = self._resolve_type_node(field_decl)
        if isinstance(resolved, c_ast.ArrayDecl):
            return f"{self._decl_type_name(resolved.type)}*"
        return f"{field_type_name}*"

    def _resolve_type_name(self, type_name: str) -> c_ast.Node:
        stars = 0
        base = type_name.strip()
        while base.endswith("*"):
            stars += 1
            base = base[:-1].strip()
        node = self._base_type_node_from_name(base)
        for _ in range(stars):
            node = c_ast.PtrDecl([], node)
        return self._resolve_type_node(node)

    def _base_type_node_from_name(self, type_name: str) -> c_ast.Node:
        if type_name in self._typedef_types:
            return self._typedef_types[type_name]
        if type_name.startswith("struct "):
            name = type_name.split(" ", 1)[1]
            return c_ast.TypeDecl(None, [], self._struct_types.get(name, c_ast.Struct(name, None)), None)
        if type_name.startswith("union "):
            name = type_name.split(" ", 1)[1]
            return c_ast.TypeDecl(None, [], self._union_types.get(name, c_ast.Union(name, None)), None)
        if type_name.startswith("enum "):
            name = type_name.split(" ", 1)[1]
            return c_ast.TypeDecl(None, [], self._enum_types.get(name, c_ast.Enum(name, None)), None)
        return c_ast.TypeDecl(None, [], c_ast.IdentifierType(type_name.split()), None)

    def _is_aggregate_value_type(self, resolved_decl: c_ast.Node) -> bool:
        """True if `resolved_decl` names a struct/union value (not via pointer).

        Used to decide whether a local declaration should be modeled as
        a stack alloca (so `s.field` works) vs an SSA scalar value.
        """
        node = resolved_decl
        if isinstance(node, c_ast.TypeDecl):
            node = node.type
        if isinstance(node, c_ast.IdentifierType) and len(node.names) == 1:
            typedef = self._typedef_types.get(node.names[0])
            if typedef is not None:
                return self._is_aggregate_value_type(self._resolve_type_node(typedef))
        return isinstance(node, (c_ast.Struct, c_ast.Union))

    def _lower_struct_init_list(
        self,
        alloc: SSAValue,
        resolved_decl: c_ast.Node,
        init_list: c_ast.InitList,
        block: SSABlock,
        env: dict[str, SSAValue],
        decl: c_ast.Decl,
    ) -> None:
        """Emit element-wise stores for a positional scalar-only InitList.

        Supported: `struct S s = {1, 2};` where each field is a scalar.
        Rejected: designated initializers, nested InitList, array fields,
        struct fields, unions, fewer/more exprs than fields.
        """
        # Find the aggregate's declared fields.
        agg_node = resolved_decl
        if isinstance(agg_node, c_ast.TypeDecl):
            agg_node = agg_node.type
        # Follow typedef to the real Struct/Union decls.
        if isinstance(agg_node, c_ast.IdentifierType) and len(agg_node.names) == 1:
            typedef = self._typedef_types.get(agg_node.names[0])
            if typedef is not None:
                agg_node = self._resolve_type_node(typedef)
                if isinstance(agg_node, c_ast.TypeDecl):
                    agg_node = agg_node.type
        if isinstance(agg_node, c_ast.Struct) and agg_node.decls is None and agg_node.name:
            agg_node = self._struct_types.get(agg_node.name, agg_node)
        if isinstance(agg_node, c_ast.Union) and agg_node.decls is None and agg_node.name:
            agg_node = self._union_types.get(agg_node.name, agg_node)

        if not isinstance(agg_node, (c_ast.Struct, c_ast.Union)):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported struct init target shape"
            )
        if isinstance(agg_node, c_ast.Union):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported union initializer"
            )

        field_decls = list(agg_node.decls or ())
        init_exprs = list(init_list.exprs or ())

        if any(getattr(f, "bitsize", None) is not None for f in field_decls):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported bitfield in struct init"
            )
        if len(init_exprs) > len(field_decls):
            raise SSAConstructionError(
                f"{self._state.function_name}: too many initializers for struct"
            )

        # Classify exprs: positional vs designated (`.field = value`).
        # Build a name→expr map for designated, ensuring designators are
        # shape we support (single `.field`, no array indexes).
        # Nested InitList (for array-typed fields) is accepted — it is
        # handled per-field below.
        positional_exprs: list[c_ast.Node] = []
        designated_exprs: dict[str, c_ast.Node] = {}
        has_designated = False
        for expr in init_exprs:
            if isinstance(expr, c_ast.NamedInitializer):
                has_designated = True
                if (
                    len(expr.name or ()) != 1
                    or not isinstance(expr.name[0], c_ast.ID)
                ):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported designator shape"
                    )
                name = expr.name[0].name
                if name in designated_exprs:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: duplicate designated field {name!r}"
                    )
                designated_exprs[name] = expr.expr
            else:
                if has_designated:
                    raise SSAConstructionError(
                        f"{self._state.function_name}: positional after designated initializer"
                    )
                positional_exprs.append(expr)

        # Reject union fields (ambiguous). Struct fields are allowed
        # when their init expr is an inner InitList — recursively
        # lowered. Array fields get indexed stores through the field's
        # address.
        for field_decl in field_decls:
            resolved_field = self._resolve_type_node(field_decl.type)
            if isinstance(resolved_field, c_ast.Union):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported union field in struct init"
                )

        # Unknown designated names (not in struct) are a build error —
        # AST codegen's diagnostic is better than ours.
        known_names = {f.name for f in field_decls}
        for name in designated_exprs:
            if name not in known_names:
                raise SSAConstructionError(
                    f"{self._state.function_name}: unknown designated field {name!r}"
                )

        coord = self._coord_key(getattr(decl, "coord", None))
        # Emit a store for every field:
        #   - if has_designated: use designated_exprs[field_name] if
        #     present, else zero-init (C99 6.7.8p19),
        #   - else positional: first len(positional_exprs) fields use
        #     the positional expr, remaining fields zero-init.
        for i, field_decl in enumerate(field_decls):
            resolved_field_decl = self._resolve_type_node(field_decl.type)
            is_array_field = isinstance(resolved_field_decl, c_ast.ArrayDecl)
            # Struct field: nested-struct recursive init via this same method.
            inner_struct = resolved_field_decl
            if isinstance(inner_struct, c_ast.TypeDecl):
                inner_struct = inner_struct.type
            if isinstance(inner_struct, c_ast.Struct) and inner_struct.decls is None and inner_struct.name:
                inner_struct = self._struct_types.get(inner_struct.name, inner_struct)
            is_struct_field = isinstance(inner_struct, c_ast.Struct)

            init_expr: c_ast.Node | None
            if has_designated:
                init_expr = designated_exprs.get(field_decl.name)
            elif i < len(positional_exprs):
                init_expr = positional_exprs[i]
            else:
                init_expr = None

            if is_array_field:
                self._lower_struct_array_field_init(
                    alloc, field_decl, resolved_field_decl, init_expr,
                    block, env, coord,
                )
                continue

            if is_struct_field:
                self._lower_struct_struct_field_init(
                    alloc, field_decl, inner_struct, init_expr,
                    block, env, coord,
                )
                continue

            # Scalar field path.
            field_type_name = self._decl_type_name(field_decl.type)
            addr_type_name = self._field_address_type_name(
                field_decl.type, field_type_name
            )
            field_addr = SSAFieldAddr(
                name=self._new_temp(),
                type_name=addr_type_name,
                base=alloc,
                field_name=field_decl.name,
                access_kind=".",
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(field_addr)
            if init_expr is not None:
                if isinstance(init_expr, (c_ast.InitList, c_ast.NamedInitializer)):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported nested init for scalar field"
                    )
                _, value = self._lower_value_expr(init_expr, block, env)
                value = self._coerce_value_to_type(
                    block, env, value, field_type_name, coord,
                )
            else:
                value = SSAConstant.from_int(
                    0, type_name=field_type_name, is_safe=True,
                )
            store = SSAStore(
                name=self._new_temp(),
                type_name="",
                addr=field_addr,
                value=value,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)

    def _lower_struct_struct_field_init(
        self,
        alloc: SSAValue,
        field_decl: c_ast.Node,
        inner_struct: c_ast.Struct,
        init_expr: c_ast.Node | None,
        block: SSABlock,
        env: dict[str, SSAValue],
        coord: str | None,
    ) -> None:
        """Phase 4 MVP: initialize a struct-typed field inside a struct.

        Accepts:
          - no init expr → zero-fill each scalar sub-field,
          - inner InitList → recursively lower through a synthetic Decl
            pointing at the inner struct alloca (via the field addr).
        """
        if init_expr is not None and not isinstance(init_expr, c_ast.InitList):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported struct-field init shape"
            )

        inner_fields = list(inner_struct.decls or ())
        if any(getattr(f, "bitsize", None) is not None for f in inner_fields):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported bitfield in nested struct init"
            )

        # Compute the field's address, which points at the inner struct.
        field_addr_type = f"struct {inner_struct.name}*" if inner_struct.name else "struct*"
        field_addr = SSAFieldAddr(
            name=self._new_temp(),
            type_name=field_addr_type,
            base=alloc,
            field_name=field_decl.name,
            access_kind=".",
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(field_addr)

        # Map from inner field name → init expr (for designators) or list (positional).
        if init_expr is None:
            inner_positional: list[c_ast.Node] = []
            inner_designated: dict[str, c_ast.Node] = {}
            inner_has_designated = False
        else:
            inner_positional = []
            inner_designated = {}
            inner_has_designated = False
            for e in init_expr.exprs or ():
                if isinstance(e, c_ast.NamedInitializer):
                    inner_has_designated = True
                    if (
                        len(e.name or ()) != 1
                        or not isinstance(e.name[0], c_ast.ID)
                    ):
                        raise SSAConstructionError(
                            f"{self._state.function_name}: unsupported nested struct designator"
                        )
                    inner_designated[e.name[0].name] = e.expr
                else:
                    if inner_has_designated:
                        raise SSAConstructionError(
                            f"{self._state.function_name}: positional after designated in nested struct init"
                        )
                    inner_positional.append(e)

        # Emit stores for each inner field.
        for i, sub_field in enumerate(inner_fields):
            resolved_sub = self._resolve_type_node(sub_field.type)
            if isinstance(resolved_sub, (c_ast.ArrayDecl, c_ast.Struct, c_ast.Union)):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported aggregate sub-field in nested struct init"
                )
            sub_type_name = self._decl_type_name(sub_field.type)
            sub_addr_type = self._field_address_type_name(sub_field.type, sub_type_name)
            sub_addr = SSAFieldAddr(
                name=self._new_temp(),
                type_name=sub_addr_type,
                base=field_addr,
                field_name=sub_field.name,
                access_kind="->",
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(sub_addr)
            if inner_has_designated:
                sub_expr = inner_designated.get(sub_field.name)
            elif i < len(inner_positional):
                sub_expr = inner_positional[i]
            else:
                sub_expr = None
            if sub_expr is not None:
                _, value = self._lower_value_expr(sub_expr, block, env)
                value = self._coerce_value_to_type(
                    block, env, value, sub_type_name, coord,
                )
            else:
                value = SSAConstant.from_int(
                    0, type_name=sub_type_name, is_safe=True,
                )
            store = SSAStore(
                name=self._new_temp(),
                type_name="",
                addr=sub_addr,
                value=value,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)

    def _lower_struct_array_field_init(
        self,
        alloc: SSAValue,
        field_decl: c_ast.Node,
        resolved_field_decl: c_ast.ArrayDecl,
        init_expr: c_ast.Node | None,
        block: SSABlock,
        env: dict[str, SSAValue],
        coord: str | None,
    ) -> None:
        """Phase 4 MVP: initialize an array field inside a struct.

        Accepts:
          - no init expr → zero-fill the entire array,
          - inner InitList with scalar exprs → positional stores,
            remaining elements zero-filled,
          - string-literal init for char arrays → byte stores + NUL.
        """
        # Resolve the element type and count.
        elem_resolved = self._resolve_type_node(resolved_field_decl.type)
        if isinstance(elem_resolved, (c_ast.ArrayDecl, c_ast.Struct, c_ast.Union)):
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported nested aggregate array field in struct init"
            )
        elem_type_name = self._decl_type_name(resolved_field_decl.type)
        if getattr(resolved_field_decl, "dim", None) is None:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported unsized array field"
            )
        count = self._array_type_count(resolved_field_decl)

        # Decode init_expr to a list of c_ast expressions or None.
        inner_exprs: list[c_ast.Node | None]
        if init_expr is None:
            inner_exprs = [None] * count
        elif isinstance(init_expr, c_ast.InitList):
            exprs = list(init_expr.exprs or ())
            for e in exprs:
                if isinstance(e, (c_ast.InitList, c_ast.NamedInitializer)):
                    raise SSAConstructionError(
                        f"{self._state.function_name}: unsupported nested/designated in array field init"
                    )
            if len(exprs) > count:
                raise SSAConstructionError(
                    f"{self._state.function_name}: too many initializers for array field"
                )
            inner_exprs = list(exprs) + [None] * (count - len(exprs))
        elif (
            isinstance(init_expr, c_ast.Constant)
            and init_expr.type == "string"
            and elem_type_name in {"char", "signed char", "unsigned char"}
        ):
            decoded = self._decode_string_literal_bytes(init_expr.value)
            if len(decoded) > count:
                raise SSAConstructionError(
                    f"{self._state.function_name}: string initializer too long for array field"
                )
            bytes_exprs: list[c_ast.Node | None] = [
                c_ast.Constant(type="int", value=str(b), coord=getattr(init_expr, "coord", None))
                for b in decoded
            ]
            if len(bytes_exprs) < count:
                bytes_exprs += [None] * (count - len(bytes_exprs))
            inner_exprs = bytes_exprs
        else:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported array field initializer shape"
            )

        # Compute address of the field start, then emit indexed stores.
        field_type_name = elem_type_name  # points to first element
        addr_type_name = f"{elem_type_name}*"
        field_addr = SSAFieldAddr(
            name=self._new_temp(),
            type_name=addr_type_name,
            base=alloc,
            field_name=field_decl.name,
            access_kind=".",
            source_coord=coord,
            available_bindings=self._binding_snapshot(env),
        )
        block.append(field_addr)

        for i in range(count):
            index_const = SSAConstant.from_int(i, type_name="int", is_safe=True)
            elem_addr = SSABinaryOp(
                name=self._new_temp(),
                type_name=addr_type_name,
                op="+",
                left=field_addr,
                right=index_const,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(elem_addr)
            expr = inner_exprs[i]
            if expr is not None:
                _, value = self._lower_value_expr(expr, block, env)
                value = self._coerce_value_to_type(
                    block, env, value, elem_type_name, coord,
                )
            else:
                value = SSAConstant.from_int(
                    0, type_name=elem_type_name, is_safe=True,
                )
            store = SSAStore(
                name=self._new_temp(),
                type_name="",
                addr=elem_addr,
                value=value,
                source_coord=coord,
                available_bindings=self._binding_snapshot(env),
            )
            block.append(store)

    def _resolve_type_node(self, node: c_ast.Node, depth: int = 0) -> c_ast.Node:
        if depth > 16:
            raise SSAConstructionError(f"{self._state.function_name}: typedef recursion limit exceeded")
        current = node
        while True:
            if isinstance(current, (c_ast.Decl, c_ast.Typename)):
                current = current.type
                continue
            if isinstance(current, c_ast.TypeDecl):
                inner = current.type
                if isinstance(inner, c_ast.IdentifierType) and len(inner.names) == 1:
                    typedef = self._typedef_types.get(inner.names[0])
                    if typedef is not None:
                        current = typedef
                        depth += 1
                        if depth > 16:
                            raise SSAConstructionError(
                                f"{self._state.function_name}: typedef recursion limit exceeded"
                            )
                        continue
                if isinstance(inner, c_ast.Struct) and inner.decls is None and inner.name:
                    tagged = self._struct_types.get(inner.name)
                    if tagged is not None:
                        current = tagged
                        continue
                if isinstance(inner, c_ast.Union) and inner.decls is None and inner.name:
                    tagged = self._union_types.get(inner.name)
                    if tagged is not None:
                        current = tagged
                        continue
                if isinstance(inner, c_ast.Enum) and inner.values is None and inner.name:
                    tagged = self._enum_types.get(inner.name)
                    if tagged is not None:
                        current = tagged
                        continue
                return current
            return current

    def _sizeof_expr(self, expr: c_ast.Node, env: dict[str, SSAValue]) -> int:
        if isinstance(expr, c_ast.Typename):
            return self._ast_type_size(expr.type)
        if isinstance(expr, c_ast.ID):
            # Local arrays have `T*`-typed SSA values (either SSAStackAlloc
            # directly, or SSAPhi when the array crosses a loop header).
            # `_state.array_alloca_sizes` records the original `(count,
            # elem_type_name)` at decl time, so we always recover the
            # right answer even when env[name] has been rewritten to a
            # phi — see gcc_torture 20030105-1.c.
            if (
                self._state is not None
                and expr.name in self._state.array_alloca_sizes
            ):
                count, elem_type_name = self._state.array_alloca_sizes[expr.name]
                return count * self._type_name_size(elem_type_name)
            if expr.name in env:
                value = env[expr.name]
                # For local arrays, the SSA value is an SSAStackAlloc that
                # carries the element type and count; its `type_name` was
                # normalized to `T*` (decayed form) for address arithmetic,
                # so using it here would yield sizeof(pointer). Use the
                # declared elem_type_name * count instead.
                if isinstance(value, SSAStackAlloc) and value.count > 0:
                    return value.count * self._type_name_size(value.elem_type_name)
                return self._type_name_size(getattr(value, "type_name", "int"))
            decl_type = self._file_scope_decl_types.get(expr.name)
            if decl_type is None:
                raise SSAConstructionError(
                    f"{self._state.function_name}: use of undeclared SSA value {expr.name!r}"
                )
            return self._ast_type_size(decl_type)
        if isinstance(expr, c_ast.Constant):
            if expr.type == "int":
                return self._type_name_size(self._int_literal_type_name(expr.value, self.target_triple))
            if expr.type == "char":
                return self._type_name_size(self._char_constant_type_name(expr.value))
            if expr.type == "float":
                return self._type_name_size(self._float_literal_type_name(expr.value))
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported constant kind {expr.type!r}"
            )
        if isinstance(expr, c_ast.Cast):
            return self._ast_type_size(expr.to_type.type)
        if isinstance(expr, c_ast.ArrayRef):
            return self._type_name_size(
                self._deref_type_name(self._expr_type_name(expr.name, env))
            )
        if isinstance(expr, c_ast.StructRef):
            field_decl = self._struct_field_decl_type(
                self._struct_ref_base_type_name(expr, env),
                expr.field.name,
                access_kind=expr.type,
            )
            return self._ast_type_size(field_decl)
        if isinstance(expr, c_ast.UnaryOp):
            if expr.op == "*":
                return self._type_name_size(
                    self._deref_type_name(self._expr_type_name(expr.expr, env))
                )
            if expr.op == "&":
                return self._type_name_size(
                    self._address_of_type_name(expr.expr, env)
                )
            if expr.op == "sizeof":
                return self._type_name_size(c_size_type_name(self.target_triple))
            if expr.op == "!":
                # C11 6.5.3.3: result of `!` has type int regardless of
                # operand type. `sizeof(!a)` where a is char is therefore
                # sizeof(int), not sizeof(char).
                return self._type_name_size("int")
            if expr.op in {"+", "-", "~"}:
                return self._type_name_size(self._expr_type_name(expr.expr, env))
        if isinstance(expr, c_ast.FuncCall):
            if not isinstance(expr.name, c_ast.ID):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported call target {type(expr.name).__name__}"
                )
            return self._type_name_size(
                self._function_return_types.get(expr.name.name, "int")
            )
        if isinstance(expr, c_ast.BinaryOp):
            return self._type_name_size(self._expr_type_name(expr, env))
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported unary op 'sizeof'"
        )

    def _expr_type_name(self, expr: c_ast.Node, env: dict[str, SSAValue]) -> str:
        if isinstance(expr, c_ast.ID):
            if expr.name in env:
                return getattr(env[expr.name], "type_name", "int")
            decl_type = self._file_scope_decl_types.get(expr.name)
            if decl_type is None:
                raise SSAConstructionError(
                    f"{self._state.function_name}: use of undeclared SSA value {expr.name!r}"
                )
            return self._decl_type_name(decl_type)
        if isinstance(expr, c_ast.Constant):
            if expr.type == "int":
                return self._int_literal_type_name(expr.value, self.target_triple)
            if expr.type == "char":
                return self._char_constant_type_name(expr.value)
            if expr.type == "string":
                return "char*"
            if expr.type == "wstring":
                return "wchar_t*"
            if expr.type == "float":
                return self._float_literal_type_name(expr.value)
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported constant kind {expr.type!r}"
            )
        if isinstance(expr, c_ast.Cast):
            return self._decl_type_name(expr.to_type.type)
        if isinstance(expr, c_ast.UnaryOp):
            if expr.op == "*":
                return self._deref_type_name(self._expr_type_name(expr.expr, env))
            if expr.op == "&":
                return self._address_of_type_name(expr.expr, env)
            if expr.op == "!":
                return "int"
            if expr.op == "sizeof":
                return c_size_type_name(self.target_triple)
            return self._expr_type_name(expr.expr, env)
        if isinstance(expr, c_ast.ArrayRef):
            return self._deref_type_name(self._expr_type_name(expr.name, env))
        if isinstance(expr, c_ast.StructRef):
            field_decl = self._struct_field_decl_type(
                self._struct_ref_base_type_name(expr, env),
                expr.field.name,
                access_kind=expr.type,
            )
            if self._field_expr_returns_address(field_decl):
                return self._field_address_type_name(field_decl, self._decl_type_name(field_decl))
            return self._decl_type_name(field_decl)
        if isinstance(expr, c_ast.FuncCall):
            if not isinstance(expr.name, c_ast.ID):
                raise SSAConstructionError(
                    f"{self._state.function_name}: unsupported call target {type(expr.name).__name__}"
                )
            return self._function_return_types.get(expr.name.name, "int")
        if isinstance(expr, c_ast.BinaryOp):
            left = self._expr_type_name(expr.left, env)
            right = self._expr_type_name(expr.right, env)
            if expr.op in {"==", "!=", "<", ">", "<=", ">="}:
                return "int"
            # Apply C11 6.3.1.8 usual arithmetic conversions (includes
            # the integer-promotion step): this makes
            # `sizeof((short)1 + 0)` return `sizeof(int) = 4` instead
            # of `sizeof(short) = 2`. The `_binary_result_type` static
            # form handles the rank table without needing typedef
            # resolution here (typedefs resolve through the instance
            # resolver when available on this builder).
            class _Fake:
                __slots__ = ("type_name",)
                def __init__(self, tn):
                    self.type_name = tn
            return self._binary_result_type(
                expr.op, _Fake(left), _Fake(right),
            )
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported expression {type(expr).__name__}"
        )

    def _struct_ref_base_type_name(self, expr: c_ast.StructRef, env: dict[str, SSAValue]) -> str:
        base_type_name = self._expr_type_name(expr.name, env)
        if expr.type == "." and not base_type_name.endswith("*"):
            return f"{base_type_name}*"
        return base_type_name

    def _address_of_type_name(self, expr: c_ast.Node, env: dict[str, SSAValue]) -> str:
        if isinstance(expr, c_ast.StructRef):
            field_decl = self._struct_field_decl_type(
                self._struct_ref_base_type_name(expr, env),
                expr.field.name,
                access_kind=expr.type,
            )
            field_type_name = self._decl_type_name(field_decl)
            return self._field_address_type_name(field_decl, field_type_name)
        return f"{self._expr_type_name(expr, env)}*"

    def _type_name_size(self, type_name: str) -> int:
        return self._ast_type_size(self._resolve_type_name(type_name))

    def _ast_type_size(self, node: c_ast.Node) -> int:
        resolved = self._resolve_type_node(node)
        if isinstance(resolved, c_ast.TypeDecl):
            inner = self._resolve_type_node(resolved.type)
            if isinstance(inner, c_ast.IdentifierType):
                return self._builtin_type_size(inner.names, self.target_triple)
            if isinstance(inner, c_ast.Struct):
                return self._struct_type_size(inner)
            if isinstance(inner, c_ast.Union):
                return self._union_type_size(inner)
            if isinstance(inner, c_ast.Enum):
                return 4
        if isinstance(resolved, c_ast.PtrDecl):
            return pointer_scalar_layout().size
        if isinstance(resolved, c_ast.ArrayDecl):
            return self._array_type_count(resolved) * self._ast_type_size(resolved.type)
        if isinstance(resolved, c_ast.Struct):
            return self._struct_type_size(resolved)
        if isinstance(resolved, c_ast.Union):
            return self._union_type_size(resolved)
        if isinstance(resolved, c_ast.Enum):
            return 4
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported unary op 'sizeof'"
        )

    def _ast_type_align(self, node: c_ast.Node) -> int:
        resolved = self._resolve_type_node(node)
        if isinstance(resolved, c_ast.TypeDecl):
            inner = self._resolve_type_node(resolved.type)
            if isinstance(inner, c_ast.IdentifierType):
                return self._builtin_type_align(inner.names, self.target_triple)
            if isinstance(inner, c_ast.Struct):
                return self._struct_type_align(inner)
            if isinstance(inner, c_ast.Union):
                return self._union_type_align(inner)
            if isinstance(inner, c_ast.Enum):
                return 4
        if isinstance(resolved, c_ast.PtrDecl):
            return pointer_scalar_layout().alignment
        if isinstance(resolved, c_ast.ArrayDecl):
            return self._ast_type_align(resolved.type)
        if isinstance(resolved, c_ast.Struct):
            return self._struct_type_align(resolved)
        if isinstance(resolved, c_ast.Union):
            return self._union_type_align(resolved)
        if isinstance(resolved, c_ast.Enum):
            return 4
        raise SSAConstructionError(
            f"{self._state.function_name}: unsupported unary op 'sizeof'"
        )

    def _struct_type_size(self, node: c_ast.Struct) -> int:
        decls = node.decls or ()
        if not decls:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported unary op 'sizeof'"
            )
        offset = 0
        max_align = 1
        for decl in decls:
            align = self._ast_type_align(decl.type)
            max_align = max(max_align, align)
            offset = self._align_up(offset, align)
            offset += self._ast_type_size(decl.type)
        return self._align_up(offset, max_align)

    def _union_type_size(self, node: c_ast.Union) -> int:
        decls = node.decls or ()
        if not decls:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported unary op 'sizeof'"
            )
        max_size = 0
        max_align = 1
        for decl in decls:
            max_size = max(max_size, self._ast_type_size(decl.type))
            max_align = max(max_align, self._ast_type_align(decl.type))
        return self._align_up(max_size, max_align)

    def _struct_type_align(self, node: c_ast.Struct) -> int:
        decls = node.decls or ()
        if not decls:
            return 1
        return max(self._ast_type_align(decl.type) for decl in decls)

    def _union_type_align(self, node: c_ast.Union) -> int:
        decls = node.decls or ()
        if not decls:
            return 1
        return max(self._ast_type_align(decl.type) for decl in decls)

    def _array_type_count(self, node: c_ast.ArrayDecl) -> int:
        dim = getattr(node, "dim", None)
        if dim is None:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported unary op 'sizeof'"
            )
        try:
            return self._eval_enum_constant(dim, self._enum_values)
        except SSAConstructionError as exc:
            raise SSAConstructionError(
                f"{self._state.function_name}: unsupported unary op 'sizeof'"
            ) from exc

    @staticmethod
    def _builtin_type_size(names: list[str], target_triple=None) -> int:
        try:
            return builtin_scalar_layout(names, target_triple).size
        except ValueError as exc:
            raise SSAConstructionError("unsupported builtin sizeof target") from exc

    @staticmethod
    def _builtin_type_align(names: list[str], target_triple=None) -> int:
        try:
            return builtin_scalar_layout(names, target_triple).alignment
        except ValueError as exc:
            raise SSAConstructionError("unsupported builtin align target") from exc

    @staticmethod
    def _int_literal_type_name(raw: str, target_triple=None) -> str:
        try:
            return integer_literal_type_name(raw, target_triple)
        except ValueError as exc:
            raise SSAConstructionError(str(exc)) from exc

    @staticmethod
    def _float_literal_type_name(raw: str) -> str:
        lower = raw.lower()
        if lower.endswith("l"):
            return "long double"
        return "float" if lower.endswith("f") else "double"

    @staticmethod
    def _align_up(value: int, align: int) -> int:
        if align <= 1:
            return value
        return (value + align - 1) & ~(align - 1)

    def _new_block(self, prefix: str) -> SSABlock:
        assert self._state is not None
        if prefix == "entry" and not self._state.blocks:
            name = "entry"
        else:
            self._state.block_counter += 1
            name = f"{prefix}.{self._state.block_counter}"
        block = SSABlock(name=name)
        self._state.blocks.append(block)
        return block

    def _new_version(self, variable_name: str) -> str:
        assert self._state is not None
        version = self._state.variable_versions.get(variable_name, 0)
        self._state.variable_versions[variable_name] = version + 1
        return f"{variable_name}.{version}"

    def _new_temp(self) -> str:
        assert self._state is not None
        # Prefix with `$` so temps can never collide with a user variable
        # versioned via `_new_version("tmp")` (e.g. `unsigned short *tmp`
        # in gcc_torture 20000412-6.c produced both `tmp.0` via the
        # phi/rename path and `tmp.0` via the value counter). `$` is
        # valid inside llvmlite quoted identifiers but illegal in a C
        # identifier, so no source variable can ever claim this prefix.
        name = f"$t.{self._state.value_counter}"
        self._state.value_counter += 1
        return name

    @staticmethod
    def _parse_int_constant(raw: str) -> int:
        return integer_literal_value(raw)

    @classmethod
    def _decode_string_literal_bytes(cls, raw: str) -> bytes:
        """Decode a C-style string literal including escape sequences.

        Accepts `"..."`, `L"..."`, `u"..."`, `U"..."`, `u8"..."`. Returns
        the decoded bytes (without the trailing NUL — caller adds that).
        """
        if not raw:
            return b""
        s = raw
        # Strip optional encoding prefix.
        for prefix in ("u8", "L", "u", "U"):
            if s.startswith(prefix + '"'):
                s = s[len(prefix):]
                break
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            s = s[1:-1]
        decoded = cls._process_escapes(s)
        return decoded.encode("latin-1", errors="replace")

    @classmethod
    def _process_escapes(cls, raw: str) -> str:
        result: list[str] = []
        i = 0
        while i < len(raw):
            if raw[i] == "\\" and i + 1 < len(raw):
                if raw[i + 1] == "x":
                    j = i + 2
                    digits: list[str] = []
                    while j < len(raw) and raw[j] in "0123456789abcdefABCDEF":
                        digits.append(raw[j])
                        j += 1
                    if digits:
                        result.append(chr(int("".join(digits), 16) & 0xFF))
                        i = j
                        continue
                if raw[i + 1] in "01234567":
                    j = i + 1
                    digits = []
                    while j < len(raw) and len(digits) < 3 and raw[j] in "01234567":
                        digits.append(raw[j])
                        j += 1
                    result.append(chr(int("".join(digits), 8) & 0xFF))
                    i = j
                    continue
                escape = cls._ESCAPE_MAP.get(raw[i + 1])
                if escape is not None:
                    result.append(escape)
                    i += 2
                    continue
            result.append(raw[i])
            i += 1
        return "".join(result)

    def _parse_char_constant(self, raw: str) -> int:
        return decode_c_character_constant(raw, self.target_triple)[0]

    def _char_constant_type_name(self, raw: str) -> str:
        return decode_c_character_constant(raw, self.target_triple)[1]

    def _eval_enum_constant(self, expr: c_ast.Node, known: dict[str, int]) -> int:
        if isinstance(expr, c_ast.Constant):
            if expr.type == "int":
                return self._parse_int_constant(expr.value)
            if expr.type == "char":
                return self._parse_char_constant(expr.value)
        if isinstance(expr, c_ast.ID) and expr.name in known:
            return known[expr.name]
        if isinstance(expr, c_ast.UnaryOp) and expr.op in {"+", "-", "~"}:
            value = self._eval_enum_constant(expr.expr, known)
            if expr.op == "+":
                return value
            if expr.op == "-":
                return -value
            return ~value
        if isinstance(expr, c_ast.BinaryOp):
            left = self._eval_enum_constant(expr.left, known)
            right = self._eval_enum_constant(expr.right, known)
            if expr.op == "+":
                return left + right
            if expr.op == "-":
                return left - right
            if expr.op == "*":
                return left * right
            if expr.op == "/":
                return left // right
            if expr.op == "%":
                return left % right
            if expr.op == "<<":
                return left << right
            if expr.op == ">>":
                return left >> right
            if expr.op == "&":
                return left & right
            if expr.op == "|":
                return left | right
            if expr.op == "^":
                return left ^ right
        raise SSAConstructionError(
            f"{self._state.function_name if self._state is not None else '<file>'}: unsupported enum constant"
        )

    @staticmethod
    def _is_safe_int_literal(raw: str) -> bool:
        return raw.isdigit() and (raw == "0" or not raw.startswith("0"))

    def _resolve_typedef_type_name(self, name: str) -> str:
        """Walk typedef chains so `u8` / `uint32_t` etc. reach their
        canonical integer spelling for rank-based arithmetic promotion.
        Preserves pointer/array/aggregate forms untouched."""
        if not name or name.endswith("*"):
            return name
        seen = set()
        current = name
        for _ in range(16):
            if current in seen:
                break
            seen.add(current)
            normalized = self._normalize_int_type_name(current)
            if normalized != current:
                current = normalized
                continue
            tokens = current.split()
            if len(tokens) == 1 and tokens[0] in self._typedef_types:
                td = self._typedef_types[tokens[0]]
                decl = self._resolve_type_node(td)
                inner = getattr(decl, "type", None) if isinstance(decl, c_ast.TypeDecl) else decl
                if isinstance(inner, c_ast.IdentifierType):
                    current = " ".join(inner.names)
                    continue
            break
        return self._normalize_int_type_name(current)

    def _expr_type_name_safe(self, expr: c_ast.Node, env: dict[str, SSAValue]) -> str | None:
        """Return a best-effort type_name for expr without raising."""
        try:
            return self._expr_type_name(expr, env)
        except SSAConstructionError:
            return None
        except Exception:
            return None

    @staticmethod
    def _normalize_int_type_name(name: str) -> str:
        """Collapse `signed short`/`signed int`/etc. onto their canonical names."""
        if not name:
            return "int"
        if name.endswith("*"):
            return name
        # Drop qualifiers and the redundant `signed` keyword.
        tokens = [
            t for t in name.split()
            if t not in ("const", "volatile", "restrict", "register", "signed")
        ]
        if not tokens:
            return "int"
        joined = " ".join(tokens)
        aliases = {
            "unsigned": "unsigned int",
            "unsigned int": "unsigned int",
            "int unsigned": "unsigned int",
            "short int": "short",
            "int short": "short",
            "unsigned short int": "unsigned short",
            "short unsigned": "unsigned short",
            "short unsigned int": "unsigned short",
            "long int": "long",
            "int long": "long",
            "unsigned long int": "unsigned long",
            "long unsigned": "unsigned long",
            "long long int": "long long",
            "long long": "long long",
            "unsigned long long int": "unsigned long long",
            "long long unsigned": "unsigned long long",
        }
        return aliases.get(joined, joined)

    def _binary_result_type(self_or_op, op_or_left=None, left_or_right=None, right=None, *, target_triple=None) -> str:
        """Compute C-level result type of a binary op.

        Works both as an instance method (self resolves typedefs) and as
        a classmethod-like fallback (when codegen only has access to the
        class, without typedef info). The classmethod form is called as
        `SSABuilder._binary_result_type(op, left, right)`.
        """
        if isinstance(self_or_op, str):
            # Static usage: no typedef map available.
            resolver = None
            op, left, right = self_or_op, op_or_left, left_or_right
        else:
            resolver = getattr(self_or_op, "_resolve_typedef_type_name", None)
            op, left, right = op_or_left, left_or_right, right
            target_triple = self_or_op.target_triple
        if op in {"==", "!=", "<", ">", "<=", ">="}:
            return "int"
        left_name = getattr(left, "type_name", "int") or "int"
        right_name = getattr(right, "type_name", "int") or "int"
        if resolver is not None:
            left_name = resolver(left_name)
            right_name = resolver(right_name)
        if (
            op == "-"
            and left_name.endswith("*")
            and right_name.endswith("*")
        ):
            return c_ptrdiff_type_name(target_triple)
        if op in {"<<", ">>"}:
            # C11 6.5.7p3: integer promotions apply to both operands of
            # shifts. A char/short operand promotes to int before the
            # shift, so `(uchar)x << 8` must happen in int, not i8
            # (which would drop all bits). gcc_torture pr65401.c.
            # Types at or above `int` rank keep their own spelling —
            # including `__int128` which must NOT fall through to "int"
            # (that would truncate `(__int128)1 << 64` to zero). See
            # clang_compat int128_keyword test.
            left_name = SSABuilder._normalize_int_type_name(left_name)
            if left_name in (
                "long", "long long", "unsigned long", "unsigned long long",
                "__int128", "unsigned __int128",
            ):
                return left_name
            return "unsigned int" if left_name == "unsigned int" else "int"
        # Pointer + integer / integer + pointer: result is pointer.
        if left_name.endswith("*") or right_name.endswith("*"):
            return left_name if left_name.endswith("*") else right_name
        # Normalize variant spellings (`signed short`, `int short`, ...)
        # onto canonical integer type names before ranking.
        left_name = SSABuilder._normalize_int_type_name(left_name)
        right_name = SSABuilder._normalize_int_type_name(right_name)
        # C11 6.3.1.8 usual arithmetic conversions. Rank-ordered subset:
        # long double > double > float > unsigned long > long > unsigned int > int > short > char.
        _RANK = {
            "long double": 90,
            "double": 80,
            "float": 70,
            "unsigned __int128": 67,
            "__int128": 65,
            "unsigned long long": 62,
            "long long": 60,
            "unsigned long": 52,
            "long": 50,
            "unsigned int": 42,
            "int": 40,
            "unsigned short": 32,
            "short": 30,
            "unsigned char": 22,
            "char": 20,
            "_Bool": 10,
        }
        lr = _RANK.get(left_name, 40)
        rr = _RANK.get(right_name, 40)
        # Anything below int-rank is promoted to int per integer promotions.
        if lr < 40 and rr < 40:
            return "int"
        if lr < 40:
            left_name, lr = "int", 40
        if rr < 40:
            right_name, rr = "int", 40
        if max(lr, rr) < 70:
            left_unsigned = builtin_integer_is_unsigned(left_name.split(), target_triple)
            right_unsigned = builtin_integer_is_unsigned(right_name.split(), target_triple)
            if left_unsigned != right_unsigned:
                unsigned_name = left_name if left_unsigned else right_name
                signed_name = right_name if left_unsigned else left_name
                unsigned_rank = lr if left_unsigned else rr
                signed_rank = rr if left_unsigned else lr
                if unsigned_rank >= signed_rank:
                    return unsigned_name
                try:
                    unsigned_width = builtin_scalar_layout(unsigned_name.split(), target_triple).size * 8
                    signed_width = builtin_scalar_layout(signed_name.split(), target_triple).size * 8
                except ValueError:
                    return left_name if lr >= rr else right_name
                if signed_width > unsigned_width:
                    return signed_name
                return "unsigned " + signed_name
        if lr >= rr:
            return left_name if lr >= 40 else "int"
        return right_name if rr >= 40 else "int"

    @staticmethod
    def _unary_result_type(op: str, operand: SSAValue) -> str:
        if op == "!":
            return "int"
        return getattr(operand, "type_name", "int")

    @staticmethod
    def _coord_key(coord) -> str | None:
        if coord is None:
            return None
        return str(coord)

    @staticmethod
    def _binding_snapshot(
        env: dict[str, SSAValue],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted((name, value.name) for name, value in env.items())
        )

    def _record_binding(
        self,
        *,
        kind: str,
        block: SSABlock,
        target_name: str,
        value: SSAValue,
        type_name: str,
        source_coord: str | None,
    ) -> None:
        assert self._state is not None
        self._state.bindings.append(
            SSABinding(
                kind=kind,
                target_name=target_name,
                value=value,
                source_coord=source_coord,
                block_name=block.name,
                type_name=type_name,
            )
        )
