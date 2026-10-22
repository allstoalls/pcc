"""Conservative proof for a single entry's compiler-consumed unsafe imports.

This is an edge proof, not module dead-code elimination. A source containing
any other import is deliberately ineligible: a reachable provider may have a
lazy dependency that the existing module-scope closure scan does not discover.
Use the original source and owned raw AST so annotations, bindings and lazy
bodies cannot disappear behind import masking or AST lifting.
"""

from pcc.frontends.python.unsafe_intrinsic_policy import (
    UNSAFE_PROVIDER_INDEPENDENT_ZERO_ARG_RETURNS,
)


# Explicit raw-parser shapes. Unknown nodes fail closed instead of looking like
# leaves. The contract test checks these fields against the parser dataclasses;
# adding syntax requires reviewing its binding and reflection behavior here.
_RAW_FIELDS = {
    "_Pass": (),
    "_Return": ("value",),
    "_Expr": ("expr",),
    "_Num": ("text", "is_int"),
    "_ComplexNum": ("text",),
    "_Str": ("parts",),
    "_Bytes": ("parts",),
    "_Name": ("ident",),
    "_BinOp": ("op", "lhs", "rhs"),
    "_Call": ("func", "args"),
    "_If": ("cond", "body", "else_body"),
    "_FuncDef": ("name", "params", "body", "decorators", "returns", "is_async"),
    "_Await": ("value",),
    "_Assign": ("target", "value", "annotation", "has_value"),
    "_AugAssign": ("target", "op", "value"),
    "_While": ("cond", "body", "else_body"),
    "_For": ("target", "iter", "body", "else_body", "is_async"),
    "_Break": (),
    "_Continue": (),
    "_Import": ("names",),
    "_ImportFrom": ("module", "names", "level"),
    "_ClassDef": ("name", "bases", "body", "decorators"),
    "_Raise": ("exc", "cause"),
    "_Try": ("body", "handlers", "else_body", "finally_body"),
    "_Attr": ("obj", "name"),
    "_Subscript": ("obj", "idx"),
    "_Slice": ("lo", "hi", "step"),
    "_List": ("elems",),
    "_Tuple": ("elems",),
    "_UnaryOp": ("op", "operand"),
    "_BoolOp": ("op", "values"),
    "_Compare": ("op", "lhs", "rhs"),
    "_Bool": ("value",),
    "_None": (),
    "_Dict": ("keys", "values"),
    "_DictCompElt": ("key", "value"),
    "_Set": ("elems",),
    "_Lambda": ("params", "body"),
    "_Del": ("targets",),
    "_Ternary": ("then_expr", "cond", "else_expr"),
    "_Comp": ("kind", "elt", "generators"),
    "_Yield": ("value", "is_from"),
    "_Starred": ("value", "is_kw"),
    "_Global": ("names",),
    "_Nonlocal": ("names",),
    "_With": ("items", "body", "is_async"),
    "_Assert": ("test", "msg"),
}

_REFLECTIVE_NAMES = frozenset({
    "__import__", "__builtins__", "__loader__", "__spec__", "__package__",
    "eval", "exec", "compile", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "hasattr", "importlib", "builtins",
    "help", "breakpoint",
})

# Frame, traceback, generator/coroutine and code-object families can recover
# globals or builtins without a reflective Name (including through string keys).
_REFLECTIVE_ATTRIBUTE_PREFIXES = ("__", "gi_", "cr_", "ag_", "tb_", "f_", "co_")

_HEADER_FIELDS = frozenset({
    "annotation", "returns", "params", "decorators", "bases",
})


def _only_consumed_unsafe_imports(module) -> bool:
    """Prove every imported binding is consumed without a provider signature."""
    bindings: dict[str, int] = {}
    for index, statement in enumerate(module.body):
        if type(statement).__name__ != "_ImportFrom":
            continue
        if statement.module != "pcc.unsafe" or statement.level != 0:
            return False
        if not statement.names:
            return False
        for imported, alias in statement.names:
            if imported not in UNSAFE_PROVIDER_INDEPENDENT_ZERO_ARG_RETURNS:
                return False
            local = alias or imported
            # Raw private names can be mangled during lifting. Do not attempt
            # to prove identity across that rewrite or implicit special names.
            if (local in bindings or local in _REFLECTIVE_NAMES
                    or "__" in local):
                return False
            bindings[local] = index
    if not bindings:
        return False

    used: set[str] = set()
    pending = []
    for index, statement in enumerate(module.body):
        if type(statement).__name__ == "_ImportFrom":
            continue
        pending.append((statement, index, True))
    while pending:
        node, index, allow_call = pending.pop()
        if node is None or isinstance(node, (int, float, bool)):
            continue
        if isinstance(node, str):
            # Binding names live in strings (parameters, handlers, globals,
            # nonlocals, definitions), not only in expression Name nodes.
            if node in bindings or node in _REFLECTIVE_NAMES:
                return False
            continue
        if isinstance(node, (tuple, list)):
            for item in node:
                pending.append((item, index, allow_call))
            continue
        kind = type(node).__name__
        if kind not in _RAW_FIELDS:
            return False
        if kind in ("_Import", "_ImportFrom"):
            # No conditional/local imports, star imports or other providers.
            return False
        if kind == "_Name":
            if node.ident in bindings or node.ident in _REFLECTIVE_NAMES:
                return False
            continue
        if kind == "_Attr":
            for prefix in _REFLECTIVE_ATTRIBUTE_PREFIXES:
                if node.name.startswith(prefix):
                    return False
        if kind == "_Call" and type(node.func).__name__ == "_Name":
            local = node.func.ident
            if local in bindings:
                # Raw args include keyword pairs and * / ** expansions.
                if (not allow_call or not isinstance(node.args, list)
                        or node.args or index <= bindings[local]):
                    return False
                used.add(local)
                continue
        if kind in ("_Str", "_Bytes"):
            # Ordinary string data cannot read a binding without reflection;
            # f-strings have separate nodes and are conservatively rejected.
            continue
        for field in _RAW_FIELDS[kind]:
            if not hasattr(node, field):
                return False
            pending.append((
                getattr(node, field), index,
                allow_call and field not in _HEADER_FIELDS,
            ))
    return len(used) == len(bindings)


def source_has_only_consumed_unsafe_imports(source: str) -> bool:
    """Fail closed at parser/shape gaps; never use host ast for this proof."""
    if "pcc.unsafe" not in source:
        return False
    from pcc.frontends.python.pipeline_import_scan import (
        _iter_source_import_from_specs,
        _iter_source_import_specs,
    )
    # Cheap rejection only. The scanner drops aliases and statement identity,
    # so it can never establish eligibility without the complete raw AST.
    if _iter_source_import_specs(source, top_level_only=False):
        return False
    imports = _iter_source_import_from_specs(source, top_level_only=False)
    if not imports:
        return False
    for module_name, names in imports:
        if module_name != "pcc.unsafe" or not names:
            return False
        for name in names:
            if name not in UNSAFE_PROVIDER_INDEPENDENT_ZERO_ARG_RETURNS:
                return False
    from pcc.frontends.python.py_parse import parse

    try:
        module = parse(source, filename="<unsafe-import-proof>")
        return _only_consumed_unsafe_imports(module)
    except Exception:
        return False
