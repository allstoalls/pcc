from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcc.py_frontend import pipeline


def _load_pcc_link_driver():
    path = Path(__file__).resolve().parents[2] / "scripts" / "pcc_link_macho.py"
    spec = importlib.util.spec_from_file_location("_pcc_link_macho_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_internal_input_manifest_preserves_mixed_module_order(tmp_path: Path) -> None:
    driver = _load_pcc_link_driver()
    manifest = tmp_path / "inputs.txt"
    manifest.write_text(
        "pcc.macho-internal-inputs.v1\n"
        "3\n"
        "PCO\t/tmp/first.pco\n"
        "ASM\t/tmp/second.s\n"
        "PCO\t/tmp/third.pco\n",
        encoding="utf-8",
    )

    assert driver._read_internal_input_manifest(str(manifest)) == [
        ("PCO", "/tmp/first.pco"),
        ("ASM", "/tmp/second.s"),
        ("PCO", "/tmp/third.pco"),
    ]


def test_direct_artifact_link_writes_ordered_mixed_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asm = tmp_path / "large.s"
    pco = tmp_path / "small.pco"
    asm.write_text(".text\n", encoding="utf-8")
    pco.write_bytes(b"pco")
    captured = []

    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    monkeypatch.setattr(pipeline, "_macho_semantic_layout_enabled", lambda: False)
    monkeypatch.setattr(pipeline, "_resolve_self_link_mode", lambda: "pcc")
    monkeypatch.setattr(
        pipeline,
        "_validate_pcc_self_link_surface",
        lambda **_kwargs: None,
    )

    def fake_link(*_args, **kwargs):
        manifest = Path(kwargs["pcc_internal_input_manifest"])
        captured.extend(manifest.read_text(encoding="utf-8").splitlines())

    monkeypatch.setattr(pipeline, "_run_self_link_command", fake_link)
    monkeypatch.setattr(
        pipeline,
        "_finish_self_backend_executable",
        lambda *_args, **_kwargs: None,
    )

    pipeline._link_with_self_backend_direct_artifacts(
        [
            ("first", "PCO", str(pco)),
            ("second", "ASM", str(asm)),
        ],
        str(tmp_path / "program"),
        None,
        False,
    )

    assert captured == [
        "pcc.macho-internal-inputs.v1",
        "2",
        "PCO\t" + str(pco.resolve()),
        "ASM\t" + str(asm.resolve()),
    ]


def test_direct_artifact_link_can_defer_after_compiled_coordinator_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asm = tmp_path / "large.s"
    pco = tmp_path / "small.pco"
    runtime = tmp_path / "runtime.a"
    asm.write_text(".text\n", encoding="utf-8")
    pco.write_bytes(b"pco")
    runtime.write_bytes(b"archive")
    plan = tmp_path / "deferred.plan"

    monkeypatch.setenv("PCC_DEFER_SELF_LINK_PLAN", str(plan))
    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    monkeypatch.setattr(pipeline, "_macho_semantic_layout_enabled", lambda: False)
    monkeypatch.setattr(pipeline, "_resolve_self_link_mode", lambda: "pcc")
    monkeypatch.setattr(
        pipeline,
        "_validate_pcc_self_link_surface",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_self_link_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("compiled coordinator must exit before deferred link")
        ),
    )

    pipeline._link_with_self_backend_direct_artifacts(
        [
            ("first", "PCO", str(pco)),
            ("second", "ASM", str(asm)),
        ],
        str(tmp_path / "program"),
        str(runtime),
        False,
    )

    lines = plan.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "pcc.deferred-self-link.v1"
    assert lines[1] == str((tmp_path / "program").resolve())
    assert lines[2] == str(runtime.resolve())
    internal_manifest = Path(lines[3])
    assert internal_manifest.is_file()
    assert internal_manifest.read_text(encoding="utf-8").splitlines()[2:] == [
        "PCO\t" + str(pco.resolve()),
        "ASM\t" + str(asm.resolve()),
    ]


def test_self_link_mode_uses_host_default_and_accepts_explicit_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "_default_self_link_mode", lambda: "pcc")
    monkeypatch.delenv("PCC_SELF_LINK", raising=False)
    assert pipeline._resolve_self_link_mode() == "pcc"
    monkeypatch.setenv("PCC_SELF_LINK", " cc ")
    assert pipeline._resolve_self_link_mode() == "cc"
    monkeypatch.setenv("PCC_SELF_LINK", " PCC ")
    assert pipeline._resolve_self_link_mode() == "pcc"


@pytest.mark.parametrize(
    ("host_platform", "machine", "expected"),
    [
        ("darwin", "arm64", "pcc"),
        ("darwin", "aarch64", "pcc"),
        ("darwin", "x86_64", "cc"),
        ("linux", "arm64", "cc"),
    ],
)
def test_default_self_link_mode_is_pcc_only_on_darwin_arm64(
    monkeypatch: pytest.MonkeyPatch,
    host_platform: str,
    machine: str,
    expected: str,
) -> None:
    monkeypatch.setattr(pipeline.sys, "platform", host_platform)
    monkeypatch.setattr(
        pipeline.os,
        "uname",
        lambda: SimpleNamespace(machine=machine),
    )
    assert pipeline._default_self_link_mode() == expected


@pytest.mark.parametrize("failure", [OSError("uname failed"), None])
def test_darwin_default_architecture_probe_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError | None,
) -> None:
    def broken_uname():
        if failure is not None:
            raise failure
        return SimpleNamespace(machine="")

    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    monkeypatch.setattr(pipeline.os, "uname", broken_uname)
    monkeypatch.delenv("PCC_SELF_LINK", raising=False)

    with pytest.raises(
        pipeline.PyPipelineError,
        match="cannot identify the Darwin host architecture",
    ):
        pipeline._resolve_self_link_mode()

    # An explicit selector is authoritative and does not need host probing.
    monkeypatch.setenv("PCC_SELF_LINK", "cc")
    assert pipeline._resolve_self_link_mode() == "cc"
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    assert pipeline._resolve_self_link_mode() == "pcc"


def test_darwin_arm64_default_routes_through_the_pcc_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    output = tmp_path / "output"

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs == {"check": True}
        produced = Path(command[command.index("--out") + 1])
        produced.write_bytes(b"pcc-link-output")
        produced.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    monkeypatch.setattr(
        pipeline.os,
        "uname",
        lambda: SimpleNamespace(machine="arm64"),
    )
    monkeypatch.delenv("PCC_SELF_LINK", raising=False)
    monkeypatch.setenv("PCC_HOST_PYTHON", "/host/python")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    pipeline._run_self_link_command(
        ["cc", "input.s", "-o", str(output)],
        "input.s",
        str(output),
        None,
        (),
        False,
    )

    assert len(calls) == 1
    assert calls[0][0] == "/host/python"
    assert calls[0][1].endswith("/scripts/pcc_link_macho.py")
    assert calls[0][0] != "cc"


def test_frontend_semantic_layout_policy_is_explicit_and_reaches_owned_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    output = tmp_path / "output"
    policy = tmp_path / "semantic-policy.json"
    policy.write_text("{}\n", encoding="utf-8")

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs == {"check": True}
        output.write_bytes(b"owned-link")
        output.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    monkeypatch.setenv("PCC_HOST_PYTHON", "/host/python")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    pipeline._run_self_link_command(
        ["cc", "input.s", "-o", str(output)],
        "input.s",
        str(output),
        None,
        (),
        False,
        semantic_layout_policy=str(policy),
    )

    command = calls[0]
    assert command[command.index("--semantic-layout-policy") + 1] == str(policy)


def test_frontend_semantic_layout_policy_records_linkage_attributes_and_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    policy_path = tmp_path / "policy.json"
    ir_text = '''
target triple = "arm64-apple-darwin23.6.0"
define i32 @main() #0 { ret i32 0 }
define i32 @public_root() { ret i32 1 }
define internal i32 @private_cold() #1 { ret i32 2 }
attributes #0 = { hot }
attributes #1 = { cold }
'''.strip()
    monkeypatch.setenv("PCC_MACHO_SEMANTIC_ROOTS", "public_root")

    pipeline._write_macho_semantic_layout_policy(
        str(policy_path), [ir_text]
    )
    payload = json.loads(policy_path.read_text(encoding="utf-8"))

    assert payload["schema"] == "pcc.frontend-macho-semantic-layout.v1"
    assert payload["entry"] == "_main"
    assert payload["roots"] == ["_public_root"]
    functions = {item["symbol"]: item for item in payload["functions"]}
    assert functions["_main"] == {
        "eliminable": False,
        "symbol": "_main",
        "temperature": "hot",
    }
    private = next(
        item for item in payload["functions"] if item["symbol"].endswith("private_cold")
    )
    assert private["eliminable"] is True
    assert private["temperature"] == "cold"


def test_frontend_semantic_layout_mode_is_opt_in_and_darwin_pcc_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    monkeypatch.delenv("PCC_MACHO_SEMANTIC_LAYOUT", raising=False)
    assert pipeline._macho_semantic_layout_enabled() is False

    monkeypatch.setenv("PCC_MACHO_SEMANTIC_LAYOUT", "on")
    assert pipeline._macho_semantic_layout_enabled() is True

    monkeypatch.setattr(pipeline.sys, "platform", "linux")
    with pytest.raises(
        pipeline.PyPipelineError,
        match="requires the pcc-owned Darwin Mach-O linker",
    ):
        pipeline._macho_semantic_layout_enabled()


@pytest.mark.parametrize("value", ["pc", "ld", "fallback", "1"])
def test_self_link_mode_rejects_unknown_values_before_running_a_linker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("PCC_SELF_LINK", value)
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(list(command)),
    )

    with pytest.raises(
        pipeline.PyPipelineError,
        match="invalid PCC_SELF_LINK value",
    ):
        pipeline._run_self_link_command(
            ["cc", "input.s", "-o", str(tmp_path / "output")],
            "input.s",
            str(tmp_path / "output"),
            None,
            (),
            False,
        )

    assert calls == []
    assert not (tmp_path / "output").exists()


def test_pcc_self_link_rejects_unimplemented_link_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pcc-owned linker must never discard public link options."""

    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    asm = tmp_path / "input.s"
    output = tmp_path / "output"
    asm.write_text("", encoding="utf-8")

    with pytest.raises(
        pipeline.PyPipelineError,
        match="pcc self-link mode does not support link arguments",
    ):
        pipeline._run_self_link_command(
            ["cc", str(asm), "-o", str(output), "-Wl,-map,map.txt"],
            str(asm),
            str(output),
            None,
            (),
            False,
            extra_link_args=("-Wl,-map,map.txt",),
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("ir_texts", "options", "message"),
    [
        (
            ["define i64 @single() { ret i64 1 }"],
            {"extra_link_args": ("-Wl,-map,map.txt",)},
            "link arguments",
        ),
        (
            [
                "define i64 @first() { ret i64 1 }",
                "define i64 @second() { ret i64 2 }",
            ],
            {"needs_libpython": True},
            "libpython link surface",
        ),
        (
            ["define i64 @single() { ret i64 1 }"],
            {"needs_native_extension_exports": True},
            "native-extension export anchors",
        ),
    ],
)
def test_pcc_self_link_rejects_unsupported_surface_before_emission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ir_texts: list[str],
    options: dict[str, object],
    message: str,
) -> None:
    """Both input shapes fail before temporary asm/object publication."""

    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    emission_calls: list[str] = []

    def unexpected_emit(*_args, **_kwargs):
        emission_calls.append("emit")
        raise AssertionError("unsupported self-link mode reached object emission")

    monkeypatch.setattr(
        pipeline,
        "_emit_self_asm_via_host_python",
        unexpected_emit,
    )
    monkeypatch.setattr(
        pipeline,
        "_emit_self_objects_many_via_host_python",
        unexpected_emit,
    )
    output = tmp_path / "output"
    with pytest.raises(pipeline.PyPipelineError, match=message):
        pipeline._link_self_backend_ir_texts_run(
            ir_texts,
            str(output),
            None,
            False,
            needs_libpython=bool(options.get("needs_libpython", False)),
            needs_native_extension_exports=bool(
                options.get("needs_native_extension_exports", False)
            ),
            extra_link_args=tuple(options.get("extra_link_args", ())),
            tmp=str(tmp_path),
            profile=None,
        )

    assert emission_calls == []
    assert not output.exists()
    assert not Path(str(output) + ".tmp").exists()


def test_pcc_self_link_accepts_object_only_multi_module_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The object-emission branch must reach the pcc linker subprocess too."""

    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    first = tmp_path / "first.o"
    second = tmp_path / "second.o"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs == {"check": True}
        produced = Path(command[command.index("--out") + 1])
        produced.write_bytes(b"pcc-link-output")
        produced.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    pipeline._run_self_link_command(
        ["cc", str(first), str(second), "-o", str(tmp_path / "output")],
        None,
        str(tmp_path / "output"),
        None,
        (str(first), str(second)),
        False,
    )

    assert len(calls) == 1
    command = calls[0]
    assert "--asm" not in command
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--object"
    ] == [str(first), str(second)]


def test_pcc_self_link_accepts_indexed_internal_asm_inputs_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    first = tmp_path / "first.s"
    second = tmp_path / "second.s"
    external = tmp_path / "external.o"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs == {"check": True}
        produced = Path(command[command.index("--out") + 1])
        produced.write_bytes(b"pcc-link-output")
        produced.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    pipeline._run_self_link_command(
        ["cc", str(first), str(second), str(external), "-o", str(tmp_path / "out")],
        None,
        str(tmp_path / "out"),
        None,
        (str(external),),
        False,
        pcc_asm_inputs=(str(first), str(second)),
    )

    command = calls[0]
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--asm"
    ] == [str(first), str(second)]
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--object"
    ] == [str(external)]


def test_pcc_self_link_passes_the_stable_output_as_incremental_patch_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    calls: list[list[str]] = []
    final_output = tmp_path / "compiler"
    temporary_output = Path(str(final_output) + ".tmp")

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs == {"check": True}
        temporary_output.write_bytes(b"pcc-link-output")
        temporary_output.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    pipeline._run_self_link_command(
        ["cc", "input.s", "-o", str(temporary_output)],
        "input.s",
        str(temporary_output),
        None,
        (),
        False,
    )

    command = calls[0]
    assert command[command.index("--previous-output") + 1] == str(final_output)


def test_pcc_multi_module_emit_keeps_internal_inputs_as_assembly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir_text = (
        'target triple = "arm64-apple-darwin23.6.0"\n'
        "define i32 @main() { ret i32 0 }\n"
    )
    emitted_asm = ".section __TEXT,__text,regular,pure_instructions\n"
    profile = {}

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("internal object path invoked cc/as")

    monkeypatch.setattr(
        pipeline,
        "_python_frontend_worker_executable",
        lambda: "",
    )
    monkeypatch.setattr(
        pipeline,
        "_source_self_backend_emit_workers_worthwhile",
        lambda _inputs: False,
    )
    monkeypatch.setattr(
        pipeline,
        "_emit_self_asm_in_process",
        lambda _text: ("self-aarch64-darwin-v0", emitted_asm),
    )
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        unexpected_run,
    )

    pairs = pipeline._emit_self_objects_many_in_process(
        [ir_text, ir_text],
        str(tmp_path),
        "cc",
        split_large_modules=False,
        profile=profile,
        internal_link=True,
    )

    assert pairs is not None
    assert [target for target, _path in pairs] == [
        "self-aarch64-darwin-v0",
        "self-aarch64-darwin-v0",
    ]
    paths = [Path(path) for _target, path in pairs]
    assert all(path.suffix == ".s" for path in paths)
    assert [path.read_text(encoding="utf-8") for path in paths] == [
        emitted_asm,
        emitted_asm,
    ]
    assert profile["counters"][
        "link_self_native_object_fastpath_inputs"
    ] == 2


def test_pcc_self_link_rejects_success_without_an_executable_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )

    with pytest.raises(
        pipeline.PyPipelineError,
        match="returned success without an executable regular output file",
    ):
        pipeline._run_self_link_command(
            ["cc", "input.s", "-o", str(tmp_path / "output")],
            "input.s",
            str(tmp_path / "output"),
            None,
            (),
            False,
        )


def test_large_multi_module_branch_uses_shared_self_link_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.o"
    second = tmp_path / "second.o"
    seen: list[tuple[object, ...]] = []
    # This test exercises cc-only link arguments; keep the oracle explicit now
    # that an unset selector owns the Darwin arm64 pcc route.
    monkeypatch.setenv("PCC_SELF_LINK", "cc")

    monkeypatch.setattr(
        pipeline,
        "_emit_self_objects_many_via_host_python",
        lambda *_args, **_kwargs: [
            ("self-aarch64-darwin-v0", str(first)),
            ("self-aarch64-darwin-v0", str(second)),
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "_run_self_link_command",
        lambda *args, **kwargs: seen.append((*args, kwargs)),
    )
    monkeypatch.setattr(
        pipeline,
        "_finish_self_backend_executable",
        lambda *_args, **_kwargs: None,
    )

    pipeline._link_self_backend_ir_texts_run(
        [
            "define i64 @first() { ret i64 1 }",
            "define i64 @second() { ret i64 2 }",
        ],
        str(tmp_path / "output"),
        None,
        False,
        needs_libpython=False,
        extra_link_args=("-Wl,-map,map.txt",),
        tmp=str(tmp_path),
        profile=None,
    )

    assert len(seen) == 1
    args = seen[0]
    assert args[1] is None
    assert args[4] == ()
    assert args[-1] == {
        "extra_link_args": ("-Wl,-map,map.txt",),
        "needs_libpython": False,
            "needs_native_extension_exports": False,
            "pcc_asm_inputs": (),
            "semantic_layout_policy": None,
        }


def test_pcc_mode_marks_multi_object_output_as_already_signed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.s"
    second = tmp_path / "second.s"
    finish_calls: list[dict[str, object]] = []
    emit_calls: list[dict[str, object]] = []
    link_calls: list[tuple[object, ...]] = []

    monkeypatch.setenv("PCC_SELF_LINK", "pcc")

    def fake_emit(*_args, **kwargs):
        emit_calls.append(dict(kwargs))
        return [
            ("self-aarch64-darwin-v0", str(first)),
            ("self-aarch64-darwin-v0", str(second)),
        ]

    monkeypatch.setattr(
        pipeline,
        "_emit_self_objects_many_via_host_python",
        fake_emit,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_self_link_command",
        lambda *args, **kwargs: link_calls.append((*args, kwargs)),
    )
    monkeypatch.setattr(
        pipeline,
        "_finish_self_backend_executable",
        lambda *_args, **kwargs: finish_calls.append(dict(kwargs)),
    )

    pipeline._link_self_backend_ir_texts_run(
        [
            "define i64 @first() { ret i64 1 }",
            "define i64 @second() { ret i64 2 }",
        ],
        str(tmp_path / "output"),
        None,
        False,
        needs_libpython=False,
        tmp=str(tmp_path),
        profile=None,
    )

    assert finish_calls == [{"signature_owned_by_pcc": True}]
    assert emit_calls[0]["internal_link"] is True
    assert link_calls[0][-1]["pcc_asm_inputs"] == (str(first), str(second))


def test_linux_pcc_link_routes_internal_assembly_without_system_assembler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.s"
    second = tmp_path / "second.s"
    emit_calls: list[dict[str, object]] = []
    link_calls: list[tuple[object, ...]] = []

    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    monkeypatch.setattr(pipeline.sys, "platform", "linux")

    def fake_emit(*_args, **kwargs):
        emit_calls.append(dict(kwargs))
        return [
            ("self-x86_64-linux-v0", str(first)),
            ("self-x86_64-linux-v0", str(second)),
        ]

    monkeypatch.setattr(
        pipeline,
        "_emit_self_objects_many_via_host_python",
        fake_emit,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_self_link_command",
        lambda *args, **kwargs: link_calls.append((*args, kwargs)),
    )
    monkeypatch.setattr(
        pipeline,
        "_finish_self_backend_executable",
        lambda *_args, **_kwargs: None,
    )

    pipeline._link_self_backend_ir_texts_run(
        [
            'target triple = "x86_64-unknown-linux-gnu"\n'
            "define i64 @first() { ret i64 1 }",
            'target triple = "x86_64-unknown-linux-gnu"\n'
            "define i64 @second() { ret i64 2 }",
        ],
        str(tmp_path / "output"),
        None,
        False,
        needs_libpython=False,
        tmp=str(tmp_path),
        profile=None,
    )

    assert emit_calls == [{
        "split_large_modules": False,
        "profile": None,
        "internal_link": True,
    }]
    assert link_calls[0][-1]["pcc_asm_inputs"] == (str(first), str(second))


def test_pcc_owned_signature_is_published_without_external_codesign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        assert kwargs == {"check": True}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    monkeypatch.setenv("PCC_SELF_BACKEND_PUBLISH_SYNC", "0")
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    (tmp_path / "output.tmp").write_bytes(b"owned signature bytes")

    pipeline._finish_self_backend_executable(
        str(tmp_path / "output.tmp"),
        str(tmp_path / "output"),
        None,
        signature_owned_by_pcc=True,
    )

    assert commands == []
    assert (tmp_path / "output").read_bytes() == b"owned signature bytes"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"needs_libpython": True}, "libpython link surface"),
        (
            {"needs_native_extension_exports": True},
            "native-extension export anchors",
        ),
    ],
)
def test_pcc_self_link_rejects_unimplemented_implied_link_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, bool],
    message: str,
) -> None:
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    with pytest.raises(pipeline.PyPipelineError, match=message):
        pipeline._run_self_link_command(
            ["cc", "input.s", "-o", str(tmp_path / "output")],
            "input.s",
            str(tmp_path / "output"),
            None,
            (),
            False,
            **options,
        )


def test_pcc_link_driver_rejects_an_empty_input_set(tmp_path: Path) -> None:
    driver = Path(__file__).resolve().parents[2] / "scripts" / "pcc_link_macho.py"
    result = subprocess.run(
        [sys.executable, str(driver), "--out", str(tmp_path / "output")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert (
        "at least one --asm, --native-object, or --object input is required"
        in result.stderr
    )


def test_pcc_link_driver_keeps_assembly_internal_until_final_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.backend import arm64_asm_driver, macho_codesign, macho_exec
    from pcc.backend.macho_obj import TEXT_SECTION_FLAGS, Section, TextSymbol
    from pcc.backend.native_object import PackedNativeObject

    driver = _load_pcc_link_driver()
    first = tmp_path / "first.s"
    second = tmp_path / "second.s"
    external = tmp_path / "external.o"
    output = tmp_path / "output"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    external.write_bytes(b"external-macho")
    linked_inputs: list[object] = []

    def fake_assemble(text: str):
        return [Section(
            sectname="__text",
            segname="__TEXT",
            data=b"\xc0\x03\x5f\xd6",
            align_log2=2,
            flags=TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_" + text, 0),),
        )], []

    def fake_link(objects, **kwargs):
        linked_inputs.extend(objects)
        assert kwargs["identifier"] == b"pcc-linked"
        return b"signed-image"

    monkeypatch.setattr(arm64_asm_driver, "assemble_file", fake_assemble)
    monkeypatch.setattr(macho_exec, "link_executable", fake_link)
    monkeypatch.setattr(
        macho_codesign,
        "parse_signature",
        lambda image: SimpleNamespace(
            identifier=b"pcc-linked",
            dataoff=len(image),
            datasize=0,
            exec_seg_base=0,
            exec_seg_limit=0,
            exec_seg_flags=1,
        ),
    )
    monkeypatch.setattr(
        macho_codesign,
        "build_signature",
        lambda *_args, **_kwargs: b"",
    )

    assert driver.main([
        "--asm",
        str(first),
        "--asm",
        str(second),
        "--object",
        str(external),
        "--out",
        str(output),
    ]) == 0

    assert all(
        isinstance(value, PackedNativeObject) for value in linked_inputs[:2]
    )
    assert linked_inputs[2:] == [b"external-macho"]
    assert output.read_bytes() == b"signed-image"


def test_pcc_link_driver_does_not_accept_native_codec_as_object(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pcc.backend.macho_obj import TEXT_SECTION_FLAGS, Section, TextSymbol
    from pcc.backend.native_object import NativeObject, encode_native_object

    driver = _load_pcc_link_driver()
    mislabeled = tmp_path / "mislabeled.o"
    output = tmp_path / "output"
    native = NativeObject.from_sections([Section(
        sectname="__text",
        segname="__TEXT",
        data=b"\xc0\x03\x5f\xd6",
        align_log2=2,
        flags=TEXT_SECTION_FLAGS,
        symbols=(TextSymbol("_main", 0),),
    )])
    mislabeled.write_bytes(encode_native_object(native))

    with pytest.raises(SystemExit, match="2"):
        driver.main([
            "--object",
            str(mislabeled),
            "--out",
            str(output),
        ])

    assert "pcc-native input requires --native-object" in capsys.readouterr().err
    assert not output.exists()


def test_pcc_link_driver_accepts_explicit_native_object_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from pcc.backend import macho_codesign, macho_exec
    from pcc.backend.macho_obj import TEXT_SECTION_FLAGS, Section, TextSymbol
    from pcc.backend.native_object import (
        NativeObject,
        PackedNativeObject,
        encode_native_object,
    )

    driver = _load_pcc_link_driver()
    native_path = tmp_path / "input.pco"
    output = tmp_path / "output"
    profile = tmp_path / "link-profile.json"
    native = NativeObject.from_sections([Section(
        sectname="__text",
        segname="__TEXT",
        data=b"\xc0\x03\x5f\xd6",
        align_log2=2,
        flags=TEXT_SECTION_FLAGS,
        symbols=(TextSymbol("_main", 0),),
    )])
    native_path.write_bytes(encode_native_object(native))
    linked_inputs: list[object] = []

    def fake_link(objects, **kwargs):
        linked_inputs.extend(objects)
        assert kwargs["identifier"] == b"pcc-linked"
        return b"signed-image"

    monkeypatch.setattr(macho_exec, "link_executable", fake_link)
    monkeypatch.setattr(
        macho_codesign,
        "parse_signature",
        lambda image: SimpleNamespace(
            identifier=b"pcc-linked",
            dataoff=len(image),
            datasize=0,
            exec_seg_base=0,
            exec_seg_limit=0,
            exec_seg_flags=1,
        ),
    )
    monkeypatch.setattr(
        macho_codesign,
        "build_signature",
        lambda *_args, **_kwargs: b"",
    )

    assert driver.main([
        "--native-object",
        str(native_path),
        "--out",
        str(output),
        "--profile-json",
        str(profile),
    ]) == 0

    assert len(linked_inputs) == 1
    assert isinstance(linked_inputs[0], PackedNativeObject)
    assert linked_inputs[0].encoded == encode_native_object(native)
    assert output.read_bytes() == b"signed-image"
    payload = json.loads(profile.read_text(encoding="utf-8"))
    assert payload["schema"] == "pcc.macho-link-profile.v1"
    assert set(payload["phases_ms"]) == {
        "assemble_pool",
        "decode_pco",
        "prepare_link",
        "sign",
        "validate",
        "write",
    }
    assert payload["inputs"] == {
        "archive": 0,
        "asm": 0,
        "native_object": 1,
        "object": 0,
    }
    assert "PCC_LINK_PROFILE " in capsys.readouterr().err


def test_pcc_link_driver_materializes_frontend_semantic_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from pcc.backend import macho_codesign, macho_exec, macho_spec
    from pcc.backend.native_object import (
        NativeObject,
        NativeSection,
        NativeSymbol,
        encode_native_object,
    )

    driver = _load_pcc_link_driver()
    native_path = tmp_path / "input.pco"
    policy_path = tmp_path / "semantic-policy.json"
    output = tmp_path / "output"
    native = NativeObject(
        (
            NativeSection(
                "__TEXT",
                "__text",
                macho_spec.S_REGULAR | macho_spec.S_ATTR_PURE_INSTRUCTIONS,
                2,
                b"M" * 4 + b"D" * 4,
            ),
        ),
        (
            NativeSymbol("_dead", 1, 4, False),
            NativeSymbol("_main", 1, 0, True),
        ),
    )
    native_path.write_bytes(encode_native_object(native))
    policy_path.write_text(
        json.dumps(
            {
                "entry": "_main",
                "functions": [
                    {
                        "eliminable": True,
                        "symbol": "_dead",
                        "temperature": "cold",
                    },
                    {
                        "eliminable": False,
                        "symbol": "_main",
                        "temperature": "hot",
                    },
                ],
                "roots": [],
                "schema": "pcc.frontend-macho-semantic-layout.v1",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    prepared: list[NativeObject] = []

    def fake_finalize(merged, **kwargs):
        prepared.append(merged)
        assert kwargs["entry"] == "_main"
        assert kwargs["identifier"] == b"pcc-linked"
        return b"signed-image"

    monkeypatch.setenv("PCC_MACHO_INCREMENTAL_LINK_CACHE", "off")
    monkeypatch.setattr(macho_exec, "link_prepared_executable", fake_finalize)
    monkeypatch.setattr(
        macho_codesign,
        "parse_signature",
        lambda image: SimpleNamespace(
            identifier=b"pcc-linked",
            dataoff=len(image),
            datasize=0,
            exec_seg_base=0,
            exec_seg_limit=0,
            exec_seg_flags=1,
        ),
    )
    monkeypatch.setattr(
        macho_codesign,
        "build_signature",
        lambda *_args, **_kwargs: b"",
    )

    assert driver.main([
        "--native-object",
        str(native_path),
        "--semantic-layout-policy",
        str(policy_path),
        "--out",
        str(output),
    ]) == 0

    assert len(prepared) == 1
    assert prepared[0].sections[0].data == b"M" * 4
    assert [symbol.name for symbol in prepared[0].symbols] == ["_main"]
    assert output.read_bytes() == b"signed-image"


def test_pcc_link_driver_refuses_to_overwrite_an_input(tmp_path: Path) -> None:
    driver = Path(__file__).resolve().parents[2] / "scripts" / "pcc_link_macho.py"
    object_path = tmp_path / "input.o"
    object_path.write_bytes(b"must-survive")
    result = subprocess.run(
        [
            sys.executable,
            str(driver),
            "--object",
            str(object_path),
            "--out",
            str(object_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "--out must not overwrite an input file" in result.stderr
    assert object_path.read_bytes() == b"must-survive"


def test_pcc_link_driver_does_not_replace_output_with_a_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_pcc_link_driver()
    output = tmp_path / "output"
    output.write_bytes(b"previous-complete-output")

    def fail_replace(_source, _destination):
        raise OSError("injected publication failure")

    monkeypatch.setattr(driver.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected publication failure"):
        driver._publish_executable(output, b"new-complete-output")

    assert output.read_bytes() == b"previous-complete-output"
    assert list(tmp_path.glob("output.pcc-link-*.tmp")) == []


def test_pcc_link_driver_patches_a_prior_artifact_to_exact_target_bytes(
    tmp_path: Path,
) -> None:
    driver = _load_pcc_link_driver()
    chunk = driver._PATCH_CHUNK_SIZE
    previous = tmp_path / "previous"
    output = tmp_path / "output.tmp"
    old_image = b"A" * (chunk * 3)
    new_image = b"A" * chunk + b"B" * chunk + b"A" * chunk
    previous.write_bytes(old_image)

    changed_chunks, patched_bytes = driver._publish_executable(
        output,
        new_image,
        previous,
    )

    assert output.read_bytes() == new_image
    assert previous.read_bytes() == old_image
    assert changed_chunks == 1
    assert patched_bytes == chunk
    assert output.stat().st_mode & 0o111


def test_pcc_link_driver_publishes_a_fresh_non_mmap_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_pcc_link_driver()
    from pcc.backend import macho_parallel

    output = tmp_path / "output"
    image = b"complete-signed-image"
    mmap_inode: int | None = None
    original_write = macho_parallel.write_mmap_output

    def record_mmap_inode(file, size, regions, *, jobs=None):
        nonlocal mmap_inode
        mmap_inode = Path(file.name).stat().st_ino
        return original_write(file, size, regions, jobs=jobs)

    monkeypatch.setattr(macho_parallel, "write_mmap_output", record_mmap_inode)
    driver._publish_executable(output, image)

    assert output.read_bytes() == image
    assert mmap_inode is not None
    assert output.stat().st_ino != mmap_inode
    assert list(tmp_path.glob("output.pcc-link-*.tmp")) == []
    assert list(tmp_path.glob("output.pcc-publish-*.tmp")) == []
