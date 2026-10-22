"""Owned platform routing and qualification evidence contracts.

These tests author host-side object/link probes only; target execution belongs
to the native platform integration gates.
"""

from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_self_backend_emit as emit
from scripts import bootstrap_platform, pcc_link_elf


def _forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected external route or side effect")


@pytest.mark.parametrize("target", ["riscv64-unknown-linux-gnu", "aarch64-pc-windows-msvc"])
def test_owned_link_drivers_reject_unknown_target_before_reading_inputs(tmp_path, target):
    from pcc.backend.owned_link_driver import main
    from pcc.backend.owned_elf_link import link_inputs
    from pcc.backend.elf_x86_64 import ElfError
    output = tmp_path / "image"
    with pytest.raises(ValueError, match="unsupported owned linker target"):
        main(["link", "--target", target, "--object", "missing.o", "--out", str(output)])
    with pytest.raises(ElfError, match="unsupported owned ELF target"):
        link_inputs(target=target, output=str(output), objects=["missing.o"])
    assert not output.exists()


@pytest.mark.parametrize("target,machine,assembly", [
    ("x86_64-unknown-linux-gnu", 62,
     ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n  ret\n"),
    ("aarch64-unknown-linux-gnu", 183,
     ".section __TEXT,__text,regular,pure_instructions\n.globl _start\n_start:\n  ret\n"),
])
def test_elf_wrapper_links_selected_target(tmp_path, target, machine, assembly):
    from pcc.backend.elf_x86_64 import parse_static_executable

    source = tmp_path / "input.s"
    source.write_text(assembly, encoding="utf-8")
    output = tmp_path / "program"
    assert pcc_link_elf.main([
        "--target", target, "--asm", str(source), "--out", str(output),
    ]) == 0
    data = output.read_bytes()
    assert struct.unpack_from("<H", data, 18)[0] == machine
    assert parse_static_executable(data)["entry"] > 0
    assert source.read_text(encoding="utf-8") == assembly
    assert not list(tmp_path.glob(".pcc-elf-*"))


def test_elf_wrapper_preserves_historical_default(tmp_path, monkeypatch):
    from pcc.backend import owned_elf_link

    seen = []
    link = owned_elf_link.link_inputs

    def capture(**kwargs):
        seen.append(kwargs["target"])
        return link(**kwargs)

    monkeypatch.setattr(owned_elf_link, "link_inputs", capture)
    source = tmp_path / "entry.s"
    source.write_text(".intel_syntax noprefix\n.text\n.globl _start\n_start:\n ret\n")
    pcc_link_elf.main(["--asm", str(source), "--out", str(tmp_path / "image")])
    assert seen == ["x86_64-unknown-linux-gnu"]


def _link_creation_supported(kind):
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source"
        source.write_bytes(b"")
        target = Path(directory) / "target"
        try:
            if kind == "hardlink":
                target.hardlink_to(source)
            else:
                target.symlink_to(source)
        except OSError:
            return False
    return True


# A filesystem that does not permit a link kind deselects that case.
@pytest.mark.parametrize("kind", [
    "direct",
    pytest.param("hardlink", marks=pytest.mark.pcc_gate(probe=lambda: _link_creation_supported("hardlink"))),
    pytest.param("symlink", marks=pytest.mark.pcc_gate(probe=lambda: _link_creation_supported("symlink"))),
    "previous",
])
def test_elf_wrapper_rejects_input_alias_before_link(tmp_path, monkeypatch, kind):
    from pcc.backend import owned_elf_link

    monkeypatch.setattr(owned_elf_link, "link_inputs", _forbidden)
    source = tmp_path / "input.o"
    source.write_bytes(b"preserve this input")
    output = source
    if kind in ("hardlink", "symlink"):
        output = tmp_path / "output"
        if kind == "hardlink":
            output.hardlink_to(source)
        else:
            output.symlink_to(source)
    arguments = ["--object", str(source), "--out", str(output)]
    if kind == "previous":
        output = tmp_path / "previous"
        output.write_bytes(b"previous program")
        arguments = ["--object", str(source), "--out", str(output),
                     "--previous-output", str(output)]
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        pcc_link_elf.main(arguments)
    assert exc.value.code == 2
    assert output.read_bytes() == before
    assert source.read_bytes() == b"preserve this input"


def test_native_batch_rejects_host_python_fallback_before_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pcc")))
    with pytest.raises(emit.SelfBackendEmitError, match="cannot invoke host Python"):
        emit.emit_objects_many_via_host_python(
            ["unsupported module"], str(tmp_path), "", emit_in_process_many=lambda *_a, **_k: None,
            profile_begin=_forbidden, profile_end=_forbidden,
            split_threshold_bytes=_forbidden, jobs_for_count=_forbidden,
            host_python_command=_forbidden, host_many_code="",
            pcc_source_root=_forbidden, small_int_decimal=_forbidden,
            profile_counter=_forbidden,
        )
    assert list(tmp_path.iterdir()) == []


def _batch_options():
    return dict(
        split_large_modules=False, profile=None, internal_link=True,
        parse_target_triple=lambda value: value,
        host_target_triple=lambda: "x86_64-unknown-linux-gnu",
        target_supported=lambda _triple: True,
        native_worker_executable=_forbidden, split_large_ir_modules=_forbidden,
        source_workers_worthwhile=_forbidden,
        worker_command_prefix_for_frontend=_forbidden,
        split_threshold_bytes=_forbidden, split_shard_bytes=_forbidden,
        jobs_for_ir_texts=_forbidden, profile_counter=lambda *_a: None,
        profiled_gc_collect=lambda *_a, **_k: None,
        profile_begin=lambda *_a: 0, profile_end=lambda *_a: None,
        run_worker_commands=_forbidden, small_int_decimal=str,
        shell_quote_arg=_forbidden, split_worker_arg="--split-worker",
        plan_cache=_forbidden, jobs=_forbidden, jobs_for_input_sizes=_forbidden,
        jobs_env="PCC_SELF_BACKEND_JOBS", run_emit_worker_pool=_forbidden,
        publish_cache=_forbidden, maintain_cache=_forbidden,
        emit_in_process=_forbidden, join_strings=lambda values, sep: sep.join(values),
    )


def test_batch_rejects_mixed_targets_before_workers_or_cache(tmp_path):
    with pytest.raises(emit.SelfBackendEmitError, match="different target platforms"):
        emit.emit_objects_many_in_process(
            ["x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"],
            str(tmp_path), "", **_batch_options(),
        )
    assert list(tmp_path.iterdir()) == []


def test_native_batch_without_worker_stays_in_process(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pcc")))
    options = _batch_options()
    options.update(native_worker_executable=lambda: "",
                   emit_in_process=lambda _text: ("self-x86_64-linux-v0", "owned assembly\n"))
    result = emit.emit_objects_many_in_process(
        ["x86_64-unknown-linux-gnu"], str(tmp_path), "", **options,
    )
    assert result[0][0] == "self-x86_64-linux-v0"
    assert Path(result[0][1]).read_text() == "owned assembly\n"


def test_native_batch_rejects_external_assembler_before_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pcc")))
    options = _batch_options()
    options["internal_link"] = False
    with pytest.raises(emit.SelfBackendEmitError, match="requires owned object emission"):
        emit.emit_objects_many_in_process(
            ["x86_64-unknown-linux-gnu"], str(tmp_path), "/usr/bin/cc", **options,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("relative", [
    "pcc/backend/owned_elf_link.py", "pcc/frontends/c/codegen/c_codegen.py",
    "pcc/runtime/py/freestanding_linux_threads.py", "pcc/runtime/Makefile",
    "scripts/bootstrap_platform.py", "scripts/platform_process_watchdog.py",
    "utils/fake_libc_include/_fake_typedefs.h",
])
def test_qualification_source_identity_binds_full_input_closure(tmp_path, monkeypatch, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"before")
    monkeypatch.setattr(bootstrap_platform, "ROOT", tmp_path)

    def git_inventory(command, **kwargs):
        assert command[:2] == ["git", "ls-files"]
        assert "--cached" in command and "--others" in command
        assert {"pcc", "scripts", "utils"}.issubset(command)
        assert kwargs["cwd"] == tmp_path
        return relative.encode() + b"\0"

    monkeypatch.setattr(bootstrap_platform.subprocess, "check_output", git_inventory)
    first = bootstrap_platform._source_snapshot()
    target.write_bytes(b"after")
    second = bootstrap_platform._source_snapshot()
    assert first["source_file_count"] == second["source_file_count"] == 1
    assert first["source_sha256"] != second["source_sha256"]
    target.unlink()
    assert bootstrap_platform._source_snapshot()["source_sha256"] not in (
        first["source_sha256"], second["source_sha256"],
    )


def test_elf_dependency_receipt_does_not_claim_static_source_ownership(tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable
    from pcc.backend.x86_64_asm_driver import assemble_file

    output = tmp_path / "program"
    output.write_bytes(link_static_executable([assemble_file(
        ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n ret\n",
    )]))
    receipt = bootstrap_platform.dependency_receipt(output)
    assert receipt["no_dynamic_dependencies"] is True
    assert "static_zero_libc" not in receipt
    assert "statically linked source ownership" in receipt["proof_scope"]
