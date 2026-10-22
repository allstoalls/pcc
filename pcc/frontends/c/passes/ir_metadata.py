"""Passes 56-62: IR Metadata Annotation.

  56. TBAA Injection               — (in tbaa.py, separate)
  57. noalias for restrict          — C99 restrict → LLVM noalias
  58. Alignment Annotation          — add align to load/store
  59. NSW/NUW Flag Annotation       — add overflow flags to arithmetic
  60. Loop Metadata                 — !llvm.loop vectorize/unroll hints
  61. Function Attribute Inference   — nounwind, readonly, willreturn, etc.
  62. Range Metadata                — !range on loads with known bounds

All operate on LLVM IR text after codegen.
"""

from __future__ import annotations

import re

from pcc.frontends.c.passes.base import IRPass
from pcc.frontends.c.passes.context import PassContext


# ── 57. noalias for restrict ────────────────────────────────────────────

class NoaliasPass(IRPass):
    """Add noalias attribute to function parameters marked with C99 restrict.

    Reads restrict_params from PassContext (set by EscapeAnalysisPass).
    Annotates the corresponding LLVM function parameter with noalias.
    """
    name = "noalias"

    def run(self, ir_text: str, ctx: PassContext) -> str:
        count = 0
        for func_info in ctx.functions.values():
            if not func_info.restrict_params:
                continue
            for param_name in func_info.restrict_params:
                # Find the function definition and annotate the parameter
                # Pattern: define ... @"func_name"(TYPE %"param_name", ...)
                pattern = re.compile(
                    rf'(define\s+\S+\s+@"{re.escape(func_info.name)}"\([^)]*?)'
                    rf'(\S+\s+%"{re.escape(param_name)}")',
                )
                def add_noalias(m):
                    nonlocal count
                    # Only add if not already present
                    if "noalias" not in m.group(2):
                        count += 1
                        return m.group(1) + "noalias " + m.group(2)
                    return m.group(0)
                ir_text = pattern.sub(add_noalias, ir_text)

        if count:
            ctx.bump("noalias.annotated", count)
            ctx.record(self.name, "annotated", f"{count} params")
        return ir_text


# ── 58. Alignment Annotation ───────────────────────────────────────────

class AlignPass(IRPass):
    """Add alignment hints to load/store instructions.

    Uses natural alignment based on type:
    - i8: align 1, i16: align 2, i32: align 4, i64/double: align 8
    """
    name = "align"

    _TYPE_ALIGN = {
        "i8": 1, "i16": 2, "i32": 4, "i64": 8,
        "float": 4, "double": 8,
    }

    # Match load without existing align
    _LOAD_RE = re.compile(
        r"^(\s+%\S+\s+=\s+load\s+)"
        r"(i8|i16|i32|i64|float|double)(?!\s*\()"
        r"(,\s+\S+\s+%\S+)((?:\s*,.*)?)$",
        re.MULTILINE,
    )
    # Match store without existing align
    _STORE_RE = re.compile(
        r"^(\s+store\s+)"
        r"(i8|i16|i32|i64|float|double)(?!\s*\()"
        r"(\s+\S+,\s+\S+\s+%\S+)((?:\s*,.*)?)$",
        re.MULTILINE,
    )

    def run(self, ir_text: str, ctx: PassContext) -> str:
        count = 0

        def add_align_load(m):
            nonlocal count
            prefix, ty, ptr, rest = m.groups()
            if "align" in rest:
                return m.group(0)
            a = self._TYPE_ALIGN.get(ty)
            if a:
                count += 1
                return f"{prefix}{ty}{ptr}, align {a}{rest}"
            return m.group(0)

        def add_align_store(m):
            nonlocal count
            prefix, ty, val_ptr, rest = m.groups()
            if "align" in rest:
                return m.group(0)
            a = self._TYPE_ALIGN.get(ty)
            if a:
                count += 1
                return f"{prefix}{ty}{val_ptr}, align {a}{rest}"
            return m.group(0)

        ir_text = self._LOAD_RE.sub(add_align_load, ir_text)
        ir_text = self._STORE_RE.sub(add_align_store, ir_text)

        if count:
            ctx.bump("align.annotated", count)
            ctx.record(self.name, "annotated", f"{count} instructions")
        return ir_text


# ── 59. NSW/NUW Flag Annotation ────────────────────────────────────────

class NSWAnnotationPass(IRPass):
    """Add nsw/nuw flags to arithmetic instructions in LLVM IR.

    Reads range information from PassContext to determine where
    overflow flags are safe to add.

    Conservative: only annotates instructions within functions where
    we have proven range info for the operands.
    """
    name = "nsw-annotation"

    # Match: %x = add i32 %a, %b (or sub, mul)
    _ARITH_RE = re.compile(
        r"^(\s+%\S+\s+=\s+)(add|sub|mul)(\s+i32\s+.*)$",
        re.MULTILINE,
    )

    def run(self, ir_text: str, ctx: PassContext) -> str:
        # For now, we don't blindly add nsw — too risky.
        # Only add when we have proven it safe via range analysis.
        # This is a placeholder that counts opportunities.
        count = 0
        for m in self._ARITH_RE.finditer(ir_text):
            if "nsw" not in m.group(0) and "nuw" not in m.group(0):
                count += 1

        if count:
            ctx.bump("nsw.opportunities", count)
            ctx.record(
                self.name, "opportunities",
                f"{count} arithmetic ops could get nsw/nuw",
            )
        return ir_text


# ── 60. Loop Metadata ──────────────────────────────────────────────────

class LoopMetadataPass(IRPass):
    """Add !llvm.loop metadata to back-edges for vectorization/unroll hints.

    Identifies loop back-edge branches (br ... !llvm.loop) and adds
    metadata suggesting vectorization and unrolling to LLVM.
    """
    name = "loop-metadata"

    # Match unconditional/conditional branch to a loop header
    _BR_RE = re.compile(
        r"^(\s+br\s+(?:i1\s+\S+,\s+)?label\s+%\S+.*)$",
        re.MULTILINE,
    )

    def run(self, ir_text: str, ctx: PassContext) -> str:
        # Loop metadata injection requires identifying back-edges,
        # which needs CFG analysis. For now, count loop-like branches.
        loop_branches = 0
        for m in self._BR_RE.finditer(ir_text):
            if "!llvm.loop" not in m.group(0):
                loop_branches += 1

        if loop_branches:
            ctx.bump("loop_metadata.branches", loop_branches)
            ctx.record(
                self.name, "analysis",
                f"{loop_branches} branches (loop metadata needs CFG analysis)",
            )
        return ir_text


# ── 61. Function Attribute Inference ───────────────────────────────────

class FuncAttrPass(IRPass):
    """Infer and add lightweight function attributes in pure Python.

    This is a source-backed Python translation of a small, conservative subset
    of LLVM's `function-attrs` pass. We currently infer:

    - `nounwind` for leaf functions without setjmp/longjmp behavior
    - `nofree` for leaf functions (no call sites that could free memory)
    - `willreturn` for leaf functions with no loops/goto/setjmp

    We intentionally stay conservative: if we cannot prove the property from
    existing HighTier analysis, we do not add the attribute.
    """
    name = "func-attr"

    _DEFINE_RE = re.compile(
        r"""
        ^
        (?P<prefix>\s*define\s+.*?@(?P<name>"[^"]+"|[\w\.\$]+)\([^)]*\))
        (?P<tail>.*?)
        \s*\{\s*$
        """,
        re.VERBOSE,
    )

    def run(self, ir_text: str, ctx: PassContext) -> str:
        count = 0
        lines = ir_text.splitlines()
        func_infos = {func.name: func for func in ctx.functions.values()}
        for index, line in enumerate(lines):
            match = self._DEFINE_RE.match(line)
            if match is None:
                continue
            func_name = match.group("name")
            if func_name.startswith('"') and func_name.endswith('"'):
                func_name = func_name[1:-1]
            func_info = func_infos.get(func_name)
            if func_info is None:
                continue

            attrs_to_add = []
            if func_info.is_leaf and not func_info.has_setjmp:
                attrs_to_add.append("nounwind")
                attrs_to_add.append("nofree")
            if (
                func_info.is_leaf
                and not func_info.has_setjmp
                and not func_info.has_goto
                and func_info.max_loop_depth == 0
            ):
                attrs_to_add.append("willreturn")

            existing_tail = (match.group("tail") or "").strip()
            # Function metadata attachments must follow function attributes:
            # ``... nounwind !dbg !7 {``.  Treat the first attachment as the
            # start of an opaque suffix so inferred attributes are never
            # appended after ``!dbg`` (which produces invalid LLVM IR).
            attachment = re.search(
                r"(?:^|\s)(?P<metadata>![A-Za-z_][\w.]*\s+!\d+)",
                existing_tail,
            )
            if attachment is None:
                attribute_tail = existing_tail
                metadata_tail = ""
            else:
                attribute_tail = existing_tail[: attachment.start()].strip()
                metadata_tail = existing_tail[attachment.start():].strip()
            existing_attrs = set(attribute_tail.split())
            to_add = [attr for attr in attrs_to_add if attr not in existing_attrs]
            if not to_add:
                continue

            new_tail = " ".join(
                part
                for part in (attribute_tail, *to_add, metadata_tail)
                if part
            )
            lines[index] = f"{match.group('prefix')} {new_tail} {{".rstrip().replace("  {", " {")
            count += len(to_add)

        if count:
            ctx.bump("func_attr.added", count)
            ctx.record(self.name, "added", f"{count} attributes")
        return "\n".join(lines)


# ── 62. Range Metadata ─────────────────────────────────────────────────

class RangeMetadataPass(IRPass):
    """Add !range metadata to loads where value range is known.

    For example, if a variable is known to be in [0, 100],
    the load can be annotated with !range !{i32 0, i32 101}.
    This helps LLVM eliminate range checks.
    """
    name = "range-metadata"

    def run(self, ir_text: str, ctx: PassContext) -> str:
        # Range metadata requires mapping IR variables back to source variables,
        # which is complex with the current text-based approach.
        # Record opportunities for future implementation.
        range_vars = 0
        for func_info in ctx.functions.values():
            for var_info in func_info.var_infos.values():
                if var_info.range_min is not None and var_info.range_max is not None:
                    range_vars += 1

        if range_vars:
            ctx.bump("range_metadata.opportunities", range_vars)
            ctx.record(
                self.name, "opportunities",
                f"{range_vars} vars with known ranges",
            )
        return ir_text
