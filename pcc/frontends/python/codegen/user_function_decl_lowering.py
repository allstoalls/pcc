"""User-function declaration helpers for Layer-1 Python codegen."""

from __future__ import annotations

import os
import sys

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import FuncDef, IntType, Name, NoneType

_I8 = ir.IntType(8)
_CSTR = _I8.as_pointer()
_VOID = ir.VoidType()


def _typed_c_abi_ir_type(name: str) -> ir.Type:
    name = name.strip()
    if name.startswith("{") and name.endswith("}"):
        body = name[1:-1].strip()
        if not body:
            raise ValueError("typed C ABI aggregate cannot be empty")
        fields: list[ir.Type] = []
        depth = 0
        start = 0
        index = 0
        while index < len(body):
            ch = body[index]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    raise ValueError("unbalanced typed C ABI aggregate: " + name)
            elif ch == "," and depth == 0:
                field_name = body[start:index].strip()
                if not field_name:
                    raise ValueError("empty typed C ABI aggregate field: " + name)
                field_type = _typed_c_abi_ir_type(field_name)
                if isinstance(field_type, ir.VoidType):
                    raise ValueError("void typed C ABI aggregate field: " + name)
                fields.append(field_type)
                start = index + 1
            index += 1
        if depth != 0:
            raise ValueError("unbalanced typed C ABI aggregate: " + name)
        field_name = body[start:].strip()
        if not field_name:
            raise ValueError("empty typed C ABI aggregate field: " + name)
        field_type = _typed_c_abi_ir_type(field_name)
        if isinstance(field_type, ir.VoidType):
            raise ValueError("void typed C ABI aggregate field: " + name)
        fields.append(field_type)
        return ir.LiteralStructType(fields)
    if name == "void":
        return _VOID
    if name == "ptr":
        return _CSTR
    if name in ("i8", "u8"):
        return _I8
    if name in ("i16", "u16"):
        return ir.IntType(16)
    if name in ("i32", "u32"):
        return ir.IntType(32)
    if name in ("i64", "u64"):
        return ir.IntType(64)
    if name == "f32":
        return ir.FloatType()
    if name == "f64":
        return ir.DoubleType()
    raise ValueError("unsupported typed C ABI type: " + name)


def _is_none_semantic_type(value) -> bool:
    """Recognize explicit ``None`` across independently compiled modules.

    During pcc1 self-host, ``FuncDef.return_ty`` can come from a separately
    compiled copy of the type module. ``isinstance`` then sees a foreign class
    identity even though the semantic type remains ``None``. The stable type
    name closes that module-identity boundary. A raw ``None`` means that the
    source omitted its return annotation and therefore keeps the dynamic
    object ABI.
    """
    return (
        isinstance(value, NoneType)
        or getattr(value, "name", "") == "None"
    )


class UserFunctionDeclLoweringMixin:
    def _user_symbol(self, name: str) -> str:
        """Mangled LLVM symbol for a user function.

        Uses the ``user_<module>_<name>`` convention from
        Section 4 of the interface contract.
        """
        mod_name = self.ast_module.name or "mod"
        # Normalise dotted module names so the mangled symbol is a
        # valid LLVM identifier (dots in LLVM identifiers work when
        # quoted but read oddly).
        sanitized: str = self._module_symbol_suffix(mod_name)
        return f"user_{sanitized}_{name}"

    def _func_decorators(self, fd: FuncDef) -> tuple:
        try:
            decorators = fd.decorators
        except AttributeError:
            return ()
        if not isinstance(decorators, tuple):
            return ()
        return decorators

    def _func_c_abi_export_symbol(self, fd: FuncDef) -> str | None:
        decorators = self._func_decorators(fd)
        i = 0
        while i < len(decorators):
            sym = self._decorator_c_abi_export_symbol(decorators[i])
            if sym is not None:
                return sym
            i += 1
        return None

    def _declare_user_function(self, fd: FuncDef) -> None:
        debug_bootstrap = str(
            os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE", "") or ""
        ).strip().lower() in ("1", "true", "yes", "on")
        if debug_bootstrap:
            sys.stderr.write(
                "debug: declare_user_function begin name="
                + str(getattr(fd, "name", ""))
                + "\n"
            )
        c_abi_sym: str | None = self._func_c_abi_export_symbol(fd)
        c_abi_variadic = False
        c_abi_typed_signature = None
        c_abi_decorators = self._func_decorators(fd)
        c_abi_index = 0
        while c_abi_index < len(c_abi_decorators):
            typed_signature = self._decorator_c_abi_typed_signature(
                c_abi_decorators[c_abi_index]
            )
            if typed_signature is not None:
                c_abi_typed_signature = typed_signature
            if self._decorator_is_c_abi_variadic_export(
                c_abi_decorators[c_abi_index]
            ):
                c_abi_variadic = True
                break
            c_abi_index += 1
        if debug_bootstrap:
            sys.stderr.write("debug: declare_user_function c_abi resolved\n")
        decorators = self._func_decorators(fd)
        if debug_bootstrap:
            sys.stderr.write(
                "debug: declare_user_function decorators type="
                + type(decorators).__name__
                + " count="
                + str(len(decorators))
                + "\n"
            )
        if decorators:
            unrecognised = []
            i = 0
            while i < len(decorators):
                d = decorators[i]
                sym = self._decorator_c_abi_export_symbol(d)
                if sym is not None:
                    i += 1
                    continue
                if not self._decorator_is_noop_whitelist(d):
                    if not (
                        (isinstance(d, Name) and d.ident in self.functions)
                        or self._decorator_is_runtime_partial_factory(d)
                        or self._decorator_is_user_factory_call(d)
                    ):
                        unrecognised.append(d)
                i += 1
            if unrecognised:
                raise NotImplementedError(
                    "Layer 1 does not handle decorators; received "
                    f"{len(decorators)} on {fd.name!r} "
                    f"(first unrecognised: "
                    f"{self._decorator_repr(unrecognised[0])})"
                )

        box_int_abi = self._funcdef_uses_boxed_int_abi(
            fd,
            c_abi_sym=c_abi_sym,
        )
        if debug_bootstrap:
            sys.stderr.write("debug: declare_user_function abi resolved\n")
        param_types: list[ir.Type] = []
        for arg_index, arg in enumerate(fd.args):
            if debug_bootstrap:
                sys.stderr.write(
                    "debug: declare_user_function arg index="
                    + str(arg_index)
                    + " name="
                    + str(getattr(arg, "name", ""))
                    + " type="
                    + type(arg).__name__
                    + "\n"
                )
            # Bare ``*`` separator: no name, no runtime slot — it only
            # marks subsequent params as keyword-only. Skip so the
            # function's IR signature matches ``_resolve_call_kwargs``
            # which already filters the marker from the arg-list side.
            if arg.name == "":
                continue
            ir_ty, _ = self._param_ir_and_bind_type(
                arg,
                require_annotation=True,
                owner_name=fd.name,
                box_int_params=box_int_abi,
            )
            param_types.append(ir_ty)

        if c_abi_typed_signature is not None:
            typed_result, typed_params = c_abi_typed_signature
            if len(typed_params) != len(param_types):
                raise ValueError(
                    "typed C ABI parameter count mismatch for " + fd.name
                )
            param_types = [_typed_c_abi_ir_type(name) for name in typed_params]

        is_generator = self._funcdef_has_yield_sentinel(fd)
        if (
            not is_generator
            and fd.name not in self._duplicate_module_function_names
        ):
            is_generator = fd.name in getattr(
                self, "_generator_func_names", set()
            )
        if is_generator:
            if not hasattr(self, "_generator_func_names"):
                self._generator_func_names = set()
            self._generator_func_names.add(fd.name)
        return_is_none = _is_none_semantic_type(fd.return_ty)
        if c_abi_typed_signature is not None:
            ret_ty = _typed_c_abi_ir_type(c_abi_typed_signature[0])
        elif is_generator:
            ret_ty = _CSTR
        elif fd.is_async and return_is_none:
            ret_ty = _CSTR
        elif return_is_none:
            # ``-> None`` maps to ``ret void`` — bare ``return`` works
            # without materialising the py_None global.
            ret_ty = _VOID
        elif box_int_abi and isinstance(fd.return_ty, IntType) and fd.return_ty.name == "int":
            ret_ty = _CSTR
        else:
            ret_ty = self._map_type(fd.return_ty)

        fnty = ir.FunctionType(ret_ty, param_types, var_arg=c_abi_variadic)
        definition_ordinal = self._function_definition_ordinals.get(fd.name, 0)
        self._function_definition_ordinals[fd.name] = definition_ordinal + 1
        duplicate_python_name = fd.name in self._duplicate_module_function_names
        if c_abi_sym is not None:
            sym = c_abi_sym
        elif duplicate_python_name:
            sym = (
                self._user_symbol(fd.name)
                + ".definition."
                + str(definition_ordinal)
            )
        else:
            sym = self._user_symbol(fd.name)
        existing = self.module.globals.get(sym)
        if duplicate_python_name and isinstance(existing, ir.Function):
            raise ValueError(
                "duplicate function definition resolves to an existing native symbol: "
                + sym
            )
        if isinstance(existing, ir.Function):
            fn = existing
        else:
            fn = ir.Function(self.module, fnty, name=sym)
            fn.linkage = "external"
        # Runtime ports explicitly manage the pending TLS exception, including
        # in private same-module helpers. Exporting an ordinary application
        # function does not select that manual transport for it or its siblings:
        # their Python calls must still branch when a callee raises.
        if self._runtime_port_module or self._freestanding_module:
            self._c_abi_export_symbols.add(sym)
        runtime_args = [a for a in fd.args if a.name != ""]
        for ir_arg, ast_arg in zip(fn.args, runtime_args):
            ir_arg.name = ast_arg.name
        self._funcdef_functions[id(fd)] = fn
        self._native_symbol_funcdefs[fn.name] = fd
        if (fd.manual_pointer_abi and (c_abi_sym is not None
                or getattr(self, "_suppress_implicit_gc_roots", False))):
            self._manual_pointer_abi_functions.add(fn.name)
        self.functions[fd.name] = fn
