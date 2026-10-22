"""A class body's compound statements, run as functions when the class is created.

class_gen runs a class body as events in source order: attribute bindings,
annotations, method publications and expression statements. Python runs the
other statements of a class body too -- ``if``/``for``/``while``/``try``/
``with``, imports, augmented assignments, ``del`` -- with the class namespace
as their locals: a name they bind is stored in the namespace (STORE_NAME); a
name they read is looked up there, then in the enclosing scopes and the
builtins (LOAD_NAME).

lower_class_body_statements turns each such top-level statement S into a
class-body function that class_gen keeps hidden, and a statement that calls
it with the class object once the preceding events have run:

- a name S binds is stored with ``setattr(cls, name, value)`` (an ``except
  ... as`` name is deleted again when the handler finishes, and ``del name``
  is ``delattr``); a binding target that is not a plain name binds a local of
  the function first;
- a read of a name the class body binds is ``cls.__dict__[name]`` while the
  namespace holds it, and the name's ordinary lookup otherwise;
- a function S defines is hoisted to a hidden class-body function before S,
  compiled like a split method, and passed to the function, which binds it
  (through its decorators);
- nested functions, lambda bodies and comprehension bodies do not see class
  names and are left alone; a lambda's defaults and a comprehension's
  outermost iterable are evaluated in the class scope and rewritten.

``__pcc_class_run__(function, names, hidden...)`` is the call class_gen runs;
``names`` lists what S may bind, which class_gen declares. A class, a walrus,
``global`` or ``nonlocal`` inside S raise NotImplementedError rather than run
wrongly.
"""

from __future__ import annotations

from dataclasses import replace

from pcc.frontends.python.py_ast import (
    Arg,
    Assign,
    Attr,
    AugAssign,
    BoolType,
    Call,
    ClassDef,
    Compare,
    Delete,
    DynType,
    ExceptHandler,
    ExprStmt,
    For,
    FuncDef,
    Global,
    If,
    IfExpr,
    Import,
    ImportFrom,
    Lambda,
    ListExpr,
    Name,
    NoneLit,
    NoneType,
    Nonlocal,
    Pass,
    StrLit,
    StrType,
    Subscript,
    Try,
    TupleExpr,
    While,
    With,
)
from pcc.frontends.python.codegen.hoist_analysis import (
    _dataclass_field_names,
    _dataclass_field_value,
)
from pcc.frontends.python.codegen.hoist_boxing import (
    _comprehension_parts,
    _is_walrus,
    _rebuild_comprehension,
    collect_scope_bindings,
)
from pcc.frontends.python.method_decorators import hidden_definition_name
from pcc.frontends.python.private_names import mangle_private_name


RUN_FUNCTION = "__pcc_class_run__"

# Statements class_gen already runs as class-body events.
_EVENT_STATEMENTS = (FuncDef, ClassDef, Assign, ExprStmt, Delete, Pass)
_SKIP_FIELDS = ("span", "ty", "annotation", "return_ty")


def run_call_parts(statement):
    """(function name, bound names, hidden function names) of a run statement."""
    if not isinstance(statement, ExprStmt):
        return None
    call = statement.expr
    if (not isinstance(call, Call) or not isinstance(call.func, Name)
            or call.func.ident != RUN_FUNCTION or len(call.args) < 2):
        return None
    function, names = call.args[0], call.args[1]
    if not isinstance(function, Name) or not isinstance(names, TupleExpr):
        return None
    bound = []
    for item in names.elems:
        if not isinstance(item, StrLit):
            return None
        bound.append(item.value)
    hidden = []
    for item in call.args[2:]:
        if not isinstance(item, Name):
            return None
        hidden.append(item.ident)
    return function.ident, tuple(bound), tuple(hidden)


def _dyn():
    return DynType(name="dyn")


def _name(span, ident):
    return Name(span, _dyn(), ident)


def _text(span, value):
    return StrLit(span, StrType(name="str"), value)


def _call(span, function, *args):
    return Call(span, _dyn(), _name(span, function), tuple(args))


def _unsupported(node, what):
    line = getattr(getattr(node, "span", None), "line", 0)
    raise NotImplementedError(
        "class-body statement at line " + str(line) + " " + what
        + " (class-body compound statements run as functions at class creation)"
    )


class _Block:
    """The rewriting of one statement S into its function's body."""

    def __init__(self, class_name, index, class_names, span):
        self.class_name = class_name
        self.index = index
        self.prefix = "__pcc_cb" + str(index) + "_"
        self.function = hidden_definition_name(index * 1000, "run")
        self.cls = self.prefix + "cls"
        self.class_names = class_names
        self.span = span
        self.hoisted = []
        self.parameters = []
        self.temporaries = 0

    def key(self, name):
        return mangle_private_name(self.class_name, name)

    def temporary(self):
        self.temporaries += 1
        return self.prefix + "t" + str(self.temporaries)

    def namespace(self, span):
        return Attr(span, _dyn(), _name(span, self.cls), "__dict__")

    def store(self, span, name, value):
        return ExprStmt(span, _call(span, "setattr", _name(span, self.cls), _text(span, self.key(name)), value))

    def delete(self, span, name):
        return ExprStmt(span, _call(span, "delattr", _name(span, self.cls), _text(span, self.key(name))))


def _read(block, expr):
    """Rewrite the class-scope reads of ``expr``."""
    if expr is None:
        return None
    if isinstance(expr, Name):
        if expr.ident in block.class_names:
            key = _text(expr.span, block.key(expr.ident))
            present = Compare(expr.span, BoolType(name="bool"), "in", key, block.namespace(expr.span))
            value = Subscript(expr.span, _dyn(), block.namespace(expr.span), key)
            return IfExpr(expr.span, _dyn(), present, value, expr)
        return expr
    if isinstance(expr, Lambda):
        params = []
        for param in expr.params:
            params.append(replace(param, default=_read(block, param.default)))
        return replace(expr, params=tuple(params))
    if _is_walrus(expr):
        _unsupported(expr, "binds a name with an assignment expression")
    parts = _comprehension_parts(expr)
    if parts is not None:
        heads, clauses = parts
        if clauses:
            target, iter_expr, ifs, source = clauses[0]
            clauses = ((target, _read(block, iter_expr), ifs, source),) + tuple(clauses[1:])
        return _rebuild_comprehension(expr, heads, clauses)
    return _rewrite_fields(block, expr)


def _rewrite_fields(block, node):
    # The field walk the hoisting passes use, which self-hosted pcc runs too.
    changes = {}
    for field_name in _dataclass_field_names(node):
        if field_name in _SKIP_FIELDS:
            continue
        value = _dataclass_field_value(node, field_name, None)
        rewritten = _read_value(block, value)
        if rewritten is not value:
            changes[field_name] = rewritten
    if not changes:
        return node
    return replace(node, **changes)


def _read_value(block, value):
    if isinstance(value, tuple):
        items = []
        changed = False
        for item in value:
            rewritten = _read_value(block, item)
            changed = changed or rewritten is not item
            items.append(rewritten)
        return tuple(items) if changed else value
    if value is None or isinstance(value, (Arg, ExceptHandler)) or not _dataclass_field_names(value):
        return value
    return _read(block, value)


def _bind(block, target, stores):
    """A target binding locals; ``stores`` gets the namespace stores after it."""
    span = target.span
    if isinstance(target, Name):
        local = block.temporary()
        stores.append(block.store(span, target.ident, _name(span, local)))
        return _name(span, local)
    if isinstance(target, (TupleExpr, ListExpr)):
        return replace(target, elems=tuple(_bind(block, elem, stores) for elem in target.elems))
    if (isinstance(target, Call) and isinstance(target.func, Name)
            and target.func.ident in ("*", "__starred__") and len(target.args) == 1):
        return replace(target, args=(_bind(block, target.args[0], stores),))
    # An attribute or subscript target stores into an object it reads.
    return _read(block, target)


def _statements(block, statements):
    out = []
    for statement in statements:
        out.extend(_statement(block, statement))
    if not out:
        out.append(Pass(block.span))
    return tuple(out)


def _optional_statements(block, statements):
    return _statements(block, statements) if statements else ()


def _statement(block, statement):
    span = statement.span
    if isinstance(statement, ClassDef):
        _unsupported(statement, "defines a class inside a compound statement")
    if isinstance(statement, (Global, Nonlocal)):
        _unsupported(statement, "declares global or nonlocal names")
    if isinstance(statement, FuncDef):
        return _definition(block, statement)
    if isinstance(statement, Assign):
        value = _read(block, statement.value)
        if len(statement.targets) == 1 and isinstance(statement.targets[0], Name):
            return (block.store(span, statement.targets[0].ident, value),)
        stores = []
        targets = tuple(_bind(block, target, stores) for target in statement.targets)
        return (replace(statement, targets=targets, value=value),) + tuple(stores)
    if isinstance(statement, AugAssign):
        target = statement.target
        if isinstance(target, Name):
            local = _name(span, block.temporary())
            # LOAD_NAME, the in-place operation on that value, STORE_NAME.
            return (
                Assign(span, (local,), _read(block, target)),
                replace(statement, target=local, value=_read(block, statement.value)),
                block.store(span, target.ident, local),
            )
        return (replace(statement, target=_read(block, target), value=_read(block, statement.value)),)
    if isinstance(statement, Delete):
        out = []
        others = []
        for target in statement.targets:
            if isinstance(target, Name):
                out.append(block.delete(span, target.ident))
            else:
                others.append(_read(block, target))
        if others:
            out.insert(0, replace(statement, targets=tuple(others)))
        return tuple(out)
    if isinstance(statement, (If, While)):
        return (replace(
            statement, cond=_read(block, statement.cond),
            body=_statements(block, statement.body),
            else_body=_optional_statements(block, statement.else_body),
        ),)
    if isinstance(statement, For):
        stores = []
        target = _bind(block, statement.target, stores)
        return (replace(
            statement, target=target, iter=_read(block, statement.iter),
            body=tuple(stores) + _statements(block, statement.body),
            else_body=_optional_statements(block, statement.else_body),
        ),)
    if isinstance(statement, With):
        stores = []
        items = []
        for context, target in statement.items:
            items.append((_read(block, context), None if target is None else _bind(block, target, stores)))
        return (replace(statement, items=tuple(items), body=tuple(stores) + _statements(block, statement.body)),)
    if isinstance(statement, Try):
        handlers = []
        for handler in statement.handlers:
            body = _statements(block, handler.body)
            name = handler.name
            if name:
                local = block.temporary()
                # ``except E as n`` binds n, and unbinds it however the
                # handler ends: ``n = e; try: body finally: n = None; del n``.
                cleanup = (
                    block.store(handler.span, name, NoneLit(handler.span, NoneType(name="None"))),
                    block.delete(handler.span, name),
                )
                body = (block.store(handler.span, name, _name(handler.span, local)),
                        Try(handler.span, body, (), (), cleanup))
                name = local
            handlers.append(replace(handler, exc_type=_read(block, handler.exc_type), name=name, body=body))
        return (replace(
            statement, body=_statements(block, statement.body), handlers=tuple(handlers),
            else_body=_optional_statements(block, statement.else_body),
            finally_body=_optional_statements(block, statement.finally_body),
        ),)
    if isinstance(statement, Import):
        out = []
        for module, alias in statement.names:
            local = block.temporary()
            if alias is None and "." in module:
                # ``import a.b`` imports a.b and binds the top package a.
                out.append(replace(statement, names=((module, block.temporary()),)))
                top = module.split(".", 1)[0]
                out.append(replace(statement, names=((top, local),)))
                out.append(block.store(span, top, _name(span, local)))
            else:
                out.append(replace(statement, names=((module, local),)))
                out.append(block.store(span, alias or module, _name(span, local)))
        return tuple(out)
    if isinstance(statement, ImportFrom):
        names = []
        stores = []
        for imported, alias in statement.names:
            if imported == "*":
                _unsupported(statement, "imports * into a class body")
            local = block.temporary()
            names.append((imported, local))
            stores.append(block.store(span, alias or imported, _name(span, local)))
        return (replace(statement, names=tuple(names)),) + tuple(stores)
    return (_rewrite_fields(block, statement),)


def _definition(block, definition):
    """Hoist a function S defines; S binds it to the name."""
    hidden = hidden_definition_name(block.index * 1000 + len(block.hoisted) + 1, definition.name)
    # Published, in class_gen's source order, before S runs.
    block.hoisted.append(replace(definition, name=hidden, decorators=(), span=block.span))
    parameter = block.prefix + "f" + str(len(block.hoisted))
    block.parameters.append(parameter)
    value = _name(definition.span, parameter)
    index = len(definition.decorators) - 1
    while index >= 0:
        value = Call(definition.span, _dyn(), _read(block, definition.decorators[index]), (value,))
        index -= 1
    return (block.store(definition.span, definition.name, value),)


def _static_unpack(target, value) -> bool:
    """class_gen's ``_class_attr_unpack_pairs``: names unpacked from a literal."""
    if not isinstance(target, TupleExpr) or not isinstance(value, TupleExpr):
        return False
    if len(target.elems) != len(value.elems):
        return False
    for target_elem, value_elem in zip(target.elems, value.elems):
        if not isinstance(target_elem, Name) and not _static_unpack(target_elem, value_elem):
            return False
    return True


def _class_gen_event(statement) -> bool:
    """Whether class_gen runs ``statement`` as one of its own events.

    An assignment is one when each target is a name or a literal tuple
    unpack; a subscript or attribute target (``table[key] = value``) or a
    dynamic unpack runs through a function like a compound statement.
    """
    if isinstance(statement, Assign):
        for target in statement.targets:
            if not isinstance(target, Name) and not _static_unpack(target, statement.value):
                return False
        return True
    return isinstance(statement, _EVENT_STATEMENTS)


def lower_class_body_statements(class_name, body):
    """A class body with each compound statement run by a hidden function."""
    class_names = set(collect_scope_bindings(tuple(body)))
    out = []
    index = 0
    for statement in body:
        if _class_gen_event(statement):
            out.append(statement)
            continue
        index += 1
        block = _Block(class_name, index, class_names, statement.span)
        rewritten = _statement(block, statement)
        span = statement.span
        dyn = _dyn()
        args = [Arg(block.cls, dyn, None, "pos")]
        for parameter in block.parameters:
            args.append(Arg(parameter, dyn, None, "pos"))
        function = FuncDef(
            span=span, name=block.function, args=tuple(args), return_ty=dyn,
            body=rewritten, decorators=(_name(span, "staticmethod"),),
        )
        bound = []
        for name in collect_scope_bindings((statement,)):
            bound.append(_text(span, mangle_private_name(class_name, name)))
        out.extend(block.hoisted)
        out.append(function)
        call_args = [_name(span, block.function), TupleExpr(span, dyn, tuple(bound))]
        for hidden in block.hoisted:
            call_args.append(_name(span, hidden.name))
        out.append(ExprStmt(span, Call(span, dyn, _name(span, RUN_FUNCTION), tuple(call_args))))
    return tuple(out)
