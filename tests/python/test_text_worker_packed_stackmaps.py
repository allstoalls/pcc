"""The text-worker boundary packs owned Linux x86 metadata without projection."""

import json
import platform
import subprocess
import sys

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import elf_x86_64 as elf
from pcc.backend import self_backend_precise_stackmaps as maps
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend import target_objects
from pcc.backend.code_profile import code_profile_identity
from pcc.backend.owned_elf_link import assemble
from pcc.backend.precise_stackmap import ARCH_X86_64, decode_stack_map
from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_self_backend_emit as worker
from tests.python.test_x86_64_emission_lifetime import (
    PROGRAM, ROOTED_PROGRAM, STARTUP, TARGET,
)


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


@pytest.fixture(autouse=True)
def _fixed_configuration(monkeypatch):
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")


def _legacy_worker(ir_path, result_path, obj_path):
    # No packed callback is the pre-optimization text-worker implementation.
    return worker.run_emit_worker(
        str(ir_path), str(result_path), str(obj_path),
        normalize_ir=pipeline._self_backend_ir_text,
        emit_native=pipeline._emit_self_asm_in_process,
    )


def _object_pair(tmp_path, ir, monkeypatch):
    source = tmp_path / "input.ll"
    source.write_text(ir)
    legacy = tmp_path / "legacy.pco"
    result = tmp_path / "legacy.result"
    assert _legacy_worker(source, result, legacy) == 0
    assert result.read_text() == "self-x86_64-linux-v0\n" + str(legacy)
    packed = tmp_path / "packed.pco"
    result = tmp_path / "packed.result"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("text PCO worker projected symbolic stack maps")

    with monkeypatch.context() as guard:
        guard.setattr(emitter, "render_x86_64_stack_map_section", forbidden)
        guard.setattr(maps, "_stack_locations", forbidden)
        assert pipeline.run_self_backend_emit_worker(
            str(source), str(result), str(packed),
        ) == 0
    assert result.read_text() == "self-x86_64-linux-v0\n" + str(packed)
    assert packed.read_bytes() == legacy.read_bytes()
    return legacy.read_bytes(), packed.read_bytes()


@pytest.mark.parametrize("ir", [
    ROOTED_PROGRAM, PROGRAM,
    f'target triple = "{TARGET}"\n@only_data = global i64 42\n',
    PROGRAM.replace(TARGET, "amd64-linux-musl"),
], ids=["rooted", "phi-and-calls", "data-only", "linux-alias"])
def test_text_pco_matches_legacy_bytes_relocations_and_stackmaps(tmp_path, monkeypatch, ir):
    legacy, packed = _object_pair(tmp_path, ir, monkeypatch)
    old_object = elf.parse_relocatable(legacy)
    new_object = elf.parse_relocatable(packed)
    assert new_object == old_object
    for section in new_object.sections:
        if section.name != ".pcc_stackmaps":
            continue
        decoded = decode_stack_map(section.data, expected_arch=ARCH_X86_64)
        assert len(section.relocations) == len(decoded.functions)
        assert all(rel.type == elf.R_X86_64_64 for rel in section.relocations)
        if ir == ROOTED_PROGRAM:
            assert any(record.locations for function in decoded.functions
                       for record in function.records)


@pytest.mark.parametrize("header", ["concrete", "missing", "placeholder"])
def test_text_pco_preserves_normalized_ir_profile_identity(tmp_path, monkeypatch, header):
    ir = PROGRAM
    if header == "missing":
        ir = ir.split("\n", 1)[1]
    elif header == "placeholder":
        ir = ir.replace(TARGET, "unknown-unknown-unknown")
    monkeypatch.setattr(pipeline, "_host_target_triple_for_self_backend", lambda: TARGET)
    normalized = pipeline._self_backend_ir_text(ir)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "schema": "pcc.code-profile.v1",
        "source_identity": "text-worker-fixture",
        "code_identity": code_profile_identity(normalized),
        "semantic_mode": "pcc-native", "runtime_abi": "test-owned-abi",
        "target": TARGET,
        "decision": {"kind": "function-order", "function_samples": [
            {"symbol": "main", "count": 100},
            {"symbol": "add_two", "count": 50},
        ]},
    }))
    monkeypatch.setenv("PCC_CODE_PROFILE", str(profile))
    monkeypatch.setenv("PCC_CODE_PROFILE_SOURCE_IDENTITY", "text-worker-fixture")
    monkeypatch.setenv("PCC_CODE_PROFILE_SEMANTIC_MODE", "pcc-native")
    monkeypatch.setenv("PCC_CODE_PROFILE_RUNTIME_ABI", "test-owned-abi")
    observed = []
    apply_profile = emitter.apply_function_order_profile

    def observe(functions, *, ir_text, target):
        assert ir_text == normalized
        ordered, decision = apply_profile(functions, ir_text=ir_text, target=target)
        observed.append((decision.status, [function.name for function in ordered]))
        return ordered, decision

    monkeypatch.setattr(emitter, "apply_function_order_profile", observe)
    _object_pair(tmp_path, ir, monkeypatch)
    assert observed == [("matched", ["main", "add_two", "pick"])] * 2


@pytest.mark.parametrize("stage", ["success", "emit", "pack"])
def test_text_pco_owns_and_releases_plans_on_every_exit(tmp_path, monkeypatch, capsys, stage):
    source = tmp_path / "input.ll"
    source.write_text(ROOTED_PROGRAM)
    output, result = tmp_path / "output.pco", tmp_path / "output.result"
    owners = []
    emit = emitter._emit_x86_64_module
    encode = target_objects.encode_assembly_object

    def observe_emit(ir_text, *, stack_map_plans_out):
        owners.append(stack_map_plans_out)
        if stage == "emit":
            stack_map_plans_out.append(object())
            raise BackendUnavailable("injected emit failure")
        return emit(ir_text, stack_map_plans_out=stack_map_plans_out)

    def observe_encode(assembly, target, *, stack_map_plans, consume_stack_map_plans):
        assert stack_map_plans is owners[0] and stack_map_plans
        assert consume_stack_map_plans is True
        if stage == "pack":
            raise BackendUnavailable("injected pack failure")
        data = encode(assembly, target, stack_map_plans=stack_map_plans,
                      consume_stack_map_plans=consume_stack_map_plans)
        assert not stack_map_plans
        return data

    def forbidden(*_args, **_kwargs):
        raise AssertionError("packed failure silently fell back to text")

    monkeypatch.setattr(emitter, "_emit_x86_64_module", observe_emit)
    monkeypatch.setattr(target_objects, "encode_assembly_object", observe_encode)
    monkeypatch.setattr(pipeline, "_emit_self_asm_in_process", forbidden)
    status = pipeline.run_self_backend_emit_worker(str(source), str(result), str(output))
    assert owners == [[]]
    assert status == (0 if stage == "success" else 1)
    assert output.exists() == result.exists() == (stage == "success")
    if stage != "success":
        assert "injected " + stage + " failure" in capsys.readouterr().err


@pytest.mark.parametrize("failed_output", ["object", "result"])
def test_text_pco_releases_plans_before_publication_failure(
    tmp_path, monkeypatch, capsys, failed_output,
):
    source = tmp_path / "input.ll"
    source.write_text(PROGRAM)
    output, result = tmp_path / "output.pco", tmp_path / "output.result"
    if failed_output == "object":
        output = tmp_path / "missing" / "output.pco"
    else:
        result = tmp_path / "missing" / "output.result"
    owners = []
    emit = emitter._emit_x86_64_module

    def observe(ir_text, *, stack_map_plans_out):
        owners.append(stack_map_plans_out)
        return emit(ir_text, stack_map_plans_out=stack_map_plans_out)

    monkeypatch.setattr(emitter, "_emit_x86_64_module", observe)
    assert pipeline.run_self_backend_emit_worker(str(source), str(result), str(output)) == 1
    assert owners == [[]]
    assert not result.exists()
    assert "self backend emit worker failed:" in capsys.readouterr().err


def test_native_cc_is_rejected_before_reading_or_emitting(tmp_path, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native cc rejection occurred after emission")

    monkeypatch.setattr(worker.sys.implementation, "name", "pcc")
    monkeypatch.setattr(pipeline, "_emit_self_packed_object_in_process", forbidden)
    monkeypatch.setattr(pipeline, "_emit_self_asm_in_process", forbidden)
    assert pipeline.run_self_backend_emit_worker(
        str(tmp_path / "missing.ll"), str(tmp_path / "result"),
        str(tmp_path / "object.pco"), "cc",
    ) == 1
    assert "native self emission cannot invoke an external assembler" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_unsupported_target_keeps_fail_closed_contract(tmp_path, capsys):
    source = tmp_path / "unsupported.ll"
    source.write_text('target triple = "riscv64-unknown-linux-gnu"\n')
    output, result = tmp_path / "output.pco", tmp_path / "output.result"
    assert pipeline.run_self_backend_emit_worker(str(source), str(result), str(output)) == 1
    assert "native emitter does not support the module target" in capsys.readouterr().err
    assert not output.exists() and not result.exists()


@pytest.mark.parametrize("target", [
    "aarch64-unknown-linux-gnu", "arm64-apple-darwin", "x86_64-pc-windows-msvc",
])
def test_non_x86_linux_pco_keeps_original_emitter(tmp_path, monkeypatch, target):
    source = tmp_path / "input.ll"
    source.write_text(f'target triple = "{target}"\ndefine i32 @answer() {{\n'
                      'entry:\n ret i32 42\n}\n')
    old, new = tmp_path / "old.pco", tmp_path / "new.pco"
    assert _legacy_worker(source, tmp_path / "old.result", old) == 0
    calls = []
    emit = pipeline._emit_self_asm_in_process

    def observe(ir):
        calls.append(ir)
        return emit(ir)

    monkeypatch.setattr(pipeline, "_emit_self_asm_in_process", observe)
    assert pipeline.run_self_backend_emit_worker(
        str(source), str(tmp_path / "new.result"), str(new),
    ) == 0
    assert calls == [source.read_text()]
    assert old.read_bytes() == new.read_bytes()


@pytest.mark.parametrize("kind", ["result-asm", "file-asm", "external-assembler"])
def test_asm_and_cc_do_not_select_packed_callback(tmp_path, monkeypatch, kind):
    source = tmp_path / "input.ll"
    source.write_text(PROGRAM)
    result = tmp_path / "result"
    output = "" if kind == "result-asm" else str(tmp_path / "output.pco")
    if kind == "file-asm":
        output = str(tmp_path / "output.s")
    cc = "explicit-assembler" if kind == "external-assembler" else ""
    commands = []

    def forbidden(_ir):
        raise AssertionError("ASM or explicit cc selected packed object callback")

    def fake_run(command, *, check):
        assert check is True
        commands.append(command)

    monkeypatch.setattr(pipeline, "_emit_self_packed_object_in_process", forbidden)
    monkeypatch.setattr(pipeline, "_emit_self_asm_in_process",
                        lambda _ir: ("self-x86_64-linux-v0", "symbolic assembly"))
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert pipeline.run_self_backend_emit_worker(str(source), str(result), output, cc) == 0
    assert result.read_text() == "self-x86_64-linux-v0\n" + (output or "symbolic assembly")
    assert commands == ([[cc, "-c", str(result) + ".s", "-o", output]] if cc else [])
    if output:
        assembly_path = tmp_path / ("result.s" if cc else "output.s")
        assert assembly_path.read_text() == "symbolic assembly"


def test_batch_text_workers_get_independent_plan_owners(tmp_path, monkeypatch):
    owners = []
    emit = emitter._emit_x86_64_module

    def observe(ir_text, *, stack_map_plans_out):
        assert all(not owner for owner in owners)
        assert all(stack_map_plans_out is not owner for owner in owners)
        owners.append(stack_map_plans_out)
        return emit(ir_text, stack_map_plans_out=stack_map_plans_out)

    monkeypatch.setattr(emitter, "_emit_x86_64_module", observe)
    lines = ["pcc.self_backend.emit_batch.v1"]
    outputs = []
    for index, ir in enumerate((PROGRAM, ROOTED_PROGRAM)):
        source = tmp_path / f"{index}.ll"
        source.write_text(ir)
        output = tmp_path / f"{index}.pco"
        outputs.append(output)
        lines.extend((str(source), str(tmp_path / f"{index}.result"), str(output), ""))
    manifest = tmp_path / "batch.manifest"
    manifest.write_text("\n".join(lines) + "\n")
    assert pipeline.run_self_backend_emit_batch_worker(str(manifest)) == 0
    assert len(owners) == 2 and all(not owner for owner in owners)
    assert all(output.read_bytes().startswith(b"\x7fELF") for output in outputs)


@pytest.mark.pcc_gate(
    probe=lambda: sys.platform.startswith("linux")
    and platform.machine() in ("x86_64", "amd64"),
)
def test_text_worker_objects_link_and_execute_identical_owned_elf(tmp_path, monkeypatch):
    def forbidden_process(*_args, **_kwargs):
        raise AssertionError("owned text-worker emission/link invoked an external process")

    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", forbidden_process)
        old, new = _object_pair(tmp_path, PROGRAM, monkeypatch)
        startup = assemble(STARTUP, TARGET)
        images = [elf.link_static_executable([elf.parse_relocatable(data), startup])
                  for data in (old, new)]
        assert images[0] == images[1]
    for name, image in zip(("legacy", "packed"), images):
        binary = tmp_path / name
        binary.write_bytes(image)
        binary.chmod(0o755)
        completed = subprocess.run([str(binary)], capture_output=True, timeout=5)
        assert completed.returncode == 42, completed.stderr
        assert completed.stdout == completed.stderr == b""
