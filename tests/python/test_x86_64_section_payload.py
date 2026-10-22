"""Bounded section publication preserves assembler offsets and object bytes."""

import builtins
import platform
import subprocess
import sys

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend import x86_64_asm_driver as assembler


class _GrowingPayload:
    """The former append-by-append bytearray publication, as a byte oracle."""

    def __init__(self):
        self.data = bytearray()

    def append(self, raw):
        self.data.extend(raw)

    def finish(self):
        return bytes(self.data)


@pytest.mark.parametrize("sizes", [(), (0,), (1, 2, 3), (8191, 1, 2),
    (8192,), (8193,), (3, 16384, 5), (15,) * 2000])
def test_payload_bounds_pending_storage_and_retires_fragments(sizes):
    payload = assembler._SectionPayload()
    fragments = [bytes((index % 251,)) * size for index, size in enumerate(sizes)]
    for fragment in fragments:
        payload.append(fragment)
        assert 0 <= payload.pending_size < assembler._SECTION_PAYLOAD_CHUNK_BYTES
        assert payload.pending_size == sum(map(len, payload.pending))
    assert payload.finish() == b"".join(fragments)
    assert payload.pending == [] and payload.chunks == [] and payload.pending_size == 0
    assert payload.finish() == b""
    payload.append(b"reuse")
    assert payload.finish() == b"reuse"


def test_assembler_does_not_grow_a_bytearray_for_each_instruction(monkeypatch):
    copied = []

    class ObservedBytearray(builtins.bytearray):
        def extend(self, value):
            copied.append(len(self) + len(value))
            return super().extend(value)

    monkeypatch.setattr(assembler, "bytearray", ObservedBytearray, raising=False)
    text = ".intel_syntax noprefix\n.text\nprobe:\n" + "mov eax, 42\n" * 2000 + "ret\n"
    obj = assembler.assemble_file(text)
    assert obj.sections[0].data == bytes.fromhex("b8 2a 00 00 00") * 2000 + b"\xc3"
    assert copied == [], "native bytearray.extend copies the entire section prefix"


MIXED = """.intel_syntax noprefix
.text
.globl probe
.type probe, @function
probe:
  mov eax, 1
  jmp .Lforward
.Lback:
  add eax, 1
  ret
  .p2align 4, 0x90
.Lforward:
  call external
  jmp .Lback
.size probe, .-probe
.data
.globl payload
.type payload, @object
payload:
  .byte 1, 2, 3
  .long .Lforward-probe
  .quad external+8
  .zero 9
  .p2align 3, 0
.size payload, .-payload
.section .tdata,"awT",@progbits
.type tls_data, @tls_object
tls_data:
  .long 123
.section .tbss,"awT",@nobits
.type tls_zero, @tls_object
tls_zero:
  .zero 257
.section .note.GNU-stack,"",@progbits
"""


@pytest.mark.parametrize("chunk_bytes", [1, 7, 16, 8192])
@pytest.mark.parametrize("text", [".intel_syntax noprefix\n", MIXED,
    ".intel_syntax noprefix\n.text\nprobe:\n" + "nop\n" * 17000 + "ret\n",
    ".intel_syntax noprefix\n.data\npayload:\n.zero 16385\n.byte 7\n"])
def test_complete_objects_equal_former_growing_payload(monkeypatch, chunk_bytes, text):
    monkeypatch.setattr(assembler, "_SECTION_PAYLOAD_CHUNK_BYTES", chunk_bytes)
    actual = elf.emit_relocatable(assembler.assemble_file(text))
    monkeypatch.setattr(assembler, "_SectionPayload", _GrowingPayload)
    expected = elf.emit_relocatable(assembler.assemble_file(text))
    assert actual == expected


@pytest.mark.parametrize("text", [
    ".intel_syntax noprefix\n.text\nprobe:\nunsupported eax\n",
    ".intel_syntax noprefix\n.data\n.long missing-other\n",
    ".intel_syntax noprefix\n.section .tbss,\"awT\",@nobits\n.byte 1\n",
    ".intel_syntax noprefix\n.text\ncall missing\n.size absent, 1\n",
])
def test_first_error_matches_former_growing_payload(monkeypatch, text):
    outcomes = []
    for payload in (assembler._SectionPayload, _GrowingPayload):
        monkeypatch.setattr(assembler, "_SectionPayload", payload)
        with pytest.raises(Exception) as error:
            assembler.assemble_file(text)
        outcomes.append((type(error.value), str(error.value)))
    assert outcomes[0] == outcomes[1]


# Linux x86_64 native execution required
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64")))
@pytest.mark.integration
def test_chunked_assembler_emits_identical_executed_native_elf(tmp_path, monkeypatch):
    """Host owned assembler and linker; actually execute both x86 ELF images."""
    text = """.intel_syntax noprefix
.text
.globl _start
.type _start, @function
_start:
  mov ecx, 2
  xor edi, edi
.Lloop:
  add edi, 1
  sub ecx, 1
  jne .Lloop
  call answer
  add edi, eax
  jmp .Lfinish
""" + "  nop\n" * 17000 + """.Lfinish:
  mov eax, 60
  syscall
.size _start, .-_start
.globl answer
.type answer, @function
answer:
  mov eax, 40
  ret
.size answer, .-answer
"""
    images = []
    for name, payload in (("chunked", assembler._SectionPayload), ("former", _GrowingPayload)):
        monkeypatch.setattr(assembler, "_SectionPayload", payload)
        image = elf.link_static_executable([assembler.assemble_file(text)])
        elf.parse_static_executable(image)
        path = tmp_path / name
        path.write_bytes(image)
        path.chmod(0o755)
        result = subprocess.run([str(path)], capture_output=True, timeout=5)
        assert (result.returncode, result.stdout, result.stderr) == (42, b"", b"")
        images.append(image)
    assert images[0] == images[1]
