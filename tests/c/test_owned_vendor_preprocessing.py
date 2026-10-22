"""Vendor inputs enter the owned preprocessor/parser without host tools."""

import subprocess
from pathlib import Path

import pytest

from pcc.frontends.c.evaluator.c_evaluator import _preprocess_translation_unit_source
from pcc.frontends.c.parse import make_c_parser


@pytest.mark.parametrize("relative,arguments", (
    ("zlib-1.3.1/adler32.c", ("-DHAVE_UNISTD_H", "-DHAVE_STDARG_H", "-U__ARM_FEATURE_CRC32")),
    ("lz4-1.10.0/lib/lz4.c", ()),
    ("lua-5.5.0/lapi.c", ("-DLUA_USE_JUMPTABLE=0", "-DLUA_NOBUILTIN")),
))
def test_owned_vendor_preprocess_and_parse(relative, arguments, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("owned vendor frontend invoked a tool: " + repr(args))

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    source = Path(__file__).resolve().parents[2] / "projects" / relative
    processed = _preprocess_translation_unit_source(
        source.read_text(), str(source.parent), False,
        include_dirs=[str(source.parent)], cpp_args=arguments,
        target_triple="arm64-apple-darwin",
    )
    assert make_c_parser().parse(processed).ext
