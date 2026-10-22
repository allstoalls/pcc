"""Pure AST analysis helpers for nested function hoisting.

This module intentionally contains no lowering state; callers pass AST
nodes and name lists in, and get plain Python data structures back.
"""
from __future__ import annotations

from pcc.frontends.python.py_ast import Arg, Assign, Attr, AugAssign, BinOp, BoolExpr, BoolLit, BoolType, Break, Call, ClassDef, ClassType, Compare, Continue, Delete, DictExpr, DictType, DynType, ExceptHandler, Expr, ExprStmt, FloatLit, FloatType, For, FuncDef, FuncType, Global, If, IfExpr, Import, ImportFrom, IntLit, IntType, Lambda, ListExpr, ListType, Module, Name, Nonlocal, NoneLit, NoneType, Pass, Raise, Return, Slice, SourceSpan, Stmt, StrLit, StrType, Subscript, Try, TupleExpr, TupleType, Type, UnaryOp, While, With

_Assign = Assign
_Attr = Attr
_AugAssign = AugAssign
_BinOp = BinOp
_BoolExpr = BoolExpr
_Break = Break
_Call = Call
_ClassDef = ClassDef
_Compare = Compare
_Continue = Continue
_Delete = Delete
_DictExpr = DictExpr
_DynType = DynType
_ExprStmt = ExprStmt
_ExprStmt2 = ExprStmt
_For = For
_FuncDef = FuncDef
_GL = Global
_Global = Global
_If = If
_IfExpr = IfExpr
_Import = Import
_ImportFrom = ImportFrom
_ImportFromStmt = ImportFrom
_ImportStmt = Import
_Lambda = Lambda
_ListExpr = ListExpr
_Name = Name
_NL = Nonlocal
_Nonlocal = Nonlocal
_Pass = Pass
_Raise = Raise
_Return = Return
_Slice = Slice
_Subscript = Subscript
_TopImport = Import
_TopImportFrom = ImportFrom
_Try = Try
_TupleExpr = TupleExpr
_UnaryOp = UnaryOp
_While = While
_With = With
_DYN = DynType(name="dyn")

_PY_BUILTINS_NS = ('ArithmeticError',
 'AssertionError',
 'AttributeError',
 'BaseException',
 'BaseExceptionGroup',
 'BlockingIOError',
 'BrokenPipeError',
 'BufferError',
 'BytesWarning',
 'ChildProcessError',
 'ConnectionAbortedError',
 'ConnectionError',
 'ConnectionRefusedError',
 'ConnectionResetError',
 'DeprecationWarning',
 'EOFError',
 'Ellipsis',
 'EncodingWarning',
 'EnvironmentError',
 'Exception',
 'ExceptionGroup',
 'False',
 'FileExistsError',
 'FileNotFoundError',
 'FloatingPointError',
 'FutureWarning',
 'GeneratorExit',
 'IOError',
 'ImportError',
 'ImportWarning',
 'IndentationError',
 'IndexError',
 'InterruptedError',
 'IsADirectoryError',
 'KeyError',
 'KeyboardInterrupt',
 'LookupError',
 'MemoryError',
 'ModuleNotFoundError',
 'NameError',
 'None',
 'NotADirectoryError',
 'NotImplemented',
 'NotImplementedError',
 'OSError',
 'OverflowError',
 'PendingDeprecationWarning',
 'PermissionError',
 'ProcessLookupError',
 'PythonFinalizationError',
 'RecursionError',
 'ReferenceError',
 'ResourceWarning',
 'RuntimeError',
 'RuntimeWarning',
 'StopAsyncIteration',
 'StopIteration',
 'SyntaxError',
 'SyntaxWarning',
 'SystemError',
 'SystemExit',
 'TabError',
 'TimeoutError',
 'True',
 'TypeError',
 'UnboundLocalError',
 'UnicodeDecodeError',
 'UnicodeEncodeError',
 'UnicodeError',
 'UnicodeTranslateError',
 'UnicodeWarning',
 'UserWarning',
 'ValueError',
 'Warning',
 'ZeroDivisionError',
 '_IncompleteInputError',
 '__build_class__',
 '__debug__',
 '__doc__',
 '__import__',
 '__loader__',
 '__name__',
 '__package__',
 '__spec__',
 'abs',
 'aiter',
 'all',
 'anext',
 'any',
 'ascii',
 'bin',
 'bool',
 'breakpoint',
 'bytearray',
 'bytes',
 'callable',
 'chr',
 'classmethod',
 'compile',
 'complex',
 'copyright',
 'credits',
 'delattr',
 'dict',
 'dir',
 'divmod',
 'enumerate',
 'eval',
 'exec',
 'exit',
 'filter',
 'float',
 'format',
 'frozenset',
 'getattr',
 'globals',
 'hasattr',
 'hash',
 'help',
 'hex',
 'id',
 'input',
 'int',
 'isinstance',
 'issubclass',
 'iter',
 'len',
 'license',
 'list',
 'locals',
 'map',
 'max',
 'memoryview',
 'min',
 'next',
 'object',
 'oct',
 'open',
 'ord',
 'pow',
 'print',
 'property',
 'quit',
 'range',
 'repr',
 'reversed',
 'round',
 'set',
 'setattr',
 'slice',
 'sorted',
 'staticmethod',
 'str',
 'sum',
 'super',
 'tuple',
 'type',
 'vars',
 'zip')


_FIELD_NAMES_BY_KIND = {
    "SourceSpan": ("file", "line", "col", "end_line", "end_col"),
    "IntType": ("name", "width"),
    "FloatType": ("name", "width"),
    "BoolType": ("name",),
    "NoneType": ("name",),
    "StrType": ("name",),
    "ListType": ("name", "elem"),
    "DictType": ("name", "key", "value"),
    "TupleType": ("name", "elems"),
    "FuncType": ("name", "params", "ret"),
    "ClassType": ("name", "module", "fields", "bases"),
    "DynType": ("name",),
    "Type": ("name",),
    "NoneLit": ("span", "ty"),
    "IntLit": ("span", "ty", "value"),
    "FloatLit": ("span", "ty", "value"),
    "BoolLit": ("span", "ty", "value"),
    "StrLit": ("span", "ty", "value"),
    "Name": ("span", "ty", "ident"),
    "BinOp": ("span", "ty", "op", "lhs", "rhs"),
    "UnaryOp": ("span", "ty", "op", "operand"),
    "Compare": ("span", "ty", "op", "lhs", "rhs"),
    "BoolExpr": ("span", "ty", "op", "left", "right"),
    "Call": ("span", "ty", "func", "args", "kwargs", "operand_order", "is_set_literal"),
    "Attr": ("span", "ty", "obj", "name"),
    "Subscript": ("span", "ty", "obj", "idx"),
    "Slice": ("span", "ty", "lo", "hi", "step"),
    "ListExpr": ("span", "ty", "elems"),
    "DictExpr": ("span", "ty", "pairs"),
    "TupleExpr": ("span", "ty", "elems"),
    "IfExpr": ("span", "ty", "cond", "then_e", "else_e"),
    "Lambda": ("span", "ty", "params", "body"),
    "Assign": ("span", "targets", "value", "annotation", "has_value"),
    "AugAssign": ("span", "target", "op", "value"),
    "ExprStmt": ("span", "expr"),
    "If": ("span", "cond", "body", "else_body"),
    "While": ("span", "cond", "body", "else_body"),
    "For": ("span", "target", "iter", "body", "else_body"),
    "Return": ("span", "value"),
    "Pass": ("span",),
    "Break": ("span",),
    "Continue": ("span",),
    "Raise": ("span", "exc", "cause"),
    "Try": ("span", "body", "handlers", "else_body", "finally_body"),
    "With": ("span", "items", "body"),
    "Import": ("span", "names"),
    "ImportFrom": ("span", "module", "names", "level"),
    "Global": ("span", "names"),
    "Nonlocal": ("span", "names"),
    "Delete": ("span", "targets"),
    "FuncDef": (
        "span",
        "name",
        "args",
        "return_ty",
        "body",
        "decorators",
        "is_method",
        "is_async",
    ),
    "ClassDef": (
        "span",
        "name",
        "bases",
        "keywords",
        "body",
        "decorators",
    ),
    "Arg": ("name", "annotation", "default", "kind", "has_default"),
    "ExceptHandler": ("exc_type", "name", "body", "span"),
    "Module": ("name", "body", "docstring"),
}




def _dataclass_field_value(obj, field_name: str, default=None):
    return getattr(obj, field_name, default)


def _dataclass_field_names(obj):
    if obj is None:
        return ()
    if (
        isinstance(obj, str)
        or isinstance(obj, int)
        or isinstance(obj, bool)
        or isinstance(obj, float)
        or isinstance(obj, bytes)
    ):
        return ()
    kind = type(obj).__name__
    if kind.startswith("_"):
        kind = kind[1:]
    cached = _FIELD_NAMES_BY_KIND.get(kind)
    if cached is not None:
        return cached
    if kind == "SourceSpan":
        return ("file", "line", "col", "end_line", "end_col")
    if kind == "IntType" or kind == "FloatType":
        return ("name", "width")
    if kind == "BoolType" or kind == "NoneType" or kind == "StrType":
        return ("name",)
    if kind == "ListType":
        return ("name", "elem")
    if kind == "DictType":
        return ("name", "key", "value")
    if kind == "TupleType":
        return ("name", "elems")
    if kind == "FuncType":
        return ("name", "params", "ret")
    if kind == "ClassType":
        return ("name", "module", "fields", "bases")
    if kind == "DynType" or kind == "Type":
        return ("name",)
    if kind == "NoneLit":
        return ("span", "ty")
    if (
        kind == "IntLit"
        or kind == "FloatLit"
        or kind == "BoolLit"
        or kind == "StrLit"
    ):
        return ("span", "ty", "value")
    if kind == "Name":
        return ("span", "ty", "ident")
    if kind == "BinOp":
        return ("span", "ty", "op", "lhs", "rhs")
    if kind == "UnaryOp":
        return ("span", "ty", "op", "operand")
    if kind == "Compare":
        return ("span", "ty", "op", "lhs", "rhs")
    if kind == "BoolExpr":
        return ("span", "ty", "op", "left", "right")
    if kind == "Call":
        return ("span", "ty", "func", "args", "kwargs", "operand_order", "is_set_literal")
    if kind == "Attr":
        return ("span", "ty", "obj", "name")
    if kind == "Subscript":
        return ("span", "ty", "obj", "idx")
    if kind == "Slice":
        return ("span", "ty", "lo", "hi", "step")
    if kind == "ListExpr":
        return ("span", "ty", "elems")
    if kind == "DictExpr":
        return ("span", "ty", "pairs")
    if kind == "TupleExpr":
        return ("span", "ty", "elems")
    if kind == "IfExpr":
        return ("span", "ty", "cond", "then_e", "else_e")
    if kind == "Lambda":
        return ("span", "ty", "params", "body")
    if kind == "Assign":
        return ("span", "targets", "value", "annotation", "has_value")
    if kind == "AugAssign":
        return ("span", "target", "op", "value")
    if kind == "ExprStmt":
        return ("span", "expr")
    if kind == "If":
        return ("span", "cond", "body", "else_body")
    if kind == "While":
        return ("span", "cond", "body", "else_body")
    if kind == "For":
        return ("span", "target", "iter", "body", "else_body")
    if kind == "Return":
        return ("span", "value")
    if kind == "Pass" or kind == "Break" or kind == "Continue":
        return ("span",)
    if kind == "Raise":
        return ("span", "exc", "cause")
    if kind == "Try":
        return ("span", "body", "handlers", "else_body", "finally_body")
    if kind == "With":
        return ("span", "items", "body")
    if kind == "Import":
        return ("span", "names")
    if kind == "ImportFrom":
        return ("span", "module", "names", "level")
    if kind == "Global":
        return ("span", "names")
    if kind == "Nonlocal":
        return ("span", "names")
    if kind == "Delete":
        return ("span", "targets")
    if kind == "FuncDef":
        return (
            "span",
            "name",
            "args",
            "return_ty",
            "body",
            "decorators",
            "is_method",
            "is_async",
        )
    if kind == "ClassDef":
        return (
            "span",
            "name",
            "bases",
            "keywords",
            "body",
            "decorators",
        )
    if kind == "Arg":
        return ("name", "annotation", "default", "kind", "has_default")
    if kind == "ExceptHandler":
        return ("exc_type", "name", "body", "span")
    if kind == "Module":
        return ("name", "body", "docstring")
    if isinstance(obj, SourceSpan):
        return ("file", "line", "col", "end_line", "end_col")
    if isinstance(obj, IntType) or isinstance(obj, FloatType):
        return ("name", "width")
    if isinstance(obj, BoolType):
        return ("name",)
    if isinstance(obj, NoneType):
        return ("name",)
    if isinstance(obj, StrType):
        return ("name",)
    if isinstance(obj, ListType):
        return ("name", "elem")
    if isinstance(obj, DictType):
        return ("name", "key", "value")
    if isinstance(obj, TupleType):
        return ("name", "elems")
    if isinstance(obj, FuncType):
        return ("name", "params", "ret")
    if isinstance(obj, ClassType):
        return ("name", "module", "fields", "bases")
    if isinstance(obj, DynType):
        return ("name",)
    if isinstance(obj, Type):
        return ("name",)
    if isinstance(obj, Expr):
        if isinstance(obj, NoneLit):
            return ("span", "ty")
        if (
            isinstance(obj, IntLit)
            or isinstance(obj, FloatLit)
            or isinstance(obj, BoolLit)
            or isinstance(obj, StrLit)
        ):
            return ("span", "ty", "value")
        if isinstance(obj, Name):
            return ("span", "ty", "ident")
        if isinstance(obj, BinOp):
            return ("span", "ty", "op", "lhs", "rhs")
        if isinstance(obj, UnaryOp):
            return ("span", "ty", "op", "operand")
        if isinstance(obj, Compare):
            return ("span", "ty", "op", "lhs", "rhs")
        if isinstance(obj, BoolExpr):
            return ("span", "ty", "op", "left", "right")
        if isinstance(obj, Call):
            return ("span", "ty", "func", "args", "kwargs", "operand_order", "is_set_literal")
        if isinstance(obj, Attr):
            return ("span", "ty", "obj", "name")
        if isinstance(obj, Subscript):
            return ("span", "ty", "obj", "idx")
        if isinstance(obj, Slice):
            return ("span", "ty", "lo", "hi", "step")
        if isinstance(obj, ListExpr):
            return ("span", "ty", "elems")
        if isinstance(obj, DictExpr):
            return ("span", "ty", "pairs")
        if isinstance(obj, TupleExpr):
            return ("span", "ty", "elems")
        if isinstance(obj, IfExpr):
            return ("span", "ty", "cond", "then_e", "else_e")
        if isinstance(obj, Lambda):
            return ("span", "ty", "params", "body")
    if isinstance(obj, Stmt):
        if isinstance(obj, Assign):
            return ("span", "targets", "value", "annotation", "has_value")
        if isinstance(obj, AugAssign):
            return ("span", "target", "op", "value")
        if isinstance(obj, ExprStmt):
            return ("span", "expr")
        if isinstance(obj, If):
            return ("span", "cond", "body", "else_body")
        if isinstance(obj, While):
            return ("span", "cond", "body", "else_body")
        if isinstance(obj, For):
            return ("span", "target", "iter", "body", "else_body")
        if isinstance(obj, Return):
            return ("span", "value")
        if isinstance(obj, Pass) or isinstance(obj, Break) or isinstance(obj, Continue):
            return ("span",)
        if isinstance(obj, Raise):
            return ("span", "exc", "cause")
        if isinstance(obj, Try):
            return ("span", "body", "handlers", "else_body", "finally_body")
        if isinstance(obj, With):
            return ("span", "items", "body")
        if isinstance(obj, Import):
            return ("span", "names")
        if isinstance(obj, ImportFrom):
            return ("span", "module", "names", "level")
        if isinstance(obj, Global):
            return ("span", "names")
        if isinstance(obj, Nonlocal):
            return ("span", "names")
        if isinstance(obj, Delete):
            return ("span", "targets")
        if isinstance(obj, FuncDef):
            return (
                "span",
                "name",
                "args",
                "return_ty",
                "body",
                "decorators",
                "is_method",
                "is_async",
            )
        if isinstance(obj, ClassDef):
            return (
                "span",
                "name",
                "bases",
                "keywords",
                "body",
                "decorators",
            )
    if isinstance(obj, Arg):
        return ("name", "annotation", "default", "kind", "has_default")
    if isinstance(obj, ExceptHandler):
        return ("exc_type", "name", "body", "span")
    if isinstance(obj, Module):
        return ("name", "body", "docstring")
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is not None:
        return fields.keys()
    return ()


def _hoist_node_kind(obj) -> str:
    if obj is None:
        return ""
    name = type(obj).__name__
    if name.startswith("_"):
        return name[1:]
    return name


def _hoist_is_funcdef(obj) -> bool:
    return isinstance(obj, _FuncDef) or _hoist_node_kind(obj) == "FuncDef"


def _hoist_is_classdef(obj) -> bool:
    return isinstance(obj, _ClassDef) or _hoist_node_kind(obj) == "ClassDef"


_HOIST_PROFILE_KEYS = (
    "compute_free_names_calls",
    "compute_free_names_cache_hits",
    "called_sibling_names_calls",
    "called_sibling_names_cache_hits",
    "referenced_sibling_names_calls",
    "referenced_sibling_names_cache_hits",
    "sibling_effective_free_names_calls",
    "sibling_effective_free_names_cache_hits",
)


def hoist_stat_inc(enabled: bool, stats, name: str) -> None:
    if not enabled:
        return
    stats[name] = stats.get(name, 0) + 1


def write_hoist_profile(enabled: bool, path: str, stats) -> None:
    if not enabled:
        return
    with open(path, "w", encoding="utf-8") as f:
        for key in _HOIST_PROFILE_KEYS:
            f.write(key + "=" + str(stats.get(key, 0)) + "\n")


def clone_funcdef(fd, name, args, return_ty, body):
    return FuncDef(
        span=fd.span,
        name=name,
        args=args,
        return_ty=return_ty,
        body=body,
        decorators=fd.decorators,
        is_method=fd.is_method,
        is_async=fd.is_async,
        has_return_annotation=fd.has_return_annotation,
        manual_pointer_abi=fd.manual_pointer_abi,
    )


def name_in(items, name):
    for item in items:
        if item == name:
            return True
    return False


def append_name_once(items, name):
    if not name_in(items, name):
        items.append(name)


def extend_names_once(items, names):
    for name in names:
        append_name_once(items, name)


def copy_names(names):
    out = []
    extend_names_once(out, names)
    return out


def _import_names_from_stmt(stmt):
    pairs = []
    raw_names = getattr(stmt, "names", ())
    for raw_name in raw_names:
        if isinstance(raw_name, (tuple, list)) and len(raw_name) >= 2:
            bound = raw_name[1] if raw_name[1] is not None else raw_name[0]
            pairs.append((raw_name[0], bound))
        elif isinstance(raw_name, (tuple, list)) and len(raw_name) >= 1:
            pairs.append((raw_name[0], raw_name[0]))
        elif hasattr(raw_name, "asname"):
            bound = getattr(raw_name, "asname", None)
            if bound is None:
                bound = getattr(raw_name, "name", None)
            pairs.append((getattr(raw_name, "name", bound), bound))
        elif isinstance(raw_name, str):
            pairs.append((raw_name, raw_name))
    return tuple(pairs)


def _is_import_stmt(stmt):
    if type(stmt).__name__ in {"Global", "Nonlocal"}:
        return False
    if isinstance(stmt, (Global, Nonlocal)):
        return False
    if type(stmt).__name__ in {"Import", "ImportFrom"}:
        return True
    if isinstance(stmt, (_ImportStmt, _ImportFromStmt)):
        return True
    raw_names = getattr(stmt, "names", ())
    if not raw_names:
        return False
    if not isinstance(raw_names, (tuple, list)):
        return False
    return all(
        isinstance(item, (tuple, list, str)) or hasattr(item, "name")
        for item in raw_names
    )


def _is_import_from_stmt(stmt):
    if type(stmt).__name__ == "ImportFrom":
        return True
    if isinstance(stmt, (_ImportFromStmt,)):
        return True
    names = getattr(stmt, "names", ())
    return (
        hasattr(stmt, "module")
        and hasattr(stmt, "names")
        and isinstance(names, (tuple, list))
        and bool(names)
    )


def copy_name_map(src):
    out = {}
    for key, value in src.items():
        out[key] = value
    return out


def update_name_map(dst, src):
    for key, value in src.items():
        dst[key] = value


def is_discard_capture_name(name):
    return (
        name == "_"
        or name == "*"
        or name == "**"
        or name.startswith("__nested_")
    )


def filter_capture_names(names):
    out = []
    for name in names:
        if not is_discard_capture_name(name):
            out.append(name)
    return tuple(out)


def filter_self_capture_names(names, original_name, hoisted_name):
    out = []
    for name in names:
        if (
            name != original_name
            and name != hoisted_name
            and not is_discard_capture_name(name)
        ):
            out.append(name)
    return tuple(out)


def body_reads_self_walk(x):
    if isinstance(x, _Name) and x.ident == "self":
        return True
    for slot in _dataclass_field_names(x):
        v = _dataclass_field_value(x, slot, None)
        if isinstance(v, tuple):
            for it in v:
                if body_reads_self_walk(it):
                    return True
        else:
            if body_reads_self_walk(v):
                return True
    return False


def body_reads_self(stmts):
    """Return True if any Name(ident='self') appears read in ``stmts``."""
    for s in stmts:
        if body_reads_self_walk(s):
            return True
    return False


def body_uses_name_walk(
    x,
    target,
    is_call_func=False,
    in_lambda=False,
):
    if x is None:
        return False
    if isinstance(x, tuple):
        for it in x:
            if body_uses_name_walk(
                it,
                target,
                in_lambda=in_lambda,
            ):
                return True
        return False
    if isinstance(x, _Name):
        return x.ident == target and (not is_call_func or in_lambda)
    if isinstance(x, _Lambda):
        for p in x.params:
            if body_uses_name_walk(p.default, target, in_lambda=in_lambda):
                return True
        from pcc.frontends.python.codegen.hoist_boxing import function_local_bindings
        lambda_fd = FuncDef(
            span=x.span, name="", args=x.params, return_ty=_DYN,
            body=(Return(span=x.span, value=x.body),),
            is_async=False, decorators=(),
        )
        if name_in(function_local_bindings(lambda_fd), target):
            return False
        return body_uses_name_walk(
            x.body,
            target,
            in_lambda=True,
        )
    if isinstance(x, _ClassDef):
        if (body_uses_name_walk(x.decorators, target, in_lambda=in_lambda)
                or body_uses_name_walk(x.bases, target, in_lambda=in_lambda)
                or body_uses_name_walk(x.keywords, target, in_lambda=in_lambda)):
            return True
        return _class_body_uses_enclosing_value(x.body, target, in_lambda)
    if isinstance(x, _Call):
        if body_uses_name_walk(
            x.func,
            target,
            is_call_func=True,
            in_lambda=in_lambda,
        ):
            return True
        for a in x.args:
            if body_uses_name_walk(a, target, in_lambda=in_lambda):
                return True
        for _, v in x.kwargs:
            if body_uses_name_walk(v, target, in_lambda=in_lambda):
                return True
        return False
    if isinstance(x, _Attr):
        return body_uses_name_walk(x.obj, target, in_lambda=in_lambda)
    if isinstance(x, _Subscript):
        return body_uses_name_walk(
            x.obj,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.idx,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _Slice):
        return body_uses_name_walk(
            x.lo,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.hi,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.step,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _BinOp):
        return body_uses_name_walk(
            x.lhs,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.rhs,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _UnaryOp):
        return body_uses_name_walk(x.operand, target, in_lambda=in_lambda)
    if isinstance(x, _Compare):
        return body_uses_name_walk(
            x.lhs,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.rhs,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _BoolExpr):
        return body_uses_name_walk(
            x.left,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.right,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _IfExpr):
        return body_uses_name_walk(
            x.cond,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.then_e,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.else_e,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, (_ListExpr, _TupleExpr)):
        for it in x.elems:
            if body_uses_name_walk(it, target, in_lambda=in_lambda):
                return True
        return False
    if isinstance(x, _DictExpr):
        for k, v in x.pairs:
            if body_uses_name_walk(
                k,
                target,
                in_lambda=in_lambda,
            ) or body_uses_name_walk(
                v,
                target,
                in_lambda=in_lambda,
            ):
                return True
        return False
    if isinstance(x, _ExprStmt):
        return body_uses_name_walk(x.expr, target, in_lambda=in_lambda)
    if isinstance(x, _Assign):
        return body_uses_name_walk(x.value, target, in_lambda=in_lambda)
    if isinstance(x, _AugAssign):
        return body_uses_name_walk(
            x.target,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.value,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _Return):
        return body_uses_name_walk(x.value, target, in_lambda=in_lambda)
    if isinstance(x, _If):
        return body_uses_name_walk(
            x.cond,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.body,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.else_body,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _While):
        return body_uses_name_walk(
            x.cond,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.body,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.else_body,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _For):
        return body_uses_name_walk(
            x.iter,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.body,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.else_body,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _Try):
        if body_uses_name_walk(x.body, target, in_lambda=in_lambda):
            return True
        for h in x.handlers:
            if body_uses_name_walk(
                _dataclass_field_value(h, "exc_type", None),
                target,
                in_lambda=in_lambda,
            ) or body_uses_name_walk(
                _dataclass_field_value(h, "body", ()),
                target,
                in_lambda=in_lambda,
            ):
                return True
        return body_uses_name_walk(
            x.else_body,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.finally_body,
            target,
            in_lambda=in_lambda,
        )
    if isinstance(x, _With):
        for ctx, _as_var in x.items:
            if body_uses_name_walk(ctx, target, in_lambda=in_lambda):
                return True
        return body_uses_name_walk(x.body, target, in_lambda=in_lambda)
    if isinstance(x, _Raise):
        return body_uses_name_walk(
            x.exc,
            target,
            in_lambda=in_lambda,
        ) or body_uses_name_walk(
            x.cause,
            target,
            in_lambda=in_lambda,
        )
    return False


def _function_body_uses_enclosing_value(fd, target, in_lambda):
    from pcc.frontends.python.codegen.hoist_boxing import function_local_bindings, scope_declared_names
    if (name_in(function_local_bindings(fd), target)
            or name_in(scope_declared_names(fd.body, True, False), target)):
        return False
    return body_uses_name_walk(fd.body, target, in_lambda=in_lambda)


def _class_body_uses_enclosing_value(body, target, in_lambda):
    from pcc.frontends.python.codegen.hoist_boxing import collect_scope_bindings, scope_declared_names
    if name_in(scope_declared_names(body, True, False), target):
        return False
    shadowed = (name_in(collect_scope_bindings(body), target)
                and not name_in(scope_declared_names(body, False, True), target))
    pending = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, _FuncDef):
            if not shadowed:
                if body_uses_name_walk(node.decorators, target, in_lambda=in_lambda):
                    return True
                for arg in node.args:
                    if body_uses_name_walk(arg.default, target, in_lambda=in_lambda):
                        return True
            # A method's lexical parent skips the class namespace.
            if _function_body_uses_enclosing_value(node, target, in_lambda):
                return True
        elif isinstance(node, _ClassDef):
            if not shadowed and body_uses_name_walk(node, target, in_lambda=in_lambda):
                return True
            if shadowed and _class_body_uses_enclosing_value(node.body, target, in_lambda):
                return True
        elif not shadowed:
            if body_uses_name_walk(node, target, in_lambda=in_lambda):
                return True
        elif isinstance(node, (_If, _While, _For, _With, _Try)):
            for field in ("body", "else_body", "finally_body"):
                pending.extend(_dataclass_field_value(node, field, ()))
            for handler in _dataclass_field_value(node, "handlers", ()):
                pending.extend(_dataclass_field_value(handler, "body", ()))
    return False


def body_uses_name_as_value(stmts, target_name):
    """Return True if ``target_name`` appears as a non-Call reference."""
    for s in stmts:
        if isinstance(s, _FuncDef) and s.name == target_name:
            continue
        if body_uses_name_walk(s, target_name):
            return True
    return False


def body_returns_name_walk_block(block, target):
    for s in block:
        if isinstance(s, _FuncDef):
            continue
        if (
            isinstance(s, _Return)
            and isinstance(s.value, _Name)
            and s.value.ident == target
        ):
            return True
        if isinstance(s, (_If, _While, _For)):
            if body_returns_name_walk_block(
                s.body, target
            ) or body_returns_name_walk_block(
                s.else_body,
                target,
            ):
                return True
            continue
        if isinstance(s, _Try):
            if body_returns_name_walk_block(s.body, target):
                return True
            for h in s.handlers:
                if body_returns_name_walk_block(
                    _dataclass_field_value(h, "body", ()),
                    target,
                ):
                    return True
            if body_returns_name_walk_block(
                s.else_body,
                target,
            ) or body_returns_name_walk_block(
                s.finally_body,
                target,
            ):
                return True
            continue
        if isinstance(s, _With):
            if body_returns_name_walk_block(s.body, target):
                return True
    return False


def body_returns_name(stmts, target_name):
    return body_returns_name_walk_block(stmts, target_name)


def body_augassigns_free_name_walk(x, local_names):
    if isinstance(x, _AugAssign) and isinstance(x.target, _Name):
        if not name_in(local_names, x.target.ident):
            return True
    for slot in _dataclass_field_names(x):
        v = _dataclass_field_value(x, slot, None)
        if isinstance(v, tuple):
            for it in v:
                if body_augassigns_free_name_walk(it, local_names):
                    return True
        else:
            if body_augassigns_free_name_walk(v, local_names):
                return True
    return False


def body_augassigns_free_name(fd, excluded):
    """Return True if the nested def mutates a free variable."""
    param_names = []
    for a in fd.args:
        if a.name != "":
            append_name_once(param_names, a.name)
    assigned_names = []
    for s in fd.body:
        if isinstance(s, _Assign):
            for t in s.targets:
                if isinstance(t, _Name):
                    append_name_once(assigned_names, t.ident)
    local = []
    extend_names_once(local, param_names)
    extend_names_once(local, assigned_names)

    for s in fd.body:
        if body_augassigns_free_name_walk(s, local):
            return True
    return False


_HOIST_FAST_YIELD_NAMES = ("_yield", "_yield_from", "__yield__", "__yield_from__")


def _hoist_fast_expr_may_need(expr) -> bool:
    if expr is None:
        return False
    if isinstance(expr, _Lambda):
        return True
    if isinstance(expr, _Call):
        if (
            isinstance(expr.func, _Name)
            and expr.func.ident in _HOIST_FAST_YIELD_NAMES
        ):
            return True
        if _hoist_fast_expr_may_need(expr.func):
            return True
        for arg in expr.args:
            if _hoist_fast_expr_may_need(arg):
                return True
        for _key, value in expr.kwargs:
            if _hoist_fast_expr_may_need(value):
                return True
        return False
    if isinstance(expr, tuple):
        for item in expr:
            if _hoist_fast_expr_may_need(item):
                return True
        return False
    for slot in _dataclass_field_names(expr):
        if slot in ("span", "ty", "annotation", "return_ty"):
            continue
        value = _dataclass_field_value(expr, slot, None)
        if isinstance(value, tuple):
            for item in value:
                if _hoist_fast_expr_may_need(item):
                    return True
        elif _hoist_fast_expr_may_need(value):
            return True
    return False


def _hoist_fast_block_may_need(block) -> bool:
    for stmt in block:
        if _hoist_fast_stmt_may_need(stmt):
            return True
    return False


def _hoist_fast_stmt_may_need(stmt) -> bool:
    if _hoist_is_funcdef(stmt) or _hoist_is_classdef(stmt):
        return True
    for slot in _dataclass_field_names(stmt):
        if slot in ("span", "annotation", "return_ty"):
            continue
        value = _dataclass_field_value(stmt, slot, None)
        if slot in ("body", "else_body", "finally_body"):
            if _hoist_fast_block_may_need(value):
                return True
            continue
        if slot == "handlers":
            for handler in value:
                if _hoist_fast_expr_may_need(
                    _dataclass_field_value(handler, "exc_type", None)
                ):
                    return True
                if _hoist_fast_block_may_need(
                    _dataclass_field_value(handler, "body", ())
                ):
                    return True
            continue
        if isinstance(value, tuple):
            for item in value:
                if _hoist_fast_expr_may_need(item):
                    return True
        elif _hoist_fast_expr_may_need(value):
            return True
    return False


def module_may_need_hoist_fast(stmts) -> bool:
    """Cheap pre-scan for modules that cannot need nested-def hoisting.

    The full hoist routine is intentionally feature-rich, but most modules
    have no nested function/class, lambda, or generator-yield sentinel inside
    a function body.  Skip constructing the heavier helper graph for those
    modules.
    """

    for stmt in stmts:
        if _hoist_is_funcdef(stmt):
            if _hoist_fast_block_may_need(_dataclass_field_value(stmt, "body", ())):
                return True
            continue
        if _hoist_is_classdef(stmt):
            for item in _dataclass_field_value(stmt, "body", ()):
                if _hoist_is_funcdef(item) and _hoist_fast_block_may_need(
                    _dataclass_field_value(item, "body", ())
                ):
                    return True
            continue
    return False
