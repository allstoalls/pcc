"""Real shared-library publication/loading with no compiler-tool fallback."""

import builtins
import ctypes
import math
import os
import platform
import threading
from pathlib import Path

import pytest

from pcc.backend import BackendUnavailable, macho_spec as spec


_HOST = os.sys.platform == "darwin" and platform.machine() == "arm64"
_NATIVE = pytest.mark.pcc_gate(unavailable=None if _HOST else "requires owned Darwin arm64 shared libraries")


@pytest.fixture(autouse=True)
def forbid_compiler_fallback(monkeypatch):
    from pcc.api import CEvaluator

    original = builtins.__import__

    def checked(name, *args, **kwargs):
        if name == "llvmlite" or name.startswith("llvmlite."):
            raise AssertionError("owned shared publication imported " + name)
        return original(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("owned API requested a system C compiler")

    monkeypatch.setattr(builtins, "__import__", checked)
    monkeypatch.setattr(CEvaluator, "_system_cc", forbidden)
    monkeypatch.delenv("PCC_BACKEND", raising=False)


def _source(tmp_path, text, name="source.c"):
    path = tmp_path / name
    path.write_text(text)
    return path


@_NATIVE
def test_public_shared_artifact_is_signed_loadable_dylib(tmp_path):
    from pcc import build
    from pcc.backend.macho_codesign import parse_signature

    source = _source(tmp_path, "int value=4; int add(int a,int b){return a+b;} int *address(void){return &value;}")
    artifact = build(source, kind="sharedlib", out_dir=tmp_path / "output", use_compile_cache=False)
    data = Path(artifact.output_path).read_bytes()
    obj = spec.parse_object(data)
    assert artifact.kind == "sharedlib" and artifact.backend == "self"
    assert obj.header["filetype"] == spec.MH_DYLIB
    assert spec.LC_MAIN not in [command.cmd for command in obj.commands]
    assert spec.LC_DYLD_EXPORTS_TRIE in [command.cmd for command in obj.commands]
    assert parse_signature(data).exec_seg_flags == 0
    assert not any(section["segname_str"] == "__PAGEZERO" for section in obj.sections())
    library = ctypes.CDLL(artifact.output_path)
    assert library.add(3, 4) == 7
    library.address.restype = ctypes.POINTER(ctypes.c_int32)
    pointer = library.address()
    assert pointer.contents.value == 4
    pointer.contents.value = 99
    assert ctypes.c_int32.in_dll(library, "value").value == 99


@_NATIVE
def test_owned_shared_module_merges_translation_units_and_private_symbols(tmp_path):
    from pcc import module

    first = _source(tmp_path, "static int value=3; int left(int x){return value+x;}", "first.c")
    second = _source(tmp_path, "static int value=7; int left(int x); int right(int x){return value+left(x);}", "second.c")
    loaded = module([first, second])
    assert loaded.left(4) == 7
    assert loaded.right(4) == 14


@_NATIVE
def test_owned_shared_modules_keep_independent_global_state(tmp_path):
    from pcc import module

    first = module(_source(tmp_path, "int value=1; int advance(void){return ++value;}", "one.c"))
    second = module(_source(tmp_path, "int value=10; int advance(void){return ++value;}", "two.c"))
    assert first.advance() == 2
    assert second.advance() == 11
    assert first.advance() == 3


@_NATIVE
def test_owned_shared_library_can_have_only_private_data(tmp_path):
    from pcc import build

    artifact = build(_source(tmp_path, "static int value=4;"), kind="sharedlib", use_compile_cache=False)
    assert artifact.exports == []
    assert spec.parse_object(Path(artifact.output_path).read_bytes()).header["filetype"] == spec.MH_DYLIB
    assert ctypes.CDLL(artifact.output_path)._handle


@_NATIVE
def test_owned_shared_library_binds_libsystem_math_and_string_calls(tmp_path):
    from pcc import module

    source = _source(tmp_path, "#include <math.h>\nint length(char *x){return strlen(x);} double sine(double x){return sin(x);}")
    loaded = module(source, libs=["m", "c"], link_args=["-lSystem"])
    loaded.sine.restype = ctypes.c_double
    loaded.sine.argtypes = [ctypes.c_double]
    assert loaded.length(b"ABCDE") == 5
    assert loaded.sine(0.25) == pytest.approx(math.sin(0.25))
    assert loaded.__pcc_artifact__.libs == ["m", "c"]


@_NATIVE
def test_owned_shared_library_preserves_zero_initialized_data_and_pointer_rebases(tmp_path):
    from pcc import module

    source = _source(tmp_path, "char zeros[70000]; char *inside=&zeros[60000]; int read(void){return zeros[69999]+*inside;} int update(void){zeros[69999]=9;*inside=7;return read();}")
    loaded = module(source)
    assert loaded.read() == 0
    assert loaded.update() == 16


@_NATIVE
def test_owned_shared_publisher_preserves_virtual_bss_and_compact_linkedit(tmp_path):
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.native_object import NativeObject
    from pcc.backend.macho_shared import link_shared_library
    from pcc.backend.macho_obj import Section, TextSymbol

    assembly = '''.section __TEXT,__text,regular,pure_instructions
.globl _read
_read:
  adrp x9, _zeros@PAGE
  add x9, x9, _zeros@PAGEOFF
  ldr w0, [x9]
  ret
.section __DATA,__data
.p2align 3
.globl _inside
_inside:
  .quad _zeros+60000
'''
    sections, undefined = assemble_file(assembly)
    sections.append(Section(sectname="__bss", segname="__DATA", flags=spec.S_ZEROFILL,
                            zerofill_size=70000, symbols=(TextSymbol("_zeros", 0),)))
    obj = NativeObject.from_sections(sections, undefined=[name for name in undefined if name != "_zeros"])
    path = tmp_path / "bss.dylib"
    data = link_shared_library([obj], target="arm64-apple-darwin", identity=str(path))
    path.write_bytes(data)
    assert len(data) < 70000
    loaded = ctypes.CDLL(str(path))
    assert loaded.read() == 0
    zeros = ctypes.c_int32.in_dll(loaded, "zeros")
    inside = ctypes.c_void_p.in_dll(loaded, "inside").value
    assert inside == ctypes.addressof(zeros) + 60000
    assert ctypes.string_at(inside, 1) == b"\0"
    zeros.value = 55
    assert loaded.read() == 55


@_NATIVE
def test_owned_shared_library_registers_tls_and_exports_thread_local_data(tmp_path):
    from pcc import module

    source = _source(tmp_path, "_Thread_local int value=4; int read(void){return value;} int advance(void){return ++value;} int *address(void){return &value;}")
    loaded = module(source)
    assert loaded.advance() == 5
    loaded.address.restype = ctypes.POINTER(ctypes.c_int32)
    assert loaded.address().contents.value == 5
    # Darwin dlsym exposes the TLV descriptor. The C accessor resolves that
    # descriptor to this thread's storage rather than reading its thunk word.
    assert ctypes.c_void_p.in_dll(loaded._lib, "value").value
    values = []

    def child():
        values.extend((loaded.read(), loaded.advance(), loaded.read()))

    thread = threading.Thread(target=child)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert values == [4, 5, 5]
    assert loaded.read() == 5


@_NATIVE
@pytest.mark.parametrize("options", [{"libs": ["z"]}, {"link_args": ["-Wl,-undefined,dynamic_lookup"]}])
def test_public_shared_api_rejects_unsupported_link_inputs(tmp_path, options):
    from pcc import build

    source = _source(tmp_path, "int answer(void){return 42;}")
    output = tmp_path / "output"
    with pytest.raises(BackendUnavailable, match="does not support link argument"):
        build(source, kind="sharedlib", out_dir=output, **options)
    assert not (output / "libpcc_module.dylib").exists()


@_NATIVE
def test_public_shared_api_rejects_missing_nonplatform_import(tmp_path):
    from pcc import build

    source = _source(tmp_path, "int missing_owned_api_symbol(void); int answer(void){return missing_owned_api_symbol();}")
    with pytest.raises(BackendUnavailable, match="unsupported libSystem import"):
        build(source, kind="sharedlib")


@pytest.mark.parametrize("target", ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
def test_owned_shared_publisher_fails_explicitly_at_other_platforms(target):
    from pcc.backend.macho_shared import link_shared_library

    with pytest.raises(BackendUnavailable, match="Darwin arm64 host"):
        link_shared_library([], target=target, identity="unsupported.dylib")
