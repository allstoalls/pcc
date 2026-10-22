"""Focused ownership contracts for self-backend link orchestration."""

from __future__ import annotations

import platform
import sys

import pytest

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_self_backend_link as self_link
from pcc.frontends.python import pipeline_self_link as link_contract


def _profiled_packed_native_image(tmp_path, monkeypatch, manifest_input, sync):
    import json
    from pcc.backend import macho_exec, macho_spec
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.macho_codesign import build_signature, parse_signature
    from pcc.backend.native_object import encode_native_object_from_sections

    sections, undefined = assemble_file(".section __TEXT,__text,regular,pure_instructions\n.globl _main\n_main:\n  movz x0, #42\n  ret\n")
    packed = tmp_path / "main.pco"
    packed.write_bytes(encode_native_object_from_sections(sections, undefined=undefined))
    output, profile = tmp_path / "program", tmp_path / "profile.json"
    manifest = tmp_path / "inputs.txt"
    manifest.write_text("pcc.macho-internal-inputs.v1\n1\nPCO\t" + str(packed) + "\n")

    def forbidden(*args, **kwargs):
        pytest.fail("profiled owned link attempted external delegation")

    prepare = macho_exec.prepare_executable_object
    transferred = []

    def checked_prepare(objects, **kwargs):
        assert kwargs.get("_consume_inputs") is True, "private CLI link kept the borrowed-input path"
        result = prepare(objects, **kwargs)
        assert objects == [], "transferred input list remained live after merge"
        transferred.append(True)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(macho_exec, "prepare_executable_object", checked_prepare)
        patch.setattr(self_link.subprocess, "run", forbidden)
        self_link.run_link_command(
            [], None, str(output), None, (), False,
            pcc_native_object_inputs=() if manifest_input else (str(packed),),
            pcc_internal_input_manifest=str(manifest) if manifest_input else None,
            link_profile_path=str(profile),
            target_triple="arm64-apple-darwin",
            resolve_self_link_mode=lambda **_kwargs: "pcc",
            validate_pcc_self_link_surface=lambda **kwargs: None,
            repo_root_for_link=forbidden, host_python_command=forbidden,
            build_pcc_link_command=forbidden, log=lambda *args: None,
            join_strings=lambda values, sep: sep.join(values),
        )
        final = tmp_path / "published"
        self_link.finish_executable(
            str(output), str(final), None, signature_owned_by_pcc=True,
            profile_begin=lambda profile: 0, profile_end=lambda *args: None,
            publish_sync_enabled=lambda: sync,
        )
    image = final.read_bytes()
    header = macho_spec.parse_object(image).header
    assert header["filetype"] == macho_spec.MH_EXECUTE
    assert header["cputype"] == macho_spec.CPU_TYPE_ARM64
    assert header["cpusubtype"] == macho_spec.CPU_SUBTYPE_ARM64_ALL
    signature = parse_signature(image)
    assert signature.identifier == b"pcc-linked"
    assert image[signature.dataoff:] == build_signature(
        image[:signature.dataoff], identifier=signature.identifier,
        exec_seg_base=signature.exec_seg_base,
        exec_seg_limit=signature.exec_seg_limit,
        exec_seg_flags=signature.exec_seg_flags,
    )
    assert transferred == [True]
    receipt = json.loads(profile.read_text())
    assert receipt["inputs"] == {"asm": 0, "native_object": 1, "object": 0, "archive": 0}
    assert receipt["phases_ms"]["prepare_link"] > 0
    assert receipt["phases_ms"]["sign"] > 0
    assert receipt["phases_ms"]["validate"] > 0
    pipeline._record_macho_link_profile({}, str(profile))
    return final


@pytest.mark.parametrize("manifest_input", [False, True])
@pytest.mark.parametrize("sync", [False, True])
def test_profiled_packed_native_link_publishes_owned_macho_without_a_host_interpreter(
    tmp_path, monkeypatch, manifest_input, sync,
):
    """Cross-target owned link/publication evidence; does not execute Mach-O."""
    _profiled_packed_native_image(tmp_path, monkeypatch, manifest_input, sync)


@pytest.mark.parametrize("manifest_input", [False, True])
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.pcc_gate(
    probe=lambda: sys.platform == "darwin" and platform.machine().lower() == "arm64",
)
def test_profiled_packed_native_link_executes_without_a_host_interpreter(
    tmp_path, monkeypatch, manifest_input, sync,
):
    """Retain real return-code verification on the matching execution host."""
    import subprocess

    final = _profiled_packed_native_image(tmp_path, monkeypatch, manifest_input, sync)
    result = subprocess.run([str(final)], capture_output=True, timeout=10)
    assert result.returncode == 42, result.stderr


def test_explicit_repo_root_owns_self_link_driver_resolution(monkeypatch, tmp_path):
    frozen = tmp_path / "frozen-source"
    frozen.mkdir()
    (frozen / "AGENTS.md").write_text("# frozen\n", encoding="utf-8")
    monkeypatch.setenv("PCC_REPO_ROOT", str(frozen))
    monkeypatch.setattr(pipeline, "__file__", "/live/repo/pcc/frontends/python/pipeline.py")
    assert pipeline._repo_root_for_link() == str(frozen)


def test_invalid_explicit_repo_root_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("PCC_REPO_ROOT", str(tmp_path))
    with pytest.raises(pipeline.PyPipelineError, match="complete pcc source root"):
        pipeline._repo_root_for_link()


def test_cc_link_owner_runs_the_exact_prepared_command(monkeypatch):
    calls = []

    def fake_run(command, *, check):
        calls.append((command, check))

    monkeypatch.setattr(self_link.subprocess, "run", fake_run)
    command = ["cc", "input.s", "-o", "program"]
    self_link.run_link_command(
        command,
        "input.s",
        "program",
        None,
        (),
        False,
        resolve_self_link_mode=lambda **_kwargs: "cc",
        validate_pcc_self_link_surface=lambda **_kwargs: None,
        repo_root_for_link=lambda: "/unused",
        host_python_command=lambda: "/unused/python3",
        build_pcc_link_command=lambda **_kwargs: [],
        log=lambda *_args: None,
        join_strings=lambda values, sep: sep.join(values),
    )
    assert calls == [(command, True)]


@pytest.mark.parametrize(
    ("platform", "mode"),
    [("darwin", "cc"), ("linux", "pcc")],
)
def test_semantic_policy_cannot_cross_cc_or_linux_link_boundary(
    monkeypatch,
    platform,
    mode,
):
    calls = []
    monkeypatch.setattr(self_link.sys, "platform", platform)
    monkeypatch.setattr(
        self_link.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(True),
    )

    with pytest.raises(
        self_link.SelfBackendLinkError,
        match="requires the pcc-owned Darwin linker",
    ):
        self_link.run_link_command(
            ["cc", "input.s", "-o", "program"],
            "input.s",
            "program",
            None,
            (),
            False,
            semantic_layout_policy="policy.json",
            resolve_self_link_mode=lambda **_kwargs: mode,
            validate_pcc_self_link_surface=lambda **_kwargs: None,
            repo_root_for_link=lambda: "/unused",
            host_python_command=lambda: "/unused/python3",
            build_pcc_link_command=lambda **_kwargs: [],
            log=lambda *_args: None,
            join_strings=lambda values, sep: sep.join(values),
        )

    assert calls == []


def test_facade_routes_ir_text_linking_to_the_owner(monkeypatch, tmp_path):
    observed = {}

    def fake_link_ir_texts(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs

    monkeypatch.setattr(self_link, "link_ir_texts", fake_link_ir_texts)
    pipeline._link_with_self_backend_ir_texts(
        ['target triple = "arm64-apple-darwin23.6.0"\n' "define i32 @main() { ret i32 0 }"],
        str(tmp_path / "program"),
        None,
        False,
        tmp_dir=str(tmp_path),
    )

    assert observed["args"][0] == ['target triple = "arm64-apple-darwin23.6.0"\n' "define i32 @main() { ret i32 0 }"]
    assert observed["kwargs"]["tmp_dir"] == str(tmp_path)
    assert observed["kwargs"]["link_run"] is pipeline._link_self_backend_ir_texts_run


def test_link_facade_has_no_second_file_path_owner():
    assert not hasattr(pipeline, "_link_with_self_backend_ir_paths")
    assert not hasattr(self_link, "link_ir_paths")


@pytest.mark.parametrize("consume", [False, True])
def test_owned_ir_is_released_after_emission_before_linking(monkeypatch, tmp_path, consume):
    first = 'target triple = "arm64-apple-darwin23.6.0"\n; first module'
    second = 'target triple = "arm64-apple-darwin23.6.0"\n; second module'
    texts = [first, second]
    normalized = []
    linked = []

    def emit(values, *_args, **_kwargs):
        assert values == texts == [first, second]
        normalized.append(values)
        return [("self-aarch64-darwin-v0", str(tmp_path / "module.s"))]

    def link(*_args, **_kwargs):
        expected = [] if consume else [first, second]
        assert texts == normalized[0] == expected
        linked.append(True)

    monkeypatch.setattr(pipeline, "_emit_self_objects_many_via_host_python", emit)
    monkeypatch.setattr(pipeline, "_run_self_link_command", link)
    monkeypatch.setattr(pipeline, "_finish_self_backend_executable", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "_macho_semantic_layout_enabled", lambda: False)
    monkeypatch.setattr(pipeline, "_self_backend_split_large_modules_enabled", lambda: False)
    pipeline._link_with_self_backend_ir_texts(
        texts, str(tmp_path / "program"), None, False,
        tmp_dir=str(tmp_path), consume_ir_texts=consume,
    )
    assert linked == [True]


def test_semantic_layout_rejects_split_module_before_emission(
    monkeypatch,
    tmp_path,
):
    emitted = []
    policies = []
    monkeypatch.setattr(self_link.sys, "platform", "darwin")

    with pytest.raises(
        self_link.SelfBackendLinkError,
        match="does not yet own split-module symbol renaming",
    ):
        self_link.link_ir_texts_run(
            ['target triple = "arm64-apple-darwin23.6.0"\n' "define i32 @main() { ret i32 0 }"],
            str(tmp_path / "program"),
            None,
            False,
            needs_libpython=False,
            tmp=str(tmp_path),
            profile=None,
            resolve_self_link_mode=lambda **_kwargs: "pcc",
            validate_pcc_self_link_surface=lambda **_kwargs: None,
            profile_begin=lambda _profile: 0,
            profile_end=lambda *_args: None,
            debug_dump_ir_texts=lambda _texts: None,
            split_large_modules_enabled=lambda: True,
            split_threshold_bytes=lambda: 1,
            emit_asm=lambda *_args: emitted.append("asm"),
            emit_objects=lambda *_args, **_kwargs: emitted.append("object"),
            runtime_archive_link_args=lambda *_args: [],
            native_extension_export_flags=lambda *_args: [],
            libpython_isolation_flags=lambda *_args: [],
            platform_link_flags=lambda: [],
            append_libpython_link_flags=lambda _cmd: None,
            log=lambda *_args: None,
            join_strings=lambda values, sep: sep.join(values),
            run_self_link_command=lambda *_args, **_kwargs: None,
            finish_self_backend_executable=lambda *_args, **_kwargs: None,
            semantic_layout_enabled=lambda: True,
            write_semantic_layout_policy=(
                lambda *_args: policies.append(True)
            ),
        )

    assert emitted == []
    assert policies == []


def test_pcc_link_selection_fails_before_silent_cc_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(self_link.subprocess, "run", lambda *a, **k: pytest.fail("external fallback"))
    try:
        self_link.run_link_command(
            ["cc", "input.s"],
            "input.s",
            str(tmp_path / "program"),
            None,
            (),
            False,
            target_triple="arm64-apple-darwin",
            resolve_self_link_mode=lambda **_kwargs: "pcc",
            validate_pcc_self_link_surface=lambda **_kwargs: None,
            repo_root_for_link=lambda: str(tmp_path),
            host_python_command=lambda: "/unused/python3",
            build_pcc_link_command=lambda **_kwargs: [],
            log=lambda *_args: None,
            join_strings=lambda values, sep: sep.join(values),
        )
    except self_link.SelfBackendLinkError as exc:
        assert "FileNotFoundError" in str(exc)
        assert "input.s" in str(exc)
    else:
        raise AssertionError("missing owned linker input must fail closed")


def test_linux_pcc_link_route_uses_owned_elf_driver_and_internal_assembly(
    monkeypatch, tmp_path
):
    # The Linux pcc route links in process through the owned ELF linker:
    # no host Python, no driver subprocess, internal assembly kept separate.
    from pcc.backend import owned_elf_link

    output = tmp_path / "program"
    calls = []

    def owned_link(**kwargs):
        calls.append(kwargs)
        output.write_bytes(b"ELF")
        output.chmod(0o755)

    def no_subprocess(command, **_kwargs):
        raise AssertionError("owned ELF link started " + repr(command))

    monkeypatch.setattr(self_link.sys, "platform", "linux")
    monkeypatch.setattr(self_link.sys, "executable", "/compiled/pcc1")
    monkeypatch.setattr(self_link.subprocess, "run", no_subprocess)
    monkeypatch.setattr(owned_elf_link, "link_inputs", owned_link)
    self_link.run_link_command(
        ["cc", "ignored"],
        None,
        str(output),
        "/runtime/libpcc.a",
        ("extra.o",),
        False,
        pcc_asm_inputs=("first.s", "second.s"),
        resolve_self_link_mode=lambda **_kwargs: "pcc",
        validate_pcc_self_link_surface=lambda **_kwargs: None,
        repo_root_for_link=lambda: str(tmp_path),
        host_python_command=lambda: (_ for _ in ()).throw(
            AssertionError("owned ELF link resolved a host Python")
        ),
        build_pcc_link_command=link_contract.build_pcc_link_command,
        log=lambda *_args: None,
        join_strings=lambda values, sep: sep.join(values),
    )
    assert len(calls) == 1
    assert calls[0]["assembly"] == ["first.s", "second.s"]
    assert calls[0]["objects"] == ["extra.o"]
    assert calls[0]["archives"] == ["/runtime/libpcc.a"]
    assert calls[0]["output"] == str(output)


def test_parallel_codegen_container_does_not_retain_prepass_ir(monkeypatch, tmp_path):
    """IR lifetime only; runtime admission, codegen and final linking are mocked."""
    import weakref
    class TextBatch(list):
        pass
    references = []
    calls = []
    def codegen(*_args, **_kwargs):
        batch = TextBatch([("entry", 'target triple = "arm64-apple-darwin23.6.0"\n' "define i32 @main() { ret i32 0 }")])
        references.append(weakref.ref(batch))
        return batch, False, False, 38, []
    def link(texts, *_args, **kwargs):
        assert references[0]() is None, "parallel result still owns pre-pass IR"
        assert len(texts) == 1
        assert kwargs["consume_ir_texts"] is True
        calls.append("link")
    source = tmp_path / "entry.py"
    source.write_text("print(1)\n")
    runtime = tmp_path / "runtime.a"
    runtime.touch()

    def admit_runtime(path, **_options):
        assert path == str(runtime)
        calls.append("runtime")
        return path

    def forbidden(*_args, **_kwargs):
        pytest.fail("IR lifetime control attempted runtime provisioning or an external tool")

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_SELF_BACKEND_SKIP_LL_TEMP", "1")
    monkeypatch.setattr(pipeline, "_explicit_runtime_archive", admit_runtime)
    monkeypatch.setattr(pipeline, "_ensure_runtime", forbidden)
    monkeypatch.setattr(pipeline.subprocess, "run", forbidden)
    monkeypatch.setattr(pipeline, "_compile_python_multi_codegen_parallel", codegen)
    monkeypatch.setattr(pipeline, "_link_with_self_backend_ir_texts", link)
    pipeline.compile_python_multi([str(source)], str(tmp_path / "output"),
        module_names=["entry"], backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(runtime))
    assert calls == ["runtime", "link"]
