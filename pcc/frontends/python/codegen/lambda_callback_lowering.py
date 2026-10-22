"""Native lambda callback object lowering for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import DynType, Lambda, Name
from pcc.frontends.python.codegen.runtime_abi import declare_runtime_global

_I8 = ir.IntType(8)
_I64 = ir.IntType(64)
_CSTR = _I8.as_pointer()
_PY_BUILTINS_NS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "object",
        "print",
        "range",
        "set",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)


def _dataclass_field_value(obj, field_name: str, default=None):
    return getattr(obj, field_name, default)


def _dataclass_field_names(obj):
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is not None:
        return fields.keys()
    if isinstance(obj, Lambda):
        return ("span", "ty", "params", "body")
    return ()


class LambdaCallbackLoweringMixin:
    def _emit_native_lambda_callback_object(self, expr: Lambda) -> Optional[ir.Value]:
        arity = len(expr.params)
        param_names = [p.name for p in expr.params]
        if arity > 3 or any(name == "" for name in param_names):
            return None

        module_names = set(getattr(self, "_module_globals", {}).keys())
        module_names.update(self.functions.keys())
        if hasattr(self, "class_lowering"):
            module_names.update(self.class_lowering.classes.keys())
        module_names.update(getattr(self, "_native_builtin_module_aliases", {}).keys())
        module_names.update(getattr(self, "_native_builtin_value_aliases", {}).keys())
        module_names.update(getattr(self, "_native_extension_module_env", {}).keys())

        def collect_free_vars(e, bound: set, acc: set) -> None:
            if isinstance(e, Lambda):
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
                    and e.ident not in _PY_BUILTINS_NS
                    and e.ident not in module_names
                    and e.ident not in ("True", "False", "None")
                ):
                    acc.add(e.ident)
                return
            if isinstance(e, tuple):
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

        free_vars: set[str] = set()
        collect_free_vars(expr.body, set(param_names), free_vars)
        if free_vars:
            return None

        if not hasattr(self, "_native_lambda_callback_counter"):
            self._native_lambda_callback_counter = 0
        idx = self._native_lambda_callback_counter
        self._native_lambda_callback_counter += 1
        sym_base = f"__native_lambda_callback_{idx}"
        fn_name = f"user_{self._module_symbol_suffix(self.ast_module.name or 'mod')}_{sym_base}"
        adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
        adapter = ir.Function(self.module, adapter_ty, name=fn_name)
        adapter.linkage = "internal"

        saved_builder = self.builder
        saved_env = self.env
        saved_env_class_hint = getattr(self, "env_class_hint", {})
        saved_current_fn = self.current_function
        saved_current_fd = self.current_func_def
        saved_loops = getattr(self, "loop_stack", [])
        saved_entry_block = getattr(self, "_current_entry_block", None)

        entry = adapter.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry)
        self.current_function = adapter
        self.current_func_def = None
        self.env = {}
        self.env_class_hint = {}
        self.loop_stack = []
        # Keep _alloca_in_entry pointed at THIS function's entry, or
        # comprehension target slots alloca into the enclosing function
        # (cross-function alloca reference).
        self._current_entry_block = entry

        param_objs: list[ir.Value] = []
        for i, pname in enumerate(param_names):
            obj = self.builder.call(
                self.runtime["py_tuple_get"],
                [adapter.args[1], ir.Constant(_I64, i)],
                name=self._fresh(f"{pname}.arg"),
            )
            slot = self.builder.alloca(_CSTR, name=f"{pname}.addr")
            self.builder.store(obj, slot)
            self.env[pname] = (slot, _CSTR, DynType(name="dyn"))
            param_objs.append(obj)

        try:
            result = self._emit_expr(expr.body)
            self._gc_release_if_owned(result, expr.body)
            for param_obj in param_objs:
                self._gc_release(param_obj)
            none_gv = declare_runtime_global(self.module, "py_None")
            self.builder.ret(self.builder.load(none_gv, name="none"))
        except NotImplementedError:
            self.builder = saved_builder
            self.env = saved_env
            self.env_class_hint = saved_env_class_hint
            self.current_function = saved_current_fn
            self.current_func_def = saved_current_fd
            self.loop_stack = saved_loops
            self._current_entry_block = saved_entry_block
            return None

        self.builder = saved_builder
        self.env = saved_env
        self.env_class_hint = saved_env_class_hint
        self.current_function = saved_current_fn
        self.current_func_def = saved_current_fd
        self.loop_stack = saved_loops
        self._current_entry_block = saved_entry_block

        captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh("lambda.callback.captures"),
        )
        fn_obj = self.builder.call(
            self.runtime["py_func_new"],
            [adapter, captures],
            name=self._fresh("lambda.callback.func"),
        )
        self._gc_release(captures)
        return fn_obj
