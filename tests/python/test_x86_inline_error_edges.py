"""x86-64 emits every inline error edge; no target may drop one.

a372d3b58 made inline error edges the default on the direct route for every
host target. Only AArch64 emitted them: the shared ``emit_function_blocks``
that x86-64 (Linux and Windows) uses never read the kernel's edge table, so
the compare after each fallible call was computed and stored and then fell
through. Errors stopped propagating; on CI the Linux x86-64 native programs
printed nothing and exited 0, and the Windows tar program swallowed
tarfile.open's exception and exited 0 without a traceback.
"""
from __future__ import annotations

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_x86_64_linux as x86
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.pipeline_frontend_worker_execution import _concrete_direct_indexed_target
from pcc.frontends.python.type_infer import infer_module

_SOURCE = """
def f(text):
    return int(text) + 1


def g(items, key):
    try:
        return items[key]
    except KeyError:
        return -1


print(f('3'), g({'a': 1}, 'b'))
"""


def _direct_module(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.setenv("PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "edges.py"
    source.write_text(_SOURCE, encoding="utf-8")
    modules, exports, derived = build_closed_world_context([str(source)], ["edges"])
    typed = infer_module(modules[-1], external_exports=exports, derived_class_map=derived)
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen.generate(typed)
    return codegen._direct_indexed_module


def _function_body(asm, name):
    start = asm.index(name + ":\n")
    return asm[start:asm.index(".size", start)].splitlines()


@pytest.mark.parametrize("triple", ["x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
def test_every_inline_edge_branches_after_its_check(tmp_path, monkeypatch, triple):
    module = _concrete_direct_indexed_target(_direct_module(tmp_path, monkeypatch), triple)
    edge_counts = {}
    edge_targets = {}
    for function in module.functions:
        kernel = getattr(function, "indexed_kernel", None)
        if kernel is None:
            continue
        labels = set()
        count = 0
        for block_id in range(len(kernel.block_names)):
            span = kernel.inline_error_edge_span(block_id)
            for edge_id in range(span.first, span.first + span.second):
                target = kernel.block_names[kernel.inline_error_edge_target(edge_id)]
                labels.add(x86._block_label(function.name, target))
                labels.add(x86._block_label(function.name, target + ".edge." + str(edge_id)))
                count += 1
        edge_counts[function.name] = count
        edge_targets[function.name] = labels
    assert edge_counts.get("user_edges_f", 0) > 0 and edge_counts.get("user_edges_g", 0) > 0
    asm = x86._emit_x86_64_module("", windows="windows" in triple, module=module)
    for name in ("user_edges_f", "user_edges_g"):
        body = _function_body(asm, name)
        # Each edge is one conditional jump to its target or to its cold
        # payload stub; stubs store their payload and jump to the landing.
        edge_branches = 0
        for line in body:
            parts = line.split()
            if (len(parts) == 2 and parts[0].startswith("j") and parts[0] != "jmp"
                    and parts[1] in edge_targets[name]):
                edge_branches += 1
        assert edge_branches == edge_counts[name], (name, edge_branches, edge_counts[name])
        stubs = [index for index, line in enumerate(body) if ".edge." in line and line.endswith(":")]
        assert stubs, name
        for index in stubs:
            assert body[index + 1].startswith("  mov DWORD PTR [rbp - "), body[index:index + 3]
            assert body[index + 2].startswith("  jmp "), body[index:index + 3]


def test_shared_emitter_refuses_edges_it_cannot_emit(tmp_path, monkeypatch):
    module = _concrete_direct_indexed_target(
        _direct_module(tmp_path, monkeypatch), "x86_64-unknown-linux-gnu")
    monkeypatch.setattr(x86, "_emit_indexed_error_edge", None)
    with pytest.raises(BackendUnavailable, match="does not support inline error edges"):
        x86._emit_x86_64_module("", module=module)


@pytest.mark.parametrize("triple", ["x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
def test_real_module_object_matches_two_pass_assembler(tmp_path, monkeypatch, triple):
    from pcc.backend import coff_x86_64 as coff
    from pcc.backend import elf_x86_64 as elf
    from pcc.backend import x86_64_asm_driver as assembler
    from tests.python.test_x86_64_parse_buffer_publication import _two_pass

    module = _concrete_direct_indexed_target(_direct_module(tmp_path, monkeypatch), triple)
    windows = "windows" in triple
    asm = x86._emit_x86_64_module("", windows=windows, module=module)
    objects = []
    for driver in (assembler, _two_pass()):
        if windows:
            with monkeypatch.context() as context:
                context.setattr(assembler, "assemble_file_keeping_labels",
                                driver.assemble_file_keeping_labels)
                objects.append(coff.assemble_object(asm))
        else:
            objects.append(elf.emit_relocatable(driver.assemble_file(asm)))
    assert objects[0] == objects[1]
