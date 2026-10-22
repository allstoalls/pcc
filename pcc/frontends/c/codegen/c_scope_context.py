"""Explicit scope-transition contexts for the C code generator."""

from __future__ import annotations


class NewScopeContext:
    """Enter and restore one lexical declaration scope."""

    def __init__(self, codegen) -> None:
        self._codegen = codegen

    def __enter__(self):
        codegen = self._codegen
        self._old_scope_id = codegen._current_scope_id
        codegen._scope_id_counter += 1
        codegen._current_scope_id = codegen._scope_id_counter
        codegen.env = codegen.env.new_child()
        codegen._decl_ast_types = codegen._decl_ast_types.new_child()
        codegen._typedef_ast_types = codegen._typedef_ast_types.new_child()
        return codegen

    def __exit__(self, exc_type, exc, traceback) -> None:
        codegen = self._codegen
        codegen.env = codegen.env.parents
        codegen._decl_ast_types = codegen._decl_ast_types.parents
        codegen._typedef_ast_types = codegen._typedef_ast_types.parents
        codegen._current_scope_id = self._old_scope_id


class NewFunctionContext:
    """Enter and restore one function-lowering scope."""

    def __init__(self, codegen) -> None:
        self._codegen = codegen

    def __enter__(self):
        codegen = self._codegen
        self._old_function = codegen.function
        self._old_display_name = codegen._function_display_name
        self._old_frame_address_marker = codegen._frame_address_marker
        self._old_builder = codegen.builder
        self._old_environment = codegen.env
        self._old_decl_ast_types = codegen._decl_ast_types
        self._old_typedef_ast_types = codegen._typedef_ast_types
        self._old_labels = codegen._labels
        self._old_scope_id = codegen._current_scope_id
        codegen.in_global = False
        codegen._scope_id_counter += 1
        codegen._current_scope_id = codegen._scope_id_counter
        codegen.env = codegen.env.new_child()
        codegen._decl_ast_types = codegen._decl_ast_types.new_child()
        codegen._typedef_ast_types = codegen._typedef_ast_types.new_child()
        codegen._labels = {}
        codegen._frame_address_marker = None
        return codegen

    def __exit__(self, exc_type, exc, traceback) -> None:
        codegen = self._codegen
        codegen.function = self._old_function
        codegen._function_display_name = self._old_display_name
        codegen._frame_address_marker = self._old_frame_address_marker
        codegen.builder = self._old_builder
        codegen.env = self._old_environment
        codegen._decl_ast_types = self._old_decl_ast_types
        codegen._typedef_ast_types = self._old_typedef_ast_types
        codegen._labels = self._old_labels
        codegen._current_scope_id = self._old_scope_id
        codegen.in_global = True
