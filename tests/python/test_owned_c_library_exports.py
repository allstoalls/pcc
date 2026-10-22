"""Missing libc exports: source body checks and admitted native C execution."""

import ast
from pathlib import Path
import re
import struct
import subprocess

import pytest

from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
from tests.owned_runtime_c_fixture import link_c_harness, runtime_ir

ROOT = Path(__file__).resolve().parents[2]
MEMORY_SOURCE = ROOT / "pcc/runtime/py/freestanding_mem_str.py"
STDIO_SOURCE = ROOT / "pcc/runtime/py/freestanding_stdio.py"


class BodyModel:
    """Execute the actual freestanding bodies with bounded memory/syscall models."""

    def __init__(self):
        self.memory = {}
        self.next_address = 4096
        self.input = []
        self.read_count = 0
        self.args = iter(())
        self.ended = 0
        self.ns = {
            "i64": int, "c_ptr": int,
            "null": lambda: 0, "ptr_is_null": lambda p: p == 0,
            "ptr_add": lambda p, n: p + n, "ptr_diff": lambda a, b: a - b,
            "load_i8": lambda p, n: self.memory[p + n],
            "store_i8": self.store_byte,
            "load_i64": self.load_word, "store_i64": self.store_word,
            "stack_alloc": self.alloc, "malloc": self.alloc, "free": lambda p: None,
            "cstr": lambda s: self.bytes(s.encode() + b"\0"),
            "abi_constant": ABI_CONSTANTS.__getitem__, "read": self.read,
            "unsigned_div_i64": lambda a, b: (a & ((1 << 64) - 1)) // b,
            "unsigned_rem_i64": lambda a, b: (a & ((1 << 64) - 1)) % b,
            "wrapping_mul_i64": lambda a, b: ((a * b + (1 << 63)) % (1 << 64)) - (1 << 63),
            "mul_overflow_i64": lambda a, b: not -(1 << 63) <= a * b < (1 << 63),
            "logical_shift_right_i64": lambda a, b: (a & ((1 << 64) - 1)) >> b,
            "f64_bits": lambda a: int.from_bytes(struct.pack("<d", a), "little"),
            "f64_signbit": lambda a: struct.pack("<d", a)[7] >> 7,
            "va_start": lambda: self.args, "va_end": self.end,
        }
        for name in ("va_arg_i32", "va_arg_u32", "va_arg_i64", "va_arg_ptr", "va_arg_f64"):
            self.ns[name] = next
        for path in (MEMORY_SOURCE, STDIO_SOURCE):
            functions = [node for node in ast.parse(path.read_text()).body
                         if isinstance(node, ast.FunctionDef)]
            for node in functions:
                node.decorator_list = []
            exec(compile(ast.Module(functions, type_ignores=[]), str(path), "exec"), self.ns)

    def alloc(self, size):
        result = self.next_address
        self.next_address += size + 16
        self.memory.update({result + n: 0 for n in range(size)})
        return result

    def bytes(self, value):
        result = self.alloc(len(value))
        for offset, byte in enumerate(value):
            self.store_byte(result, offset, byte)
        return result

    def store_byte(self, pointer, offset, byte):
        assert pointer + offset in self.memory, "out-of-bounds write"
        self.memory[pointer + offset] = byte & 255

    def raw(self, pointer, count):
        return bytes(self.memory[pointer + n] for n in range(count))

    def load_word(self, pointer, offset):
        return int.from_bytes(self.raw(pointer + offset, 8), "little", signed=True)

    def store_word(self, pointer, offset, value):
        for index, byte in enumerate(value.to_bytes(8, "little", signed=True)):
            self.store_byte(pointer, offset + index, byte)

    def read(self, descriptor, output, count):
        self.read_count += 1
        assert count == 1
        if not self.input:
            return 0
        byte = self.input.pop(0)
        if byte < 0:
            return byte
        self.store_byte(output, 0, byte)
        return 1

    def stream(self, value, readable=True):
        result = self.alloc(ABI_CONSTANTS["stdio.file.size"])
        self.input = list(value)
        self.store_word(result, ABI_CONSTANTS["stdio.file.magic_offset"], ABI_CONSTANTS["stdio.file.magic"])
        self.store_word(result, ABI_CONSTANTS["stdio.file.flags_offset"],
                        ABI_CONSTANTS["stdio.flag.readable"] if readable else 0)
        return result

    def flags(self, stream):
        return self.load_word(stream, ABI_CONSTANTS["stdio.file.flags_offset"])

    def end(self, cursor):
        assert cursor is self.args
        self.ended += 1


@pytest.mark.parametrize("source,count,expected", [
    (b"gosh\0", 2, b"gollo!"),
    (b"gosh\0", 4, b"gosho!"),
    (b"g\0", 5, b"g\0\0\0\0!"),
    (b"\0", 5, b"\0\0\0\0\0!"),
    (b"\xff\x80\0", 3, b"\xff\x80\0lo!"),
    (b"", 0, b"hello!"),
])
def test_strncpy_body_respects_exact_bound_and_padding(source, count, expected):
    model = BodyModel()
    output = model.bytes(b"hello!")
    src = model.bytes(source)
    assert model.ns["pcc_strncpy"](output, src, count) == output
    assert model.raw(output, 6) == expected


def test_getc_body_returns_unsigned_bytes_and_eof_error_flags():
    model = BodyModel()
    stream = model.stream([0, 128, 255])
    assert [model.ns["getc"](stream) for _ in range(4)] == [0, 128, 255, -1]
    assert model.flags(stream) & ABI_CONSTANTS["stdio.flag.eof"]
    assert not model.ns["ferror"](stream)
    stream = model.stream([-5])
    assert model.ns["getc"](stream) == -1
    assert model.ns["ferror"](stream)
    assert not model.flags(stream) & ABI_CONSTANTS["stdio.flag.eof"]


@pytest.mark.parametrize("count", [-2147483648, -1, 0, 1])
def test_fgets_body_nonpositive_or_single_byte_capacity_never_reads(count):
    model = BodyModel()
    output = model.bytes(b"guard")
    stream = model.stream(b"x\n")
    assert model.ns["fgets"](output, count, stream) == (output if count == 1 else 0)
    assert model.read_count == 0
    assert model.raw(output, 5) == (b"\0uard" if count == 1 else b"guard")


def test_fgets_body_preserves_newline_bound_nul_and_partial_eof():
    model = BodyModel()
    output = model.bytes(b"?????")
    stream = model.stream(b"abcd\nef\x00g")
    for expected in (b"abc\0?", b"d\n\0\0?", b"ef\0\0?", b"g\0\0\0?"):
        assert model.ns["fgets"](output, 4, stream) == output
        assert model.raw(output, 5) == expected
    previous = model.raw(output, 5)
    assert model.ns["fgets"](output, 4, stream) == 0
    assert model.raw(output, 5) == previous
    assert model.flags(stream) & ABI_CONSTANTS["stdio.flag.eof"]
    assert not model.ns["ferror"](stream)


@pytest.mark.parametrize("input_bytes,readable", [([-5], True), ([65, -5], True), ([65], False)])
def test_fgets_body_read_errors_return_null(input_bytes, readable):
    model = BodyModel()
    stream = model.stream(input_bytes, readable=readable)
    output = model.bytes(b"????")
    assert model.ns["fgets"](output, 4, stream) == 0
    assert model.ns["ferror"](stream)
    assert not model.flags(stream) & ABI_CONSTANTS["stdio.flag.eof"]


def test_sprintf_body_reuses_formatting_counts_and_variadic_cursor():
    model = BodyModel()
    output = model.bytes(b"?" * 96)
    text = model.bytes(b"owned\0")
    fmt = model.bytes(b"%02d|%*.*s|%#x|%lld|%.2f|%%\0")
    model.args = iter((3, -6, 3, text, 42, -(1 << 40), 1.25))
    expected = b"03|own   |0x2a|-1099511627776|1.25|%"
    assert model.ns["sprintf"](output, fmt) == len(expected)
    assert model.raw(output, len(expected) + 2) == expected + b"\0?"
    assert model.ended == 1
    assert list(model.args) == []


def test_sprintf_body_empty_output_and_formatter_error_end_cursor():
    model = BodyModel()
    output = model.bytes(b"???")
    assert model.ns["sprintf"](output, model.bytes(b"\0")) == 0
    assert model.raw(output, 3) == b"\0??"
    assert model.ns["sprintf"](output, 0) == -1
    assert model.ended == 2


def test_missing_exports_have_exact_emitted_c_abi(tmp_path):
    memory = runtime_ir(MEMORY_SOURCE, tmp_path / "memory.ll").read_text()
    stdio = runtime_ir(STDIO_SOURCE, tmp_path / "stdio.ll").read_text()
    signatures = {
        "strncpy": (memory, "ptr", ["ptr", "ptr", "i64"]),
        "getc": (stdio, "i32", ["ptr"]),
        "fgets": (stdio, "ptr", ["ptr", "i32", "ptr"]),
        "sprintf": (stdio, "i32", ["ptr", "ptr", "..."]),
    }
    for symbol, (text, result, arguments) in signatures.items():
        match = re.search(r"^define\s+(?:external\s+)?" + result + r"\s+@\"?" + symbol + r"\"?\(([^\n]*)\)", text, re.M)
        assert match, symbol
        assert [part.strip().split()[0] for part in match[1].split(",")] == arguments
    fgets_body = re.search(r"^define[^\n]*@fgets\(.*?^}", stdio, re.M | re.S)
    assert fgets_body is not None
    assert re.search(r"sext i32 %count to i64", fgets_body[0])


@pytest.mark.integration
def test_missing_exports_execute_in_owned_c_program(tmp_path):
    source = tmp_path / "owned_c_library_exports.c"
    output = tmp_path / "owned_c_library_exports"
    source.write_text(r'''
#include <stdio.h>
#include <string.h>
#include <limits.h>
int main(int argc, char **argv) {
    char buffer[128] = "hello!";
    char line[5] = {'?', '?', '?', '?', '?'};
    const unsigned char bytes[5] = {255, 10, 'a', 0, 'b'};
    FILE *stream;
    int count;
    if (argc != 2) return 1;
    if (strncpy(buffer, "go", 2) != buffer || strcmp(buffer, "gollo!")) return 2;
    if (strncpy(buffer, "x", 5) != buffer || memcmp(buffer, "x\0\0\0\0!", 6)) return 3;
    count = sprintf(buffer, "%02d|%*.*s|%#x|%lld|%.2f|%%", 3, -6, 3, "owned", 42u, -1099511627776LL, 1.25);
    if (count != 36 || strcmp(buffer, "03|own   |0x2a|-1099511627776|1.25|%")) return 4;
    count = sprintf(buffer, "%d%d%d%d%d%d%d%d|%.1f%.1f%.1f%.1f%.1f%.1f%.1f%.1f%.1f", 1, 2, 3, 4, 5, 6, 7, 8, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0);
    if (count != 36 || strcmp(buffer, "12345678|1.02.03.04.05.06.07.08.09.0")) return 5;
    stream = fopen(argv[1], "wb");
    if (!stream || fwrite(bytes, 1, 5, stream) != 5 || fclose(stream)) return 6;
    stream = fopen(argv[1], "rb");
    if (!stream) return 7;
    if (fgets(line, INT_MIN, stream) || line[0] != '?' || ftell(stream)) return 8;
    if (fgets(line, 1, stream) != line || line[0] || ftell(stream)) return 9;
    if (getc(stream) != 255 || getc(stream) != '\n') return 10;
    if (fgets(line, 4, stream) != line || memcmp(line, "a\0b\0?", 5)) return 11;
    if (fgets(line, 4, stream) != NULL || ferror(stream)) return 12;
    if (getc(stream) != EOF || fclose(stream)) return 13;
    stream = fopen(argv[1], "wb");
    if (!stream || fgets(line, 4, stream) || !ferror(stream)) return 14;
    if (fclose(stream)) return 15;
    return 0;
}
''')
    link_c_harness(source, output, defines=())
    result = subprocess.run([str(output), str(tmp_path / "payload.bin")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
