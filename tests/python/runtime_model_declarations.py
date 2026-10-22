"""Read named integer runtime declarations without executing runtime source."""
from __future__ import annotations

import ast
import operator


_INTEGER_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
}


class _ModuleBindings(ast.NodeVisitor):
    """Find possible rebindings without evaluating unsupported statements."""

    def __init__(self):
        self.names = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_FunctionDef(self, node):
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def visit_Import(self, node):
        self.names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)

    def visit_ImportFrom(self, node):
        self.names.update(alias.asname or alias.name for alias in node.names)

    def visit_ExceptHandler(self, node):
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name is not None:
            self.names.add(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest is not None:
            self.names.add(node.rest)
        self.generic_visit(node)


def _invalidate_bindings(node, constants):
    visitor = _ModuleBindings()
    visitor.visit(node)
    for name in visitor.names:
        constants.pop(name, None)


def _constant(node, bindings):
    if node is None:
        raise ValueError('uninitialized runtime declaration constant')
    if isinstance(node, ast.Constant) and type(node.value) in (int, str):
        return node.value
    if isinstance(node, ast.Name):
        value = bindings.get(node.id)
        if type(value) in (int, str):
            return value
        raise ValueError('unresolved runtime declaration constant: ' + node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert)):
        value = _constant(node.operand, bindings)
        if type(value) is int:
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.USub):
                return -value
            return ~value
    if isinstance(node, ast.BinOp) and type(node.op) in _INTEGER_OPERATORS:
        left, right = _constant(node.left, bindings), _constant(node.right, bindings)
        if type(left) is int and type(right) is int:
            if isinstance(node.op, (ast.LShift, ast.RShift)) and not 0 <= right < 64:
                raise ValueError('runtime declaration shift must be in [0, 64)')
            try:
                return _INTEGER_OPERATORS[type(node.op)](left, right)
            except ArithmeticError as error:
                raise ValueError('invalid runtime declaration arithmetic') from error
    raise ValueError('runtime declaration is not a static constant: ' + ast.dump(node))


def global_i32_declarations(module: ast.Module, bindings=None):
    """Resolve source-order literals, names and integer constant arithmetic.

    Caller-supplied bindings may provide imported ABI constants. Unknown or
    reassigned values are rejected when used by a declaration, never executed
    or replaced by a guessed count. Unsupported writes, including conditional
    writes, invalidate their targets without interpreting control flow.
    Function/class bodies are not traversed. Direct module imports use the
    caller-supplied namespace; imports inside unsupported control flow do not.
    """
    constants = dict(bindings or {})
    declarations = {}
    for node in module.body:
        targets = []
        value = None
        if isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) for target in node.targets):
            targets = [target.id for target in node.targets]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        if targets:
            try:
                resolved = _constant(value, constants)
            except ValueError:
                _invalidate_bindings(node, constants)
            else:
                for name in targets:
                    constants[name] = resolved
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == 'define_global_i32'):
            _invalidate_bindings(node, constants)
            continue
        call = node.value
        if len(call.args) != 2 or call.keywords:
            raise ValueError('define_global_i32 model declarations require two positional arguments')
        name, count = (_constant(argument, constants) for argument in call.args)
        if type(name) is not str or type(count) is not int:
            raise ValueError('define_global_i32 requires a string name and integer value')
        if name in declarations:
            raise ValueError('duplicate runtime declaration: ' + name)
        declarations[name] = count
    return declarations
