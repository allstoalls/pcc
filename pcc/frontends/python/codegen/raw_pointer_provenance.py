"""Prove an explicit raw ABI view without globally retyping opaque pointers."""
from pcc.frontends.python.py_ast import (
    Assign, Attr, AugAssign, Call, ClassDef, Delete, DynType, ExceptHandler,
    For, FuncDef, Global, IfExpr, Import, ImportFrom, IntType, Lambda, Name, Nonlocal,
    RawPointerType, TupleExpr, ListExpr, With,
)
from pcc.frontends.python.py_ast_contract import PY_AST_FIELD_NAME_OVERRIDES

# These are explicit unsafe pointer operations, not Python-object producers.
# The result stays unknown in inference: only an explicit c_ptr destination
# selects this unmanaged ABI view. In particular, tag_int is NOT a raw address.
_RAW_VIEW_INTRINSICS = frozenset({
    'malloc', 'calloc', 'realloc', 'cstr', 'global_addr', 'function_addr',
    'global_load_ptr', 'ptr_add', 'stack_alloc', 'int_to_ptr', 'null', 'frame_address', 'load_ptr',
    'memset', 'memcpy', 'memmove', 'getenv', 'call_ptr0', 'call_ptr1', 'call_ptr2',
    'call_ptr3', 'call_ptr4', 'call_ptr_ptr_i64', 'call_ptr_ptr_ptr_i32',
    'call_ptr_ptr_ptr_i64_ptr', 'call_ptr_i64_i64', 'dynamic_library_open',
    'dynamic_library_open_global', 'dynamic_library_symbol', 'darwin_libsystem_symbol',
    'darwin_errno_location', 'page_alloc', 'getcwd', 'va_arg_ptr', 'va_cursor',
    'uname_field', 'initial_environ', 'va_start',
})


def _binds(target, name):
    if isinstance(target, Name):
        return target.ident == name
    if isinstance(target, (TupleExpr, ListExpr)):
        return any(_binds(item, name) for item in target.elems)
    return False


def _local_sources(function, name):
    """All lexical stores, conservatively rejecting non-simple bindings."""
    sources = []
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if node is None or isinstance(node, (str, int, float, bool, bytes)):
            continue
        if isinstance(node, (Global, Nonlocal)) and name in node.names:
            return None
        if isinstance(node, (FuncDef, ClassDef)):
            if node.name == name:
                return None
            continue
        if isinstance(node, Lambda):
            continue
        if isinstance(node, Assign):
            if any(_binds(target, name) for target in node.targets):
                if node.annotation is not None and not isinstance(node.annotation, RawPointerType):
                    return None
                if not node.has_value:
                    continue
                if not all(isinstance(target, Name) for target in node.targets):
                    return None
                sources.append(node.value)
        elif isinstance(node, (Delete, AugAssign)):
            targets = node.targets if isinstance(node, Delete) else (node.target,)
            if any(_binds(target, name) for target in targets):
                return None
        elif isinstance(node, For) and _binds(node.target, name):
            return None
        elif isinstance(node, With):
            if any(target is not None and _binds(target, name) for _expr, target in node.items):
                return None
        elif isinstance(node, ExceptHandler) and node.name == name:
            return None
        elif isinstance(node, Import):
            if any((alias or module.split('.')[0]) == name for module, alias in node.names):
                return None
        elif isinstance(node, ImportFrom):
            if any((alias or imported) == name for imported, alias in node.names):
                return None
        elif isinstance(node, Call) and isinstance(node.func, Name) and node.func.ident == '_walrus':
            if node.args and _binds(node.args[0], name):
                return None
        if isinstance(node, (tuple, list)):
            pending.extend(node)
            continue
        for field in PY_AST_FIELD_NAME_OVERRIDES.get(type(node).__name__, ()):
            if field not in ('span', 'ty', 'annotation', 'return_ty'):
                pending.append(getattr(node, field, None))
    return sources


def raw_abi_expression_provenance(host, expr, visiting=()):
    """True only for a proven low-level view at an explicit raw destination.

    Unknown parameters and Python results do not qualify merely because their
    physical representation is ptr. Every local store must have a raw origin;
    mixed branches, rebinding, deletion and lexical shadowing fail closed.
    """
    if isinstance(expr, Name):
        function = host.current_func_def
        if function is None or expr.ident in visiting:
            return False
        sources = _local_sources(function, expr.ident)
        if sources is None:
            return False
        for arg in function.args:
            if arg.name == expr.ident:
                if not isinstance(arg.annotation, RawPointerType):
                    return False
                if not sources:
                    return True
        if not sources:
            return False
        return all(raw_abi_expression_provenance(host, source, visiting + (expr.ident,))
                   for source in sources)
    if isinstance(expr.ty, RawPointerType):
        return True
    if not isinstance(expr.ty, (DynType, IntType)):
        return False
    if isinstance(expr, IfExpr):
        return (raw_abi_expression_provenance(host, expr.then_e, visiting)
                and raw_abi_expression_provenance(host, expr.else_e, visiting))
    if not isinstance(expr, Call) or not isinstance(expr.func, Name):
        return False
    name = expr.func.ident
    if name in host.functions:
        return False
    if host._unsafe_intrinsic_for_name(name) in _RAW_VIEW_INTRINSICS:
        return True
    if name in host.env or name in getattr(host, '_module_globals', {}):
        return False
    declaration = getattr(host, '_extern_decls', {}).get(name)
    if declaration is not None:
        restype = declaration[2]
        return restype == 'c_rawptr' or (
            restype == 'c_ptr' and not host._raw_addresses_are_ints()
        )
    return False
