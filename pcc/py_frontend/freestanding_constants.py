"""Compile-time integer constants for freestanding runtime modules.

A freestanding module (``__pcc_freestanding__ = True``) has no module init, so
it cannot hold a runtime global: every module-scope statement is rejected, and
``define_global_i64`` builds a *mutable* global whose reads are memory loads.
That is why the runtime's lifecycle tags and header offsets were spelled as
bare literals at each of their use sites -- naming them was not expressible,
not undesirable. ``freestanding_allocator.py`` in particular carries the
allocator's hottest provenance predicate and cannot afford a load where a
literal stands today.

This pass admits ``NAME = <integer literal>`` at module scope in a freestanding
module and substitutes it into every use, before type inference runs. What the
rest of the pipeline sees is exactly the literal that would otherwise be typed
out -- the same ``IntLit`` type the parser gives one, and the same
``UnaryOp('-', IntLit)`` shape it gives a negative literal -- so naming a
constant cannot change the emitted IR. That is the property the change is
gated on: re-emitting a renamed module must produce a byte-identical ``.ll``.

Shadowing is respected conservatively: if a function binds the name at all
(parameter, assignment, ``for`` target, ``except ... as``, ``with ... as``,
``global``/``nonlocal``, a nested ``def``), no use inside that function is
substituted. A name whose value is not a plain integer literal, or that is
assigned more than once, is not a constant and is left for the ordinary
module-scope rejection to report.

Reflection goes through the wire schema rather than ``dataclasses``: this
module is inside the self-host closure, which has no host dataclass
implementation to reflect over.
"""

from __future__ import annotations

from .pipeline_ast_wire import (
    _py_ast_field_names,
    _py_ast_field_value,
    _py_ast_node_replace,
)
from .py_ast import (
    Assign,
    AugAssign,
    Delete,
    DynType,
    ExceptHandler,
    For,
    FuncDef,
    Global,
    IntLit,
    IntType,
    ListExpr,
    Module,
    Name,
    Nonlocal,
    TupleExpr,
    UnaryOp,
    With,
)

_FREESTANDING_MARKER = "__pcc_freestanding__"
# Type and position fields hold ``Type``/``SourceSpan`` nodes, which are not
# code and must never be rewritten or descended into.
_NON_CODE_FIELDS = ("span", "ty", "annotation", "return_ty")
_INT = IntType(name="int")
_DYN = DynType(name="dyn")


def _is_ast_node(value) -> bool:
    # ``None`` must be rejected before the schema is consulted:
    # ``type(None).__name__`` is ``"NoneType"``, which collides with the AST's
    # own ``NoneType`` and would otherwise answer "node with a ``name`` field"
    # for every absent optional child -- and ``getattr(None, "name", None)`` is
    # ``None`` again, so a generic walk recurses forever.
    if value is None:
        return False
    return len(_py_ast_field_names(value)) > 0


def _literal_int_value(expr):
    """The integer a literal expression denotes, or None if it is not one."""
    if isinstance(expr, IntLit):
        return expr.value
    if isinstance(expr, UnaryOp) and isinstance(expr.operand, IntLit):
        if expr.op == "-":
            return -expr.operand.value
        if expr.op == "+":
            return expr.operand.value
        if expr.op == "~":
            return ~expr.operand.value
    return None


def _literal_expr(value: int, at: Name):
    """Spell ``value`` exactly as the parser would at ``at``'s position.

    The types matter as much as the shape: the parser gives every ``IntLit`` an
    ``int`` type and leaves the ``UnaryOp`` wrapping a negative literal ``dyn``.
    Reusing the Name's own type would hand inference a ``dyn`` literal and
    change what gets emitted, which is the one thing this pass must not do.
    """

    if value < 0:
        return UnaryOp(
            span=at.span,
            ty=_DYN,
            op="-",
            operand=IntLit(span=at.span, ty=_INT, value=-value),
        )
    return IntLit(span=at.span, ty=_INT, value=value)


def _collect_target_names(target, out) -> None:
    if isinstance(target, Name):
        out.add(target.ident)
        return
    if isinstance(target, TupleExpr) or isinstance(target, ListExpr):
        for element in target.elems:
            _collect_target_names(element, out)
    # ``x.f = v`` and ``x[i] = v`` read ``x``; neither binds it.


def _collect_bound_names(node, out) -> None:
    """Every name the subtree binds, so a shadowed constant is left alone."""
    if isinstance(node, FuncDef):
        out.add(node.name)
        for arg in node.args:
            if arg.name:
                out.add(arg.name)
    elif isinstance(node, Assign):
        for target in node.targets:
            _collect_target_names(target, out)
    elif isinstance(node, AugAssign):
        _collect_target_names(node.target, out)
    elif isinstance(node, For):
        _collect_target_names(node.target, out)
    elif isinstance(node, With):
        for item in node.items:
            if item[1] is not None:
                _collect_target_names(item[1], out)
    elif isinstance(node, ExceptHandler):
        if node.name:
            out.add(node.name)
    elif isinstance(node, Global) or isinstance(node, Nonlocal):
        for bound in node.names:
            out.add(bound)
    elif isinstance(node, Delete):
        for target in node.targets:
            _collect_target_names(target, out)
    for field_name in _py_ast_field_names(node):
        if field_name in _NON_CODE_FIELDS:
            continue
        _collect_bound_names_in(_py_ast_field_value(node, field_name, None), out)


def _collect_bound_names_in(value, out) -> None:
    if isinstance(value, tuple):
        for item in value:
            _collect_bound_names_in(item, out)
        return
    if _is_ast_node(value):
        _collect_bound_names(value, out)


def _substitute_in(value, constants):
    if isinstance(value, tuple):
        items = []
        changed = False
        for item in value:
            new_item = _substitute_in(item, constants)
            if new_item is not item:
                changed = True
            items.append(new_item)
        if not changed:
            return value
        return tuple(items)
    if _is_ast_node(value):
        return _substitute(value, constants)
    return value


def _substitute(node, constants):
    if isinstance(node, Name):
        if node.ident in constants:
            return _literal_expr(constants[node.ident], node)
        return node
    if isinstance(node, FuncDef):
        bound = set()
        _collect_bound_names(node, bound)
        live = {}
        for name in constants:
            if name not in bound:
                live[name] = constants[name]
        if not live:
            return node
        constants = live
    changes = {}
    for field_name in _py_ast_field_names(node):
        if field_name in _NON_CODE_FIELDS:
            continue
        old = _py_ast_field_value(node, field_name, None)
        new = _substitute_in(old, constants)
        if new is not old:
            changes[field_name] = new
    if not changes:
        return node
    return _py_ast_node_replace(node, changes)


def _declares_freestanding(module: Module) -> bool:
    for stmt in module.body:
        if (
            isinstance(stmt, Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], Name)
            and stmt.targets[0].ident == _FREESTANDING_MARKER
        ):
            return True
    return False


def collect_module_int_constants(module: Module):
    """Module-scope ``NAME = <integer literal>`` bindings, by name."""
    constants = {}
    disqualified = set()
    for stmt in module.body:
        if not isinstance(stmt, Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, Name) or target.ident == _FREESTANDING_MARKER:
            continue
        value = _literal_int_value(stmt.value)
        if value is None or target.ident in constants:
            # Not a literal, or rebound: not a compile-time constant. Leave it
            # to the module-scope rejection rather than folding a guess.
            disqualified.add(target.ident)
            continue
        constants[target.ident] = value
    for name in disqualified:
        if name in constants:
            del constants[name]
    return constants


def fold_module_int_constants(module: Module) -> Module:
    """Replace a freestanding module's integer constants with their values.

    A module that is not freestanding is returned untouched: elsewhere a
    module-scope ``NAME = 5`` is a real global with assignable semantics, and
    folding it would silently change what the program means.
    """
    if not _declares_freestanding(module):
        return module
    constants = collect_module_int_constants(module)
    if not constants:
        return module
    body = []
    for stmt in module.body:
        if (
            isinstance(stmt, Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], Name)
            and stmt.targets[0].ident in constants
        ):
            continue
        body.append(_substitute(stmt, constants))
    return _py_ast_node_replace(module, {"body": tuple(body)})
