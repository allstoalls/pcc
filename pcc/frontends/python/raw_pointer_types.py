"""Verified extern pointer markers shared by inference and export metadata."""

from __future__ import annotations

from pcc.frontends.python.py_ast import (
    Assign, ClassDef, ClassType, FuncDef, Import, ImportFrom, Name, RawPointerType,
)


TYPE_RAW_POINTER = RawPointerType(name="pcc.extern.c_rawptr")


def raw_pointer_annotation_names(module):
    """Names proven to denote imported markers, rather than a user's class.

    A subsequent binding shadows an import. Unqualified spellings alone are
    deliberately insufficient: an ordinary ``class c_ptr`` remains managed.
    """
    markers = set()
    modules = {}
    for stmt in module.body:
        if isinstance(stmt, ImportFrom):
            for name, alias in stmt.names:
                local = alias or name
                markers.discard(local)
                modules.pop(local, None)
                if not stmt.level and stmt.module == "pcc.extern" and name in ("c_ptr", "c_rawptr"):
                    markers.add(local)
                elif not stmt.level and stmt.module == "pcc" and name == "extern":
                    modules[local] = "pcc.extern"
        elif isinstance(stmt, Import):
            for name, alias in stmt.names:
                local = alias or name.split(".")[0]
                markers.discard(local)
                modules.pop(local, None)
                if name == "pcc.extern":
                    modules[local] = "pcc.extern" if alias else "pcc"
                elif name == "pcc":
                    modules[local] = "pcc"
        elif isinstance(stmt, (ClassDef, FuncDef)):
            markers.discard(stmt.name)
            modules.pop(stmt.name, None)
        elif isinstance(stmt, Assign):
            for target in stmt.targets:
                if isinstance(target, Name):
                    markers.discard(target.ident)
                    modules.pop(target.ident, None)
    for local, module_name in modules.items():
        prefix = local + ".extern" if module_name == "pcc" else local
        markers.add(prefix + ".c_ptr")
        markers.add(prefix + ".c_rawptr")
    return markers


def resolve_raw_pointer_annotation(ty, marker_names):
    """Canonicalize only a verified, unresolved marker annotation."""
    if not isinstance(ty, ClassType) or ty.fields or ty.bases or ty.valueclass:
        return ty
    name = ty.module + "." + ty.name if ty.module else ty.name
    if name in marker_names:
        return TYPE_RAW_POINTER
    return ty


def verified_c_abi_export_symbol(module, function):
    """The actual supported export spelling, backed by a real extern import."""
    from pcc.frontends.python.py_ast import (
        Attr,
        BoolType,
        Call,
        DynType,
        FloatType,
        IntType,
        NoneType,
        StrLit,
        TupleExpr,
    )
    factories = ('c_abi_export', 'c_abi_variadic_export', 'c_abi_typed_export')
    names = {}
    modules = {}
    for stmt in module.body:
        if isinstance(stmt, ImportFrom):
            for name, alias in stmt.names:
                local = alias or name
                names.pop(local, None)
                modules.pop(local, None)
                if not stmt.level and stmt.module == 'pcc.extern' and name in factories:
                    names[local] = name
                elif not stmt.level and stmt.module == 'pcc' and name == 'extern':
                    modules[local] = 'pcc.extern'
        elif isinstance(stmt, Import):
            for name, alias in stmt.names:
                local = alias or name.split('.')[0]
                names.pop(local, None)
                modules.pop(local, None)
                if name in ('pcc', 'pcc.extern'):
                    modules[local] = name if alias else 'pcc'
        elif isinstance(stmt, (FuncDef, ClassDef)):
            names.pop(stmt.name, None)
            modules.pop(stmt.name, None)
        elif isinstance(stmt, Assign):
            for target in stmt.targets:
                if isinstance(target, Name):
                    names.pop(target.ident, None)
                    modules.pop(target.ident, None)
    for decorator in function.decorators:
        if not isinstance(decorator, Call):
            continue
        callee = decorator.func
        factory = ''
        if isinstance(callee, Name) and callee.ident in factories:
            factory = names.get(callee.ident, '')
        elif isinstance(callee, Attr) and callee.name in factories:
            owner = callee.obj
            if isinstance(owner, Name) and owner.ident == 'extern' and modules.get('extern') == 'pcc.extern':
                factory = callee.name
            elif (isinstance(owner, Attr) and owner.name == 'extern'
                  and isinstance(owner.obj, Name) and owner.obj.ident == 'pcc'
                  and modules.get('pcc') == 'pcc'):
                factory = callee.name
        count = 3 if factory == 'c_abi_typed_export' else 1
        if factory and len(decorator.args) == count and isinstance(decorator.args[0], StrLit):
            if factory == 'c_abi_typed_export':
                # A generic sibling declaration must match the real typed
                # override, not just its symbol. Unsupported projections stay
                # unqualified rather than asserting an incorrect ptr ABI.
                result = decorator.args[1]
                parameters = decorator.args[2]
                if not isinstance(result, StrLit) or not isinstance(parameters, TupleExpr):
                    return ''
                raw_names = raw_pointer_annotation_names(module)
                annotations = [arg.annotation for arg in function.args if arg.name]
                annotations.append(function.return_ty)
                projections = []
                for index, annotation in enumerate(annotations):
                    annotation = resolve_raw_pointer_annotation(annotation, raw_names)
                    if annotation is None or isinstance(annotation, (DynType, RawPointerType)):
                        projection = 'ptr'
                    elif isinstance(annotation, IntType):
                        projection = 'i64'
                    elif isinstance(annotation, FloatType):
                        projection = 'f64'
                    elif isinstance(annotation, BoolType):
                        projection = 'i1'
                    elif isinstance(annotation, NoneType):
                        projection = 'void' if index == len(annotations) - 1 else 'ptr'
                    else:
                        return ''
                    projections.append(projection)
                if len(parameters.elems) != len(projections) - 1 or result.value != projections[-1]:
                    return ''
                for index, parameter in enumerate(parameters.elems):
                    if not isinstance(parameter, StrLit) or parameter.value != projections[index]:
                        return ''
            return decorator.args[0].value
    return ''
