"""Conservative AST-level global dead-code elimination."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import collect_ids
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


def _is_static_decl(decl) -> bool:
    storage = tuple(getattr(decl, "storage", ()) or ())
    return "static" in storage


class GlobalDCEPass(ASTPass):
    """Remove unreachable static functions from the translation unit."""

    name = "global-dce"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST) or not ast.ext:
            return None

        funcdefs: dict[str, c_ast.FuncDef] = {}
        static_func_names: set[str] = set()
        static_func_decl_names: set[str] = set()
        root_names: set[str] = set()
        global_init_refs: set[str] = set()

        for ext in ast.ext:
            if isinstance(ext, c_ast.FuncDef) and getattr(ext, "decl", None) is not None:
                name = ext.decl.name
                if not name:
                    continue
                funcdefs[name] = ext
                if _is_static_decl(ext.decl):
                    static_func_names.add(name)
                else:
                    root_names.add(name)
            elif (
                isinstance(ext, c_ast.Decl)
                and isinstance(ext.type, c_ast.FuncDecl)
                and ext.name
                and _is_static_decl(ext)
            ):
                static_func_decl_names.add(ext.name)
            elif isinstance(ext, c_ast.Decl) and ext.init is not None:
                global_init_refs.update(collect_ids(ext.init))

        if not static_func_names and not static_func_decl_names:
            return None

        roots = set(root_names)
        roots.update(ctx.entry_roots)
        roots.update(name for name in global_init_refs if name in funcdefs)

        reachable: set[str] = set()
        worklist = list(roots)
        while worklist:
            name = worklist.pop()
            if name in reachable:
                continue
            funcdef = funcdefs.get(name)
            if funcdef is None:
                continue
            reachable.add(name)
            for ref_name in collect_ids(funcdef.body):
                if ref_name in funcdefs and ref_name not in reachable:
                    worklist.append(ref_name)

        removed = 0
        new_ext = []
        for ext in ast.ext:
            if (
                isinstance(ext, c_ast.FuncDef)
                and getattr(ext, "decl", None) is not None
                and ext.decl.name in static_func_names
                and ext.decl.name not in reachable
            ):
                removed += 1
                ctx.record(self.name, "remove_static_function", ext.decl.name)
                continue
            if (
                isinstance(ext, c_ast.Decl)
                and isinstance(ext.type, c_ast.FuncDecl)
                and ext.name in static_func_decl_names
                and ext.name not in reachable
            ):
                removed += 1
                ctx.record(self.name, "remove_static_decl", ext.name)
                continue
            new_ext.append(ext)

        if not removed:
            return None

        ctx.bump("global_dce.removed", removed)
        ast.ext = new_ext
        return ast
