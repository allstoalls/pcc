"""Select scalar ALU operands without redundant register round trips."""

import re

from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm


_TRIPLE = 'target triple = "arm64-apple-darwin23.6.0"\n'


def test_subtract_uses_existing_allocated_operands_and_result():
    source = _TRIPLE + '''
define void @difference(ptr %left, ptr %right, ptr %out) {
entry:
  %a = load i64, ptr %left
  %b = load i64, ptr %right
  %difference = sub i64 %a, %b
  store i64 %difference, ptr %out
  ret void
}
'''
    assembly = emit_aarch64_darwin_asm(source, optimize=False)
    assert re.search(r"sub x[1-8], x[1-8], x[1-8]", assembly)
    assert "mov x9, x1" not in assembly
    assert "mov x10, x2" not in assembly


def test_small_add_uses_immediate_and_existing_destination():
    source = _TRIPLE + '''
define void @increment(ptr %in, ptr %out) {
entry:
  %a = load i64, ptr %in
  %sum = add i64 %a, 1
  store i64 %sum, ptr %out
  ret void
}
'''
    assembly = emit_aarch64_darwin_asm(source, optimize=False)
    assert re.search(r"add x[1-8], x[1-8], #1", assembly)


def _execute(source, executable, optimize):
    import platform
    import subprocess
    import sys
    import pytest
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.native_object import NativeObject
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
    from pcc.backend.self_backend_parse import parse_self_backend_module

    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("AArch64 Darwin execution")
    transport = emit_aarch64_darwin_indexed_transport(
        parse_self_backend_module(source), optimize=optimize,
    )
    sections, undefined = transport.assemble_sections()
    image = link_executable([NativeObject.from_sections(sections, undefined=undefined)])
    if transport.encoded_line_records is not None:
        transport.encoded_line_records.close()
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_integer_alu_execution_and_aliases(tmp_path):
    for bits in (8, 16, 32, 64):
        mask = (1 << bits) - 1
        left, right = (1 << (bits - 1)) + 13, 3
        signed = left - (1 << bits)
        quotient = -(abs(signed) // right)
        operations = {
            "add": left + right, "sub": left - right, "mul": left * right,
            "sdiv": quotient, "srem": signed - quotient * right,
            "udiv": left // right, "urem": left % right,
            "and": left & right, "or": left | right, "xor": left ^ right,
            "shl": left << right, "lshr": left >> right, "ashr": signed >> right,
        }
        source = _TRIPLE + f'@left = global i{bits} {left}\n@right = global i{bits} {right}\n'
        calls = []
        for operation, expected in operations.items():
            for keep_live in (0, 1, 2):
                name = operation + str(int(keep_live))
                tail = f'  ret i{bits} %result\n'
                result = expected & mask
                if keep_live == 1:
                    tail = f'  %keep = xor i{bits} %a, %b\n  %mixed = xor i{bits} %result, %keep\n  ret i{bits} %mixed\n'
                    result ^= left ^ right
                elif keep_live == 2:
                    # Keep lhs live so the dead rhs register can become result.
                    tail = f'  %mixed = xor i{bits} %result, %a\n  ret i{bits} %mixed\n'
                    result ^= left
                source += f'''define i{bits} @{name}(ptr %left, ptr %right) {{
entry:
  %a = load i{bits}, ptr %left
  %b = load i{bits}, ptr %right
  %result = {operation} i{bits} %a, %b
{tail}}}
'''
                calls += [f'  %r{name} = call i{bits} @{name}(ptr @left, ptr @right)',
                          f'  %bad{name} = icmp ne i{bits} %r{name}, {result}']
        bad_names = [line.split(" =", 1)[0].strip() for line in calls if "icmp ne" in line]
        aggregate = bad_names[0]
        for index, name in enumerate(bad_names[1:]):
            calls.append(f'  %combined{index} = or i1 {aggregate}, {name}')
            aggregate = f'%combined{index}'
        source += 'define i32 @main() {\nentry:\n' + '\n'.join(calls)
        source += f'\n  %status = zext i1 {aggregate} to i32\n  ret i32 %status\n}}\n'
        for optimize in (False, True):
            _execute(source, tmp_path / f'alu-{bits}-{optimize}', optimize)


def test_add_sub_immediate_boundaries_execute(tmp_path):
    for bits in (32, 64):
        mask = (1 << bits) - 1
        start = mask - 15
        source = _TRIPLE + f'@input = global i{bits} {start}\n'
        calls, errors = [], []
        for index, (operation, immediate) in enumerate(
            (op, value) for op in ("add", "sub") for value in (-4096, -4095, -1, 0, 1, 4095, 4096)
        ):
            expected = (start + immediate if operation == "add" else start - immediate) & mask
            source += f'''define i{bits} @case{index}(ptr %input) {{
entry:
  %a = load i{bits}, ptr %input
  %result = {operation} i{bits} %a, {immediate}
  ret i{bits} %result
}}
'''
            calls += [f'  %r{index} = call i{bits} @case{index}(ptr @input)',
                      f'  %bad{index} = icmp ne i{bits} %r{index}, {expected}']
            errors.append(f'%bad{index}')
        aggregate = errors[0]
        for index, error in enumerate(errors[1:]):
            calls.append(f'  %combined{index} = or i1 {aggregate}, {error}')
            aggregate = f'%combined{index}'
        source += 'define i32 @main() {\nentry:\n' + '\n'.join(calls)
        source += f'\n  %status = zext i1 {aggregate} to i32\n  ret i32 %status\n}}\n'
        for optimize in (False, True):
            _execute(source, tmp_path / f'immediate-{bits}-{optimize}', optimize)
