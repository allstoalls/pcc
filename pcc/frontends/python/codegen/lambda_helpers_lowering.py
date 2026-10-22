"""Lambda helper lowering for L1CodeGen.

State-swapping lambda wrapping stays host-owned in layer1.py; this
module only contains helper routines that do not assign to self fields.
"""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, BoolType, Call, DictType, DynType, FloatType, IntType, Lambda, ListType, Name, NoneType, SourceSpan, StrType, TupleExpr, TupleType, Type
from pcc.frontends.python.codegen.layer1_support import _dataclass_field_names, _dataclass_field_value
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.hoist_boxing import CELL_CAPTURE, CELL_READ, CELL_UNBOUND

_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()


_PY_BUILTINS_NS = (
    CELL_CAPTURE,
    CELL_READ,
    CELL_UNBOUND,
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "BaseException",
    "BaseExceptionGroup",
    "BlockingIOError",
    "BrokenPipeError",
    "BufferError",
    "BytesWarning",
    "ChildProcessError",
    "ConnectionAbortedError",
    "ConnectionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "DeprecationWarning",
    "EOFError",
    "Ellipsis",
    "EncodingWarning",
    "EnvironmentError",
    "Exception",
    "ExceptionGroup",
    "False",
    "FileExistsError",
    "FileNotFoundError",
    "FloatingPointError",
    "FutureWarning",
    "GeneratorExit",
    "IOError",
    "ImportError",
    "ImportWarning",
    "IndentationError",
    "IndexError",
    "InterruptedError",
    "IsADirectoryError",
    "KeyError",
    "KeyboardInterrupt",
    "LookupError",
    "MemoryError",
    "ModuleNotFoundError",
    "NameError",
    "None",
    "NotADirectoryError",
    "NotImplemented",
    "NotImplementedError",
    "OSError",
    "OverflowError",
    "PendingDeprecationWarning",
    "PermissionError",
    "ProcessLookupError",
    "PythonFinalizationError",
    "RecursionError",
    "ReferenceError",
    "ResourceWarning",
    "RuntimeError",
    "RuntimeWarning",
    "StopAsyncIteration",
    "StopIteration",
    "SyntaxError",
    "SyntaxWarning",
    "SystemError",
    "SystemExit",
    "TabError",
    "TimeoutError",
    "True",
    "TypeError",
    "UnboundLocalError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "UnicodeError",
    "UnicodeTranslateError",
    "UnicodeWarning",
    "UserWarning",
    "ValueError",
    "Warning",
    "ZeroDivisionError",
    "_IncompleteInputError",
    "__build_class__",
    "__debug__",
    "__doc__",
    "__import__",
    "__loader__",
    "__name__",
    "__package__",
    "__spec__",
    "abs",
    "aiter",
    "all",
    "anext",
    "any",
    "ascii",
    "bin",
    "bool",
    "breakpoint",
    "bytearray",
    "bytes",
    "callable",
    "chr",
    "classmethod",
    "compile",
    "complex",
    "copyright",
    "credits",
    "delattr",
    "dict",
    "dir",
    "divmod",
    "enumerate",
    "eval",
    "exec",
    "exit",
    "filter",
    "float",
    "format",
    "frozenset",
    "getattr",
    "globals",
    "hasattr",
    "hash",
    "help",
    "hex",
    "id",
    "input",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "license",
    "list",
    "locals",
    "map",
    "max",
    "memoryview",
    "min",
    "next",
    "object",
    "oct",
    "open",
    "ord",
    "pow",
    "print",
    "property",
    "quit",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "setattr",
    "slice",
    "sorted",
    "staticmethod",
    "str",
    "sum",
    "super",
    "tuple",
    "type",
    "vars",
    "zip",
)


class LambdaHelperLoweringMixin:
    def _lambda_free_vars(
        self,
        expr: Lambda,
        param_names: list[str],
    ) -> set[str]:
        builtins_ns = _PY_BUILTINS_NS
        module_names = set(getattr(self, "_module_globals", {}).keys())
        module_names.update(self.functions.keys())
        module_names.update(getattr(self, "_hoist_wrap_caps", {}).keys())
        if hasattr(self, "class_lowering"):
            module_names.update(self.class_lowering.classes.keys())
        module_names.update(getattr(self, "_cpy_module_env", {}).keys())
        module_names.update(getattr(self, "_native_extension_module_env", {}).keys())
        module_names.update(getattr(self, "_native_module_aliases", {}).keys())
        module_names.update(getattr(self, "_native_module_object_aliases", {}).keys())
        module_names.update(getattr(self, "_native_builtin_module_aliases", {}).keys())
        module_names.update(getattr(self, "_native_builtin_value_aliases", {}).keys())

        lexical_shadow_names = set(getattr(self, "_lambda_lexical_shadow_names", set()))
        cur_fd = getattr(self, "current_func_def", None)
        if cur_fd is not None:
            for p in getattr(cur_fd, "params", ()) or ():
                pname = getattr(p, "name", "")
                if pname:
                    lexical_shadow_names.add(pname)
        current_fn = getattr(self, "current_function", None)
        class_lowering = getattr(self, "class_lowering", None)
        if current_fn is not None and class_lowering is not None:
            for _class_name, info in getattr(class_lowering, "classes", {}).items():
                matched_method_name = ""
                for method_name, method_fn in getattr(info, "methods", {}).items():
                    if method_fn is current_fn:
                        matched_method_name = method_name
                        break
                if matched_method_name == "":
                    continue
                for method_name, method_fd in getattr(info, "method_defs", ()):
                    if method_name != matched_method_name:
                        continue
                    for p in getattr(method_fd, "args", ()) or ():
                        pname = getattr(p, "name", "")
                        if pname:
                            lexical_shadow_names.add(pname)
                    break
                break

        def collect(e, bound: set[str], acc: set[str]) -> None:
            def add_target_names(target, target_bound: set[str]) -> None:
                if isinstance(target, Name):
                    target_bound.add(target.ident)
                    return
                if isinstance(target, TupleExpr):
                    for elem in target.elems:
                        add_target_names(elem, target_bound)

            if isinstance(e, Lambda):
                nested_bound = set(bound)
                for p in getattr(e, "params", ()) or ():
                    pname = getattr(p, "name", "")
                    if pname:
                        nested_bound.add(pname)
                collect(e.body, nested_bound, acc)
                return
            if (
                isinstance(e, Call)
                and isinstance(e.func, Name)
                and e.func.ident
                in (
                    "__listcomp__",
                    "_list_comp",
                    "_gen_comp",
                    "__genexpr__",
                    "__setcomp__",
                    "_set_comp",
                    "__dictcomp__",
                    "_dict_comp",
                )
            ):
                comp_bound = set(bound)
                if e.func.ident.startswith("__"):
                    if e.func.ident == "__dictcomp__" and len(e.args) >= 3:
                        body_exprs = e.args[:2]
                        gens = e.args[2]
                    elif len(e.args) >= 2:
                        body_exprs = e.args[:1]
                        gens = e.args[1]
                    else:
                        body_exprs = ()
                        gens = None
                    if isinstance(gens, TupleExpr):
                        for gen in gens.elems:
                            if isinstance(gen, TupleExpr) and len(gen.elems) >= 3:
                                target, iter_e, ifs_tuple = gen.elems[:3]
                                collect(iter_e, bound, acc)
                                add_target_names(target, comp_bound)
                                collect(ifs_tuple, comp_bound, acc)
                    for body_expr in body_exprs:
                        collect(body_expr, comp_bound, acc)
                    return
                body_exprs = e.args[:1] if e.args else ()
                gen_calls = e.args[1:] if len(e.args) > 1 else ()
                for gen_call in gen_calls:
                    if (
                        isinstance(gen_call, Call)
                        and isinstance(gen_call.func, Name)
                        and gen_call.func.ident == "_gen_clause"
                        and len(gen_call.args) == 3
                    ):
                        target, iter_e, ifs_tuple = gen_call.args
                        collect(iter_e, bound, acc)
                        add_target_names(target, comp_bound)
                        collect(ifs_tuple, comp_bound, acc)
                for body_expr in body_exprs:
                    collect(body_expr, comp_bound, acc)
                return
            if isinstance(e, Name):
                if e.ident in bound:
                    return
                # A local/parameter in the current emitting function must
                # shadow any module-level hoisted helper with the same name.
                # pproxy's HTTP CONNECT path has ``accept(...): async def
                # reply(...); return await self.http_accept(reply)`` and
                # ``http_accept(self, reply): return lambda writer:
                # reply(...)``.  The lambda must capture the ``reply``
                # parameter, not resolve the older hoisted ``__nested_reply``.
                if e.ident in self.env or e.ident in lexical_shadow_names:
                    acc.add(e.ident)
                    return
                if (
                    e.ident not in builtins_ns
                    and e.ident not in module_names
                    and e.ident
                    not in (
                        "True",
                        "False",
                        "None",
                        "...",
                        "*",
                        "__starred__",
                        "**",
                    )
                ):
                    acc.add(e.ident)
                return
            if isinstance(e, tuple):
                for it in e:
                    collect(it, bound, acc)
                return
            for slot in _dataclass_field_names(e):
                if slot in ("span", "ty"):
                    continue
                v = _dataclass_field_value(e, slot, None)
                if isinstance(v, tuple):
                    for it in v:
                        collect(it, bound, acc)
                elif v is not None and _dataclass_field_names(v):
                    collect(v, bound, acc)

        free_vars: set[str] = set()
        collect(expr.body, set(param_names), free_vars)
        return free_vars

    def _emit_lambda_adapter_name_root(self, expr, label):
        """Copy only an exact owning slot in the currently active adapter."""
        if not isinstance(expr, Name):
            return None
        scope = getattr(self, "_lambda_adapter_root_scope", None)
        if scope is None or scope[0] is not self.current_function:
            return None
        entry = self.env.get(expr.ident)
        if entry is None:
            return None
        for name, source in scope[1]:
            if name != expr.ident or entry[0] is not source:
                continue
            # The adapter registers each root before tuple_get and publishes
            # its NEW result immediately. Scope activation follows binding;
            # scope restoration precedes the balancing root cleanup.
            self._slot_call_root_record(source)
            output = self._new_slot_call_root(label)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cleanup = self._cpy_operand_cleanup_block
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                self._slot_call_copy_source(output, source, False, expr.span)
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cleanup
            return output
        return None

    def _maybe_emit_native_lambda_func(self, expr: Lambda) -> Optional[ir.Value]:
        """Lower a small no-closure lambda to a pcc-native function object."""
        arity = len(expr.params)
        if arity > 3:
            return None
        param_names = [p.name for p in expr.params]
        if any(name == "" for name in param_names):
            return None
        free_vars = self._lambda_free_vars(expr, param_names)
        # Recheck module-resolvable names at the native-lambda boundary.  The
        # recursive collector above is a nested closure; in a self-hosted
        # compiler its captured ``module_names`` set can conservatively report
        # module globals/functions as free.  Load those through the normal
        # module resolver instead of rejecting or capturing them as locals.
        module_names = set(getattr(self, "_module_globals", {}).keys())
        module_names.update(self.functions.keys())
        module_names.update(getattr(self, "_hoist_wrap_caps", {}).keys())
        if hasattr(self, "class_lowering"):
            module_names.update(self.class_lowering.classes.keys())
        module_names.update(getattr(self, "_cpy_module_env", {}).keys())
        module_names.update(
            getattr(self, "_native_extension_module_env", {}).keys()
        )
        module_names.update(getattr(self, "_native_module_aliases", {}).keys())
        module_names.update(
            getattr(self, "_native_module_object_aliases", {}).keys()
        )
        module_names.update(
            getattr(self, "_native_builtin_module_aliases", {}).keys()
        )
        module_names.update(
            getattr(self, "_native_builtin_value_aliases", {}).keys()
        )
        for fv in free_vars:
            if fv not in self.env and fv not in module_names:
                return None
        free_var_names = tuple(sorted(fv for fv in free_vars if fv in self.env))
        default_params = tuple(
            (i, p)
            for i, p in enumerate(expr.params)
            if getattr(p, "has_default", False)
            and getattr(p, "default", None) is not None
        )
        default_capture_index = {
            i: len(free_var_names) + default_i
            for default_i, (i, _p) in enumerate(default_params)
        }

        legacy_adapter = any(
            getattr(self, "_cpy_env_flags", {}).get(name, False)
            or self._is_valueclass_payload_type(self.env[name][2])
            for name in free_var_names
        ) or any(self._expr_looks_cpython(param.default) for _index, param in default_params)

        if not hasattr(self, "_native_lambda_func_counter"):
            self._native_lambda_func_counter = 0
        idx = self._native_lambda_func_counter
        self._native_lambda_func_counter += 1
        fn_name = (
            f"user_{self._module_symbol_suffix(self.ast_module.name or 'mod')}"
            f"__native_lambda_{idx}"
        )
        adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
        adapter = ir.Function(self.module, adapter_ty, name=fn_name)
        adapter.linkage = "internal"

        saved_builder = self.builder
        saved_env = self.env
        saved_env_class_hint = getattr(self, "env_class_hint", {})
        saved_env_class_object_hint = getattr(self, "env_class_object_hint", {})
        saved_cpy_env_flags = dict(getattr(self, "_cpy_env_flags", {}))
        saved_cpy_values = set(getattr(self, "_cpy_values", set()))
        saved_owned_cpy_values = set(getattr(self, "_owned_cpy_values", set()))
        saved_current_fn = self.current_function
        saved_current_fd = self.current_func_def
        saved_loops = getattr(self, "loop_stack", [])
        saved_entry_block = getattr(self, "_current_entry_block", None)
        saved_try_err_block = getattr(self, "_try_err_block", None)
        saved_cpy_operand_cleanup_block = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )
        # The lambda body is a separate function: its parameters must not
        # overwrite the enclosing function's per-local ownership flags.  A
        # ``lambda value=value: value`` inside ``for value in ...`` replaced
        # the loop variable's flag, so the enclosing exit cleanup found a
        # fresh never-set flag and leaked the last element.
        saved_ownership_state = _swap_in_fresh_ownership_state(self)
        saved_adapter_scope = getattr(self, "_lambda_adapter_root_scope", None)

        entry = adapter.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry)
        self.current_function = adapter
        self.current_func_def = None
        self.env = {}
        self.env_class_hint = {}
        self.env_class_object_hint = {}
        self._cpy_env_flags = {}
        self._cpy_values = set()
        self._owned_cpy_values = set()
        self.loop_stack = []
        self._try_err_block = None
        self._cpy_operand_cleanup_block = None
        # _alloca_in_entry targets _current_entry_block; without this switch
        # a comprehension inside the lambda body allocas its target slot in
        # the ENCLOSING function's entry (cross-function alloca reference ->
        # "self backend expected pointer value 'st.addr.N'").
        self._current_entry_block = entry

        if not legacy_adapter:
            # Every slot starts registered EMPTY before its tuple-get producer.
            # The return root outlives argument roots and keeps the callback's
            # result authoritative while argument finalizers run.
            return_slot = self._new_slot_call_root("lambda.return")
            error_target = self._ensure_fn_err_exit()
            roots = [return_slot]
            bindings = []
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
            self._cpy_operand_cleanup_block = self._try_err_block
            for index, name in enumerate(free_var_names):
                slot = self._new_slot_call_root("lambda.capture." + name)
                roots.append(slot)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
                self._cpy_operand_cleanup_block = self._try_err_block
                value = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [adapter.args[0], ir.Constant(_I64, index)],
                    name=self._fresh(name + ".cap"),
                )
                self._publish_slot_call_owned(slot, value, label="lambda captured argument")
                self._emit_post_call_err_check(expr.span)
                self.env[name] = (slot, _CSTR, DynType(name="dyn"))
                bindings.append((name, slot))

            args_len = self.builder.call(
                self.runtime["py_tuple_len"], [adapter.args[1]],
                name=self._fresh("lambda.args.len"),
            )
            for index, name in enumerate(param_names):
                slot = self._new_slot_call_root("lambda.parameter." + name)
                roots.append(slot)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if index in default_capture_index:
                    has_arg = self.builder.icmp_signed(
                        ">", args_len, ir.Constant(_I64, index),
                        name=self._fresh(name + ".has_arg"),
                    )
                    arg_bb = adapter.append_basic_block(name=self._fresh(name + ".arg"))
                    default_bb = adapter.append_basic_block(name=self._fresh(name + ".default"))
                    cont_bb = adapter.append_basic_block(name=self._fresh(name + ".cont"))
                    self.builder.cbranch(has_arg, arg_bb, default_bb)
                    self.builder.position_at_end(arg_bb)
                    value = self.builder.call(
                        self.runtime["py_tuple_get"], [adapter.args[1], ir.Constant(_I64, index)],
                        name=self._fresh(name + ".arg"),
                    )
                    self._publish_slot_call_owned(slot, value, label="lambda positional argument")
                    self._emit_post_call_err_check(expr.span)
                    self.builder.branch(cont_bb)
                    self.builder.position_at_end(default_bb)
                    value = self.builder.call(
                        self.runtime["py_tuple_get"],
                        [adapter.args[0], ir.Constant(_I64, default_capture_index[index])],
                        name=self._fresh(name + ".default"),
                    )
                    self._publish_slot_call_owned(slot, value, label="lambda default argument")
                    self._emit_post_call_err_check(expr.span)
                    self.builder.branch(cont_bb)
                    self.builder.position_at_end(cont_bb)
                else:
                    value = self.builder.call(
                        self.runtime["py_tuple_get"], [adapter.args[1], ir.Constant(_I64, index)],
                        name=self._fresh(name + ".arg"),
                    )
                    self._publish_slot_call_owned(slot, value, label="lambda positional argument")
                    self._emit_post_call_err_check(expr.span)
                self.env[name] = (slot, _CSTR, DynType(name="dyn"))
                bindings.append((name, slot))

            self._lambda_adapter_root_scope = (adapter, tuple(bindings))
            try:
                result_root = self._emit_slot_call_operand(expr.body, "lambda.body.result")
                roots.append(result_root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), error_target)
                self._cpy_operand_cleanup_block = self._try_err_block
                moved = self.builder.call(
                    self.runtime["pcc_gc_root_move"],
                    [self._as_gc_ptr(return_slot), self._as_gc_ptr(result_root)],
                    name=self._fresh("lambda.return.move"),
                )
                self._slot_call_check_status(moved, "lambda return owner move", expr.span)
                self._lambda_adapter_root_scope = saved_adapter_scope
                self._release_slot_call_roots(tuple(roots[1:]))
                self._try_err_block = self._slot_call_cleanup_block((return_slot,), error_target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self.builder.ret(self._take_slot_call_root(return_slot))
            except NotImplementedError:
                self.builder = saved_builder
                self.env = saved_env
                self.env_class_hint = saved_env_class_hint
                self.env_class_object_hint = saved_env_class_object_hint
                self._cpy_env_flags = saved_cpy_env_flags
                self._cpy_values = saved_cpy_values
                self._owned_cpy_values = saved_owned_cpy_values
                self.current_function = saved_current_fn
                self.current_func_def = saved_current_fd
                self.loop_stack = saved_loops
                self._current_entry_block = saved_entry_block
                self._try_err_block = saved_try_err_block
                self._cpy_operand_cleanup_block = saved_cpy_operand_cleanup_block
                self._lambda_adapter_root_scope = saved_adapter_scope
                _restore_ownership_state(self, saved_ownership_state)
                return None

        else:
            # Register the return root first so argument roots can leave in LIFO
            # order while the result survives their finalizers/relocation.
            return_slot = self._alloca_in_entry(
                _CSTR, name=self._fresh("lambda.return.root"), init_null=True,
            )
            return_ptr = self._as_gc_ptr(return_slot)
            self._emit_current_gc_frame_enter_lifo(self._gc_one_slot_frame_map(), return_slot)
            argument_roots = []
            for i, fv in enumerate(free_var_names):
                cap = self.builder.call(
                    self.runtime["py_tuple_get"],
                    [adapter.args[0], ir.Constant(_I64, i)],
                    name=self._fresh(f"{fv}.cap"),
                )
                slot = self._enter_container_temp_root(cap, self._fresh(f"{fv}.cap"))
                argument_roots.append((slot, True))
                self.env[fv] = (slot, _CSTR, DynType(name="dyn"))

            args_len = self.builder.call(
                self.runtime["py_tuple_len"],
                [adapter.args[1]],
                name=self._fresh("lambda.args.len"),
            )
            for i, pname in enumerate(param_names):
                slot = self.builder.alloca(_CSTR, name=f"{pname}.addr")
                if i in default_capture_index:
                    has_arg = self.builder.icmp_signed(
                        ">",
                        args_len,
                        ir.Constant(_I64, i),
                        name=self._fresh(f"{pname}.has_arg"),
                    )
                    arg_bb = adapter.append_basic_block(name=self._fresh(f"{pname}.arg"))
                    default_bb = adapter.append_basic_block(
                        name=self._fresh(f"{pname}.default")
                    )
                    cont_bb = adapter.append_basic_block(name=self._fresh(f"{pname}.cont"))
                    self.builder.cbranch(has_arg, arg_bb, default_bb)

                    self.builder.position_at_end(arg_bb)
                    obj = self.builder.call(
                        self.runtime["py_tuple_get"],
                        [adapter.args[1], ir.Constant(_I64, i)],
                        name=self._fresh(f"{pname}.arg"),
                    )
                    self.builder.store(obj, slot)
                    self.builder.branch(cont_bb)

                    self.builder.position_at_end(default_bb)
                    obj = self.builder.call(
                        self.runtime["py_tuple_get"],
                        [
                            adapter.args[0],
                            ir.Constant(_I64, default_capture_index[i]),
                        ],
                        name=self._fresh(f"{pname}.default"),
                    )
                    self.builder.store(obj, slot)
                    self.builder.branch(cont_bb)

                    self.builder.position_at_end(cont_bb)
                else:
                    obj = self.builder.call(
                        self.runtime["py_tuple_get"],
                        [adapter.args[1], ir.Constant(_I64, i)],
                        name=self._fresh(f"{pname}.arg"),
                    )
                    self.builder.store(obj, slot)
                # Both tuple-get arms return NEW references. Keep their common
                # value traced across arbitrary callback bodies and retire it on
                # normal return or exception, just like a named-function adapter.
                obj = self.builder.load(slot, name=self._fresh(f"{pname}.bound"))
                root = self._enter_container_temp_root(obj, self._fresh(f"{pname}.arg"))
                argument_roots.append((root, True))
                self.env[pname] = (root, _CSTR, DynType(name="dyn"))

            argument_lifetimes = tuple(argument_roots)
            self._try_err_block = self._make_cpy_operand_cleanup_block(
                (), (), self._ensure_fn_err_exit(), "lambda.arguments.error",
                rooted_pcc_lifetimes=((return_slot, False),) + argument_lifetimes,
            )
            self._cpy_operand_cleanup_block = self._try_err_block

            try:
                body_val = self._emit_expr(expr.body)
                if isinstance(getattr(body_val, "type", None), ir.VoidType):
                    result_obj = self._emit_none_literal()
                elif isinstance(getattr(body_val, "type", None), ir.PointerType):
                    result_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        body_val,
                        expr.body.ty,
                    )
                else:
                    result_obj = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        body_val,
                        expr.body.ty,
                    )
                result_owned = self._owned_release_needed(result_obj, expr.body) or (
                    self._container_store_temp_needs_release(
                        expr.body, expr.body.ty, False, result_obj,
                    )
                )
                if not result_owned:
                    # ``lambda value: value`` returns a borrowed argument. Give
                    # the caller its reference before retiring the argument root.
                    self._gc_retain(result_obj)
                self.builder.call(self.runtime["pcc_gc_store_root"], [return_ptr, result_obj])
                self._release_rooted_pcc_lifetimes(argument_lifetimes)
                result_obj = self._leave_return_cleanup_root(result_obj, return_slot, return_ptr)
                self.builder.ret(result_obj)
            except NotImplementedError:
                self.builder = saved_builder
                self.env = saved_env
                self.env_class_hint = saved_env_class_hint
                self.env_class_object_hint = saved_env_class_object_hint
                self._cpy_env_flags = saved_cpy_env_flags
                self._cpy_values = saved_cpy_values
                self._owned_cpy_values = saved_owned_cpy_values
                self.current_function = saved_current_fn
                self.current_func_def = saved_current_fd
                self.loop_stack = saved_loops
                self._current_entry_block = saved_entry_block
                self._try_err_block = saved_try_err_block
                self._cpy_operand_cleanup_block = saved_cpy_operand_cleanup_block
                _restore_ownership_state(self, saved_ownership_state)
                return None

        self.builder = saved_builder
        self.env = saved_env
        self.env_class_hint = saved_env_class_hint
        self.env_class_object_hint = saved_env_class_object_hint
        self._cpy_env_flags = saved_cpy_env_flags
        self._cpy_values = saved_cpy_values
        self._owned_cpy_values = saved_owned_cpy_values
        self.current_function = saved_current_fn
        self.current_func_def = saved_current_fd
        self.loop_stack = saved_loops
        self._current_entry_block = saved_entry_block
        self._try_err_block = saved_try_err_block
        self._cpy_operand_cleanup_block = saved_cpy_operand_cleanup_block
        self._lambda_adapter_root_scope = saved_adapter_scope
        _restore_ownership_state(self, saved_ownership_state)

        # Foreign Python values retain their existing explicit bridge path.
        # Ordinary native captures/defaults use the same authoritative slot
        # producers as other call operands, in their existing capture order.
        capture_exprs = tuple(
            Name(
                span=expr.span,
                ty=saved_env[name][2],
                ident=name,
            )
            for name in free_var_names
        )
        default_exprs = tuple(param.default for _index, param in default_params)
        legacy_bridge = any(saved_cpy_env_flags.get(name, False) for name in free_var_names)
        legacy_bridge = legacy_bridge or any(
            self._expr_looks_cpython(default) for default in default_exprs
        )
        legacy_bridge = legacy_bridge or any(
            self._is_valueclass_payload_type(capture.ty) for capture in capture_exprs
        )
        if not legacy_bridge:
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cleanup = self._cpy_operand_cleanup_block
            output = self._slot_call_result_sink(expr)
            owns_output = output is None
            if owns_output:
                output = self._new_slot_call_root("lambda.native.result")
            roots = [output] if owns_output else []
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                captures_root = self._new_slot_call_root("lambda.native.captures")
                roots.append(captures_root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                captures = self.builder.call(
                    self.runtime["py_tuple_new"],
                    [ir.Constant(_I64, len(capture_exprs) + len(default_exprs))],
                    name=self._fresh("lambda.native.captures.new"),
                )
                self._publish_slot_call_owned(captures_root, captures, label="lambda captures")
                self._emit_post_call_err_check(expr.span)
                self._guard_cpy_value_not_null(self.builder.load(captures_root))
                for index, capture in enumerate(capture_exprs + default_exprs):
                    item = self._emit_slot_call_operand(capture, "lambda.native.capture")
                    roots.append(item)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    self._slot_call_runtime_call(
                        "py_tuple_set_item", (captures_root, item),
                        suffix_args=(ir.Constant(_I64, index),),
                        argument_order=(0, 2, 1), span=expr.span,
                    )
                    self._release_slot_call_roots((item,))
                    roots.pop()
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_func_new", (captures_root,), result_slot=output,
                    suffix_args=(adapter,), argument_order=(1, 0), span=expr.span,
                )
                self._guard_cpy_value_not_null(self.builder.load(output))
                self._release_slot_call_roots((captures_root,))
                if owns_output:
                    self._try_err_block = self._slot_call_cleanup_block((output,), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    return self._take_slot_call_root(output)
                # The caller owns this already-published root; a borrowed load
                # only satisfies the expression interface, not a new handoff.
                return self.builder.load(output, name=self._fresh("lambda.native.rooted"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cleanup

        captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, len(free_var_names) + len(default_params))],
            name=self._fresh("lambda.native.captures"),
        )
        for i, fv in enumerate(free_var_names):
            cap_slot = saved_env.get(fv)
            cap_ty: Type = DynType(name="dyn")
            if cap_slot is not None:
                cap_ty = cap_slot[2]
            raw = self._emit_name(
                Name(span=expr.span, ty=cap_ty, ident=fv),
            )
            raw_is_cpy = raw in getattr(self, "_cpy_values", ())
            obj = self._emit_value_as_pcc_object_or_bridge(
                raw,
                cap_ty,
                "lambda.cap.bridge",
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [captures, ir.Constant(_I64, i), obj],
            )
            if raw_is_cpy:
                # py_tuple_set_item retains rather than steals; the bridge
                # result carries one pcc reference owned by this expression.
                self._gc_release(obj)
        for default_i, (_param_i, param) in enumerate(default_params):
            default_expr = param.default
            if default_expr is None:
                continue
            raw = self._emit_expr(default_expr)
            raw_is_cpy = raw in getattr(self, "_cpy_values", ())
            obj = self._emit_value_as_pcc_object_or_bridge(
                raw,
                default_expr.ty,
                "lambda.default.bridge",
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [
                    captures,
                    ir.Constant(_I64, len(free_var_names) + default_i),
                    obj,
                ],
            )
            if raw_is_cpy:
                self._gc_release(obj)
        fn_obj = self.builder.call(
            self.runtime["py_func_new"],
            [adapter, captures],
            name=self._fresh("lambda.native.func"),
        )
        self._gc_release(captures)
        # py_func_new returns a NEW reference, including when the lambda is
        # used directly as an argument rather than hoisted into a local def.
        self._note_owned_object_value(fn_obj)
        return fn_obj

    def _capture_value_as_cpython(self, name: str) -> Optional[ir.Value]:
        """Load ``name`` from the current outer scope as a CPython object.

        Captured values already tagged as CPython must stay in that object
        space; re-marshalling them through ``py_cpy_from_pcc_obj`` treats
        the CPython heap pointer as a pcc runtime object and can corrupt
        closure-wrapped lambdas / hoisted helpers.
        """

        def _native_capture_type(ty: Type) -> bool:
            return isinstance(
                ty,
                (
                    IntType,
                    BoolType,
                    FloatType,
                    StrType,
                    NoneType,
                    ListType,
                    DictType,
                    TupleType,
                ),
            )

        if name in self.env:
            slot, _ir_ty, ty = self.env[name]
            val = self.builder.load(slot, name=self._fresh(f"{name}.outer"))
            if (
                getattr(self, "_cpy_env_flags", {}).get(name, False)
                and isinstance(val.type, ir.PointerType)
                and not _native_capture_type(ty)
            ):
                # J2': inside a generator the slot may hold a CpyHandle
                # box — unbox before increfing the RAW foreign ref the
                # lambda capture contract expects.
                gen_stack = getattr(self, "_generator_ctx_stack", ())
                if len(gen_stack) > 0 and name in gen_stack[-1].get(
                    "cpy_boxed_names", ()
                ):
                    val = self.builder.call(
                        self.runtime["py_cpy_handle_get"],
                        [val],
                        name=self._fresh(f"{name}.cpy.unbox"),
                    )
                self.builder.call(self.runtime["py_cpy_incref"], [val])
                return val
            cpy_val, _ = self._marshal_to_cpython(val, ty)
            return cpy_val

        cap_span = None
        cur = getattr(self, "current_func_def", None)
        if cur is not None:
            cap_span = cur.span
        elif getattr(self.ast_module, "body", ()):
            cap_span = self.ast_module.body[0].span
        else:
            cap_span = SourceSpan(
                file=self.ast_module.name or "<generated>",
                line=1,
                col=1,
                end_line=1,
                end_col=1,
            )
        cap_name = Name(
            span=cap_span,
            ty=DynType(name="dyn"),
            ident=name,
        )
        val = self._emit_name(cap_name)
        if val in getattr(self, "_cpy_values", ()) and isinstance(
            val.type, ir.PointerType
        ):
            self.builder.call(self.runtime["py_cpy_incref"], [val])
            return val
        cpy_val, _ = self._marshal_to_cpython(val, cap_name.ty)
        return cpy_val

    def _maybe_emit_simple_lambda(self, expr) -> Optional[ir.Value]:
        """Lower a restricted set of lambdas to ``operator.attrgetter``
        / ``operator.itemgetter`` / ``operator.methodcaller`` CPython
        callables.

        Supported shapes (single-param lambda with body):
        - ``lambda x: x.a`` / ``lambda x: x.a.b`` → attrgetter
        - ``lambda x: x[N]`` (integer literal) → itemgetter(N)
        - ``lambda x: x[S]`` (string literal) → itemgetter(S)
        - ``lambda x: x.method()`` (no-arg method) → methodcaller

        Returns the CPython callable SSA value, tagged in
        ``_cpy_values``, or ``None`` if the lambda doesn't match
        a simple shape (caller then raises NotImplementedError).
        """
        if len(expr.params) != 1 or expr.params[0].name == "":
            return None
        param = expr.params[0].name
        body = expr.body
        dotted = self._lambda_attr_chain(body, param)
        if dotted is not None:
            return self._emit_operator_getter(
                "attrgetter",
                dotted,
            )
        idx = self._lambda_simple_subscript(body, param)
        if idx is not None:
            return self._emit_operator_getter(
                "itemgetter",
                idx,
            )
        method = self._lambda_method_call(body, param)
        if method is not None:
            return self._emit_operator_getter(
                "methodcaller",
                method,
            )
        return None

    def _lambda_method_call(self, expr, param_name):
        """If ``expr`` is ``Name(param).method()`` (no-arg method
        call), return the method name. Else None."""
        if not isinstance(expr, Call):
            return None
        if not isinstance(expr.func, Attr):
            return None
        if expr.args or expr.kwargs:
            return None
        if not (isinstance(expr.func.obj, Name) and expr.func.obj.ident == param_name):
            return None
        return expr.func.name

    def _lambda_attr_chain(self, expr, param_name):
        """If ``expr`` is ``Name(param)`` / ``Attr(Attr(Name(param)), ...)``,
        return the dotted attr chain (e.g. ``"a.b.c"``). Else None."""
        parts: list[str] = []
        cur = expr
        while isinstance(cur, Attr):
            parts.append(cur.name)
            cur = cur.obj
        if isinstance(cur, Name) and cur.ident == param_name and parts:
            return self._join_reversed_strs(parts)
        return None

    def _kwargs_are_only_keepends(self, kwargs: tuple) -> bool:
        for key, _value in kwargs:
            if key != "keepends":
                return False
        return True

    def _maybe_emit_lambda_wrap(self, expr) -> Optional[ir.Value]:
        """Emit a 1/2/3-arg lambda as a pcc FuncDef and wrap its
        function pointer as a CPython PyCFunction callable.

        Supports lambdas with no free variables (beyond builtins /
        module-level symbols). Each parameter is tagged as a CPython
        value, so attribute / method / subscript operations inside
        the body go through the CPython dispatch.
        Returns the CPython callable SSA value, or ``None`` if the
        lambda has >3 params, has free vars, or the body type isn't
        marshallable back to PyObject*."""
        arity = len(expr.params)
        if arity > 3:
            return None
        param_names = [p.name for p in expr.params]
        if any(n == "" for n in param_names):
            return None
        # Reject free vars beyond builtins / module globals. We don't
        # yet thread captures through the trampoline.
        builtins_ns = _PY_BUILTINS_NS
        module_names = set(getattr(self, "_module_globals", {}).keys())
        module_names.update(self.functions.keys())
        module_names.update(getattr(self, "_hoist_wrap_caps", {}).keys())
        if hasattr(self, "class_lowering"):
            module_names.update(self.class_lowering.classes.keys())
        module_names.update(getattr(self, "_cpy_module_env", {}).keys())
        module_names.update(getattr(self, "_native_extension_module_env", {}).keys())
        from pcc.frontends.python.py_ast import Lambda as _Lambda

        def collect_free_vars(e, bound: set, acc: set) -> None:
            if isinstance(e, _Lambda):
                nested_bound = set(bound)
                for p in getattr(e, "params", ()) or ():
                    pname = getattr(p, "name", "")
                    if pname:
                        nested_bound.add(pname)
                collect_free_vars(e.body, nested_bound, acc)
                return
            if isinstance(e, Name):
                if (
                    e.ident not in bound
                    and e.ident not in builtins_ns
                    and e.ident not in module_names
                    and e.ident
                    not in (
                        "True",
                        "False",
                        "None",
                        "...",
                        "*",
                        "__starred__",
                        "**",
                    )
                ):
                    acc.add(e.ident)
                return
            if isinstance(e, tuple):
                # Inner tuples (e.g. ``Call.kwargs = ((name, Expr), ...)``)
                # — recurse into each element.
                for it in e:
                    collect_free_vars(it, bound, acc)
                return
            for slot in _dataclass_field_names(e):
                if slot in ("span", "ty"):
                    continue
                v = _dataclass_field_value(e, slot, None)
                if isinstance(v, tuple):
                    for it in v:
                        collect_free_vars(it, bound, acc)
                elif v is not None and _dataclass_field_names(v):
                    collect_free_vars(v, bound, acc)

        free_vars: set = set()
        collect_free_vars(expr.body, set(param_names), free_vars)
        # Each free var must resolve in the current scope (present in
        # ``self.env`` or via the value-position name resolver). If
        # any doesn't, bail.
        for fv in free_vars:
            if fv not in self.env and fv not in module_names:
                direct_hoist = f"__nested_{fv}"
                if direct_hoist in self.functions:
                    continue
                if any(name.startswith(f"{direct_hoist}_") for name in self.functions):
                    continue
                return None

        # Build a new top-level ir.Function with (ptr, ...) -> ptr ABI
        # where the number of ptr params matches the lambda arity.
        # Save IRBuilder state so we can emit the body in isolation.
        sym_base = f"__lambda_{len(getattr(self, '_lambda_counter', []))}"
        if not hasattr(self, "_lambda_counter"):
            setattr(self, "_lambda_counter", [])
        self._lambda_counter.append(sym_base)
        fn_name = f"user_{self._module_symbol_suffix(self.ast_module.name or 'mod')}_{sym_base}"
        fnty = ir.FunctionType(_CSTR, [_CSTR] * arity)
        # Reuse an existing declaration if we've already laid this
        # lambda down (shouldn't happen in practice with the counter
        # but belt-and-braces).
        existing = self.module.globals.get(fn_name)
        if isinstance(existing, ir.Function):
            fn_ir = existing
        else:
            fn_ir = ir.Function(self.module, fnty, name=fn_name)
            fn_ir.linkage = "internal"

        # Snapshot outer state. ``env`` + ``builder`` + function fields
        # are the only things we need to swap — the module globals,
        # class_lowering, runtime map, and module itself are shared
        # across functions so they stay put.
        saved_builder = self.builder
        saved_env = self.env
        saved_env_class_hint = getattr(self, "env_class_hint", {})
        saved_env_class_object_hint = getattr(self, "env_class_object_hint", {})
        saved_cpy_env_flags = dict(getattr(self, "_cpy_env_flags", {}))
        saved_cpy_values = set(getattr(self, "_cpy_values", set()))
        saved_owned_cpy_values = set(getattr(self, "_owned_cpy_values", set()))
        saved_current_fn = self.current_function
        saved_current_fd = self.current_func_def
        saved_loops = getattr(self, "loop_stack", [])
        saved_entry_block = getattr(self, "_current_entry_block", None)
        saved_try_err_block = getattr(self, "_try_err_block", None)
        saved_cpy_operand_cleanup_block = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )

        entry = fn_ir.append_basic_block(name="entry")
        setattr(self, "builder", ir.IRBuilder(entry))
        setattr(self, "current_function", fn_ir)
        setattr(self, "current_func_def", None)
        setattr(self, "env", {})
        setattr(self, "env_class_hint", {})
        setattr(self, "env_class_object_hint", {})
        setattr(self, "_cpy_env_flags", {})
        setattr(self, "_cpy_values", set())
        setattr(self, "_owned_cpy_values", set())
        setattr(self, "loop_stack", [])
        setattr(self, "_try_err_block", None)
        setattr(self, "_cpy_operand_cleanup_block", None)
        # _alloca_in_entry targets _current_entry_block; keep it in sync with
        # the function being emitted or comprehension target slots land in
        # the enclosing function's entry (cross-function alloca reference).
        setattr(self, "_current_entry_block", entry)

        # Allocate a slot per lambda param, store each incoming arg,
        # tag both the stored value and subsequent loads as CPython —
        # the trampoline hands us CPython PyObject*s, so every
        # attr / method dispatch inside the body must take the CPython
        # path.
        for ir_arg, pname in zip(fn_ir.args, param_names):
            slot = self.builder.alloca(_CSTR, name=f"{pname}.addr")
            self.builder.store(ir_arg, slot)
            self.env[pname] = (slot, _CSTR, DynType(name="dyn"))
            self._cpy_env_flags[pname] = True

        # Create one internal global per free var. The lambda body
        # reads the capture through a load of this global. The wrap-
        # site (in the outer body) stores the captured value into the
        # global before producing the wrapped callable. This gives
        # correct one-shot closure semantics for ``sorted(xs, key=<fn>)``
        # patterns where the lambda is used immediately and not
        # retained. Multi-shot retention with differing captures
        # would need per-capsule state which is TODO.
        capture_globals: dict = {}
        for fv in sorted(free_vars):
            gv_name = f".lambda_capture_{sym_base}_{fv}"
            gv = ir.GlobalVariable(self.module, _CSTR, name=gv_name)
            gv.linkage = "internal"
            gv.initializer = ir.Constant(_CSTR, None)
            capture_globals[fv] = gv
            # Expose via env so reads resolve. Store type is DynType
            # and tag as CPython so attr / method dispatch uses
            # py_cpy_*.
            cap_slot = self.builder.alloca(
                _CSTR,
                name=f"{fv}.cap.addr",
            )
            loaded = self.builder.load(gv, name=self._fresh(f"{fv}.cap"))
            self.builder.store(loaded, cap_slot)
            self.env[fv] = (cap_slot, _CSTR, DynType(name="dyn"))
            self._cpy_env_flags[fv] = True

        # Evaluate the body expression. Any unsupported shape bubbles
        # up as NotImplementedError — callers see the original error
        # rather than a silent bad wrapper.
        try:
            body_val = self._emit_expr(expr.body)
            # Marshal the result back to a CPython PyObject*. Body
            # values that are already CPython-tagged (e.g. from
            # ``x.attr`` via py_cpy_getattr) pass through; scalar
            # (i64 / double / i1) values get boxed and re-converted.
            if isinstance(getattr(body_val, "type", None), ir.VoidType):
                # A lambda body can call a side-effecting helper that
                # returns ``None``. Codegen may lower that call to a
                # ``void`` SSA value even when type-infer left the
                # expression as DynType; the trampoline still needs to
                # hand CPython a real ``None`` object.
                body_val = self._emit_none_literal()
                cpy_val, owned = self._marshal_to_cpython(
                    body_val,
                    NoneType(name="None"),
                )
            else:
                cpy_val, owned = self._marshal_to_cpython_consuming_source(
                    body_val,
                    expr.body.ty,
                    expr.body,
                )
            # The CPython trampoline follows the ordinary callable ABI: every
            # non-NULL result is a new reference.  Transfer an already-owned
            # result, or promote a borrowed parameter/global before returning.
            if owned:
                self._forget_owned_cpy_value(cpy_val)
            else:
                self.builder.call(self.runtime["py_cpy_incref"], [cpy_val])
            self.builder.ret(cpy_val)
        except NotImplementedError:
            # Restore outer state and drop this synthesized function
            # (ir.Function stays declared but will be unused — the
            # linker discards internal symbols with no callers).
            setattr(self, "builder", saved_builder)
            setattr(self, "env", saved_env)
            setattr(self, "env_class_hint", saved_env_class_hint)
            setattr(self, "env_class_object_hint", saved_env_class_object_hint)
            setattr(self, "_cpy_env_flags", saved_cpy_env_flags)
            setattr(self, "_cpy_values", saved_cpy_values)
            setattr(self, "_owned_cpy_values", saved_owned_cpy_values)
            setattr(self, "current_function", saved_current_fn)
            setattr(self, "current_func_def", saved_current_fd)
            setattr(self, "loop_stack", saved_loops)
            setattr(self, "_current_entry_block", saved_entry_block)
            setattr(self, "_try_err_block", saved_try_err_block)
            setattr(
                self,
                "_cpy_operand_cleanup_block",
                saved_cpy_operand_cleanup_block,
            )
            return None

        # Restore outer state now that the lambda body is fully emitted.
        setattr(self, "builder", saved_builder)
        setattr(self, "env", saved_env)
        setattr(self, "env_class_hint", saved_env_class_hint)
        setattr(self, "env_class_object_hint", saved_env_class_object_hint)
        setattr(self, "_cpy_env_flags", saved_cpy_env_flags)
        setattr(self, "_cpy_values", saved_cpy_values)
        setattr(self, "_owned_cpy_values", saved_owned_cpy_values)
        setattr(self, "current_function", saved_current_fn)
        setattr(self, "current_func_def", saved_current_fd)
        setattr(self, "loop_stack", saved_loops)
        setattr(self, "_current_entry_block", saved_entry_block)
        setattr(self, "_try_err_block", saved_try_err_block)
        setattr(
            self,
            "_cpy_operand_cleanup_block",
            saved_cpy_operand_cleanup_block,
        )

        # Before wrapping, store each captured outer-scope value into
        # its dedicated lambda-capture global. Next time the lambda
        # body runs it will see the value current at wrap time — good
        # enough for one-shot ``sorted(xs, key=<fn>)`` semantics.
        for fv, capture_gv in capture_globals.items():
            cpy_val = self._capture_value_as_cpython(fv)
            if cpy_val is None:
                continue
            self.builder.store(cpy_val, capture_gv)

        # Bitcast the function to ``ptr`` (i8*) and wrap via the
        # arity-matched runtime helper. Returns a CPython callable
        # we tag in ``_cpy_values``.
        wrap_helper = {
            0: "py_cpy_wrap_pcc_0arg",
            1: "py_cpy_wrap_pcc_1arg",
            2: "py_cpy_wrap_pcc_2arg",
            3: "py_cpy_wrap_pcc_3arg",
        }[arity]
        fn_ptr = self.builder.bitcast(
            fn_ir,
            _CSTR,
            name=self._fresh(f"{sym_base}.fnptr"),
        )
        result = self.builder.call(
            self.runtime[wrap_helper],
            [fn_ptr],
            name=self._fresh(f"cpy.{sym_base}"),
        )
        return self._mark_owned_cpy_value(result)


_PER_FUNCTION_OWNERSHIP_DICTS = (
    "_owned_local_flag_slots",
    "_owned_local_flag_allocas",
)
_PER_FUNCTION_OWNERSHIP_SETS = (
    "_owned_local_names",
    "_owned_local_has_value",
    "_gc_rooted_local_names",
    "_borrowed_gc_rooted_local_names",
    "_for_target_owned_names",
)


def _swap_in_fresh_ownership_state(host) -> list:
    """Give a nested function body empty per-local ownership tables.

    Returns the enclosing function's tables (attribute name, object or None
    when absent) for ``_restore_ownership_state``.
    """
    saved: list = []
    for attr in _PER_FUNCTION_OWNERSHIP_DICTS:
        saved.append((attr, getattr(host, attr, None)))
        setattr(host, attr, {})
    for attr in _PER_FUNCTION_OWNERSHIP_SETS:
        saved.append((attr, getattr(host, attr, None)))
        setattr(host, attr, set())
    return saved


def _restore_ownership_state(host, saved: list) -> None:
    # An absent table reads as empty everywhere (``getattr(host, attr, {})``),
    # so restoring it as an empty container is equivalent and avoids delattr.
    for attr, value in saved:
        if value is not None:
            setattr(host, attr, value)
        elif attr in _PER_FUNCTION_OWNERSHIP_DICTS:
            setattr(host, attr, {})
        else:
            setattr(host, attr, set())
