"""Indexed x86 emission retires each function's temporary compatibility state."""

import platform
import subprocess
import sys
import weakref

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import elf_x86_64 as elf
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.owned_elf_link import assemble
from pcc.backend.precise_stackmap import ARCH_X86_64, decode_stack_map
from pcc.backend.self_backend_indexed_codec import (
    decode_indexed_module_file,
    encode_indexed_module_file,
)
from pcc.backend.self_backend_kernel import (
    IndexedFunctionKernel,
    get_indexed_function_kernel,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
from tests.python.test_precise_stackmap_abi import _target_final_stackmap_ir


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


TARGET = "x86_64-unknown-linux-gnu"
PROGRAM = '''target triple = "x86_64-unknown-linux-gnu"
define i32 @pick(i1 %condition) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  br label %merge
no:
  br label %merge
merge:
  %value = phi i32 [ 40, %yes ], [ 7, %no ]
  ret i32 %value
}
define i32 @add_two(i32 %value) {
entry:
  %result = add i32 %value, 2
  ret i32 %result
}
define i32 @main() {
entry:
  %selected = call i32 @pick(i1 true)
  %result = call i32 @add_two(i32 %selected)
  ret i32 %result
}
'''
ROOTED_PROGRAM = _target_final_stackmap_ir(TARGET) + '''
define i32 @after_probe(i32 %value) {
entry:
  %result = add i32 %value, 2
  ret i32 %result
}
'''
STARTUP = '''.intel_syntax noprefix
.text
.globl _start
.type _start, @function
_start:
  call main
  mov edi, eax
  mov eax, 60
  syscall
.size _start, .-_start
.section .note.GNU-stack,"",@progbits
'''


@pytest.fixture(autouse=True)
def _fixed_emission_configuration(monkeypatch):
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")


def _indexed_module(tmp_path, source=PROGRAM):
    """Round-trip the actual worker format, rather than clearing parsed fields."""
    path = tmp_path / "input.pidx"
    source_module = parse_self_backend_module(source)
    try:
        encode_indexed_module_file(str(path), source_module)
    finally:
        for function in source_module.functions:
            get_indexed_function_kernel(function).close_native_tables()
    module = decode_indexed_module_file(str(path))
    for function in module.functions:
        assert function.blocks == []
        assert function.block_map == {}
        kernel = get_indexed_function_kernel(function)
        assert kernel.instruction_arenas == []
        assert kernel.block_diagnostic_projections == 0
    return module


def _legacy_module(tmp_path, source=PROGRAM):
    module = _indexed_module(tmp_path, source)
    for function in module.functions:
        get_indexed_function_kernel(function).materialize_legacy_blocks(function)
        assert function.blocks
        assert function.block_map
    return module


def _assert_tables_closed(kernel):
    # These owned arenas cover CFG, instructions, PHIs, calls and frame slots.
    # The CPython arena uses a list, while native pcc uses the same close()
    # contract to free its raw allocation. This test asserts that contract.
    for name in (
        "block_facts", "instruction_metadata", "phi_scalars",
        "call_scalars", "slot_scalars",
    ):
        arena = getattr(kernel, name)
        assert len(arena) == 0, name
        with pytest.raises(RuntimeError, match="compiler int arena is closed"):
            arena.diagnostic_values()


def _emit(module, packed):
    plans = [] if packed else None
    assembly = emit_indexed_assembly(module, stack_map_plans_out=plans)
    encoded = encode_assembly_object(assembly, TARGET, stack_map_plans=plans)
    return assembly, encoded


class _TrackedAssemblyLine(str):
    """A normal assembly string whose lifetime can be observed without owning it."""


@pytest.mark.parametrize("packed", [False, True], ids=["symbolic-maps", "packed-maps"])
def test_instruction_lines_retire_before_the_next_function(tmp_path, monkeypatch, packed):
    expected_assembly, expected_object = _emit(_legacy_module(tmp_path), packed)
    module = _indexed_module(tmp_path)
    real_emit_function = emitter._emit_function
    references = []
    observed = []

    def traced_emit_function(function, plan, *, phase_timing=None):
        # Retaining the module's individual instruction lines keeps these
        # references live. Compacting completed output preserves only text.
        assert all(reference() is None for reference in references)
        lines = real_emit_function(function, plan, phase_timing=phase_timing)
        for index, line in enumerate(lines):
            if line.startswith("  ") and not line.lstrip().startswith("."):
                tracked = _TrackedAssemblyLine(line)
                lines[index] = tracked
                references.append(weakref.ref(tracked))
                observed.append(function.name)
                break
        else:
            raise AssertionError("fixture emitted no ordinary instruction line")
        return lines

    monkeypatch.setattr(emitter, "_emit_function", traced_emit_function)
    assembly, encoded = _emit(module, packed)
    assert observed == [function.name for function in module.functions]
    assert all(reference() is None for reference in references)
    assert assembly == expected_assembly
    assert encoded == expected_object
    assert assembly.endswith("\n") and not assembly.endswith("\n\n")


def test_win64_assembly_preserves_text_indexed_and_legacy_bytes(tmp_path):
    from pcc.backend.self_backend_x86_64_windows import emit_x86_64_windows_asm

    source = PROGRAM.replace(TARGET, "x86_64-pc-windows-msvc")
    expected = emit_x86_64_windows_asm(source)
    legacy = emit_indexed_assembly(_legacy_module(tmp_path, source))
    indexed = emit_indexed_assembly(_indexed_module(tmp_path, source))
    assert indexed.encode("utf-8") == legacy.encode("utf-8") == expected.encode("utf-8")
    for name in ("pick", "add_two", "main"):
        assert ".seh_proc " + name + "\n" in indexed
    assert indexed.count(".seh_endproc\n") == 3
    assert indexed.endswith("\n") and not indexed.endswith("\n\n")


@pytest.mark.parametrize("source", [PROGRAM, ROOTED_PROGRAM], ids=["phi-call", "rooted"])
@pytest.mark.parametrize("packed", [False, True], ids=["symbolic-maps", "packed-maps"])
def test_indexed_functions_retire_before_next_function_and_metadata(
    tmp_path, monkeypatch, source, packed,
):
    expected_assembly, expected_object = _emit(_legacy_module(tmp_path, source), packed)
    module = _indexed_module(tmp_path, source)
    emitted = []
    real_emit_function = emitter._emit_function
    real_render = emitter.render_x86_64_stack_map_section
    rendered = []

    def assert_retired():
        for function, blocks, block_map, kernel in emitted:
            assert function.blocks is blocks
            assert function.block_map is block_map
            assert not function.blocks and not function.block_map
            _assert_tables_closed(kernel)

    def traced_emit_function(function, plan, *, phase_timing=None):
        # The previous function must be retired before this one's projection
        # is allocated. Checking only the final module would miss peak usage.
        assert_retired()
        kernel = get_indexed_function_kernel(function)
        blocks, block_map = function.blocks, function.block_map
        assert not blocks and not block_map
        assert len(kernel.block_facts) > 0
        before = kernel.block_diagnostic_projections
        lines = real_emit_function(function, plan, phase_timing=phase_timing)
        assert kernel.block_diagnostic_projections > before
        assert function.blocks is blocks
        assert function.block_map is block_map
        assert not blocks and not block_map
        emitted.append((function, blocks, block_map, kernel))
        return lines

    def traced_render(lines, plans, **kwargs):
        assert_retired()
        assert len(emitted) == len(module.functions)
        rendered.append(True)
        return real_render(lines, plans, **kwargs)

    monkeypatch.setattr(emitter, "_emit_function", traced_emit_function)
    monkeypatch.setattr(emitter, "render_x86_64_stack_map_section", traced_render)
    plans = [] if packed else None
    assembly = emit_indexed_assembly(module, stack_map_plans_out=plans)
    assert len(emitted) == len(module.functions)
    assert_retired()
    assert rendered == ([] if packed else [True])
    # Packed metadata resolves machine offsets only now, after every kernel
    # has closed. The symbolic path has already rendered under the same rule.
    encoded = encode_assembly_object(assembly, TARGET, stack_map_plans=plans)
    assert assembly == expected_assembly
    assert encoded == expected_object
    obj = elf.parse_relocatable(encoded)
    section = next(section for section in obj.sections if section.name == ".pcc_stackmaps")
    decoded = decode_stack_map(section.data, expected_arch=ARCH_X86_64)
    assert len(decoded.functions) == len(module.functions)
    if source == ROOTED_PROGRAM:
        assert any(record.locations for function in decoded.functions for record in function.records)


@pytest.mark.parametrize("failure_stage", ["projection", "instructions"])
def test_indexed_projection_cleanup_preserves_the_original_failure(
    tmp_path, monkeypatch, failure_stage,
):
    module = _indexed_module(tmp_path)
    target = module.functions[0]
    entry_state = []
    failure = BackendUnavailable("injected x86 emission failure")
    real_emit_function = emitter._emit_function
    real_materialize = emitter._materialize_indexed_control_flow
    real_emit_blocks = emitter.emit_function_blocks

    def traced_emit_function(function, plan, *, phase_timing=None):
        assert function is target
        entry_state.append((function.blocks, function.block_map))
        return real_emit_function(function, plan, phase_timing=phase_timing)

    def fail_after_projection(function, kernel):
        real_materialize(function, kernel)
        if entry_state and function is target and failure_stage == "projection":
            assert function.blocks and function.block_map
            assert any(block.phis for block in function.blocks)
            raise failure

    def fail_during_instructions(function, **kwargs):
        if entry_state and function is target and failure_stage == "instructions":
            assert function.blocks and function.block_map
            assert any(block.phis for block in function.blocks)
            raise failure
        return real_emit_blocks(function, **kwargs)

    monkeypatch.setattr(emitter, "_emit_function", traced_emit_function)
    monkeypatch.setattr(emitter, "_materialize_indexed_control_flow", fail_after_projection)
    monkeypatch.setattr(emitter, "emit_function_blocks", fail_during_instructions)
    try:
        with pytest.raises(BackendUnavailable, match="injected x86 emission failure") as caught:
            emit_indexed_assembly(module)
        assert caught.value is failure
        assert len(entry_state) == 1
        blocks, block_map = entry_state[0]
        assert target.blocks is blocks and not blocks
        assert target.block_map is block_map and not block_map
        assert all(not function.blocks and not function.block_map for function in module.functions)
        assert not emitter._X86_EMISSION_ACTIVE
        assert not emitter._WINDOWS_ABI
    finally:
        for function in module.functions:
            get_indexed_function_kernel(function).close_native_tables()


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_emitter_preserves_supplied_legacy_blocks(tmp_path, monkeypatch, fail):
    module = _legacy_module(tmp_path)
    real_emit_function = emitter._emit_function
    real_emit_blocks = emitter.emit_function_blocks
    observed = []
    failure = BackendUnavailable("injected legacy emission failure")

    def traced_emit_function(function, plan, *, phase_timing=None):
        blocks, block_map = function.blocks, function.block_map
        assert blocks and block_map
        snapshots = [(block, block.instructions) for block in blocks]
        try:
            return real_emit_function(function, plan, phase_timing=phase_timing)
        finally:
            assert function.blocks is blocks
            assert function.block_map is block_map
            for block, instructions in snapshots:
                assert function.block_map[block.name] is block
                assert block.instructions is instructions
            # Verification may migrate scalar PHIs into the kernel before
            # emission; reattaching those PHIs is existing legacy behavior.
            observed.append(function.name)

    def maybe_fail(function, **kwargs):
        if fail:
            raise failure
        return real_emit_blocks(function, **kwargs)

    monkeypatch.setattr(emitter, "_emit_function", traced_emit_function)
    monkeypatch.setattr(emitter, "emit_function_blocks", maybe_fail)
    try:
        if fail:
            with pytest.raises(BackendUnavailable, match="injected legacy emission failure") as caught:
                emit_indexed_assembly(module)
            assert caught.value is failure
            assert observed == [module.functions[0].name]
        else:
            assert emit_indexed_assembly(module)
            assert observed == [function.name for function in module.functions]
    finally:
        for function in module.functions:
            get_indexed_function_kernel(function).close_native_tables()


@pytest.mark.pcc_gate(
    probe=lambda: sys.platform.startswith("linux")
    and platform.machine() in ("x86_64", "amd64"),
)
def test_retired_indexed_functions_link_and_execute_owned_elf(tmp_path, monkeypatch):
    def forbidden_process(*args, **kwargs):
        raise AssertionError("owned emission/link attempted an external process")

    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", forbidden_process)
        legacy_assembly, legacy_object = _emit(_legacy_module(tmp_path), False)
        indexed_assembly, indexed_object = _emit(_indexed_module(tmp_path), False)
        _, packed_object = _emit(_indexed_module(tmp_path), True)
        assert indexed_assembly == legacy_assembly
        assert indexed_object == packed_object == legacy_object
        startup = assemble(STARTUP, TARGET)
        images = [
            elf.link_static_executable([elf.parse_relocatable(data), startup])
            for data in (legacy_object, indexed_object, packed_object)
        ]
        assert images[0] == images[1] == images[2]
    for name, image in zip(("legacy", "indexed", "packed"), images):
        binary = tmp_path / name
        binary.write_bytes(image)
        binary.chmod(0o755)
        result = subprocess.run([str(binary)], capture_output=True, timeout=10)
        assert result.returncode == 42, result.stderr
        assert result.stdout == result.stderr == b""
