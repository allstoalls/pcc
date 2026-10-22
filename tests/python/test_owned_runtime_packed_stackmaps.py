"""Runtime object production uses packed plans without text projection."""

import platform
import subprocess
import sys

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.backend.target_objects import encode_assembly_object
from tests.python.test_precise_stackmap_abi import _target_final_stackmap_ir


TARGET = "x86_64-unknown-linux-gnu"


@pytest.mark.parametrize("ir", [
    _target_final_stackmap_ir(TARGET),
    f'target triple = "{TARGET}"\ndefine i64 @rax() {{\nentry:\n ret i64 42\n}}',
    f'target triple = "{TARGET}"\n@only_data = global i64 42',
], ids=["rooted-function", "register-named-function", "globals-only"])
def test_runtime_packed_object_is_byte_identical(ir, monkeypatch):
    expected = encode_assembly_object(emitter.emit_x86_64_linux_asm(ir), TARGET)

    def forbidden(*args, **kwargs):
        raise AssertionError("runtime object route rendered textual stack maps")

    monkeypatch.setattr(emitter, "render_x86_64_stack_map_section", forbidden)
    monkeypatch.setattr(maps, "_stack_locations", forbidden)
    assert emit_owned_object(ir, TARGET) == expected


# executes the emitted Linux x86-64 image
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64")))
def test_runtime_owned_object_links_and_executes(tmp_path):
    from pcc.backend.x86_64_asm_driver import assemble_file

    ir = f'''target triple = "{TARGET}"
define i64 @answer() {{
entry:
 ret i64 42
}}
'''
    start = assemble_file('''.intel_syntax noprefix
.text
.globl _start
.type _start, @function
_start:
 call answer
 mov edi, eax
 mov eax, 60
 syscall
.size _start, .-_start
''')
    obj = elf.parse_relocatable(emit_owned_object(ir, TARGET))
    binary = tmp_path / "answer"
    binary.write_bytes(elf.link_static_executable([start, obj]))
    binary.chmod(0o755)
    completed = subprocess.run([str(binary)], capture_output=True, timeout=5)
    assert completed.returncode == 42, completed.stderr
