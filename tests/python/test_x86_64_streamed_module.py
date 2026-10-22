"""x86-64 streams finished functions into their sections, without module text.

pcc1's default route emits AArch64 as structured records into its section
builder. x86-64 joined every function into one module text, and the owned
assembler (COFF first rewriting the text for its unwind directives) split and
parsed that text again. Both x86 targets now feed each finished function to
``X86ModuleBuilder``; the text route remains the oracle and must produce the
same object bytes, unwind markers and packed stack maps included.
"""
from __future__ import annotations

import pytest

from pcc.backend import self_backend_x86_64_linux as x86
from pcc.backend.precise_stackmap import PRECISE_STACKMAP_TABLES_ENV
from pcc.backend import target_objects
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.pipeline_frontend_worker_execution import _concrete_direct_indexed_target
from pcc.frontends.python.type_infer import infer_module

_EDGES = """
def f(text):
    return int(text) + 1


def g(items, key):
    try:
        return items[key]
    except KeyError:
        return -1


print(f('3'), g({'a': 1}, 'b'))
"""

_MIXED = """
LIMIT = 40
NAMES = ['alpha', 'beta', 'gamma']
TABLE = {'x': 1.5, 'y': -2.25}


class Counter:
    def __init__(self, start):
        self.value = start
        self.seen = []

    def step(self, amount):
        self.value += amount
        self.seen.append(self.value)
        return self.value


def collatz(n):
    steps = 0
    while n != 1 and steps < LIMIT:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return steps


def describe(counter, scale):
    parts = []
    for index, name in enumerate(NAMES):
        parts.append(name + ':' + str(counter.step(index) * scale))
    try:
        parts.append(str(TABLE['z']))
    except KeyError as error:
        parts.append('missing ' + str(error))
    return ', '.join(parts) + ' / ' + str(TABLE['x'] + TABLE['y'])


print(collatz(27), describe(Counter(3), 2), b'raw' * 2)
"""


def _source(directory, source_text):
    source = directory / "streamed.py"
    source.write_text(source_text, encoding="utf-8")
    return source


def _direct_module(source, monkeypatch, triple):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    modules, exports, derived = build_closed_world_context([str(source)], ["streamed"])
    typed = infer_module(modules[-1], external_exports=exports, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen.generate(typed)
    return _concrete_direct_indexed_target(codegen._direct_indexed_module, triple)


@pytest.mark.parametrize("tables", ["0", "1"], ids=["default", "stack-map-tables"])
@pytest.mark.parametrize("source_text", [_EDGES, _MIXED], ids=["edges", "mixed"])
@pytest.mark.parametrize("triple", ["x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
def test_streamed_module_object_matches_text_route(tmp_path, monkeypatch, triple, source_text, tables):
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, tables)
    # Emission closes each function's native tables, so each route gets its
    # own freshly generated module from the same source file.
    source = _source(tmp_path, source_text)
    text_module = _direct_module(source, monkeypatch, triple)
    plans = []
    assembly = x86._emit_x86_64_module(
        "", windows="windows" in triple, module=text_module, stack_map_plans_out=plans,
    )
    assert bool(plans) == (tables == "1")
    if "windows" in triple:
        assert ".seh_proc" in assembly
    expected = target_objects.encode_assembly_object(
        assembly, triple, stack_map_plans=plans, consume_stack_map_plans=True,
    )
    streamed = target_objects.emit_x86_64_streamed_module(
        _direct_module(source, monkeypatch, triple),
    )
    assert streamed.encode_object() == expected
    # Any plans were consumed into the packed section, as on the text route.
    assert streamed.stack_map_plans == []


def test_streamed_emission_requires_packed_stack_maps(tmp_path, monkeypatch):
    from pcc.backend import BackendUnavailable
    from pcc.backend.x86_64_asm_driver import X86ModuleBuilder

    module = _direct_module(_source(tmp_path, _EDGES), monkeypatch, "x86_64-unknown-linux-gnu")
    with pytest.raises(BackendUnavailable, match="packs its stack maps"):
        x86._emit_x86_64_module("", module=module, builder=X86ModuleBuilder())
