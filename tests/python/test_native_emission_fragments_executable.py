"""Host-built strict native prerequisite; this does not qualify a pcc1 worker."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import pytest


_MODULES = (
    "pcc.backend.self_backend_aarch64_fragments",
    "pcc.backend.self_backend_value_arena",
    "pcc.backend.self_backend_aarch64_darwin_regs",
    "pcc.backend.self_backend_aarch64_darwin_slots",
    "pcc.backend.self_backend_aarch64_darwin_mem",
    "pcc.backend.arm64_encode",
    "pcc.backend.aarch64_fp_immediates",
    "pcc.backend.macho_spec",
    "pcc.backend.macho_obj",
    "pcc.backend.self_backend_ir",
    "pcc.unsafe",
)


_CANARY = '''
from pcc.backend.self_backend_aarch64_fragments import AArch64EmissionFragments
from pcc.backend.self_backend_aarch64_darwin_regs import append_add_offset
from pcc.backend.self_backend_aarch64_darwin_slots import append_load_slot_to_reg_parts, append_store_reg_to_slot_parts
from pcc.backend.self_backend_value_arena import CompilerInt2, CompilerInt4

def replay(owner: AArch64EmissionFragments) -> None:
    record_id = owner.next_record_id()
    while record_id >= 0:
        record: CompilerInt4 = owner.records.get4_unchecked(record_id)
        if record.second == -1:
            print(owner.symbol_names[record.first])
        else:
            print(record.first)
        record_id = owner.next_record_id()

def run() -> None:
    owner = AArch64EmissionFragments()
    try:
        independent = AArch64EmissionFragments()
        try:
            assert owner.records.uses_native_storage
            assert owner.cursor.uses_native_storage
            assert owner.spans.nodes.uses_native_storage
            assert owner.spans.spans.uses_native_storage
            assert independent.records.uses_native_storage
            first: CompilerInt2 = owner.new_fragment()
            suffix: CompilerInt2 = owner.new_fragment()
            isolated: CompilerInt2 = independent.new_fragment()
            owner.append_label(first, "reload")
            append_load_slot_to_reg_parts(owner, first, 65536, False, 64, "x16")
            append_add_offset(owner, first, "x16", "x16", -305419896)
            append_store_reg_to_slot_parts(owner, first, "x16", 65544, False, 64)
            owner.append_label(suffix, "exit")
            owner.append_nop(suffix)
            owner.extend_fragment(first, suffix)
            owner.append_move(suffix, "x0", "x1")
            owner.start_cursor(first)
            owner.append_nop(first)
            independent.append_label(isolated, "isolated")
            independent.append_nop(isolated)
            replay(owner)
            independent.start_cursor(isolated)
            replay(independent)
            assert owner.spans.projection_count == 0
            assert independent.spans.projection_count == 0
            print("native-fragments-ok")
        finally:
            independent.close()
    finally:
        owner.close()

run()
'''.lstrip()


# native fragment canary requires an explicit immutable PCC_RUNTIME_ARCHIVE
@pytest.mark.pcc_gate(env="PCC_RUNTIME_ARCHIVE")
def test_host_built_native_fragments_execute_reload_helpers(tmp_path):
    from pcc.backend.arm64_encode import assemble_text
    from pcc.backend.self_backend_aarch64_darwin_regs import emit_add_offset
    from pcc.backend.self_backend_aarch64_darwin_slots import (
        load_slot_to_reg_parts,
        store_reg_to_slot_parts,
    )
    from pcc.backend.self_backend_ir import TypeDesc
    from pcc.frontends.python.pipeline import compile_python_multi

    archive_name = os.environ.get("PCC_RUNTIME_ARCHIVE")
    archive = Path(archive_name).resolve()
    assert archive.is_file()
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    repo = Path(__file__).resolve().parents[2]
    sources = []
    for name in _MODULES:
        path = repo / (name.replace(".", "/") + ".py")
        if name == "pcc.unsafe":
            path = repo / "pcc/unsafe/__init__.py"
        assert path.is_file(), path
        sources.append(str(path))

    pointer = TypeDesc("ptr", pointee=TypeDesc("void"))
    lines = load_slot_to_reg_parts(65536, pointer, "x16")
    lines.extend(emit_add_offset("x16", "x16", -305419896))
    lines.extend(store_reg_to_slot_parts("x16", 65544, pointer))
    code = assemble_text("\n".join(lines)).code
    expected = "reload\n" + "".join(
        str(int.from_bytes(code[index:index + 4], "little")) + "\n"
        for index in range(0, len(code), 4)
    ) + "exit\n3573751839\nisolated\n3573751839\nnative-fragments-ok\n"

    consumer = tmp_path / "fragment_canary.py"
    consumer.write_text(_CANARY, encoding="utf-8")
    output = tmp_path / "fragment_canary"
    (tmp_path / "fragment_canary.expected").write_text(expected, encoding="utf-8")
    compile_python_multi(
        [str(consumer), *sources], str(output),
        entry_module="pcc.backend.fragment_canary",
        module_names=["pcc.backend.fragment_canary", *_MODULES],
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
        recursive_stdlib=False, target_triple="arm64-apple-darwin23.6.0",
        runtime_archive=str(archive),
    )
    result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
    (tmp_path / "fragment_canary.stdout").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "fragment_canary.stderr").write_text(result.stderr, encoding="utf-8")
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_hash
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected
