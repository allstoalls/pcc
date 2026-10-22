"""Owned AArch64 atomic lowering keeps narrow accesses and aggregate fields.

These are cross-host assembly/object checks. The original native C signedness,
wrap and neighboring-byte cases stay in tests/c/test_owned_narrow_atomics.py.
"""

import re
import subprocess

import pytest

from pcc.backend import BackendUnavailable, macho_spec
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm


@pytest.fixture(autouse=True)
def forbid_external_compilers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external compiler process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")


def _emit(body):
    text = 'target triple = "arm64-apple-darwin"\n' + body
    assembly = emit_aarch64_darwin_asm(text, optimize=False)
    data = emit_owned_object(text, "arm64-apple-darwin")
    obj = macho_spec.parse_object(data)
    assert any(section["sectname_str"] == "__text" and section["size"] > 0
               for section in obj.sections())
    return assembly


@pytest.mark.parametrize("width, suffix", [(8, "b"), (16, "h")])
@pytest.mark.parametrize("op, instruction", [
    ("add", "add"), ("sub", "sub"), ("and", "and"),
    ("or", "orr"), ("xchg", "mov"),
])
@pytest.mark.parametrize("ordering", ["monotonic", "acquire", "release", "acq_rel", "seq_cst"])
def test_narrow_rmw_preserves_access_width(width, suffix, op, instruction, ordering):
    assembly = _emit(f"""
define i{width} @probe(ptr %address) {{
entry:
  %prior = atomicrmw {op} ptr %address, i{width} -5 {ordering}
  ret i{width} %prior
}}
""")
    assert f"  ldaxr{suffix} w11, [x9]" in assembly
    assert f"  stlxr{suffix} w13, w12, [x9]" in assembly
    assert f"  {instruction} w12," in assembly
    assert "  cbnz w13, Lat_" in assembly
    assert "  ldaxr w11, [x9]" not in assembly
    assert re.search(r"movz w10, #" + str((1 << width) - 5) + r"\b", assembly)


@pytest.mark.parametrize("width, suffix, flag_offset", [(8, "b", 1), (16, "h", 2)])
@pytest.mark.parametrize("weak", [False, True])
def test_narrow_cmpxchg_stores_old_value_and_success_at_exact_width(width, suffix, flag_offset, weak):
    modifier = "weak " if weak else ""
    assembly = _emit(f"""
define {{i{width}, i1}} @probe(ptr %address) {{
entry:
  %pair = cmpxchg {modifier}ptr %address, i{width} -2, i{width} -7 acq_rel acquire
  ret {{i{width}, i1}} %pair
}}
""")
    assert f"  ldaxr{suffix} w12, [x9]" in assembly
    assert f"  stlxr{suffix} w13, w11, [x9]" in assembly
    assert f"  str{suffix} w12, [x15]" in assembly
    assert f"  strb w14, [x15, #{flag_offset}]" in assembly
    assert "  str w12, [x15]" not in assembly
    assert "  clrex" in assembly
    assert "  cset w14, eq" in assembly
    assert re.search(r"movz w10, #" + str((1 << width) - 2) + r"\b", assembly)
    assert re.search(r"movz w11, #" + str((1 << width) - 7) + r"\b", assembly)


@pytest.mark.parametrize("width", [1, 128])
@pytest.mark.parametrize("kind", ["rmw", "cmpxchg"])
def test_unsupported_atomic_width_stays_explicit(width, kind):
    operation = (
        f"%v = atomicrmw xchg ptr %p, i{width} 0 seq_cst\n  ret i{width} %v"
        if kind == "rmw" else
        f"%v = cmpxchg ptr %p, i{width} 0, i{width} 1 seq_cst monotonic\n  ret {{i{width}, i1}} %v"
    )
    result_type = f"i{width}" if kind == "rmw" else f"{{i{width}, i1}}"
    with pytest.raises(BackendUnavailable, match="atomicrmw|cmpxchg"):
        _emit(f"define {result_type} @probe(ptr %p) {{\nentry:\n  {operation}\n}}\n")
