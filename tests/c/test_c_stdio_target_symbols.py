"""Standard streams retain target ABI and ordinary C binding scope."""

from pathlib import Path
import subprocess

import pytest

from pcc.backend import macho_spec
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.c.codegen import c_codegen
from pcc.frontends.c.codegen.c_codegen import CCodeGenerator
from pcc.frontends.c.parse.c_parser import CParser
from pcc.frontends.c.preprocessor import Preprocessor


STREAMS = {"stdin": "__stdinp", "stdout": "__stdoutp", "stderr": "__stderrp"}
TARGETS = (
    ("arm64-apple-darwin", True),
    ("aarch64-apple-macosx14.0.0", True),
    ("x86_64-unknown-linux-gnu", False),
    ("aarch64-unknown-linux-gnu", False),
    ("x86_64-pc-windows-msvc", False),
)
FILE_TYPE = "typedef struct stream_record FILE;\n"


@pytest.fixture(autouse=True)
def forbid_external_compilation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external compiler process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _module(source, target="arm64-apple-darwin", *, preprocess=False):
    if preprocess:
        header_dir = Path(c_codegen.__file__).resolve().parents[4] / "utils/fake_libc_include"
        processor = Preprocessor(target_triple=target, include_dirs=[str(header_dir)])
        source = processor.preprocess(source)
    generator = CCodeGenerator(translation_unit_name="stdio_symbols")
    generator.module.triple = target
    generator.generate_code(CParser().parse(source))
    return generator.module


def _undefined_macho(module):
    data = emit_owned_object(str(module), str(module.triple))
    obj = macho_spec.parse_object(data)
    return {symbol["name"] for symbol in obj.symbols()
            if symbol["n_type"] & macho_spec.N_TYPE == macho_spec.N_UNDF}


@pytest.mark.parametrize("target, darwin", TARGETS)
@pytest.mark.parametrize("stream", STREAMS)
def test_implicit_stream_uses_target_symbol(target, darwin, stream):
    module = _module("void *read_stream(void) { return " + stream + "; }", target)
    expected = STREAMS[stream] if darwin else stream
    assert expected in module.globals
    if expected != stream:
        assert stream not in module.globals


@pytest.mark.parametrize("target, darwin", TARGETS)
@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("scope", ["file", "block", "file_and_block"])
def test_explicit_file_stream_reference_uses_target_abi(target, darwin, stream, scope):
    declaration = "extern FILE *" + stream + ";"
    outer = declaration if scope != "block" else ""
    inner = declaration if scope != "file" else ""
    source = FILE_TYPE + outer + " FILE *read_stream(void) { " + inner + " return " + stream + "; }"
    module = _module(source, target)
    expected = STREAMS[stream] if darwin else stream
    assert expected in module.globals
    if expected != stream:
        assert stream not in module.globals


@pytest.mark.parametrize("target, darwin", TARGETS)
@pytest.mark.parametrize("stream", STREAMS)
def test_owned_stdio_header_selects_the_target_macros(target, darwin, stream):
    source = "#include <stdio.h>\nFILE *read_stream(void) { return " + stream + "; }\n"
    module = _module(source, target, preprocess=True)
    expected = STREAMS[stream] if darwin else stream
    assert expected in module.globals
    if expected != stream:
        assert stream not in module.globals


@pytest.mark.parametrize("shape", ["implicit", "file", "block", "header", "header_undef"])
def test_owned_macho_relocations_name_darwin_stream_globals(shape):
    if shape.startswith("header"):
        source = "#include <stdio.h>\n"
        if shape == "header_undef":
            source += "\n".join("#undef " + name for name in STREAMS) + "\n"
            source += "\n".join("extern FILE *" + name + ";" for name in STREAMS) + "\n"
    elif shape == "file":
        source = FILE_TYPE + "\n".join("extern FILE *" + name + ";" for name in STREAMS)
    else:
        source = FILE_TYPE
    for name in STREAMS:
        inner = "extern FILE *" + name + "; " if shape == "block" else ""
        source += "\nvoid *read_" + name + "(void) { " + inner + "return " + name + "; }\n"
    module = _module(source, preprocess=shape.startswith("header"))
    assert _undefined_macho(module) == {"_" + name for name in STREAMS.values()}


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("binding", ["parameter", "automatic", "static"])
def test_local_stream_binding_is_not_an_external_global(stream, binding):
    if binding == "parameter":
        source = "void *read_stream(void *" + stream + ") { return " + stream + "; }"
    else:
        prefix = "static " if binding == "static" else ""
        source = "void *read_stream(void) { " + prefix + "void *" + stream + " = 0; return " + stream + "; }"
    module = _module(source)
    assert stream not in module.globals
    assert STREAMS[stream] not in module.globals
    assert _undefined_macho(module) == set()


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize("definition", ["tentative", "initialized", "after_use", "static_with_local_extern"])
def test_translation_unit_stream_definition_preserves_its_own_storage(stream, definition):
    if definition == "after_use":
        source = FILE_TYPE + "extern FILE *" + stream + "; FILE *read_stream(void) { return " + stream + "; } FILE *" + stream + ";"
    elif definition == "static_with_local_extern":
        source = FILE_TYPE + "static FILE *" + stream + "; FILE *read_stream(void) { extern FILE *" + stream + "; return " + stream + "; }"
    else:
        initializer = " = 0" if definition == "initialized" else ""
        source = FILE_TYPE + "FILE *" + stream + initializer + "; FILE *read_stream(void) { return " + stream + "; }"
    module = _module(source)
    symbol = (
        "__pcc_internal_stdio_symbols_" + stream
        if definition == "static_with_local_extern" else stream
    )
    assert symbol in module.globals
    assert STREAMS[stream] not in module.globals
    assert _undefined_macho(module) == set()


@pytest.mark.parametrize("stream", STREAMS)
def test_nonpointer_external_binding_is_not_a_stream(stream):
    module = _module("extern int " + stream + "; int read_stream(void) { return " + stream + "; }")
    assert stream in module.globals
    assert STREAMS[stream] not in module.globals
    assert _undefined_macho(module) == {"_" + stream}


@pytest.mark.parametrize("stream", STREAMS)
def test_macro_redirected_stream_preserves_the_expanded_identifier(stream):
    source = FILE_TYPE + "#define " + stream + " custom_stream\nextern FILE *" + stream + "; FILE *read_stream(void) { return " + stream + "; }"
    module = _module(source, preprocess=True)
    assert _undefined_macho(module) == {"_custom_stream"}


@pytest.mark.parametrize("stream", STREAMS)
def test_owned_header_macro_can_be_shadowed_by_a_parameter(stream):
    source = "#include <stdio.h>\nFILE *read_stream(FILE *" + stream + ") { return " + stream + "; }"
    assert _undefined_macho(_module(source, preprocess=True)) == set()


@pytest.mark.parametrize("symbol", STREAMS.values())
def test_explicit_darwin_abi_name_is_not_remapped(symbol):
    module = _module("void *read_stream(void) { return " + symbol + "; }")
    assert symbol in module.globals
    assert set(module.globals) == {"read_stream", symbol}
    assert _undefined_macho(module) == {"_" + symbol}


def test_unsupported_darwin_architecture_keeps_its_c_abi_diagnostic():
    with pytest.raises(ValueError, match="unsupported C target ABI: x86_64-apple-darwin"):
        _module("void *read_stream(void) { return stdout; }", "x86_64-apple-darwin23.0.0")
