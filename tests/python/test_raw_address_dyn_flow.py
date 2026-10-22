"""Raw native addresses keep their value through untyped Python helpers.

Outside runtime-port modules a raw address is an ``int``.  An unannotated
helper returns it through the dyn ABI (a tagged small int), a tuple element
carries it the same way, and a typed ``int`` joined from two addresses may be
boxed.  Every address operand used to pass those representations through
unchanged, so ``dlsym`` received ``handle << 1 | 1`` and the zlib, bz2 and
lzma ports failed with "system zlib is missing the inflate ABI" or jumped to a
tagged function pointer.  Address operands (loader handles, symbol names,
indirect callees, ``ptr_to_int``, ``c_rawptr`` externs) now untag dyn values
and unbox ``int`` ones.  A dyn value stored by ``store_ptr`` is still an object
reference by design, which is why the stdlib helpers are annotated ``-> int``.
"""

import os
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="loads /usr/lib/libz.1.dylib"
)


RAW_PROGRAM = '''from pcc.extern import c_int64, c_obj, c_rawptr, extern
from pcc.unsafe import (
    call_ptr0,
    cstr,
    dynamic_library_open,
    dynamic_library_symbol,
    free,
    int_to_ptr,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_is_null,
    ptr_to_int,
    store_i64,
    store_ptr,
)

_py_bytes_new = extern("py_bytes_new", (c_rawptr, c_int64), c_obj)


def _open():
    return dynamic_library_open(cstr("/usr/lib/libz.1.dylib"), "darwin")


def _symbols(handle):
    return (
        dynamic_library_symbol(handle, cstr("zlibVersion")),
        dynamic_library_symbol(handle, cstr("inflate")),
    )


def _slot(buf, index):
    return ptr_add(buf, index * 8)


def _slot_int(buf, index) -> int:
    return ptr_add(buf, index * 8)


def main():
    handle = _open()
    version_fn, inflate_fn = _symbols(handle)
    print(ptr_is_null(handle), ptr_is_null(version_fn), ptr_is_null(inflate_fn))
    print(ptr_is_null(call_ptr0(version_fn)))
    buf = malloc(64)
    kept = ptr_to_int(_slot(buf, 1))
    store_i64(int_to_ptr(kept), 0, 42)
    print(load_i64(buf, 8))
    store_ptr(buf, 16, _slot_int(buf, 3) if kept else null())
    print(ptr_to_int(load_ptr(buf, 16)) - ptr_to_int(buf))
    store_i64(buf, 24, 0x6F6C6C6568)
    print(_py_bytes_new(_slot(buf, 3), 5))
    free(buf)


main()
'''

RAW_EXPECTED = "False False False\nFalse\n42\n24\nb'hello'\n"


CODEC_PROGRAM = '''import bz2
import lzma
import zlib


def main():
    data = b"pcc compression " * 4096
    b = bz2.compress(data)
    print("bz2", len(b) < len(data), bz2.decompress(b) == data)
    d = bz2.BZ2Decompressor()
    print("bz2-stream", d.decompress(b) == data, d.eof)
    x = lzma.compress(data)
    print("lzma", len(x) < len(data), lzma.decompress(x) == data)
    ld = lzma.LZMADecompressor()
    print("lzma-stream", ld.decompress(x) == data, ld.eof)
    z = zlib.compress(data, 9)
    zd = zlib.decompressobj()
    print("zlib-stream", zd.decompress(z) + zd.flush() == data)
    co = zlib.compressobj()
    print("zlib-obj", zlib.decompress(co.compress(data) + co.flush()) == data)
    for sample in (b"", b"a", data):
        print(zlib.crc32(sample), zlib.adler32(sample), zlib.adler32(sample, 7))


main()
'''


def _compile_and_run_all_collectors(tmp_path, name, program, compiler, archive):
    source = tmp_path / (name + ".py")
    source.write_text(program, encoding="utf-8")
    binary = tmp_path / name
    compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(archive),
    )
    outputs = []
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        outputs.append(ran.stdout)
    return source, outputs


def test_raw_addresses_survive_untyped_helpers_and_joins(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    _source, outputs = _compile_and_run_all_collectors(
        tmp_path, "raw_address_flow", RAW_PROGRAM, python_program_compiler,
        pcc_runtime_archive,
    )
    for backend, out in enumerate(outputs):
        assert out == RAW_EXPECTED, f"GC{backend}: {out!r}"


def test_zlib_bz2_lzma_match_cpython_without_libpython(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source, outputs = _compile_and_run_all_collectors(
        tmp_path, "codec_roundtrip", CODEC_PROGRAM, python_program_compiler,
        pcc_runtime_archive,
    )
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=120,
    )
    assert reference.returncode == 0, reference.stderr
    for backend, out in enumerate(outputs):
        assert out == reference.stdout, f"GC{backend}: {out!r}"
