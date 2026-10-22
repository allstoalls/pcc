"""Finite closed-world range proof for bounded typed-int accumulator loops.

This is intentionally not a general integer optimizer.  It recognizes only a
single-loop accumulator shape whose module-level calls provide literal bounds,
plus pure single-return helpers reached by that loop.  Anything unknown keeps
the ordinary arbitrary-precision object projection.
"""

from __future__ import annotations

from pcc.frontends.python.py_ast import Assign, Attr, AugAssign, BinOp, BoolExpr, Call, ClassDef, Compare, Delete, DictExpr, Expr, ExprStmt, For, FuncDef, If, IfExpr, IntLit, IntType, Lambda, ListExpr, ListType, Module, Name, Raise, Return, Slice, Stmt, Subscript, Try, TupleExpr, UnaryOp, While, With


_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1
_MAX_PROVEN_ITERATIONS = 10_000_000


def _function_has_bounded_int_signature(
    function: FuncDef,
    allow_list: bool,
) -> bool:
    if (
        function.is_async
        or function.is_method
        or function.decorators
        or not isinstance(function.return_ty, IntType)
    ):
        return False
    for arg in function.args:
        if arg.kind not in ("pos", "pos_only") or arg.default is not None:
            return False
        if isinstance(arg.annotation, IntType):
            continue
        if (
            allow_list
            and isinstance(arg.annotation, ListType)
            and isinstance(arg.annotation.elem, IntType)
        ):
            continue
        return False
    return True


def _checked_interval(lo: int, hi: int):
    if lo > hi or lo < _I64_MIN or hi > _I64_MAX:
        return None
    return (lo, hi)


def _literal_value(expr: Expr):
    if isinstance(expr, IntLit):
        value = int(expr.value)
        interval = _checked_interval(value, value)
        if interval is None:
            return None
        return (interval, -1)
    if isinstance(expr, ListExpr):
        if not expr.elems:
            return ((0, 0), 0)
        values: list[int] = []
        for elem in expr.elems:
            if not isinstance(elem, IntLit):
                return None
            value = int(elem.value)
            if _checked_interval(value, value) is None:
                return None
            values.append(value)
        return ((min(values), max(values)), len(values))
    return None


def _function_map(module: Module) -> dict[str, FuncDef]:
    result: dict[str, FuncDef] = {}
    duplicates: set[str] = set()
    for stmt in module.body:
        if not isinstance(stmt, FuncDef):
            continue
        if stmt.name in result:
            duplicates.add(stmt.name)
        result[stmt.name] = stmt
    for name in duplicates:
        result.pop(name, None)
    return result


def _expr_children(expr: Expr) -> tuple[Expr, ...]:
    if isinstance(expr, (BinOp, Compare)):
        return (expr.lhs, expr.rhs)
    if isinstance(expr, BoolExpr):
        return (expr.left, expr.right)
    if isinstance(expr, UnaryOp):
        return (expr.operand,)
    if isinstance(expr, Call):
        out: list[Expr] = [expr.func]
        out.extend(expr.args)
        for _name, value in expr.kwargs:
            out.append(value)
        return tuple(out)
    if isinstance(expr, Attr):
        return (expr.obj,)
    if isinstance(expr, Subscript):
        return (expr.obj, expr.idx)
    if isinstance(expr, Slice):
        parts: list[Expr] = []
        if expr.lo is not None:
            parts.append(expr.lo)
        if expr.hi is not None:
            parts.append(expr.hi)
        if expr.step is not None:
            parts.append(expr.step)
        return tuple(parts)
    if isinstance(expr, (ListExpr, TupleExpr)):
        return expr.elems
    if isinstance(expr, DictExpr):
        parts = []
        for key, value in expr.pairs:
            parts.append(key)
            parts.append(value)
        return tuple(parts)
    if isinstance(expr, IfExpr):
        return (expr.cond, expr.then_e, expr.else_e)
    return ()


def _stmt_exprs(stmt: Stmt) -> tuple[Expr, ...]:
    if isinstance(stmt, Assign):
        return stmt.targets + (stmt.value,)
    if isinstance(stmt, AugAssign):
        return (stmt.target, stmt.value)
    if isinstance(stmt, ExprStmt):
        return (stmt.expr,)
    if isinstance(stmt, Return) and stmt.value is not None:
        return (stmt.value,)
    if isinstance(stmt, (If, While)):
        return (stmt.cond,)
    if isinstance(stmt, For):
        return (stmt.target, stmt.iter)
    if isinstance(stmt, Raise):
        parts: list[Expr] = []
        if stmt.exc is not None:
            parts.append(stmt.exc)
        if stmt.cause is not None:
            parts.append(stmt.cause)
        return tuple(parts)
    if isinstance(stmt, Delete):
        return stmt.targets
    if isinstance(stmt, With):
        parts = []
        for context, target in stmt.items:
            parts.append(context)
            if target is not None:
                parts.append(target)
        return tuple(parts)
    return ()


def _collect_expr_function_uses(
    expr: Expr,
    caller: str,
    functions: dict[str, FuncDef],
    calls: dict[str, list[tuple[str, Call]]],
    escapes: set[str],
) -> None:
    """Collect direct calls without misclassifying their callee as an escape."""
    if isinstance(expr, Call):
        if isinstance(expr.func, Name) and expr.func.ident in functions:
            calls.setdefault(expr.func.ident, []).append((caller, expr))
        else:
            _collect_expr_function_uses(
                expr.func,
                caller,
                functions,
                calls,
                escapes,
            )
        for arg in expr.args:
            _collect_expr_function_uses(arg, caller, functions, calls, escapes)
        for _name, value in expr.kwargs:
            _collect_expr_function_uses(value, caller, functions, calls, escapes)
        return
    if isinstance(expr, Name):
        if expr.ident in functions:
            escapes.add(expr.ident)
        return
    if isinstance(expr, Lambda):
        for arg in expr.params:
            if arg.default is not None:
                _collect_expr_function_uses(
                    arg.default,
                    caller,
                    functions,
                    calls,
                    escapes,
                )
        _collect_expr_function_uses(
            expr.body,
            "<nested>",
            functions,
            calls,
            escapes,
        )
        return
    for child in _expr_children(expr):
        _collect_expr_function_uses(child, caller, functions, calls, escapes)


def _collect_stmt_function_uses(
    stmts: tuple[Stmt, ...],
    caller: str,
    functions: dict[str, FuncDef],
    calls: dict[str, list[tuple[str, Call]]],
    escapes: set[str],
) -> None:
    for stmt in stmts:
        if isinstance(stmt, FuncDef):
            for decorator in stmt.decorators:
                _collect_expr_function_uses(
                    decorator,
                    caller,
                    functions,
                    calls,
                    escapes,
                )
            for arg in stmt.args:
                if arg.default is not None:
                    _collect_expr_function_uses(
                        arg.default,
                        caller,
                        functions,
                        calls,
                        escapes,
                    )
            if caller != "<module>":
                _collect_stmt_function_uses(
                    stmt.body,
                    "<nested>",
                    functions,
                    calls,
                    escapes,
                )
            continue
        if isinstance(stmt, ClassDef):
            for base in stmt.bases:
                _collect_expr_function_uses(
                    base,
                    caller,
                    functions,
                    calls,
                    escapes,
                )
            for _name, value in stmt.keywords:
                _collect_expr_function_uses(
                    value,
                    caller,
                    functions,
                    calls,
                    escapes,
                )
            for decorator in stmt.decorators:
                _collect_expr_function_uses(
                    decorator,
                    caller,
                    functions,
                    calls,
                    escapes,
                )
            _collect_stmt_function_uses(
                stmt.body,
                "<nested>",
                functions,
                calls,
                escapes,
            )
            continue
        for expr in _stmt_exprs(stmt):
            _collect_expr_function_uses(
                expr,
                caller,
                functions,
                calls,
                escapes,
            )
        if isinstance(stmt, (If, While, For)):
            _collect_stmt_function_uses(
                stmt.body,
                caller,
                functions,
                calls,
                escapes,
            )
            _collect_stmt_function_uses(
                stmt.else_body,
                caller,
                functions,
                calls,
                escapes,
            )
            continue
        if isinstance(stmt, Try):
            _collect_stmt_function_uses(
                stmt.body,
                caller,
                functions,
                calls,
                escapes,
            )
            for handler in stmt.handlers:
                if handler.exc_type is not None:
                    _collect_expr_function_uses(
                        handler.exc_type,
                        caller,
                        functions,
                        calls,
                        escapes,
                    )
                _collect_stmt_function_uses(
                    handler.body,
                    caller,
                    functions,
                    calls,
                    escapes,
                )
            _collect_stmt_function_uses(
                stmt.else_body,
                caller,
                functions,
                calls,
                escapes,
            )
            _collect_stmt_function_uses(
                stmt.finally_body,
                caller,
                functions,
                calls,
                escapes,
            )
            continue
        if isinstance(stmt, With):
            _collect_stmt_function_uses(
                stmt.body,
                caller,
                functions,
                calls,
                escapes,
            )


def _direct_call_sites(module: Module, functions: dict[str, FuncDef]):
    calls: dict[str, list[tuple[str, Call]]] = {}
    escapes: set[str] = set()
    streams: list[tuple[str, tuple[Stmt, ...]]] = [("<module>", module.body)]
    for name, function in functions.items():
        streams.append((name, function.body))
    for caller, stmts in streams:
        _collect_stmt_function_uses(
            stmts,
            caller,
            functions,
            calls,
            escapes,
        )
    return calls, escapes


def _contains_call(calls: list[Call], candidate: Call) -> bool:
    for call in calls:
        if call is candidate:
            return True
    return False


def _expr_interval(
    expr: Expr,
    env: dict[str, tuple[int, int]],
    functions: dict[str, FuncDef],
    approved: set[str],
    active: set[str],
):
    if isinstance(expr, IntLit):
        value = int(expr.value)
        return _checked_interval(value, value)
    if isinstance(expr, Name):
        return env.get(expr.ident)
    if isinstance(expr, Compare):
        if _expr_interval(expr.lhs, env, functions, approved, active) is None:
            return None
        if _expr_interval(expr.rhs, env, functions, approved, active) is None:
            return None
        return (0, 1)
    if isinstance(expr, BinOp):
        lhs = _expr_interval(expr.lhs, env, functions, approved, active)
        rhs = _expr_interval(expr.rhs, env, functions, approved, active)
        if lhs is None or rhs is None:
            return None
        a, b = lhs
        c, d = rhs
        if expr.op == "+":
            return _checked_interval(a + c, b + d)
        if expr.op == "-":
            return _checked_interval(a - d, b - c)
        if expr.op == "*":
            values = (a * c, a * d, b * c, b * d)
            return _checked_interval(min(values), max(values))
        if expr.op == "%" and c == d and c > 0:
            return _checked_interval(0, c - 1)
        if expr.op == "//" and c == d and c > 0:
            return _checked_interval(a // c, b // c)
        return None
    if isinstance(expr, Call):
        if expr.kwargs or not isinstance(expr.func, Name):
            return None
        target = functions.get(expr.func.ident)
        if (
            target is None
            or target.name in active
            or not _function_has_bounded_int_signature(target, False)
            or len(expr.args) != len(target.args)
        ):
            return None
        arg_ranges = []
        for arg_expr in expr.args:
            interval = _expr_interval(arg_expr, env, functions, approved, active)
            if interval is None:
                return None
            arg_ranges.append(interval)
        if len(target.body) != 1 or not isinstance(target.body[0], Return):
            return None
        returned = target.body[0].value
        if returned is None:
            return None
        target_env: dict[str, tuple[int, int]] = {}
        for index, arg in enumerate(target.args):
            target_env[arg.name] = arg_ranges[index]
        active.add(target.name)
        interval = _expr_interval(returned, target_env, functions, approved, active)
        active.remove(target.name)
        if interval is not None:
            approved.add(target.name)
        return interval
    return None


def _flatten_accumulator_delta(expr: Expr, name: str):
    terms: list[Expr] = []
    found = 0
    pending: list[Expr] = [expr]
    while pending:
        node = pending.pop()
        if isinstance(node, BinOp) and node.op == "+":
            pending.append(node.rhs)
            pending.append(node.lhs)
            continue
        if isinstance(node, Name) and node.ident == name:
            found += 1
            continue
        terms.append(node)
    if found != 1:
        return None
    return terms


def _prove_accumulator_update(
    stmt: Assign,
    env: dict[str, tuple[int, int]],
    iterations: int,
    functions: dict[str, FuncDef],
    approved: set[str],
    active: set[str],
):
    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], Name):
        return None
    name = stmt.targets[0].ident
    initial = env.get(name)
    if initial is None:
        return None
    terms = _flatten_accumulator_delta(stmt.value, name)
    if terms is None or not terms:
        return None
    delta = (0, 0)
    for term in terms:
        part = _expr_interval(term, env, functions, approved, active)
        if part is None:
            return None
        delta = _checked_interval(delta[0] + part[0], delta[1] + part[1])
        if delta is None:
            return None
    scaled_lo = delta[0] * iterations
    scaled_hi = delta[1] * iterations
    return name, _checked_interval(
        initial[0] + min(scaled_lo, scaled_hi),
        initial[1] + max(scaled_lo, scaled_hi),
    )


def _prove_loop_root(
    function: FuncDef,
    literal_args,
    functions: dict[str, FuncDef],
):
    if not _function_has_bounded_int_signature(function, True):
        return None
    if len(literal_args) != len(function.args):
        return None
    env: dict[str, tuple[int, int]] = {}
    sequences: dict[str, tuple[tuple[int, int], int]] = {}
    for index, arg in enumerate(function.args):
        value = literal_args[index]
        if value is None:
            return None
        interval, length = value
        env[arg.name] = interval
        if length >= 0:
            sequences[arg.name] = (interval, length)

    loop = None
    returned = None
    for stmt in function.body:
        if isinstance(stmt, Assign):
            if loop is not None or len(stmt.targets) != 1 or not isinstance(stmt.targets[0], Name):
                return None
            interval = _expr_interval(stmt.value, env, functions, set(), set())
            if interval is None:
                return None
            env[stmt.targets[0].ident] = interval
            continue
        if isinstance(stmt, While) or isinstance(stmt, For):
            if loop is not None:
                return None
            loop = stmt
            continue
        if isinstance(stmt, Return):
            if returned is not None or stmt.value is None:
                return None
            returned = stmt.value
            continue
        return None
    if loop is None or not isinstance(returned, Name):
        return None

    approved: set[str] = {function.name}
    active: set[str] = {function.name}
    iterations = -1
    accumulator_stmt = None
    if isinstance(loop, While):
        cond = loop.cond
        if (
            loop.else_body
            or not isinstance(cond, Compare)
            or cond.op != "<"
            or not isinstance(cond.lhs, Name)
            or not isinstance(cond.rhs, Name)
        ):
            return None
        counter = cond.lhs.ident
        bound = env.get(cond.rhs.ident)
        start = env.get(counter)
        if bound is None or start is None or start[0] != start[1]:
            return None
        iterations = max(0, bound[1] - start[0])
        if iterations > _MAX_PROVEN_ITERATIONS:
            return None
        env[counter] = (start[0], max(start[0], bound[1] - 1))
        counter_updates = 0
        for body_stmt in loop.body:
            if not isinstance(body_stmt, Assign) or len(body_stmt.targets) != 1 or not isinstance(body_stmt.targets[0], Name):
                return None
            target_name = body_stmt.targets[0].ident
            if target_name == counter:
                if not (
                    isinstance(body_stmt.value, BinOp)
                    and body_stmt.value.op == "+"
                    and isinstance(body_stmt.value.lhs, Name)
                    and body_stmt.value.lhs.ident == counter
                    and isinstance(body_stmt.value.rhs, IntLit)
                    and int(body_stmt.value.rhs.value) == 1
                ):
                    return None
                counter_updates += 1
            else:
                if accumulator_stmt is not None:
                    return None
                accumulator_stmt = body_stmt
        if counter_updates != 1:
            return None
    else:
        if loop.else_body or loop.is_async or not isinstance(loop.target, Name) or not isinstance(loop.iter, Name):
            return None
        sequence = sequences.get(loop.iter.ident)
        if sequence is None or len(loop.body) != 1 or not isinstance(loop.body[0], Assign):
            return None
        env[loop.target.ident] = sequence[0]
        iterations = sequence[1]
        accumulator_stmt = loop.body[0]

    if accumulator_stmt is None:
        return None
    update = _prove_accumulator_update(
        accumulator_stmt,
        env,
        iterations,
        functions,
        approved,
        active,
    )
    if update is None or update[1] is None:
        return None
    accumulator_name, final_interval = update
    env[accumulator_name] = final_interval
    if returned.ident != accumulator_name:
        return None
    return approved


def compute_bounded_int_abi_function_names(module: Module) -> list[str]:
    functions = _function_map(module)
    calls, escapes = _direct_call_sites(module, functions)
    approved_roots: list[set[str]] = []
    root_calls: list[Call] = []
    for name, function in functions.items():
        if not any(isinstance(stmt, (While, For)) for stmt in function.body):
            continue
        sites = calls.get(name, [])
        module_sites = [call for caller, call in sites if caller == "<module>"]
        if not module_sites or len(module_sites) != len(sites) or name in escapes:
            continue
        closure: set[str] = set()
        proven_calls: list[Call] = []
        valid = True
        for call in module_sites:
            if call.kwargs:
                valid = False
                break
            literal_args = [_literal_value(arg) for arg in call.args]
            proven = _prove_loop_root(function, literal_args, functions)
            if proven is None:
                valid = False
                break
            closure.update(proven)
            proven_calls.append(call)
        if valid:
            approved_roots.append(closure)
            root_calls.extend(proven_calls)

    approved: set[str] = set()
    for closure in approved_roots:
        if any(name in escapes for name in closure):
            continue
        valid = True
        for name in closure:
            for caller, call in calls.get(name, []):
                if caller == "<module>":
                    if not _contains_call(root_calls, call):
                        valid = False
                        break
                elif caller not in closure:
                    valid = False
                    break
            if not valid:
                break
        if valid:
            approved.update(closure)
    return sorted(approved)


__all__ = ["compute_bounded_int_abi_function_names"]
