"""Shared AST visitor/transformer utilities for pass implementations."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast


# ---------------------------------------------------------------------------
# Explicit per-node child handlers.
#
# Each handler visits a concrete ``c_ast.Node`` instance's children and
# rewrites those children in-place when the transformer returns a new
# node (or drops it from a list). Using a registry keyed by concrete
# class identity avoids ``getattr``/``setattr`` with dynamic attribute
# names — ``scripts/audit_selfhost.py`` flags that pattern as a
# self-host blocker under the ``dynamic-attr`` kind.
# ---------------------------------------------------------------------------


def _visit_list(transformer, lst):
    """Visit every ``c_ast.Node`` in *lst* and return a new list."""
    if lst is None:
        return None
    new_list = []
    for item in lst:
        if isinstance(item, c_ast.Node):
            result = transformer.visit(item)
            if result is not None:
                new_list.append(result)
        else:
            new_list.append(item)
    return new_list


def _visit_maybe(transformer, child):
    """Visit a single optional child node; passthrough if not a Node."""
    if isinstance(child, c_ast.Node):
        return transformer.visit(child)
    return child


# ---------------------------------------------------------------------------
# Per-class child handlers.
#
# Each function receives (transformer, node) and mutates ``node`` in place
# by visiting each child field explicitly by concrete attribute name.
# Fields named in the former ``_list_attrs`` map are rewritten via
# ``_visit_list``; ``_node_attrs`` fields go through ``_visit_maybe``.
# Fields that never hold ``c_ast.Node`` children (``name``, ``op``, ``type``
# string fields, etc.) are left untouched, matching the previous behaviour.
# ---------------------------------------------------------------------------


def _children_FileAST(t, n):
    n.ext = _visit_list(t, n.ext)


def _children_Compound(t, n):
    n.block_items = _visit_list(t, n.block_items)


def _children_ParamList(t, n):
    n.params = _visit_list(t, n.params)


def _children_ExprList(t, n):
    n.exprs = _visit_list(t, n.exprs)


def _children_InitList(t, n):
    n.exprs = _visit_list(t, n.exprs)


def _children_DeclList(t, n):
    n.decls = _visit_list(t, n.decls)


def _children_Case(t, n):
    n.stmts = _visit_list(t, n.stmts)
    n.expr = _visit_maybe(t, n.expr)


def _children_Default(t, n):
    n.stmts = _visit_list(t, n.stmts)


def _children_EnumeratorList(t, n):
    n.enumerators = _visit_list(t, n.enumerators)


def _children_BinaryOp(t, n):
    n.left = _visit_maybe(t, n.left)
    n.right = _visit_maybe(t, n.right)


def _children_UnaryOp(t, n):
    n.expr = _visit_maybe(t, n.expr)


def _children_Assignment(t, n):
    n.lvalue = _visit_maybe(t, n.lvalue)
    n.rvalue = _visit_maybe(t, n.rvalue)


def _children_Decl(t, n):
    n.type = _visit_maybe(t, n.type)
    n.init = _visit_maybe(t, n.init)
    n.bitsize = _visit_maybe(t, n.bitsize)


def _children_If(t, n):
    n.cond = _visit_maybe(t, n.cond)
    n.iftrue = _visit_maybe(t, n.iftrue)
    n.iffalse = _visit_maybe(t, n.iffalse)


def _children_For(t, n):
    n.init = _visit_maybe(t, n.init)
    n.cond = _visit_maybe(t, n.cond)
    n.next = _visit_maybe(t, n.next)
    n.stmt = _visit_maybe(t, n.stmt)


def _children_While(t, n):
    n.cond = _visit_maybe(t, n.cond)
    n.stmt = _visit_maybe(t, n.stmt)


def _children_DoWhile(t, n):
    n.cond = _visit_maybe(t, n.cond)
    n.stmt = _visit_maybe(t, n.stmt)


def _children_Switch(t, n):
    n.cond = _visit_maybe(t, n.cond)
    n.stmt = _visit_maybe(t, n.stmt)


def _children_Return(t, n):
    n.expr = _visit_maybe(t, n.expr)


def _children_FuncCall(t, n):
    n.name = _visit_maybe(t, n.name)
    n.args = _visit_maybe(t, n.args)


def _children_Cast(t, n):
    n.to_type = _visit_maybe(t, n.to_type)
    n.expr = _visit_maybe(t, n.expr)


def _children_TernaryOp(t, n):
    n.cond = _visit_maybe(t, n.cond)
    n.iftrue = _visit_maybe(t, n.iftrue)
    n.iffalse = _visit_maybe(t, n.iffalse)


def _children_ArrayRef(t, n):
    n.name = _visit_maybe(t, n.name)
    n.subscript = _visit_maybe(t, n.subscript)


def _children_StructRef(t, n):
    n.name = _visit_maybe(t, n.name)
    n.field = _visit_maybe(t, n.field)


def _children_FuncDef(t, n):
    n.decl = _visit_maybe(t, n.decl)
    n.body = _visit_maybe(t, n.body)


def _children_TypeDecl(t, n):
    n.type = _visit_maybe(t, n.type)


def _children_PtrDecl(t, n):
    n.type = _visit_maybe(t, n.type)


def _children_ArrayDecl(t, n):
    n.type = _visit_maybe(t, n.type)
    n.dim = _visit_maybe(t, n.dim)


def _children_FuncDecl(t, n):
    n.args = _visit_maybe(t, n.args)
    n.type = _visit_maybe(t, n.type)


def _children_Typename(t, n):
    n.type = _visit_maybe(t, n.type)


def _children_NamedInitializer(t, n):
    n.expr = _visit_maybe(t, n.expr)


def _children_Label(t, n):
    n.stmt = _visit_maybe(t, n.stmt)


def _children_Alignas(t, n):
    n.alignment = _visit_maybe(t, n.alignment)


def _children_StaticAssert(t, n):
    n.cond = _visit_maybe(t, n.cond)
    n.message = _visit_maybe(t, n.message)


def _children_noop(t, n):
    """No children to recurse into (Goto, Break, Continue, ...)."""
    return


# Dispatch registry, keyed by concrete class. Populated lazily to keep
# the import cycle shallow; callers go through ``_get_children_handler``
# so ``None`` means "no registered recursion — treat as leaf".
_CHILDREN_HANDLERS: dict[type, object] = {}

# Explicit name → class lookup table used by ``_build_dispatch`` to
# resolve ``visit_<ClassName>`` methods into concrete ``c_ast`` node
# classes. Kept as an explicit dict so the self-host audit does not
# flag a ``getattr(c_ast, dynamic_name)`` here.
_NODE_NAME_TO_CLASS: dict[str, type] = {
    "Alignas": c_ast.Alignas,
    "ArrayDecl": c_ast.ArrayDecl,
    "ArrayRef": c_ast.ArrayRef,
    "Assignment": c_ast.Assignment,
    "BinaryOp": c_ast.BinaryOp,
    "Break": c_ast.Break,
    "Case": c_ast.Case,
    "Cast": c_ast.Cast,
    "Compound": c_ast.Compound,
    "CompoundLiteral": c_ast.CompoundLiteral,
    "ComputedGoto": c_ast.ComputedGoto,
    "Constant": c_ast.Constant,
    "Continue": c_ast.Continue,
    "Decl": c_ast.Decl,
    "DeclList": c_ast.DeclList,
    "Default": c_ast.Default,
    "DoWhile": c_ast.DoWhile,
    "EllipsisParam": c_ast.EllipsisParam,
    "EmptyStatement": c_ast.EmptyStatement,
    "Enum": c_ast.Enum,
    "Enumerator": c_ast.Enumerator,
    "EnumeratorList": c_ast.EnumeratorList,
    "ExprList": c_ast.ExprList,
    "FileAST": c_ast.FileAST,
    "For": c_ast.For,
    "FuncCall": c_ast.FuncCall,
    "FuncDecl": c_ast.FuncDecl,
    "FuncDef": c_ast.FuncDef,
    "GenericAssociation": c_ast.GenericAssociation,
    "GenericSelection": c_ast.GenericSelection,
    "Goto": c_ast.Goto,
    "ID": c_ast.ID,
    "IdentifierType": c_ast.IdentifierType,
    "If": c_ast.If,
    "InitList": c_ast.InitList,
    "Label": c_ast.Label,
    "LabelAddress": c_ast.LabelAddress,
    "NamedInitializer": c_ast.NamedInitializer,
    "ParamList": c_ast.ParamList,
    "PtrDecl": c_ast.PtrDecl,
    "RangeDesignator": c_ast.RangeDesignator,
    "Return": c_ast.Return,
    "StaticAssert": c_ast.StaticAssert,
    "StmtExpr": c_ast.StmtExpr,
    "Struct": c_ast.Struct,
    "StructRef": c_ast.StructRef,
    "Switch": c_ast.Switch,
    "TernaryOp": c_ast.TernaryOp,
    "TypeDecl": c_ast.TypeDecl,
    "Typedef": c_ast.Typedef,
    "Typename": c_ast.Typename,
    "UnaryOp": c_ast.UnaryOp,
    "Union": c_ast.Union,
    "While": c_ast.While,
}


def _get_children_handler(node):
    if not _CHILDREN_HANDLERS:
        _CHILDREN_HANDLERS.update({
            c_ast.FileAST: _children_FileAST,
            c_ast.Compound: _children_Compound,
            c_ast.ParamList: _children_ParamList,
            c_ast.ExprList: _children_ExprList,
            c_ast.InitList: _children_InitList,
            c_ast.DeclList: _children_DeclList,
            c_ast.Case: _children_Case,
            c_ast.Default: _children_Default,
            c_ast.EnumeratorList: _children_EnumeratorList,
            c_ast.BinaryOp: _children_BinaryOp,
            c_ast.UnaryOp: _children_UnaryOp,
            c_ast.Assignment: _children_Assignment,
            c_ast.Decl: _children_Decl,
            c_ast.If: _children_If,
            c_ast.For: _children_For,
            c_ast.While: _children_While,
            c_ast.DoWhile: _children_DoWhile,
            c_ast.Switch: _children_Switch,
            c_ast.Return: _children_Return,
            c_ast.FuncCall: _children_FuncCall,
            c_ast.Cast: _children_Cast,
            c_ast.TernaryOp: _children_TernaryOp,
            c_ast.ArrayRef: _children_ArrayRef,
            c_ast.StructRef: _children_StructRef,
            c_ast.FuncDef: _children_FuncDef,
            c_ast.TypeDecl: _children_TypeDecl,
            c_ast.PtrDecl: _children_PtrDecl,
            c_ast.ArrayDecl: _children_ArrayDecl,
            c_ast.FuncDecl: _children_FuncDecl,
            c_ast.Typename: _children_Typename,
            c_ast.NamedInitializer: _children_NamedInitializer,
            c_ast.Label: _children_Label,
            c_ast.Alignas: _children_Alignas,
            c_ast.StaticAssert: _children_StaticAssert,
            c_ast.Goto: _children_noop,
            c_ast.EmptyStatement: _children_noop,
            c_ast.Continue: _children_noop,
            c_ast.Break: _children_noop,
        })
    return _CHILDREN_HANDLERS.get(type(node))


class ASTTransformer:
    """Bottom-up AST transformer. Override ``_dispatch_visitor`` via the
    ``visit_<NodeType>`` convention to transform nodes.

    Return the node (possibly modified) to keep it.
    Return a different node to replace it.
    Return None to remove it (only valid in list contexts).
    """

    def visit(self, node):
        if node is None:
            return None
        visitor = self._resolve_visitor(type(node))
        return visitor(node)

    def _resolve_visitor(self, cls):
        owner = type(self)
        disp = owner.__dict__.get("_dispatch")
        if disp is None:
            disp = owner._build_dispatch()
            owner._dispatch = disp
        fn = disp.get(cls)
        if fn is None:
            return self.generic_visit
        return fn.__get__(self, owner)

    @classmethod
    def _build_dispatch(cls) -> dict[type, object]:
        """Collect ``visit_<ConcreteClassName>`` methods into a dispatch map.

        Walks the MRO so subclass overrides win. Matches concrete
        ``c_ast`` node classes by exact name via ``_NODE_NAME_TO_CLASS``
        — an explicit registry, so the self-host audit does not flag a
        runtime ``getattr(c_ast, <dynamic_name>)`` here.
        """
        table: dict[type, object] = {}
        seen: set[str] = set()
        for klass in cls.__mro__:
            for attr_name, attr_val in klass.__dict__.items():
                if not attr_name.startswith("visit_"):
                    continue
                if not callable(attr_val):
                    continue
                node_name = attr_name[len("visit_"):]
                if node_name in seen:
                    continue
                seen.add(node_name)
                node_cls = _NODE_NAME_TO_CLASS.get(node_name)
                if node_cls is not None:
                    table[node_cls] = attr_val
        return table

    def visit_StaticAssert(self, node):
        # Assertions must reach semantic checking with their original operands.
        # Optimizations may otherwise erase invalid operands or signed overflow.
        return node

    def generic_visit(self, node):
        """Recursively visit children, then return the node."""
        self._visit_children(node)
        return node

    def _visit_children(self, node):
        """Visit and potentially replace all children of a node."""
        if node is None:
            return
        handler = _get_children_handler(node)
        if handler is not None:
            handler(self, node)


def is_constant_int(node) -> bool:
    """Check if node is an integer constant."""
    return isinstance(node, c_ast.Constant) and node.type == "int"


def is_plain_decimal_int_constant(node) -> bool:
    if not is_constant_int(node):
        return False
    value = getattr(node, "value", "")
    return (
        isinstance(value, str)
        and value.isdigit()
        and (value == "0" or not value.startswith("0"))
    )


def is_constant_float(node) -> bool:
    return isinstance(node, c_ast.Constant) and node.type in ("float", "double")


def get_int_value(node) -> int | None:
    """Extract integer value from a Constant node."""
    if not is_constant_int(node):
        return None
    try:
        val = node.value.rstrip("uUlL")
        if val.startswith("0x") or val.startswith("0X"):
            return int(val, 16)
        if val.startswith("0b") or val.startswith("0B"):
            return int(val, 2)
        if val.startswith("0") and len(val) > 1 and val[1:].isdigit():
            return int(val, 8)
        return int(val)
    except (ValueError, TypeError):
        return None


def get_safe_int_value(node) -> int | None:
    """Extract an int value only for plain decimal literals.

    Canonicalization runs before typed codegen, so hex/octal/suffixed literals
    are not safe to fold there: their width and unsignedness matter.
    """
    if not is_plain_decimal_int_constant(node):
        return None
    try:
        return int(node.value)
    except (ValueError, TypeError):
        return None


def make_int_constant(value: int, coord=None) -> c_ast.Constant:
    """Create an integer Constant node."""
    return c_ast.Constant("int", str(value), coord=coord)


def make_float_constant(value: float, coord=None) -> c_ast.Constant:
    return c_ast.Constant("double", repr(value), coord=coord)


def nodes_equal(a, b) -> bool:
    """Check structural equality of two AST nodes (conservative)."""
    if type(a) != type(b):
        return False
    if isinstance(a, c_ast.ID) and isinstance(b, c_ast.ID):
        return a.name == b.name
    if isinstance(a, c_ast.Constant) and isinstance(b, c_ast.Constant):
        return a.type == b.type and a.value == b.value
    return False


def is_side_effect_free(node) -> bool:
    """Conservative check: can this expression be safely removed?"""
    if isinstance(node, (c_ast.Constant, c_ast.ID)):
        return True
    if isinstance(node, c_ast.BinaryOp):
        return is_side_effect_free(node.left) and is_side_effect_free(node.right)
    if isinstance(node, c_ast.UnaryOp):
        if node.op in ("++", "--", "p++", "p--"):
            return False
        return is_side_effect_free(node.expr)
    if isinstance(node, c_ast.Cast):
        return is_side_effect_free(node.expr)
    if isinstance(node, c_ast.TernaryOp):
        return (
            is_side_effect_free(node.cond)
            and is_side_effect_free(node.iftrue)
            and is_side_effect_free(node.iffalse)
        )
    # FuncCall, Assignment, etc. have side effects
    return False


def collect_ids(node) -> set[str]:
    """Collect all ID names referenced in a subtree."""
    names = set()
    if node is None:
        return names
    if isinstance(node, c_ast.ID):
        names.add(node.name)
    for _, child in node.children():
        if isinstance(child, c_ast.Node):
            names.update(collect_ids(child))
    return names


def contains_node_type(node, node_types) -> bool:
    """Return True if a subtree contains any node of the given type(s)."""
    if node is None:
        return False
    if isinstance(node, node_types):
        return True
    for _, child in node.children():
        if isinstance(child, c_ast.Node) and contains_node_type(child, node_types):
            return True
    return False


def has_unstructured_control_flow(funcdef) -> bool:
    """Detect constructs that make naive AST rewrites unsafe."""
    if funcdef is None:
        return False
    risky_nodes = (
        c_ast.Goto,
        c_ast.Label,
        c_ast.Switch,
        c_ast.Case,
        c_ast.Default,
    )
    return contains_node_type(getattr(funcdef, "body", None), risky_nodes)


def has_type_sensitive_introspection(funcdef) -> bool:
    """Detect contexts where source-level type preservation matters."""
    body = getattr(funcdef, "body", None)
    if body is None:
        return False

    if contains_node_type(body, c_ast.GenericSelection):
        return True

    def _walk(node):
        if node is None:
            return False
        if isinstance(node, c_ast.UnaryOp) and node.op in (
            "sizeof",
            "_Alignof",
            "__alignof",
            "__alignof__",
        ):
            return True
        for _, child in node.children():
            if isinstance(child, c_ast.Node) and _walk(child):
                return True
        return False

    return _walk(body)
