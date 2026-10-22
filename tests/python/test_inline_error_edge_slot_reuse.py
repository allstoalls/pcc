"""Inline error edge conditions free their stack slots at their last use.

The kernel records an inline edge's condition use at trigger + 1, which no
instruction use list names, so stack preparation never freed it: every call
site's condition held its own slot for the rest of the function and every
block end re-sorted all of them. With inline edges on, prepare_layout of the
prior gate went 22.8 s -> 86.5 s under pcc1 (hashlib 0.12 s -> 1.44 s on the
host).
"""
from __future__ import annotations

from pathlib import Path

from pcc.backend.self_backend_aarch64_darwin import (
    _aggregate_returned_indirect,
    _aggregate_returned_indirect_indexed,
)
from pcc.backend.self_backend_prepare import prepare_parsed_module_for_target
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.pipeline_frontend_worker_execution import _concrete_direct_indexed_target
from pcc.frontends.python.type_infer import infer_module


def test_inline_edge_conditions_reuse_slots_across_call_sites(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "chain.py"
    calls = "".join("    total += int(text)\n" for _ in range(60))
    source.write_text("def chain(text):\n    total = 0\n" + calls + "    return total\n\nprint(chain('1'))\n")
    modules, exports, derived = build_closed_world_context([str(source)], ["chain"])
    typed = infer_module(modules[-1], external_exports=exports, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen.generate(typed)
    module = _concrete_direct_indexed_target(codegen._direct_indexed_module, "arm64-apple-darwin")
    prepared = prepare_parsed_module_for_target(
        module,
        aggregate_returned_indirect=_aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=_aggregate_returned_indirect_indexed,
        materialize_legacy_slots=False,
    )
    function = next(fn for fn in prepared.functions if fn.name.endswith("_chain"))
    kernel = function.indexed_kernel
    offsets = []
    for block_id in range(len(kernel.block_names)):
        span = kernel.inline_error_edge_span(block_id)
        for edge_id in range(span.first, span.first + span.second):
            offsets.append(kernel.value_slot_offset(kernel.inline_error_edge_condition(edge_id)))
    assert len(offsets) >= 60
    assert len(set(offsets)) <= 4, sorted(set(offsets))
