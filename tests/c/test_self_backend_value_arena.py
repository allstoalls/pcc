from __future__ import annotations

from pathlib import Path

import pytest

from pcc.backend.self_backend_value_arena import CompilerIntArena


def test_compiler_int_arena_host_oracle_grows_reads_writes_and_closes() -> None:
    arena = CompilerIntArena(1)
    assert not arena.uses_native_storage
    for value in (7, -1, 1 << 40, 9):
        arena.append(value)
    arena.set(1, -17)
    arena.set_unchecked(3, 27)

    assert len(arena) == 4
    assert arena.get_unchecked(3) == 27
    assert arena.diagnostic_values() == [7, -17, 1 << 40, 27]
    with pytest.raises(IndexError):
        arena.get(-1)
    with pytest.raises(IndexError):
        arena.set(4, 0)

    arena.close()
    arena.close()
    with pytest.raises(RuntimeError, match="closed"):
        arena.append(1)

    records = CompilerIntArena(1)
    records.append2(1, 2)
    records.append3(3, 4, 5)
    records.append4(6, 7, 8, 9)
    records.append_zeros(2)
    records.set2_unchecked(9, 21, 22)
    records.set3_unchecked(1, 12, 13, 14)
    pair = records.get2_unchecked(0)
    triple = records.get3_unchecked(0)
    quad = records.get4_unchecked(0)
    assert (pair.first, pair.second) == (1, 12)
    assert (triple.first, triple.second, triple.third) == (1, 12, 13)
    assert (quad.first, quad.second, quad.third, quad.fourth) == (1, 12, 13, 14)
    assert records.diagnostic_values()[-2:] == [21, 22]
    records.close()

    ordered = CompilerIntArena(1)
    for value in (7, -4, 7, 0, 1 << 40, -9, 3):
        ordered.append(value)
    ordered.sort()
    assert ordered.diagnostic_values() == [-9, -4, 0, 3, 7, 7, 1 << 40]
    ordered.clear()
    ordered.sort()
    assert ordered.diagnostic_values() == []
    ordered.close()

    source = CompilerIntArena()
    target = CompilerIntArena()
    for value in (1, 2, 4, 8):
        source.append(value)
        target.append(0)
    target.copy_prefix_from_unchecked(source, 1, 3)
    assert target.diagnostic_values() == [2, 4, 8, 0]
    target.or_prefix_from_unchecked(source, 0, 3)
    assert target.diagnostic_values() == [3, 6, 12, 0]
    target.zero_prefix_unchecked(2)
    assert target.diagnostic_values() == [0, 0, 12, 0]

    uses = CompilerIntArena()
    definitions = CompilerIntArena()
    live_out = CompilerIntArena()
    live_in = CompilerIntArena()
    scratch = CompilerIntArena()
    for value in (1, 2):
        uses.append(value)
    for value in (4, 1):
        definitions.append(value)
    for _index in range(2):
        live_out.append(0)
        live_in.append(0)
    scratch.append(5)
    scratch.append(3)
    assert scratch.converge_liveness_row_unchecked(
        uses,
        definitions,
        live_out,
        live_in,
        0,
        2,
        (1 << 30) - 1,
    )
    assert live_out.diagnostic_values() == [5, 3]
    assert live_in.diagnostic_values() == [1, 2]
    assert not scratch.converge_liveness_row_unchecked(
        uses,
        definitions,
        live_out,
        live_in,
        0,
        2,
        (1 << 30) - 1,
    )
    for arena in (source, target, uses, definitions, live_out, live_in, scratch):
        arena.close()


def test_compiler_int_arena_self_backend_lowers_native_payload(tmp_path) -> None:
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.pipeline import compile_python

    arena_source = Path(__file__).parents[2] / "pcc" / "backend" / (
        "self_backend_value_arena.py"
    )
    llvm_path = tmp_path / "self_backend_value_arena.ll"
    compile_python(
        str(arena_source),
        str(llvm_path),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
        target_triple="arm64-apple-darwin23.6.0",
        python_library=True,
        recursive_stdlib=False,
    )
    ir_text = llvm_path.read_text(encoding="utf-8")

    assert "call ptr (i64) @malloc" in ir_text
    assert "call ptr (ptr, i64) @realloc" in ir_text
    assert "call void (ptr) @free" in ir_text
    assert "unsafe.load.i64" in ir_text
    assert "unsafe.ptr.to.int" in ir_text
    assert "unsafe.int.to.ptr" in ir_text
    assert "CompilerIntArena_get_unchecked" in ir_text
    assert "CompilerIntArena_set_unchecked" in ir_text
    assert "CompilerIntArena_set3_unchecked" in ir_text
    assert "CompilerIntArena_set2_unchecked" in ir_text
    assert "CompilerIntArena_append_zeros" in ir_text
    assert "CompilerIntArena_append4" in ir_text
    assert "CompilerIntArena_get4_unchecked" in ir_text
    get4_start = ir_text.index("CompilerIntArena_get4_unchecked")
    get4_end = ir_text.index("\n}", get4_start)
    get4_body = ir_text[get4_start:get4_end]
    assert "py_valuebox_new" not in get4_body

    assembly = emit_self_asm(ir_text)
    assert "_user_pcc_backend_self_backend_value_arena_CompilerIntArena_append:" in assembly
    assert "bl _malloc" in assembly
    assert "bl _realloc" in assembly
    assert "bl _free" in assembly
