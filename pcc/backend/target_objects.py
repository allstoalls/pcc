"""Owned indexed emission and object serialization selected by target ABI."""

from . import BackendUnavailable
from .self_backend_target_match import (
    is_aarch64_darwin_triple, is_aarch64_linux_triple,
    is_x86_64_linux_triple, is_x86_64_windows_triple,
)


def emit_indexed_assembly(
    module, optimize=False, *, stack_map_plans_out=None, phase_timing=None,
):
    if is_aarch64_darwin_triple(module.triple) or is_aarch64_linux_triple(module.triple):
        from .self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_module
        return emit_aarch64_darwin_indexed_module(
            module, optimize=optimize, phase_timing=phase_timing,
        )
    if is_x86_64_linux_triple(module.triple) or is_x86_64_windows_triple(module.triple):
        from .self_backend_x86_64_linux import _emit_x86_64_module
        return _emit_x86_64_module(
            "", module=module, windows=is_x86_64_windows_triple(module.triple),
            stack_map_plans_out=stack_map_plans_out, phase_timing=phase_timing,
        )
    raise BackendUnavailable("indexed emission target is unavailable: " + module.triple)


class X86StreamedModule:
    """x86-64 sections streamed while the module was emitted.

    The x86 counterpart of AArch64's structured transport: emission placed and
    encoded every function as it finished, so writing the object only closes
    the layout, packs the stack maps and serializes ELF or COFF.
    """

    def __init__(self, builder, stack_map_plans, windows: bool) -> None:
        self.builder = builder
        self.stack_map_plans = stack_map_plans
        self.windows = windows

    def encode_object(self, phase_timing=None) -> bytes:
        plans = self.stack_map_plans
        if self.windows:
            from .coff_x86_64 import assemble_builder_object
            if phase_timing is None:
                return assemble_builder_object(
                    self.builder, stack_map_plans=plans, consume_stack_map_plans=True,
                )
            return assemble_builder_object(
                self.builder, stack_map_plans=plans, consume_stack_map_plans=True,
                phase_timing=phase_timing,
            )
        from .x86_64_asm_driver import assemble_builder
        from .self_backend_x86_64_linux import _asm_symbol, _block_label
        from .elf_x86_64 import emit_relocatable
        if phase_timing is None:
            return emit_relocatable(assemble_builder(
                self.builder, (), plans,
                function_symbol=_asm_symbol, block_label=_block_label,
                consume_stack_map_plans=True,
            ))
        started = phase_timing.start()
        obj = assemble_builder(
            self.builder, (), plans,
            function_symbol=_asm_symbol, block_label=_block_label,
            consume_stack_map_plans=True, phase_timing=phase_timing,
        )
        phase_timing.add(9, started)
        started = phase_timing.start()
        encoded = emit_relocatable(obj)
        phase_timing.add(10, started)
        return encoded


def emit_x86_64_streamed_module(module, *, phase_timing=None) -> X86StreamedModule:
    """Emit an indexed x86-64 module straight into its sections."""
    if not (is_x86_64_linux_triple(module.triple) or is_x86_64_windows_triple(module.triple)):
        raise BackendUnavailable("streamed x86 emission target is unavailable: " + module.triple)
    from .x86_64_asm_driver import X86ModuleBuilder
    from .self_backend_x86_64_linux import _emit_x86_64_module

    windows = is_x86_64_windows_triple(module.triple)
    builder = X86ModuleBuilder(unwind=windows)
    plans = []
    _emit_x86_64_module(
        "", module=module, windows=windows, stack_map_plans_out=plans,
        phase_timing=phase_timing, builder=builder,
    )
    return X86StreamedModule(builder, plans, windows)


def encode_assembly_object(
    assembly, target, *, stack_map_plans=None, consume_stack_map_plans=False,
    phase_timing=None,
):
    if stack_map_plans is not None and is_x86_64_windows_triple(target):
        from .coff_x86_64 import assemble_object
        return assemble_object(
            assembly, stack_map_plans=stack_map_plans,
            consume_stack_map_plans=consume_stack_map_plans,
            phase_timing=phase_timing,
        )
    if stack_map_plans is not None:
        if not is_x86_64_linux_triple(target):
            raise BackendUnavailable("packed stack maps require the Linux x86 target")
        from .x86_64_asm_driver import assemble_file_with_stack_maps
        from .self_backend_x86_64_linux import _asm_symbol, _block_label
        from .elf_x86_64 import emit_relocatable
        if phase_timing is None:
            return emit_relocatable(assemble_file_with_stack_maps(
                assembly, stack_map_plans,
                function_symbol=_asm_symbol, block_label=_block_label,
                consume_stack_map_plans=consume_stack_map_plans,
            ))
        started = phase_timing.start()
        obj = assemble_file_with_stack_maps(
            assembly, stack_map_plans,
            function_symbol=_asm_symbol, block_label=_block_label,
            consume_stack_map_plans=consume_stack_map_plans,
            phase_timing=phase_timing,
        )
        phase_timing.add(9, started)
        started = phase_timing.start()
        encoded = emit_relocatable(obj)
        phase_timing.add(10, started)
        return encoded
    if is_aarch64_linux_triple(target) or is_x86_64_linux_triple(target):
        from .owned_elf_link import assemble
        from .elf_x86_64 import emit_relocatable
        if phase_timing is None:
            return emit_relocatable(assemble(assembly, target))
        started = phase_timing.start()
        obj = assemble(assembly, target, phase_timing=phase_timing)
        phase_timing.add(9, started)
        started = phase_timing.start()
        encoded = emit_relocatable(obj)
        phase_timing.add(10, started)
        return encoded
    if is_x86_64_windows_triple(target):
        from .coff_x86_64 import assemble_object
        return assemble_object(assembly, phase_timing=phase_timing)
    if is_aarch64_darwin_triple(target):
        from .arm64_asm_driver import assemble_file
        from .native_object import encode_native_object_from_sections
        started = phase_timing.start() if phase_timing is not None else 0
        sections, undefined = assemble_file(assembly)
        if phase_timing is not None:
            phase_timing.add(9, started)
            started = phase_timing.start()
        encoded = encode_native_object_from_sections(sections, undefined=undefined)
        if phase_timing is not None:
            phase_timing.add(10, started)
        return encoded
    raise BackendUnavailable("object emission target is unavailable: " + target)
