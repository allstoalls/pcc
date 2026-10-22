"""Concrete source-level translations for a subset of LLVM loop passes."""

from __future__ import annotations

import copy

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, collect_ids, contains_node_type, get_int_value, is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


def _coord(node) -> str:
    coord = getattr(node, "coord", None)
    return f"{coord}" if coord else "?"


def _loop_body_items(loop_node) -> list[c_ast.Node]:
    body = getattr(loop_node, "stmt", None)
    if isinstance(body, c_ast.Compound):
        return list(body.block_items or ())
    if body is None:
        return []
    return [body]


def _assign_target_name(node) -> str | None:
    if isinstance(node, c_ast.ID):
        return node.name
    return None


def _make_compound(items, coord=None):
    return c_ast.Compound(block_items=list(items), coord=coord)


def _make_empty_compound(coord=None):
    return c_ast.Compound(block_items=[], coord=coord)


def _is_local_decl_init(init):
    if not isinstance(init, c_ast.DeclList):
        return False
    decls = list(init.decls or ())
    if len(decls) != 1:
        return False
    decl = decls[0]
    return isinstance(decl, c_ast.Decl) and bool(decl.name) and decl.init is not None


def _parse_local_iv_init(init):
    if not _is_local_decl_init(init):
        return None
    decl = list(init.decls or ())[0]
    start = get_int_value(decl.init)
    if start is None:
        return None
    return decl.name, start


def _collect_declared_names(node) -> set[str]:
    names = set()
    if node is None:
        return names
    if isinstance(node, c_ast.Decl) and node.name:
        names.add(node.name)
    for _, child in node.children():
        if isinstance(child, c_ast.Node):
            names.update(_collect_declared_names(child))
    return names


def _parse_step(next_expr, var_name) -> int | None:
    if isinstance(next_expr, c_ast.UnaryOp):
        if isinstance(next_expr.expr, c_ast.ID) and next_expr.expr.name == var_name:
            if next_expr.op in ("++", "p++"):
                return 1
            if next_expr.op in ("--", "p--"):
                return -1
    if isinstance(next_expr, c_ast.Assignment):
        if isinstance(next_expr.lvalue, c_ast.ID) and next_expr.lvalue.name == var_name:
            if next_expr.op == "+=":
                return get_int_value(next_expr.rvalue)
            if next_expr.op == "-=":
                value = get_int_value(next_expr.rvalue)
                return -value if value is not None else None
    return None


def _compute_trip_count(for_node) -> tuple[str, int, int] | None:
    parsed = _parse_local_iv_init(for_node.init)
    if parsed is None:
        return None
    var_name, start = parsed
    cond = getattr(for_node, "cond", None)
    if not isinstance(cond, c_ast.BinaryOp):
        return None
    if not isinstance(cond.left, c_ast.ID) or cond.left.name != var_name:
        return None
    bound = get_int_value(cond.right)
    if bound is None:
        return None
    step = _parse_step(getattr(for_node, "next", None), var_name)
    if step is None or step == 0:
        return None

    trip_count = None
    if cond.op == "<" and step > 0:
        trip_count = max(0, (bound - start + step - 1) // step)
    elif cond.op == "<=" and step > 0:
        trip_count = max(0, (bound - start + step) // step)
    elif cond.op == ">" and step < 0:
        trip_count = max(0, (start - bound - step - 1) // (-step))
    elif cond.op == ">=" and step < 0:
        trip_count = max(0, (start - bound - step) // (-step))
    if trip_count is None:
        return None
    return var_name, start, step, trip_count


def _has_disallowed_loop_control(node) -> bool:
    risky = (
        c_ast.Break,
        c_ast.Continue,
        c_ast.Goto,
        c_ast.Label,
        c_ast.Switch,
        c_ast.Case,
        c_ast.Default,
        c_ast.Return,
        c_ast.For,
        c_ast.While,
        c_ast.DoWhile,
    )
    return contains_node_type(node, risky)


class _IVConstantReplacer(ASTTransformer):
    def __init__(self, iv_name: str, value: int):
        self.iv_name = iv_name
        self.value = value

    def visit_ID(self, node):
        if node.name == self.iv_name:
            return c_ast.Constant("int", str(self.value), coord=node.coord)
        return node


class _LoopCompoundTransformer(ASTTransformer):
    pass_name = "llvm-loop"

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node, detail=""):
        self.changed = True
        self.ctx.record(self.pass_name, action, _coord(node), detail)
        self.ctx.bump(f"{self.pass_name}.{action}")

    def _record(self, action, node, detail=""):
        self.ctx.record(self.pass_name, action, _coord(node), detail)
        self.ctx.bump(f"{self.pass_name}.{action}")

    def visit_Compound(self, node):
        self._visit_children(node)
        if not node.block_items:
            return node

        rewritten = []
        for item in node.block_items:
            replacement = self._rewrite_item(item)
            if replacement is None:
                rewritten.append(item)
                continue
            self.changed = True
            rewritten.extend(replacement)
        node.block_items = rewritten
        return node

    def _rewrite_item(self, item):
        return None


class _LoopDeletionTransformer(_LoopCompoundTransformer):
    pass_name = "loop-deletion"

    def _rewrite_item(self, item):
        if not isinstance(item, c_ast.For):
            return None
        trip_info = _compute_trip_count(item)
        if trip_info is None:
            return None
        _var_name, _start, _step, trip_count = trip_info
        if trip_count < 0:
            return None
        if _loop_body_items(item):
            return None
        self._mark("delete_empty_loop", item, f"trip_count={trip_count}")
        return []


class _LoopFullUnrollTransformer(_LoopCompoundTransformer):
    pass_name = "loop-unroll-full"
    max_trip_count = 4
    min_trip_count = 0

    def _rewrite_item(self, item):
        if not isinstance(item, c_ast.For):
            return None
        trip_info = _compute_trip_count(item)
        if trip_info is None:
            return None
        var_name, start, step, trip_count = trip_info
        if not (self.min_trip_count <= trip_count <= self.max_trip_count):
            return None

        body_items = _loop_body_items(item)
        if not body_items:
            self._mark("delete_empty_loop", item, f"trip_count={trip_count}")
            return []
        if _has_disallowed_loop_control(item.stmt):
            return None
        if any(isinstance(body_item, c_ast.Decl) for body_item in body_items):
            return None

        emitted = []
        for iteration in range(trip_count):
            value = start + iteration * step
            replacer = _IVConstantReplacer(var_name, value)
            for body_item in body_items:
                emitted.append(replacer.visit(copy.deepcopy(body_item)))

        self._mark("full_unroll", item, f"trip_count={trip_count}")
        return emitted


class _LoopUnrollTransformer(_LoopFullUnrollTransformer):
    pass_name = "loop-unroll"
    min_trip_count = 5
    max_trip_count = 8


class _SimpleLoopUnswitchTransformer(_LoopCompoundTransformer):
    pass_name = "simple-loop-unswitch"

    def _rewrite_item(self, item):
        if not isinstance(item, c_ast.For):
            return None

        if_node = None
        body = getattr(item, "stmt", None)
        if isinstance(body, c_ast.If):
            if_node = body
        elif isinstance(body, c_ast.Compound) and len(body.block_items or ()) == 1:
            only = body.block_items[0]
            if isinstance(only, c_ast.If):
                if_node = only
        if if_node is None:
            return None

        if not is_side_effect_free(if_node.cond):
            return None

        modified = set()
        if item.cond is not None:
            # The loop condition is reevaluated every iteration. Treat names
            # written there as loop-varying so we do not unswitch on a branch
            # that depends on a value updated by the condition itself, such as
            # `for (...; (c = z[i]) != 0; ...) if (c == delim)`.
            modified.update(self._collect_modified_names(item.cond))
        if item.stmt is not None:
            modified.update(self._collect_modified_names(item.stmt))
        if item.next is not None:
            modified.update(self._collect_modified_names(item.next))

        if collect_ids(if_node.cond).intersection(modified):
            return None

        then_loop = copy.deepcopy(item)
        then_loop.stmt = copy.deepcopy(if_node.iftrue) if if_node.iftrue is not None else _make_empty_compound(item.coord)

        else_loop = copy.deepcopy(item)
        else_loop.stmt = (
            copy.deepcopy(if_node.iffalse)
            if if_node.iffalse is not None
            else _make_empty_compound(item.coord)
        )

        self._mark("unswitch", item)
        return [
            c_ast.If(
                cond=copy.deepcopy(if_node.cond),
                iftrue=then_loop,
                iffalse=else_loop,
                coord=item.coord,
            )
        ]

    @staticmethod
    def _collect_modified_names(node) -> set[str]:
        names = set()
        if node is None:
            return names
        if isinstance(node, c_ast.Assignment):
            name = _assign_target_name(node.lvalue)
            if name:
                names.add(name)
        if isinstance(node, c_ast.UnaryOp) and node.op in ("++", "--", "p++", "p--"):
            if isinstance(node.expr, c_ast.ID):
                names.add(node.expr.name)
        if isinstance(node, c_ast.Decl) and node.name:
            names.add(node.name)
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                names.update(_SimpleLoopUnswitchTransformer._collect_modified_names(child))
        return names


class _LICMTransformer(_LoopCompoundTransformer):
    pass_name = "licm"

    def _rewrite_item(self, item):
        if not isinstance(item, c_ast.For):
            return None
        body = getattr(item, "stmt", None)
        if not isinstance(body, c_ast.Compound) or not body.block_items:
            return None

        declared = _collect_declared_names(body)
        if item.init is not None:
            declared.update(_collect_declared_names(item.init))

        hoisted = []
        remaining = []
        changed = False
        hoist_prefix = True
        trailing_modified = set()
        if item.next is not None:
            trailing_modified.update(self._collect_modified_names(item.next))
        sibling_modified = [
            self._collect_modified_names(stmt) for stmt in body.block_items
        ]
        sibling_has_call = [
            self._contains_func_call(stmt) for stmt in body.block_items
        ]
        next_has_call = (
            item.next is not None and self._contains_func_call(item.next)
        )
        for idx, stmt in enumerate(body.block_items):
            modified_elsewhere = set(trailing_modified)
            for other_idx, other_modified in enumerate(sibling_modified):
                if other_idx == idx:
                    continue
                modified_elsewhere.update(other_modified)
            siblings_contain_call = next_has_call or any(
                flag for other_idx, flag in enumerate(sibling_has_call)
                if other_idx != idx
            )
            if hoist_prefix and self._is_hoistable_invariant_assignment(
                stmt,
                modified_elsewhere,
                declared,
                siblings_contain_call,
            ):
                hoisted.append(copy.deepcopy(stmt))
                changed = True
                self._record("hoist_assignment", stmt)
                continue
            hoist_prefix = False
            remaining.append(stmt)

        if not changed:
            return None

        new_loop = copy.deepcopy(item)
        new_loop.stmt = _make_compound(remaining, coord=getattr(body, "coord", None))
        self._mark("licm", item, f"hoisted={len(hoisted)}")
        return hoisted + [new_loop]

    @staticmethod
    def _collect_modified_names(node) -> set[str]:
        return _SimpleLoopUnswitchTransformer._collect_modified_names(node)

    @staticmethod
    def _is_hoistable_invariant_assignment(
        stmt,
        modified_elsewhere: set[str],
        declared: set[str],
        siblings_contain_call: bool,
    ) -> bool:
        if not isinstance(stmt, c_ast.Assignment) or stmt.op != "=":
            return False
        if not isinstance(stmt.lvalue, c_ast.ID):
            return False
        target = stmt.lvalue.name
        if target in declared:
            return False
        if target in modified_elsewhere:
            return False
        if target in collect_ids(stmt.rvalue):
            return False
        if not is_side_effect_free(stmt.rvalue):
            return False
        if collect_ids(stmt.rvalue).intersection(modified_elsewhere):
            return False
        # Function calls in other statements may read OR write any
        # non-local name; we don't do alias analysis, so refuse to
        # hoist stores whose effect a call could observe. Without
        # this guard `for(...){g=0; bump();}` turns into
        # `g=0; for(...){bump();}` and the per-iteration reset is
        # lost (hanoi bench regression).
        if siblings_contain_call:
            return False
        return True

    @staticmethod
    def _contains_func_call(node) -> bool:
        if isinstance(node, c_ast.FuncCall):
            return True
        for _, child in (node.children() if hasattr(node, "children") else ()):
            if isinstance(child, c_ast.Node):
                if _LICMTransformer._contains_func_call(child):
                    return True
        return False


class LoopDeletionPass(ASTPass):
    name = "loop-deletion"

    def run(self, ast, ctx: PassContext):
        tx = _LoopDeletionTransformer(ctx)
        ast = tx.visit(ast)
        return ast if tx.changed else None


class LoopFullUnrollPass(ASTPass):
    name = "loop-unroll-full"

    def run(self, ast, ctx: PassContext):
        tx = _LoopFullUnrollTransformer(ctx)
        ast = tx.visit(ast)
        return ast if tx.changed else None


class LoopUnrollPass(ASTPass):
    name = "loop-unroll"

    def run(self, ast, ctx: PassContext):
        tx = _LoopUnrollTransformer(ctx)
        ast = tx.visit(ast)
        return ast if tx.changed else None


class SimpleLoopUnswitchPass(ASTPass):
    name = "simple-loop-unswitch"

    def run(self, ast, ctx: PassContext):
        tx = _SimpleLoopUnswitchTransformer(ctx)
        ast = tx.visit(ast)
        return ast if tx.changed else None


class LICMPass(ASTPass):
    name = "licm"

    def run(self, ast, ctx: PassContext):
        tx = _LICMTransformer(ctx)
        ast = tx.visit(ast)
        return ast if tx.changed else None


class _LoopRotateTransformer(_LoopCompoundTransformer):
    pass_name = "loop-rotate"

    def _rewrite_item(self, item):
        if not isinstance(item, c_ast.While):
            return None
        body = getattr(item, "stmt", None)
        if contains_node_type(body, (c_ast.Goto, c_ast.Label)):
            return None
        rotated = c_ast.If(
            cond=copy.deepcopy(item.cond),
            iftrue=c_ast.DoWhile(
                cond=copy.deepcopy(item.cond),
                stmt=copy.deepcopy(body),
                coord=item.coord,
            ),
            iffalse=None,
            coord=item.coord,
        )
        self._mark("rotate", item)
        return [rotated]


class LoopRotatePass(ASTPass):
    name = "loop-rotate"

    def run(self, ast, ctx: PassContext):
        tx = _LoopRotateTransformer(ctx)
        ast = tx.visit(ast)
        return ast if tx.changed else None


class _IndVarsTransformer(ASTTransformer):
    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node):
        self.changed = True
        self.ctx.record("indvars", action, _coord(node))
        self.ctx.bump(f"indvars.{action}")

    def visit_For(self, node):
        self._visit_children(node)
        next_expr = getattr(node, "next", None)
        if not isinstance(next_expr, c_ast.Assignment) or next_expr.op != "=":
            return node
        if not isinstance(next_expr.lvalue, c_ast.ID):
            return node
        var_name = next_expr.lvalue.name
        rhs = next_expr.rvalue
        if not isinstance(rhs, c_ast.BinaryOp):
            return node

        value = None
        if rhs.op == "+":
            if isinstance(rhs.left, c_ast.ID) and rhs.left.name == var_name:
                value = get_int_value(rhs.right)
            elif isinstance(rhs.right, c_ast.ID) and rhs.right.name == var_name:
                value = get_int_value(rhs.left)
            if value is None:
                return node
            if value == 1:
                self._mark("normalize_increment", node)
                node.next = c_ast.UnaryOp("p++", c_ast.ID(var_name, coord=rhs.coord), coord=node.coord)
            else:
                self._mark("normalize_step", node)
                node.next = c_ast.Assignment(
                    "+=",
                    c_ast.ID(var_name, coord=rhs.coord),
                    c_ast.Constant("int", str(value), coord=rhs.coord),
                    coord=node.coord,
                )
            return node

        if (
            rhs.op == "-"
            and isinstance(rhs.left, c_ast.ID)
            and rhs.left.name == var_name
        ):
            value = get_int_value(rhs.right)
            if value is None:
                return node
            if value == 1:
                self._mark("normalize_decrement", node)
                node.next = c_ast.UnaryOp("p--", c_ast.ID(var_name, coord=rhs.coord), coord=node.coord)
            else:
                self._mark("normalize_step", node)
                node.next = c_ast.Assignment(
                    "-=",
                    c_ast.ID(var_name, coord=rhs.coord),
                    c_ast.Constant("int", str(value), coord=rhs.coord),
                    coord=node.coord,
                )
        return node


class IndVarsPass(ASTPass):
    name = "indvars"

    def run(self, ast, ctx: PassContext):
        tx = _IndVarsTransformer(ctx)
        ast = tx.visit(ast)
        return ast if tx.changed else None
