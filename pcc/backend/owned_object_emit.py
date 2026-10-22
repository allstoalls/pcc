"""Owned IR-to-object boundary shared by runtime builds and the host tool.

Keep the reference emitter and its llvmlite parser out of the native
compiler's dependency closure. This module accepts already prepared IR and
uses only pcc's assembler, object writers and self backend.
"""

from __future__ import annotations

import re

def _contains_module_asm(ir_text: str) -> bool:
    for line in ir_text.splitlines():
        if line.lstrip().startswith('module asm "'):
            return True
    return False


def emit_owned_object(ir_text: str, triple: str) -> bytes:
    from .self_backend_parse import parse_self_backend_module
    from .target_objects import emit_indexed_assembly, encode_assembly_object
    from .self_backend_dispatch import emit_self_asm, self_backend_target_identity
    from .self_backend_target_passes import resolve_self_target_pass_names, resolve_self_target_pass_transport
    from .self_backend_target_match import is_aarch64_darwin_triple

    if _contains_module_asm(ir_text):
        raise ValueError("module-level assembly is unsupported by the pcc object emitter")

    target_line = 'target triple = "' + triple + '"'
    declared = re.search(r'^\s*target\s+triple\s*=\s*"([^"\n]*)"', ir_text, re.MULTILINE)
    if declared and declared.group(1) not in ("", "unknown-unknown-unknown"):
        if self_backend_target_identity(declared.group(1)) != self_backend_target_identity(triple):
            raise ValueError("target triple mismatch: module declares " + declared.group(1) + ", requested " + triple)
    if declared:
        ir_text = re.sub(r'^\s*target\s+triple\s*=\s*"[^"\n]*"', target_line, ir_text, count=1, flags=re.MULTILINE)
    else:
        ir_text = target_line + "\n" + ir_text
    transport_kind = resolve_self_target_pass_transport()
    passes = resolve_self_target_pass_names(transport=transport_kind)
    if transport_kind == "text" and passes:
        # Explicit text passes retain their established dispatch and order.
        assembly = emit_self_asm(ir_text, triple)
        if is_aarch64_darwin_triple(triple):
            from .arm64_asm_driver import assemble_file
            from .native_object import NativeObject

            sections, undefined = assemble_file(assembly)
            return NativeObject.from_sections(sections, undefined=undefined).to_macho()
        return encode_assembly_object(assembly, triple)
    module = parse_self_backend_module(ir_text)
    # Keep instruction records through assembly/object encoding. This avoids
    # rendering and reparsing an assembly file for every runtime member.
    if is_aarch64_darwin_triple(triple):
        from .self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
        from .native_object import NativeObject

        transport = emit_aarch64_darwin_indexed_transport(module, optimize=True, structured_instructions=True)
        try:
            sections, undefined = transport.assemble_sections()
            return NativeObject.from_sections(sections, undefined=undefined).to_macho()
        finally:
            if transport.encoded_line_records is not None:
                transport.encoded_line_records.close()
    from .self_backend_target_match import is_x86_64_linux_triple

    if is_x86_64_linux_triple(triple):
        # Reuse the indexed frontend's packed metadata route. Runtime members
        # need the same precise roots, but no intermediate assembly rendering
        # and reparsing of every stack-map field. Final label offsets are still
        # resolved by the owned assembler before the plans are consumed.
        stack_map_plans = []
        assembly = emit_indexed_assembly(module, stack_map_plans_out=stack_map_plans)
        return encode_assembly_object(
            assembly, triple, stack_map_plans=stack_map_plans,
            consume_stack_map_plans=True,
        )
    assembly = emit_indexed_assembly(module)
    return encode_assembly_object(assembly, triple)
