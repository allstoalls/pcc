"""Declaration lowering for the C frontend."""

from pcc.ir.compat import ir_c as ir

from pcc.frontends.c.codegen.c_declaration_state import CodegenError, is_thread_local_storage
from pcc.frontends.c.codegen.c_layout import is_struct_ir_type as _is_struct_ir_type
from pcc.frontends.c.codegen.c_types import get_ir_type, int8_t, int32_t, int64_t
from pcc.frontends.c.ast import c_ast as c_ast


class CDeclarationLoweringMixin:
    def codegen_Decl(self, node):

        type_str = ""

        # Skip anonymous/unnamed declarations
        if node.name is None and not isinstance(
            node.type, (c_ast.Struct, c_ast.Union, c_ast.Enum, c_ast.FuncDecl)
        ):
            if not (
                isinstance(node.type, c_ast.TypeDecl)
                and isinstance(node.type.type, (c_ast.Struct, c_ast.Union, c_ast.Enum))
            ):
                return None, None

        if node.name is not None:
            self._record_decl_ast_type(node.name, node.type)

        thread_local = is_thread_local_storage(node.storage)
        if thread_local and (
            isinstance(node.type, c_ast.FuncDecl)
            or (node.storage and any(word in node.storage for word in ("auto", "register", "typedef")))
            or (not self.in_global and not any(word in node.storage for word in ("static", "extern")))
        ):
            raise CodegenError("invalid thread-local storage declaration for " + str(node.name))

        # Standalone tag definitions such as:
        #   struct S { ... };
        #   union U { ... };
        #   enum E { ... };
        # do not declare objects. Register the aggregate/enum type and stop.
        if node.name is None and isinstance(node.type, c_ast.TypeDecl):
            inner = node.type.type
            if isinstance(inner, c_ast.Struct):
                self.codegen_Struct(inner)
                return None, None
            if isinstance(inner, c_ast.Union):
                self.codegen_Union(inner)
                return None, None
            if isinstance(inner, c_ast.Enum):
                self.codegen_Enum(inner)
                return None, None

        # `extern T name;` inside a function body refers to the file-scope
        # `name` (C11 6.2.2p4). Rebind the local scope so the declared name
        # resolves to the global's storage rather than creating a fresh
        # alloca that shadows the outer declaration (gcc_torture scope-1.c).
        is_extern_local = (
            not self.in_global
            and node.storage and "extern" in node.storage
            and node.name is not None
            and not isinstance(node.type, c_ast.FuncDecl)
            and node.init is None
        )
        if is_extern_local:
            ir_type = self._extern_decl_ir_type(node.name, node.type)
            self._bind_local_extern_object(node.name, ir_type, storage=node.storage)
            return None, None

        # Static local objects: stored as internal globals with function-scoped names
        is_static = node.storage and "static" in node.storage
        if is_static and not self.in_global and not isinstance(node.type, c_ast.FuncDecl):
            ir_type = self._static_local_ir_type(node.type, init_node=node.init)
            # Create unique global name
            global_name = self._static_local_symbol_name(node.name)
            gv = self._create_bound_global(node.name, ir_type, symbol_name=global_name, storage=node.storage)
            gv.linkage = "internal"
            if node.init:
                gv.initializer = self._build_const_init(node.init, ir_type)
            else:
                gv.initializer = self._zero_initializer(ir_type)
            return None, None

        if self._is_global_extern_decl(node):
            ir_type = self._extern_decl_ir_type(node.name, node.type)
            self._prepare_file_scope_object(
                node.name,
                ir_type,
                storage=node.storage,
                has_initializer=False,
            )
            return None, None

        if isinstance(node.type, c_ast.Enum):
            return self.codegen_Enum(node.type)

        # Forward function declaration: int foo(int x);
        if isinstance(node.type, c_ast.FuncDecl):
            funcname = node.name
            function_type, ir_type = self._build_function_ir_type(node.type)
            symbol_name = funcname
            if self.in_global:
                function_type = self._preferred_file_scope_function_ir_type(
                    funcname,
                    function_type,
                    getattr(node.type, "args", None) is not None,
                )
                symbol_name = self._register_file_scope_function(
                    funcname,
                    function_type,
                    storage=node.storage,
                    funcspec=node.funcspec,
                    is_definition=False,
                )
            # Skip if already exists (module globals, libc, or env)
            existing = self.module.globals.get(symbol_name)
            if existing:
                if self._func_decl_returns_unsigned(node.type):
                    self._mark_unsigned_return(existing)
                self.define(funcname, (None, existing))
                return None, None
            try:
                func = ir.Function(
                    self.module,
                    function_type,
                    name=symbol_name,
                )
                if self._func_decl_returns_unsigned(node.type):
                    self._mark_unsigned_return(func)
                self.define(funcname, (ir_type, func))
            except Exception:
                # Already exists (libc or previous decl)
                existing = self.module.globals.get(symbol_name)
                if existing:
                    if self._func_decl_returns_unsigned(node.type):
                        self._mark_unsigned_return(existing)
                    self.define(funcname, (ir_type, existing))
            return None, None

        # Bare struct/union/type definition
        if isinstance(node.type, c_ast.Union):
            if node.name is None:
                self.codegen_Union(node.type)
            return None, None

        if isinstance(node.type, c_ast.Struct) and node.name is None:
            self.codegen_Struct(node.type)
            return None, None

        if isinstance(node.type, c_ast.TypeDecl):
            if isinstance(node.type.type, c_ast.IdentifierType):
                # Check if the type resolves to a struct or pointer via typedef
                resolved = self._resolve_type_str(node.type.type.names)
                if isinstance(resolved, ir.FunctionType):
                    funcname = node.type.declname
                    symbol_name = funcname
                    if self.in_global:
                        symbol_name = self._register_file_scope_function(
                            funcname,
                            resolved,
                            storage=node.storage,
                            funcspec=node.funcspec,
                            is_definition=False,
                        )
                    existing = self.module.globals.get(symbol_name)
                    if existing:
                        self.define(funcname, (resolved.return_type, existing))
                        return None, None
                    try:
                        func = ir.Function(self.module, resolved, name=symbol_name)
                        self.define(funcname, (resolved.return_type, func))
                    except Exception:
                        existing = self.module.globals.get(symbol_name)
                        if existing:
                            self.define(funcname, (resolved.return_type, existing))
                    return None, None
                if (
                    isinstance(resolved, (ir.PointerType, ir.ArrayType))
                    or _is_struct_ir_type(resolved)
                    or getattr(resolved, "is_union", False)
                ):
                    name = node.type.declname
                    ir_type = resolved
                    if not self.in_global:
                        ret = self._alloca_in_entry(ir_type, name)
                        self.define(name, (ir_type, ret))
                    else:
                        ret, write_initializer = self._prepare_file_scope_object(
                            name,
                            ir_type,
                            storage=node.storage,
                            has_initializer=node.init is not None,
                        )
                        if ret is None:
                            return None, None
                    if node.init is not None:
                        if self.in_global:
                            if write_initializer:
                                ret.initializer = self._build_const_init(
                                    node.init, ir_type
                                )
                        else:
                            if getattr(ir_type, "is_union", False):
                                self._safe_store(self._zero_initializer(ir_type), ret)
                                self._init_runtime_value(ret, ir_type, node.init)
                            elif isinstance(node.init, c_ast.InitList) and _is_struct_ir_type(
                                ir_type
                            ):
                                self._safe_store(self._zero_initializer(ir_type), ret)
                                self._init_runtime_aggregate(ret, node.init, ir_type)
                            else:
                                init_val, _ = self.codegen(node.init)
                                if init_val is not None:
                                    if init_val.type != ir_type:
                                        init_val = self._implicit_convert(
                                            init_val, ir_type
                                        )
                                    self._safe_store(init_val, ret)
                    elif self.in_global and write_initializer:
                        ret.initializer = self._zero_initializer(ir_type)
                    return None, None

            if isinstance(node.type.type, (c_ast.Struct, c_ast.Union)):
                name = node.type.declname
                codegen_fn = (
                    self.codegen_Union
                    if isinstance(node.type.type, c_ast.Union)
                    else self.codegen_Struct
                )
                if node.type.type.name is None or getattr(node.type.type, "decls", None) is not None:
                    struct_type = codegen_fn(node.type.type)
                    if self.in_global and node.name and node.type.type.name is None:
                        # Preserve the repository's legacy behavior for
                        # file-scope anonymous aggregates declared as:
                        #   struct { ... } Name;
                        # Existing tests rely on a later `struct Name`
                        # resolving to the same aggregate type.
                        self.define(self._tag_type_key(node.name), (struct_type, None))
                    if not self.in_global:
                        ret = self._alloca_in_entry(struct_type, name)
                        self.define(name, (struct_type, ret))
                    else:
                        ret, write_initializer = self._prepare_file_scope_object(
                            name,
                            struct_type,
                            storage=node.storage,
                            has_initializer=node.init is not None,
                        )
                        if ret is None:
                            return None, None
                    if node.init is not None:
                        if self.in_global:
                            if write_initializer:
                                ret.initializer = self._build_const_init(
                                    node.init, struct_type
                                )
                        else:
                            if getattr(struct_type, "is_union", False):
                                self._safe_store(
                                    self._zero_initializer(struct_type), ret
                                )
                                self._init_runtime_value(ret, struct_type, node.init)
                            elif isinstance(node.init, c_ast.InitList):
                                self._safe_store(
                                    self._zero_initializer(struct_type), ret
                                )
                                self._init_runtime_aggregate(
                                    ret, node.init, struct_type
                                )
                            else:
                                init_val, _ = self.codegen(node.init)
                                if init_val is not None:
                                    if init_val.type != struct_type:
                                        init_val = self._implicit_convert(
                                            init_val, struct_type
                                        )
                                    self._safe_store(init_val, ret)
                    elif self.in_global and write_initializer:
                        ret.initializer = self._zero_initializer(struct_type)
                    return None, None
                else:
                    struct_type = self.env[
                        self._tag_type_key(node.type.type.name)
                    ][0]
                    if not self.in_global:
                        ret = self._alloca_in_entry(struct_type, name)
                        self.define(name, (struct_type, ret))
                    else:
                        ret, write_initializer = self._prepare_file_scope_object(
                            name,
                            struct_type,
                            storage=node.storage,
                            has_initializer=node.init is not None,
                        )
                        if ret is None:
                            return None, None
                    if node.init is not None:
                        if self.in_global:
                            if write_initializer:
                                ret.initializer = self._build_const_init(
                                    node.init, struct_type
                                )
                        else:
                            if getattr(struct_type, "is_union", False):
                                self._safe_store(
                                    self._zero_initializer(struct_type), ret
                                )
                                self._init_runtime_value(ret, struct_type, node.init)
                            elif isinstance(node.init, c_ast.InitList):
                                self._safe_store(
                                    self._zero_initializer(struct_type), ret
                                )
                                self._init_runtime_aggregate(
                                    ret, node.init, struct_type
                                )
                            else:
                                init_val, _ = self.codegen(node.init)
                                if init_val is not None:
                                    if init_val.type != struct_type:
                                        init_val = self._implicit_convert(
                                            init_val, struct_type
                                        )
                                    self._safe_store(init_val, ret)
                    elif self.in_global and write_initializer:
                        ret.initializer = self._zero_initializer(struct_type)
                    return None, None
            else:
                if isinstance(node.type.type, c_ast.IdentifierType):
                    type_str = node.type.type.names
                    is_unsigned = self._is_unsigned_type_names(type_str)
                    ir_type = self._get_ir_type(type_str)
                    type_str = self._resolve_type_str(type_str)
                    if isinstance(type_str, ir.Type):
                        type_str = "int"  # fallback for alloca name
                else:
                    if isinstance(node.type.type, c_ast.Enum):
                        self.codegen_Enum(node.type.type)
                    type_str = "int"
                    is_unsigned = False
                    ir_type = self._resolve_ast_type(node.type)
                if self._is_floating_ir_type(ir_type):
                    init = 0.0
                else:
                    init = 0

                if node.init is not None:
                    if self.in_global:
                        init_val = self._build_const_init(node.init, ir_type)
                    else:
                        # SSA promotion: skip alloca for single-def non-escaping scalars.
                        # Inspired by clang's EmitAutoVarAlloca + Graal's PEA.
                        _ssa_promote = self._should_ssa_promote(node.name)
                        if not _ssa_promote:
                            var_addr, var_ir_type = self.create_entry_block_alloca(
                                node.name, type_str, 1, storage=node.storage
                            )
                            if is_unsigned:
                                self._mark_unsigned(var_addr)
                        init_val, _ = self.codegen(node.init)
                else:
                    init_val = self._zero_initializer(ir_type)
                    _ssa_promote = False
                if self.in_global:
                    var_addr, write_initializer = self._prepare_file_scope_object(
                        node.name,
                        ir_type,
                        storage=node.storage,
                        has_initializer=node.init is not None,
                    )
                    if var_addr is None:
                        return None, None
                    var_ir_type = ir_type
                    if write_initializer:
                        var_addr.initializer = init_val
                else:
                    if _ssa_promote:
                        # Direct SSA: define as value, no alloca/store
                        init_val = self._implicit_convert(
                            init_val,
                            ir_type,
                            target_unsigned=is_unsigned,
                        )
                        init_val = self._tag_value_from_decl_type(init_val, node.type)
                        self.define(node.name, (ir_type, init_val))
                        if is_unsigned:
                            self._mark_unsigned(init_val)
                        return None, None
                    else:
                        if node.init is None:
                            var_addr, var_ir_type = self.create_entry_block_alloca(
                                node.name, type_str, 1, storage=node.storage
                            )
                            if is_unsigned:
                                self._mark_unsigned(var_addr)
                        init_val = self._implicit_convert(
                            init_val,
                            ir_type,
                            target_unsigned=is_unsigned,
                        )
                        init_val = self._tag_value_from_decl_type(init_val, node.type)
                        self._safe_store(init_val, var_addr)
                if self.in_global and is_unsigned:
                    self._mark_unsigned(var_addr)

        elif isinstance(node.type, c_ast.ArrayDecl):
            array_list = []
            array_node = node.type
            var_addr = None
            var_ir_type = None
            elem_ir_type = None
            write_initializer = True
            inferred_elem_type = None
            try:
                if isinstance(node.type.type, c_ast.ArrayDecl):
                    inferred_elem_type = self._build_array_ir_type(node.type.type)
                else:
                    inferred_elem_type = self._resolve_ast_type(node.type.type)
            except Exception:
                inferred_elem_type = None
            inferred_top_dim = self._infer_array_count_from_initializer(
                node.init, inferred_elem_type
            )
            while True:
                array_next_type = array_node.type
                if isinstance(array_next_type, c_ast.TypeDecl):
                    dynamic_dim_val = None
                    if array_node.dim:
                        try:
                            dim_val = self._eval_dim(array_node.dim)
                        except CodegenError:
                            dim_val = None
                            dynamic_dim_val, _ = self.codegen(array_node.dim)
                    else:
                        dim_val = 0
                    if (
                        dim_val == 0
                        and array_node is node.type
                        and inferred_top_dim is not None
                    ):
                        dim_val = inferred_top_dim
                    if dynamic_dim_val is not None:
                        if self.in_global or array_node is not node.type:
                            raise CodegenError(
                                "only one-dimensional local VLAs are supported"
                            )
                        elem_ir_type = self._resolve_ast_type(array_next_type)
                        if not isinstance(dynamic_dim_val.type, ir.IntType):
                            dynamic_dim_val = self.builder.fptoui(
                                dynamic_dim_val, ir.IntType(64)
                            )
                        elif dynamic_dim_val.type.width != 64:
                            dynamic_dim_val = self._implicit_convert(
                                dynamic_dim_val, ir.IntType(64)
                            )
                        var_addr = self.builder.alloca(
                            elem_ir_type,
                            size=dynamic_dim_val,
                            name=node.name,
                        )
                        self.define(node.name, (ir.PointerType(elem_ir_type), var_addr))
                        self._mark_vla_binding(var_addr, dynamic_dim_val)
                        if self._has_unsigned_scalar_pointee(node.type):
                            self._mark_unsigned_pointee(var_addr)
                        return None, var_addr
                    array_list.append(dim_val)
                    elem_ir_type = self._resolve_ast_type(array_next_type)
                    break

                elif isinstance(array_next_type, c_ast.ArrayDecl):
                    dim_val = self._eval_dim(array_node.dim)
                    if (
                        dim_val == 0
                        and array_node is node.type
                        and inferred_top_dim is not None
                    ):
                        dim_val = inferred_top_dim
                    array_list.append(dim_val)
                    array_node = array_next_type
                    continue
                elif isinstance(array_next_type, c_ast.PtrDecl):
                    # Array of pointers: int *arr[3]
                    dim = self._eval_dim(array_node.dim)
                    if (
                        dim == 0
                        and array_node is node.type
                        and inferred_top_dim is not None
                    ):
                        dim = inferred_top_dim
                    elem_ir = self._resolve_ast_type(array_next_type)
                    elem_ir_type = elem_ir
                    dims = array_list + [dim]
                    arr_ir = elem_ir
                    for current_dim in reversed(dims):
                        arr_ir = self._checked_array_ir_type(arr_ir, current_dim)
                    arr_ir.dim_array = dims
                    if not self.in_global:
                        var_addr = self._alloca_in_entry(arr_ir, node.name)
                        self.define(node.name, (arr_ir, var_addr))
                    else:
                        var_addr, write_initializer = self._prepare_file_scope_object(
                            node.name,
                            arr_ir,
                            storage=node.storage,
                            has_initializer=node.init is not None,
                        )
                        if var_addr is None:
                            return None, None
                    var_ir_type = arr_ir
                    break
                else:
                    raise Exception("TODO implement")

            if var_addr is None:
                var_ir_type = elem_ir_type
                for dim in reversed(array_list):
                    var_ir_type = self._checked_array_ir_type(var_ir_type, dim)
                var_ir_type.dim_array = array_list
                if not self.in_global:
                    var_addr = self._alloca_in_entry(var_ir_type, node.name)
                else:
                    var_addr, write_initializer = self._prepare_file_scope_object(
                        node.name,
                        var_ir_type,
                        storage=node.storage,
                        has_initializer=node.init is not None,
                    )
                    if var_addr is None:
                        return None, None
                self.define(node.name, (var_ir_type, var_addr))

            if self._has_unsigned_scalar_pointee(node.type):
                self._mark_unsigned_pointee(var_addr)

            if self._has_unsigned_scalar_pointee(node.type):
                self._mark_unsigned_pointee(var_addr)

            # Handle array initialization: int a[3] = {1, 2, 3}; or
            # char s[] = "hi"; or const char *names[] = {"a", helper};
            if node.init is not None:
                if self.in_global:
                    if write_initializer:
                        try:
                            const_init = self._build_const_init(node.init, var_ir_type)
                            str(const_init)
                            var_addr.initializer = const_init
                        except Exception as exc:
                            raise ValueError(
                                "global array initializer could not be lowered as "
                                "a compile-time constant"
                            ) from exc
                elif isinstance(node.init, c_ast.InitList):
                    self._safe_store(self._zero_initializer(var_ir_type), var_addr)
                    self._init_runtime_value(var_addr, var_ir_type, node.init)
                elif self._is_array_string_initializer(node.init, var_ir_type):
                    self._safe_store(self._zero_initializer(var_ir_type), var_addr)
                    data = self._string_literal_data(node.init)
                    idx0 = ir.Constant(int64_t, 0)
                    for i, value in enumerate(data[: var_ir_type.count]):
                        elem_ptr = self.builder.gep(
                            var_addr,
                            [idx0, ir.Constant(int64_t, i)],
                            inbounds=True,
                        )
                        self.builder.store(ir.Constant(elem_ir_type, value), elem_ptr)
            elif self.in_global and write_initializer:
                var_addr.initializer = self._zero_initializer(var_ir_type)

        elif isinstance(node.type, c_ast.PtrDecl):

            point_level = 1
            sub_node = node.type
            resolved_pointee_type = None
            write_initializer = True

            while True:
                sub_next_type = sub_node.type
                if isinstance(sub_next_type, c_ast.TypeDecl):
                    if isinstance(sub_next_type.type, c_ast.Struct):
                        # pointer to struct: struct { int x; } *p
                        resolved_pointee_type = self.codegen_Struct(sub_next_type.type)
                        type_str = "struct"
                    elif isinstance(sub_next_type.type, c_ast.Union):
                        resolved_pointee_type = self.codegen_Union(sub_next_type.type)
                        type_str = "union"
                    elif isinstance(sub_next_type.type, c_ast.Enum):
                        self.codegen_Enum(sub_next_type.type)
                        resolved_pointee_type = int32_t
                        type_str = "int"
                    else:
                        type_str = sub_next_type.type.names
                        resolved = self._get_ir_type(type_str)
                        if isinstance(resolved, ir.Type):
                            resolved_pointee_type = resolved
                        if _is_struct_ir_type(resolved):
                            type_str = "struct"
                    break
                elif isinstance(sub_next_type, c_ast.PtrDecl):
                    point_level += 1
                    sub_node = sub_next_type
                    continue
                elif isinstance(sub_next_type, c_ast.ArrayDecl):
                    resolved_pointee_type = self._build_array_ir_type(sub_next_type)
                    type_str = "array"
                    break
                elif isinstance(sub_next_type, c_ast.FuncDecl):
                    # Function pointer: int (*fp)(int, int)
                    func_ir_type = self._build_func_ptr_type(sub_next_type)
                    if not self.in_global:
                        var_addr = self._alloca_in_entry(func_ir_type, node.name)
                        self.define(node.name, (func_ir_type, var_addr))
                    else:
                        var_addr, write_initializer = self._prepare_file_scope_object(
                            node.name,
                            func_ir_type,
                            storage=node.storage,
                            has_initializer=node.init is not None,
                        )
                        if var_addr is None:
                            return None, None
                    if self._func_decl_returns_unsigned(sub_next_type):
                        self._mark_unsigned_return(var_addr)
                    if node.init is not None:
                        init_val, _ = self.codegen(node.init)
                        # For global scope, set as initializer directly
                        if self.in_global and isinstance(var_addr, ir.GlobalVariable):
                            # NULL (i64 0) → null pointer of correct type
                            if (isinstance(init_val.type, ir.IntType)
                                    and isinstance(init_val, ir.Constant)
                                    and self._constant_raw_value(init_val) == 0):
                                init_val = ir.Constant(
                                    var_addr.value_type, None
                                )
                            var_addr.initializer = init_val
                        else:
                            if (
                                isinstance(init_val, ir.Constant)
                                and isinstance(init_val.type, ir.IntType)
                                and self._constant_raw_value(init_val) == 0
                            ):
                                init_val = ir.Constant(func_ir_type, None)
                            elif init_val.type != func_ir_type:
                                init_val = self._implicit_convert(
                                    init_val, func_ir_type
                                )
                            self._safe_store(init_val, var_addr)
                    return None, var_addr
                pass

            if resolved_pointee_type is not None:
                ir_type = resolved_pointee_type
                if isinstance(ir_type, ir.VoidType):
                    ir_type = int8_t
                for _ in range(point_level):
                    ir_type = ir.PointerType(ir_type)
                if not self.in_global:
                    var_addr = self._alloca_in_entry(ir_type, node.name)
                    self.define(node.name, (ir_type, var_addr))
                else:
                    var_addr, write_initializer = self._prepare_file_scope_object(
                        node.name,
                        ir_type,
                        storage=node.storage,
                        has_initializer=node.init is not None,
                    )
                    if var_addr is None:
                        return None, None
                var_ir_type = ir_type
            else:
                if self.in_global:
                    pointee_ir_type = self._get_ir_type(type_str)
                    if isinstance(pointee_ir_type, ir.VoidType):
                        pointee_ir_type = int8_t
                    for _ in range(point_level):
                        pointee_ir_type = ir.PointerType(pointee_ir_type)
                    var_ir_type = pointee_ir_type
                    var_addr, write_initializer = self._prepare_file_scope_object(
                        node.name,
                        var_ir_type,
                        storage=node.storage,
                        has_initializer=node.init is not None,
                    )
                    if var_addr is None:
                        return None, None
                else:
                    var_addr, var_ir_type = self.create_entry_block_alloca(
                        node.name,
                        type_str,
                        1,
                        point_level=point_level,
                        storage=node.storage,
                    )

            if self._has_unsigned_scalar_pointee(node.type):
                self._mark_unsigned_pointee(var_addr)

            if node.init is not None:
                if self.in_global:
                    if write_initializer:
                        try:
                            const_init = self._build_const_init(node.init, var_ir_type)
                            str(const_init)
                            var_addr.initializer = const_init
                        except Exception:
                            var_addr.initializer = ir.Constant(var_ir_type, None)
                else:
                    init_val, _ = self.codegen(node.init)
                    init_val = self._decay_array_expr_to_pointer(
                        node.init, init_val, f"{node.name}.initdecay"
                    )
                    if isinstance(init_val.type, ir.ArrayType) and isinstance(
                        var_ir_type, ir.PointerType
                    ):
                        init_val = self._implicit_convert(init_val, var_ir_type)
                    elif init_val.type != var_ir_type:
                        init_val = self._implicit_convert(init_val, var_ir_type)
                    self._safe_store(init_val, var_addr)
        else:
            return None, None

        return None, var_addr
