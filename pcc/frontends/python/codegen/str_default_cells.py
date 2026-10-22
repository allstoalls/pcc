"""Omitted ``str`` defaults bound as the default's own object.

Python binds the object a ``def`` evaluated, so a call site that fills an
omitted default must hand over that very object. Every module pools one
immortal object per ``str`` literal (``_emit_str_literal``), and a ``def``
evaluates its ``str`` defaults through that pool, so a call site in the
defining module rebuilds the same object. A call site in another module has
its own pool; it loads the defining module's object from a pointer cell.

The defining module emits a cell for each ``str`` default of every function
it defines. Its exports mark such defaults with ``default_str_cell`` (the
owning module), and the caller derives the cell symbol from the owner and
the value, so neither side needs the other's pool.
"""
from __future__ import annotations

import hashlib

from pcc.ir.compat import ir
from pcc.frontends.python.py_ast import (
    Call,
    DynType,
    FuncDef,
    Name,
    StrLit,
    StrType,
)
from pcc.frontends.python.codegen.module_name_lowering import module_symbol_suffix

STR_DEFAULT_CELL_SENTINEL = "__pcc_str_default_cell__"
_PTR = ir.IntType(8).as_pointer()
_STMT_LIST_FIELDS = ("body", "orelse", "finalbody")
_STMT_GROUP_FIELDS = ("handlers", "cases")


def str_default_cell_symbol(owning_module: str, value: str):
    """The cell symbol for ``value`` defined by ``owning_module``, or None."""
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    return "pcc_strdefault_" + module_symbol_suffix(owning_module) + "_" + digest[:32]


def str_default_cell_expr(owning_module, default, span):
    """A cross-module formal's default as a load of its owner's cell, or None."""
    if not owning_module or not isinstance(default, StrLit):
        return None
    if str_default_cell_symbol(str(owning_module), default.value) is None:
        return None
    return Call(
        span=span,
        ty=StrType(name="str"),
        func=Name(span, DynType(name="dyn"), STR_DEFAULT_CELL_SENTINEL),
        args=(StrLit(span, StrType(name="str"), str(owning_module)), default),
        kwargs=(),
    )


def str_default_cell_ref(expr):
    """``(owning_module, value)`` of a cell default, or None."""
    if not isinstance(expr, Call) or expr.kwargs or len(expr.args) != 2:
        return None
    func = expr.func
    if not isinstance(func, Name) or func.ident != STR_DEFAULT_CELL_SENTINEL:
        return None
    owner = expr.args[0]
    value = expr.args[1]
    if not isinstance(owner, StrLit) or not isinstance(value, StrLit):
        return None
    return owner.value, value.value


def emit_str_default_cell_load(host, owning_module: str, value: str):
    name = str_default_cell_symbol(owning_module, value)
    cell = host.module.globals.get(name)
    if cell is None:
        cell = ir.GlobalVariable(host.module, _PTR, name=name)
        cell.linkage = "external"
    loaded = host.builder.load(cell, name=host._fresh("str.default"))
    # The owner's pooled literal: immortal and module static.
    host._note_never_gc_object(loaded)
    return loaded


def _collect_funcdefs(stmts, out: list) -> None:
    for stmt in stmts:
        if isinstance(stmt, FuncDef):
            out.append(stmt)
        for field in _STMT_LIST_FIELDS:
            nested = getattr(stmt, field, None)
            if isinstance(nested, (tuple, list)):
                _collect_funcdefs(nested, out)
        for field in _STMT_GROUP_FIELDS:
            groups = getattr(stmt, field, None)
            if isinstance(groups, (tuple, list)):
                for group in groups:
                    nested = getattr(group, "body", None)
                    if isinstance(nested, (tuple, list)):
                        _collect_funcdefs(nested, out)


def _module_funcdefs(host) -> list:
    nodes = host._local_funcdef_nodes
    if nodes is None:
        nodes = []
        _collect_funcdefs(host.ast_module.body, nodes)
        ids = set()
        for node in nodes:
            ids.add(id(node))
        # The node list keeps every id in the set alive.
        host._local_funcdef_nodes = nodes
        host._local_funcdef_ids = ids
    return nodes


def funcdef_defined_here(host, fd) -> bool:
    """Whether ``fd`` is a ``def`` of the module being generated."""
    if fd is None:
        return False
    _module_funcdefs(host)
    return id(fd) in host._local_funcdef_ids


def emit_module_str_default_cells(host) -> None:
    """Define a cell for each ``str`` default of every function here.

    A superset of the defaults the exports mark (top-level functions and
    methods of top-level classes); an unused cell costs one word.
    """
    if host._freestanding_module or host._runtime_port_module:
        return
    owner = host.ast_module.name
    if not owner:
        return
    for fd in _module_funcdefs(host):
        for arg in fd.args:
            default = arg.default
            if not isinstance(default, StrLit):
                continue
            name = str_default_cell_symbol(owner, default.value)
            if name is None or host.module.globals.get(name) is not None:
                continue
            cell = ir.GlobalVariable(host.module, _PTR, name=name)
            cell.global_constant = True
            cell.initializer = host._emit_str_literal(default.value).bitcast(_PTR)
