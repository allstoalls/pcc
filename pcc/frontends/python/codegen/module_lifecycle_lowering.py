"""Module lifecycle lowering for L1CodeGen."""

from __future__ import annotations

import os

from pcc.ir.compat import ir
from pcc.backend.precise_stackmap import (
    PRECISE_STACKMAP_TABLE_SYMBOL,
    precise_stackmap_tables_enabled,
    precise_stackmap_target_publishes_tables,
)
from pcc.frontends.python.pipeline_dependency_closure import (
    _native_stdlib_root_for_path,
)

from pcc.frontends.python.py_ast import Call, ExprStmt, Name, Stmt
from pcc.frontends.python.codegen.generator_lowering import funcdef_has_source_yield
from pcc.frontends.python.codegen.runtime_abi import declare_runtime_global
from pcc.frontends.python.codegen.native_os import (
    NATIVE_OS_OWNED_CALLABLES,
    NATIVE_OS_DESCRIPTOR_CONSTANTS,
    native_os_descriptor_constant,
)

_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_VOID = ir.VoidType()
_CSTR = _I8.as_pointer()


class ModuleLifecycleLoweringMixin:
    def _emit_module_docstring_binding(self) -> None:
        """Publish the module's finite import identity and ``__doc__``."""
        source_filename = getattr(self, "_module_source_path", "") or ""
        if source_filename:
            source_filename = os.path.abspath(source_filename)
        else:
            for stmt in self.ast_module.body:
                span = getattr(stmt, "span", None)
                filename = getattr(span, "file", "")
                if filename and not filename.startswith("<"):
                    source_filename = os.path.abspath(filename)
                    break
        if source_filename == "":
            source_filename = (self.ast_module.name or "pcc_py_module") + ".py"
        storage_module_name = self.ast_module.name or "__main__"
        module_name = storage_module_name if self._skip_program_main else "__main__"
        if module_name == "__main__":
            package_name = ""
        elif os.path.basename(source_filename) == "__init__.py":
            package_name = module_name
        elif "." in module_name:
            package_name = module_name.rsplit(".", 1)[0]
        else:
            package_name = ""
        for attr_name, text in (
            ("__name__", module_name),
            ("__package__", package_name),
            ("__file__", source_filename),
        ):
            value = self._emit_str_literal(text)
            self._publish_module_scope_import_binding(attr_name, value)
            self._gc_release(value)
        docstring = self.ast_module.docstring
        if docstring is None:
            value = self._emit_none_literal()
        else:
            value = self._emit_str_literal(docstring)
        self._publish_module_scope_import_binding("__doc__", value)
        self._gc_release(value)

    def _emit_owned_stdlib_module_bindings(self) -> None:
        """Publish real owned API values in the compiled standard port object.

        Importers with a closed module graph read this namespace. Native
        attribute shortcuts alone cannot satisfy from-import or module values.
        Only the compiler's actual os port receives these definitions; a user
        module called os keeps its own namespace and initialization behavior.
        """
        if self.ast_module.name != "os":
            return
        source = getattr(self, "_module_source_path", "") or ""
        if not source:
            return
        # Use source-discovery provenance. Compiled bootstrap codegen modules
        # have synthetic __file__ values, and configured roots can relocate
        # the actual provider. Retained-AST workers restore this input path.
        provider_root = _native_stdlib_root_for_path(source)
        if provider_root is None:
            return
        if os.path.abspath(source) != os.path.join(provider_root, "os.py"):
            return
        module_name = self._pooled_cstr_ptr("os", ".owned.module.os")
        members = [(name, producer, None) for name, producer in NATIVE_OS_OWNED_CALLABLES]
        for name in NATIVE_OS_DESCRIPTOR_CONSTANTS:
            value = native_os_descriptor_constant(name, self._target_sys_platform_text())
            if value is not None:
                members.append((name, "py_int_from_i64", value))
        for name, producer, constant in members:
            output = self._new_slot_call_root("owned.module.member." + name)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                arguments = () if constant is None else (ir.Constant(ir.IntType(64), constant),)
                self._slot_call_runtime_call(producer, (), result_slot=output, suffix_args=arguments)
                status = self._slot_call_runtime_call(
                    "py_module_attr_set", (output,),
                    suffix_args=(module_name, self._attr_name_ptr(name)),
                    argument_order=(1, 2, 0),
                )
                self._slot_call_check_status(status, "module member publication")
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
            self._release_slot_call_roots((output,))

    def _emit_module_teardown(self) -> None:
        """Emit a per-module function that clears object globals."""
        fn_name = self._module_teardown_name()
        existing = self.module.globals.get(fn_name)
        if existing is not None:
            if existing.blocks:
                return
            fn = existing
        else:
            fnty = ir.FunctionType(_VOID, [])
            fn = ir.Function(self.module, fnty, name=fn_name)
            fn.linkage = "external"
        entry = fn.append_basic_block("entry")
        body_bb = fn.append_basic_block("body")
        done_bb = fn.append_basic_block("done")

        saved_builder = self.builder
        saved_fn = self.current_function
        saved_fd = self.current_func_def
        setattr(self, "builder", ir.IRBuilder(entry))
        setattr(self, "current_function", fn)
        setattr(self, "current_func_def", None)

        guard_name = f".pcc.module.fini.{self._module_symbol_suffix()}"
        guard = self.module.globals.get(guard_name)
        if not isinstance(guard, ir.GlobalVariable):
            guard = ir.GlobalVariable(self.module, _I32, name=guard_name)
            guard.linkage = "internal"
            guard.initializer = ir.Constant(_I32, 0)
        seen = self.builder.load(guard, name=self._fresh("mod.fini.seen"))
        already = self.builder.icmp_signed(
            "!=",
            seen,
            ir.Constant(_I32, 0),
            name=self._fresh("mod.fini.done"),
        )
        self.builder.cbranch(already, done_bb, body_bb)

        self.builder.position_at_end(body_bb)
        self.builder.store(ir.Constant(_I32, 1), guard)
        module_name_ptr = self._pooled_cstr_ptr(
            self.ast_module.name or "__main__",
            ".pcc.module.fini.name",
        )
        for _name, item in reversed(list(self._module_globals.items())):
            gv, declared_ty = item
            if self._is_valueclass_payload_type(declared_ty):
                self._clear_module_global_valueclass_payload_roots(gv, declared_ty)
                continue
            if not self._module_global_needs_teardown(gv, declared_ty):
                continue
            # Executed module assignments are also retained by the module
            # attribute dictionary.  Drop that duplicate owner before the
            # LLVM global root so a last-reference __del__ runs while imports
            # and the rest of the module namespace are still available.
            self.builder.call(
                self.runtime["py_module_attr_del"],
                [module_name_ptr, self._attr_name_ptr(_name)],
                name=self._fresh("mod.fini.attr.del"),
            )
            if self._cpy_module_flags.get(_name, False):
                value = self.builder.load(
                    gv,
                    name=self._fresh("mod.fini.cpy.value"),
                )
                self.builder.store(ir.Constant(value.type, None), gv)
                self.builder.call(self.runtime["py_cpy_decref"], [value])
                continue
            value = self.builder.load(
                gv,
                name=self._fresh("mod.fini.value"),
            )
            self._gc_unpin(value)
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [
                    self._as_gc_ptr(gv, name=self._fresh("mod.fini.slot")),
                    ir.Constant(_CSTR, None),
                ],
            )
        for gv in reversed(
            list(getattr(self, "_native_module_attr_globals", {}).values())
        ):
            value = self.builder.load(
                gv,
                name=self._fresh("mod.attr.fini.value"),
            )
            self._gc_unpin(value)
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [
                    self._as_gc_ptr(gv, name=self._fresh("mod.attr.fini.slot")),
                    ir.Constant(_CSTR, None),
                ],
            )
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        self.builder.ret_void()

        setattr(self, "builder", saved_builder)
        setattr(self, "current_function", saved_fn)
        setattr(self, "current_func_def", saved_fd)

    def _emit_module_top_init(self, body: list["Stmt"]) -> None:
        """Emit ``void _pcc_py_module_top_<mod>()`` holding the
        module-level statements. Used when this compilation unit is a
        secondary module in a multi-file compile — the entry module's
        ``@main`` must call this before its own top-level body."""
        mod_name = self.ast_module.name or "mod"
        sanitised = self._module_symbol_suffix(mod_name)
        fnty = ir.FunctionType(_VOID, [])
        fn = ir.Function(
            self.module,
            fnty,
            name=f"_pcc_py_module_top_{sanitised}",
        )
        fn.linkage = "external"
        entry = fn.append_basic_block("entry")
        body_bb = fn.append_basic_block("body")
        done_bb = fn.append_basic_block("done")
        saved_builder = self.builder
        saved_fn = self.current_function
        saved_fd = self.current_func_def
        saved_env = self.env
        saved_loops = self.loop_stack
        saved_box_int_locals = self._box_int_locals
        saved_exact_int_flags = self._exact_int_env_flags
        setattr(self, "builder", ir.IRBuilder(entry))
        setattr(self, "current_function", fn)
        setattr(self, "current_func_def", None)
        setattr(self, "env", {})
        setattr(self, "_box_int_locals", self._should_box_python_ints())
        setattr(self, "_exact_int_env_flags", {})
        setattr(self, "loop_stack", [])
        self._emit_thread_safepoint()

        guard_name = f".pcc.module.init.{sanitised}"
        guard = self.module.globals.get(guard_name)
        if not isinstance(guard, ir.GlobalVariable):
            guard = ir.GlobalVariable(self.module, _I32, name=guard_name)
            guard.linkage = "internal"
            guard.initializer = ir.Constant(_I32, 0)
        seen = self.builder.load(guard, name=self._fresh("mod.init.seen"))
        already = self.builder.icmp_signed(
            "!=",
            seen,
            ir.Constant(_I32, 0),
            name=self._fresh("mod.init.done"),
        )
        self.builder.cbranch(already, done_bb, body_bb)

        self.builder.position_at_end(body_bb)
        self.builder.store(ir.Constant(_I32, 1), guard)
        self._emit_static_literal_init_call()
        self._emit_module_root_enters()
        self._emit_module_docstring_binding()

        # ``@main`` and ``_pcc_py_module_top_*`` are IR functions like any other,
        # so they need the same per-function reset the user-function prologue
        # does.  Without it, module-level statements inherit whatever
        # ``_try_err_block`` / ``_active_handler_excs`` the LAST lowered
        # function left behind -- and the user-function prologue documents why
        # that is invalid: "an exception handler cannot span a Python function
        # boundary ... a retained IR value from an outer/nested emission is
        # invalid in fn".  A module-level ``try`` then branched into another
        # function's blocks, which is why pcc1 could not compile a nine-line
        # module-level try/except while the same construct inside a function was
        # fine.  It also explains the host/pcc1 split: whether the carried-over
        # block happened to be None depended on which function was lowered last.
        self._try_err_block = None
        self._cpy_operand_cleanup_block = None
        self._active_handler_excs = []
        self._emit_owned_stdlib_module_bindings()
        self._emit_stmts(tuple(body))

        if not self._builder_block_is_terminated():
            # Make initialized sibling-module globals and classes visible to
            # the public C-API import bridge. This is the native equivalent of
            # publishing a module namespace after executing its top level;
            # extensions using PyImport_ImportModule must see the same compiled
            # modules that direct pcc imports already resolved.
            self._emit_globals_builtin()
            self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        if not self._builder_block_is_terminated():
            self._emit_owned_local_cleanup()
            self.builder.ret_void()

        setattr(self, "builder", saved_builder)
        setattr(self, "current_function", saved_fn)
        setattr(self, "current_func_def", saved_fd)
        setattr(self, "env", saved_env)
        setattr(self, "_box_int_locals", saved_box_int_locals)
        setattr(self, "_exact_int_env_flags", saved_exact_int_flags)
        setattr(self, "loop_stack", saved_loops)


    def _is_user_main_call_stmt(self, stmt) -> bool:
        """Zero-arg module-level ``main()`` expression statement (the
        user's own trailing call).  The self-backend entry skips it and
        instead calls the function directly at the end so its return
        value can become the process exit code."""
        if not isinstance(stmt, ExprStmt):
            return False
        expr = getattr(stmt, "expr", None)
        if expr is None:
            expr = getattr(stmt, "value", None)
        if not isinstance(expr, Call):
            return False
        if getattr(expr, "args", ()) or getattr(expr, "kwargs", ()):
            return False
        func = getattr(expr, "func", None)
        return isinstance(func, Name) and getattr(func, "ident", None) == "main"

    def _emit_trailing_main_exit_code(self, stmt) -> ir.Value:
        """Invoke the user's trailing module-level ``main()`` and turn
        its return value into the process exit code (self-backend pcc
        convention): ``None`` -> 0, int -> the value. Falls back to
        plain statement emission (exit 0) when a zero-arg direct call
        cannot be formed."""
        fn = self.functions.get("main")
        if not isinstance(fn, ir.Function) or len(fn.args) != 0:
            self._emit_stmts((stmt,))
            return ir.Constant(_I32, 0)
        ast_func_def = self._find_user_funcdef("main")
        if ast_func_def.is_async or funcdef_has_source_yield(ast_func_def):
            # A source generator/coroutine call constructs its object. The
            # trailing-main exit convention must neither drive that object
            # nor treat it as an integer return value.
            self._emit_stmts((stmt,))
            return ir.Constant(_I32, 0)
        # Use the ordinary call boundary. Effect analysis can give a regular
        # Python main() the resumable ABI when it reaches Event.wait() etc.
        # Expression lowering drives that child for a synchronous caller and
        # preserves call-result roots/errors. A raw builder.call would return
        # the generator without executing main's body. Its declared IR return
        # type is then also the wrong type for the eventual exit value.
        ret_val = self._emit_expr(stmt.expr)
        ret_ty = ret_val.type
        if isinstance(ret_ty, ir.VoidType):
            return ir.Constant(_I32, 0)
        if isinstance(ret_ty, ir.IntType):
            if ret_ty.width == 32:
                return ret_val
            if ret_ty.width > 32:
                return self.builder.trunc(
                    ret_val, _I32, name=self._fresh("user.main.exit")
                )
            return self.builder.zext(
                ret_val, _I32, name=self._fresh("user.main.exit")
            )
        if not isinstance(ret_ty, ir.PointerType):
            return ir.Constant(_I32, 0)
        # Boxed object return: None -> 0, otherwise unbox as int.
        none_gv = declare_runtime_global(self.module, "py_None")
        none_val = self.builder.load(none_gv, name=self._fresh("user.main.none"))
        is_none = self.builder.icmp_unsigned(
            "==", ret_val, none_val, name=self._fresh("user.main.isnone")
        )
        code_slot = self.builder.alloca(_I32, name=self._fresh("user.main.code"))
        self.builder.store(ir.Constant(_I32, 0), code_slot)
        bb_unbox = self.current_function.append_basic_block("user.main.unbox")
        bb_done = self.current_function.append_basic_block("user.main.done")
        self.builder.cbranch(is_none, bb_done, bb_unbox)
        self.builder.position_at_end(bb_unbox)
        ov_slot = self.builder.alloca(_I32, name=self._fresh("user.main.ov"))
        self.builder.store(ir.Constant(_I32, 0), ov_slot)
        as_i64 = self.builder.call(
            self.runtime["py_int_to_i64"],
            [ret_val, ov_slot],
            name=self._fresh("user.main.i64"),
        )
        self._emit_post_call_err_check(stmt.span)
        as_i32 = self.builder.trunc(
            as_i64, _I32, name=self._fresh("user.main.exit")
        )
        self.builder.store(as_i32, code_slot)
        self.builder.branch(bb_done)
        self.builder.position_at_end(bb_done)
        self._gc_release(ret_val)
        return self.builder.load(
            code_slot, name=self._fresh("user.main.code.load")
        )

    def _emit_program_main(self, body: list["Stmt"]) -> None:
        """Synthesize ``i32 @main(i32 argc, i8** argv)`` holding
        module-level statements.

        Emits each queued module-level statement; a ``class`` statement
        creates its class where it executes. Returns 0.
        """
        if self.module.globals.get("main") is not None:
            # User provided a C-style ``main`` function already; leave
            # it alone. This is a pcc-py convention for hand-written
            # entry points.
            return
        fnty = ir.FunctionType(_I32, [_I32, _CSTR.as_pointer()])
        fn = ir.Function(self.module, fnty, name="main")
        entry = fn.append_basic_block("entry")
        saved_builder = self.builder
        saved_fn = self.current_function
        saved_fd = self.current_func_def
        saved_env = self.env
        saved_loops = self.loop_stack
        saved_box_int_locals = self._box_int_locals
        saved_exact_int_flags = self._exact_int_env_flags
        setattr(self, "builder", ir.IRBuilder(entry))
        setattr(self, "current_function", fn)
        setattr(self, "current_func_def", None)
        setattr(self, "env", {})
        setattr(self, "_box_int_locals", self._should_box_python_ints())
        setattr(self, "_exact_int_env_flags", {})
        setattr(self, "loop_stack", [])
        triple = getattr(self, "_target_triple", "") or ""
        if not triple:
            from pcc.frontends.python.pipeline_targets import host_target_triple
            triple = host_target_triple()
        if precise_stackmap_tables_enabled(triple) and precise_stackmap_target_publishes_tables(triple):
            # The Mach-O executable link defines this name at the start of the
            # linked table (ELF and PE links do not yet); the GC stack walk
            # finds the table through the pointer published here. It is
            # main's first instruction: every mapped frame then enters and
            # leaves under one walk state (entry-hoisted enters follow it).
            table = self.module.globals.get(PRECISE_STACKMAP_TABLE_SYMBOL)
            if table is None:
                table = ir.GlobalVariable(
                    self.module, ir.IntType(8), name=PRECISE_STACKMAP_TABLE_SYMBOL,
                )
                table.linkage = "external"
                table.global_constant = True
            self.builder.call(
                self.runtime["pcc_gc_register_precise_stackmap_table"],
                [table],
            )
        self._emit_thread_safepoint()

        self.builder.call(
            self.runtime["py_set_program_args"],
            [fn.args[0], fn.args[1]],
        )
        if self.emit_cpy_main_exitcode:
            # Establish CPython on the process main thread before any module
            # statement can launch a worker whose first action enters the
            # libpython bridge.  The per-function emission guard keeps later
            # imports in this generated main from adding a duplicate call.
            self._ensure_cpy_init()

        sibling_init_functions = []
        for sibling_mod in self._sibling_module_inits:
            sanitised_sib = self._module_symbol_suffix(sibling_mod)
            sib_top = f"_pcc_py_module_top_{sanitised_sib}"
            existing = self.module.globals.get(sib_top)
            if existing is None:
                sib_fn = ir.Function(
                    self.module,
                    ir.FunctionType(_VOID, []),
                    name=sib_top,
                )
                sib_fn.linkage = "external"
            else:
                sib_fn = existing
            sibling_init_functions.append((sibling_mod, sib_fn))

        # Register every compiled sibling before executing any of them. A C
        # extension may import a sibling that is later in the eager order; the
        # runtime can now execute that guarded top-init on demand.
        for sibling_mod, sib_fn in sibling_init_functions:
            mod_name_ptr = self._ptr_to_cstr(
                self._cstr_global(
                    sibling_mod,
                    f".pcc.compiled.init.name.{sibling_mod}",
                )
            )
            init_ptr = sib_fn
            if init_ptr.type != _CSTR:
                init_ptr = self.builder.bitcast(
                    init_ptr,
                    _CSTR,
                    name=self._fresh("compiled.init.fn"),
                )
            self.builder.call(
                self.runtime["py_compiled_module_register_init"],
                [mod_name_ptr, init_ptr],
            )

        # Sibling top-level code runs when an import statement reaches it.
        # Eager dependency-first execution cannot preserve a package's partial
        # state across cycles (parent setup; import child; child reads parent).
        self._emit_static_literal_init_call()
        self._emit_module_root_enters()
        self._emit_module_docstring_binding()
        user_body = list(body)
        trailing_main_stmt = None
        if (
            not self.emit_cpy_main_exitcode
            and user_body
            and self._is_user_main_call_stmt(user_body[-1])
        ):
            trailing_main_stmt = user_body.pop()
        # ``@main`` and ``_pcc_py_module_top_*`` are IR functions like any other,
        # so they need the same per-function reset the user-function prologue
        # does.  Without it, module-level statements inherit whatever
        # ``_try_err_block`` / ``_active_handler_excs`` the LAST lowered
        # function left behind -- and the user-function prologue documents why
        # that is invalid: "an exception handler cannot span a Python function
        # boundary ... a retained IR value from an outer/nested emission is
        # invalid in fn".  A module-level ``try`` then branched into another
        # function's blocks, which is why pcc1 could not compile a nine-line
        # module-level try/except while the same construct inside a function was
        # fine.  It also explains the host/pcc1 split: whether the carried-over
        # block happened to be None depended on which function was lowered last.
        self._try_err_block = None
        self._cpy_operand_cleanup_block = None
        self._active_handler_excs = []
        self._emit_owned_stdlib_module_bindings()
        self._emit_stmts(tuple(user_body))

        if not self._builder_block_is_terminated():
            if self.emit_cpy_main_exitcode:
                exit_code = self.builder.call(
                    self.runtime["py_cpy_main_exitcode"],
                    [],
                    name=self._fresh("cpy.exitcode"),
                )
            elif trailing_main_stmt is not None:
                exit_code = self._emit_trailing_main_exit_code(
                    trailing_main_stmt
                )
            else:
                exit_code = ir.Constant(_I32, 0)
            self._emit_module_teardown_call(self.module.name or "mod")
            for sibling_mod in reversed(self._sibling_module_inits):
                self._emit_module_teardown_call(sibling_mod)
            self._emit_owned_local_cleanup()
            self.builder.ret(exit_code)

        setattr(self, "builder", saved_builder)
        setattr(self, "current_function", saved_fn)
        setattr(self, "current_func_def", saved_fd)
        setattr(self, "env", saved_env)
        setattr(self, "_box_int_locals", saved_box_int_locals)
        setattr(self, "_exact_int_env_flags", saved_exact_int_flags)
        setattr(self, "loop_stack", saved_loops)
