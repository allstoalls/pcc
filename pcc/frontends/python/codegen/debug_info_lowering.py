"""Source line information for the Python frontend.

The C frontend has emitted ``DICompileUnit``/``DISubprogram``/``DILocation``
for a long time; the Python frontend emitted none, so a pcc-compiled program
could only ever be debugged by rebuilding it with print statements.  That
instrument-rebuild loop is what this module removes.

Design notes
------------
Locations are stamped by ``IRBuilder.debug_location`` at the single point where
instructions are appended, so statement lowering marks a boundary once rather
than every emit site decorating itself.  That mirrors LLVM's
``SetCurrentDebugLocation`` and keeps the diff off the hundreds of lowering
call sites.

Scope is the ``DISubprogram`` of the function being lowered, falling back to
the file for module-level code.  LLVM rejects a ``DILocation`` whose scope
belongs to a different subprogram than the enclosing function, so the scope
stack is saved and restored around nested function lowering.
"""

from __future__ import annotations

import os

from pcc.ir.compat import ir
from pcc.frontends.python.py_ast import Module as AstModule


def debug_info_requested() -> bool:
    """Whether to emit line info.

    ponytail: read from the environment because ``_init_l1_state`` takes a
    fixed argument list that the self-hosted build depends on.  Thread it as a
    real option once the Python pipeline grows an options object.
    """
    return bool(os.environ.get("PCC_PY_DEBUG_INFO", "").strip())


def debug_source_path(module: AstModule, explicit_source_path: str = "") -> str:
    """Use the producer's actual input path, or an original parser span.

    Module.name is an import identity without directories or extension; it
    must never be substituted for a debugger source-file identity.
    """
    if explicit_source_path:
        return explicit_source_path
    for statement in module.body:
        span = getattr(statement, "span", None)
        path = getattr(span, "file", "")
        if path:
            return path
    return "<module>"


class DebugInfoLoweringMixin:
    def _di_init(self, source_path: str) -> None:
        self._di_file = None
        self._di_compile_unit = None
        self._di_scope = None
        self._di_subprograms: dict[str, object] = {}
        if not debug_info_requested():
            return
        directory, _, filename = str(source_path or "<module>").rpartition("/")
        self._di_file = self.module.add_debug_info(
            "DIFile", {"filename": filename or "<module>", "directory": directory}
        )
        self._di_compile_unit = self.module.add_debug_info(
            "DICompileUnit",
            {
                "language": ir.DIToken("DW_LANG_Python"),
                "file": self._di_file,
                "producer": "pcc python frontend",
                "isOptimized": False,
                "runtimeVersion": 0,
                "emissionKind": ir.DIToken("FullDebug"),
            },
            is_distinct=True,
        )
        self._di_scope = self._di_file
        # Pass the compile-unit node directly, not wrapped in a list: a list
        # makes the builder manufacture an extra ``!{!cu}`` tuple, LLVM rejects
        # the named operand as an invalid compile unit, and all DWARF is
        # silently dropped during object emission.
        self.module.add_named_metadata("llvm.dbg.cu", self._di_compile_unit)
        flags = self.module.add_named_metadata("llvm.module.flags")
        i32 = ir.IntType(32)
        flags.add(
            self.module.add_metadata(
                [
                    ir.Constant(i32, 7),
                    ir.MetaDataString(self.module, "Dwarf Version"),
                    ir.Constant(i32, 4),
                ]
            )
        )
        flags.add(
            self.module.add_metadata(
                [
                    ir.Constant(i32, 1),
                    ir.MetaDataString(self.module, "Debug Info Version"),
                    ir.Constant(i32, 3),
                ]
            )
        )

    def _di_enabled(self) -> bool:
        return getattr(self, "_di_file", None) is not None

    def _di_declare_function(self, fn_ir, name: str, line: int):
        """Attach a ``DISubprogram`` and return the previous scope.

        The caller restores the returned scope when the body is finished; a
        location left pointing into a finished subprogram makes LLVM drop the
        whole compile unit.
        """
        if not self._di_enabled():
            return None
        previous = self._di_scope
        subroutine = self.module.add_debug_info(
            "DISubroutineType", {"types": self.module.add_metadata([None])}
        )
        subprogram = self.module.add_debug_info(
            "DISubprogram",
            {
                "name": name,
                "file": self._di_file,
                "line": max(int(line or 1), 1),
                "type": subroutine,
                "isLocal": False,
                "unit": self._di_compile_unit,
                "scope": self._di_file,
            },
            is_distinct=True,
        )
        if hasattr(fn_ir, "set_metadata"):
            fn_ir.set_metadata("dbg", subprogram)
        self._di_subprograms[name] = subprogram
        self._di_scope = subprogram
        return previous

    def _di_restore_scope(self, previous) -> None:
        if not self._di_enabled():
            return
        self._di_scope = previous if previous is not None else self._di_file
        builder = getattr(self, "builder", None)
        if builder is not None:
            builder.debug_location = None

    def _di_locate(self, node) -> None:
        """Point the builder at ``node``'s source span."""
        if not self._di_enabled():
            return
        builder = getattr(self, "builder", None)
        if builder is None:
            return
        scope = self._di_scope
        if scope is None or scope is self._di_file:
            # LLVM rejects a DILocation whose scope is a DIFile ("location
            # requires a valid scope") and discards the whole compile unit.
            # ponytail: module-level statements therefore carry no line info;
            # give the synthesized module-init function its own DISubprogram
            # if that attribution turns out to matter.
            return
        span = getattr(node, "span", None)
        line = getattr(span, "line", 0) if span is not None else 0
        if not line:
            return
        # Preserve producer coordinates. The owned lifter currently has
        # line-only spans; precise column normalization remains separate.
        builder.debug_location = self.module.add_debug_info(
            "DILocation",
            {
                "line": max(int(line), 1),
                "column": max(int(getattr(span, "col", 0) or 0), 0),
                "scope": scope,
            },
        )
