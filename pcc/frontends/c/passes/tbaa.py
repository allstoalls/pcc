"""LowTier Pass: TBAA (Type-Based Alias Analysis) Metadata Injection.

Adds TBAA metadata to LLVM IR load/store instructions based on C's
strict aliasing rule (C99 6.5p7):

  - int* and float* never alias
  - char* may alias anything ("omnipotent char")
  - struct members follow their element types

This pass operates on LLVM IR text because llvmlite doesn't support
TBAA metadata natively.

LLVM metadata uses numeric IDs:
  !0 = !{!"Simple C/C++ TBAA"}
  !1 = !{!"omnipotent char", !0, i64 0}
  !2 = !{!"int", !1, i64 0}
  load i32, i32* %ptr, !tbaa !2
"""

from __future__ import annotations

import re

from pcc.frontends.c.passes.base import IRPass
from pcc.frontends.c.passes.context import PassContext


class TBAAPass(IRPass):
    name = "tbaa"

    # Match load instructions: %x = load TYPE, TYPE* %ptr[, ...]
    _LOAD_RE = re.compile(
        r"^(\s+%\S+\s+=\s+load\s+)(\w+)"
        r"(,\s+\S+\s+%\S+)"
        r"(.*)$",
        re.MULTILINE,
    )

    # Match store instructions: store TYPE %val, TYPE* %ptr[, ...]
    _STORE_RE = re.compile(
        r"^(\s+store\s+)(\w+)"
        r"(\s+\S+,\s+\S+\s+%\S+)"
        r"(.*)$",
        re.MULTILINE,
    )

    # LLVM IR type → TBAA type label
    _TYPE_TO_TBAA = {
        "i8": "omnipotent char",
        "i16": "short",
        "i32": "int",
        "i64": "long",
        "float": "float",
        "double": "double",
    }

    def run(self, ir_text: str, ctx: PassContext) -> str:
        # Find the highest existing metadata ID in the IR
        existing_ids = [int(m) for m in re.findall(r"^!(\d+)\s*=", ir_text, re.MULTILINE)]
        next_id = max(existing_ids, default=-1) + 1

        # Assign numeric IDs for TBAA nodes
        # !N+0 = root, !N+1 = omnipotent char, !N+2.. = type-specific
        root_id = next_id
        char_id = next_id + 1

        # Build map: tbaa_type_label -> numeric ID (allocated on demand)
        type_id_map: dict[str, int] = {}
        current_id = next_id + 2  # start after root + char

        used_tbaa_types: set[str] = set()

        # First pass: discover which types we need
        for m in self._LOAD_RE.finditer(ir_text):
            ir_type = m.group(2)
            tbaa = self._TYPE_TO_TBAA.get(ir_type)
            if tbaa and "!tbaa" not in m.group(4):
                used_tbaa_types.add(tbaa)

        for m in self._STORE_RE.finditer(ir_text):
            ir_type = m.group(2)
            tbaa = self._TYPE_TO_TBAA.get(ir_type)
            if tbaa and "!tbaa" not in m.group(4):
                used_tbaa_types.add(tbaa)

        if not used_tbaa_types:
            return ir_text

        # Assign IDs to used types
        for t in sorted(used_tbaa_types):
            if t == "omnipotent char":
                type_id_map[t] = char_id
            else:
                type_id_map[t] = current_id
                current_id += 1

        # Second pass: annotate instructions with !tbaa !N references
        def annotate_load(m: re.Match) -> str:
            prefix, ir_type, ptr_part, rest = m.groups()
            tbaa = self._TYPE_TO_TBAA.get(ir_type)
            if tbaa and "!tbaa" not in rest and tbaa in type_id_map:
                return f"{prefix}{ir_type}{ptr_part}{rest}, !tbaa !{type_id_map[tbaa]}"
            return m.group(0)

        def annotate_store(m: re.Match) -> str:
            prefix, ir_type, val_ptr_part, rest = m.groups()
            tbaa = self._TYPE_TO_TBAA.get(ir_type)
            if tbaa and "!tbaa" not in rest and tbaa in type_id_map:
                return f"{prefix}{ir_type}{val_ptr_part}{rest}, !tbaa !{type_id_map[tbaa]}"
            return m.group(0)

        ir_text = self._LOAD_RE.sub(annotate_load, ir_text)
        ir_text = self._STORE_RE.sub(annotate_store, ir_text)

        # Append metadata definitions
        md_lines = []
        md_lines.append(f'!{root_id} = !{{!"Simple C/C++ TBAA"}}')
        md_lines.append(f'!{char_id} = !{{!"omnipotent char", !{root_id}, i64 0}}')
        for t in sorted(used_tbaa_types):
            tid = type_id_map[t]
            if t != "omnipotent char":
                md_lines.append(f'!{tid} = !{{!"{t}", !{char_id}, i64 0}}')

        ir_text = ir_text.rstrip() + "\n\n" + "\n".join(md_lines) + "\n"

        ctx.bump("tbaa.types_annotated", len(used_tbaa_types))
        ctx.record(self.name, "injected", "tbaa_metadata", str(used_tbaa_types))

        return ir_text
