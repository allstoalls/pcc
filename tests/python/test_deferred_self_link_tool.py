from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "run_pcc_deferred_link.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("pcc_deferred_link_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plan(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime.a"
    inputs = tmp_path / "inputs.txt"
    runtime.write_bytes(b"archive")
    inputs.write_text(
        "pcc.macho-internal-inputs.v1\n1\nPCO\t/tmp/module.pco\n",
        encoding="utf-8",
    )
    plan = tmp_path / "link.plan"
    plan.write_text(
        "pcc.deferred-self-link.v1\n"
        + str(tmp_path / "program")
        + "\n"
        + str(runtime)
        + "\n"
        + str(inputs)
        + "\n"
        + str(tmp_path / "profile.json")
        + "\n"
        + str(tmp_path / "artifacts")
        + "\n0\n",
        encoding="utf-8",
    )
    return plan


def test_deferred_link_runs_owned_driver_and_persists_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    plan = _plan(tmp_path)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        assert kwargs == {"check": False, "timeout": 45}
        output = Path(command[command.index("--out") + 1])
        output.write_bytes(b"executable")
        output.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    result = tool.run(plan, timeout_s=45)

    assert result["status"] == "COMPLETE"
    assert commands[0][1].endswith("pcc_link_macho.py")
    assert commands[0][commands[0].index("--internal-input-manifest") + 1].endswith(
        "inputs.txt"
    )
    receipt = json.loads(
        Path(str(plan) + ".result.json").read_text(encoding="utf-8")
    )
    assert receipt["output"] == str(tmp_path / "program")
    assert (tmp_path / "artifacts").exists() is False


def test_deferred_link_plan_rejects_relative_or_missing_inputs(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    plan = tmp_path / "bad.plan"
    plan.write_text(
        "pcc.deferred-self-link.v1\nrelative-output\n\nmissing\n"
        + str(tmp_path / "profile.json")
        + "\n\n0\n",
        encoding="utf-8",
    )

    with pytest.raises(tool.DeferredLinkError, match="absolute path"):
        tool.read_plan(plan)


def test_deferred_link_cli_stops_before_plan_when_compiler_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    plan = tmp_path / "not-produced.plan"
    calls = []

    def fail(command, **kwargs):
        calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(command, 17)

    monkeypatch.setattr(tool.subprocess, "run", fail)

    assert tool.main([str(plan), "--", "pcc1", "source.py"]) == 17
    assert calls == [(["pcc1", "source.py"], {"check": False})]


@pytest.mark.parametrize("native", [False, True], ids=["host-linker", "native-linker"])
def test_frontend_codegen_plan_runs_worker_then_ordered_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
) -> None:
    tool = _load_tool()
    worker = tmp_path / "pcc1"
    worker.write_bytes(b"worker")
    worker.chmod(0o755)
    native_linker = tmp_path / "native-linker"
    native_linker.write_bytes(b"\xcf\xfa\xed\xfe" + b"test native linker")
    native_linker.chmod(0o755)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"archive")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    ast_dir = tmp_path / "ast"
    ast_dir.mkdir()
    manifests = []
    result_paths = []
    for index, name in enumerate(("entry", "leaf")):
        (ast_dir / ("module_" + str(index) + ".json")).write_text(
            "{}", encoding="utf-8"
        )
        source = tmp_path / (name + ".py")
        source.write_text("print(42)\n", encoding="utf-8")
        result_path = tmp_path / ("worker_" + str(index) + ".tsv")
        manifest = tmp_path / ("worker_" + str(index) + ".manifest")
        manifest.write_text(
            "pcc.frontends.python.codegen_worker.v4\n"
            + str(result_path)
            + "\n"
            + str(artifacts)
            + "\nexports\ncodegen\n"
            + str(ast_dir)
            + "\nentry\noff\non\n0\n0\n1\n0\t"
            + name
            + "\t"
            + str(source)
            + "\n1\n0\n",
            encoding="utf-8",
        )
        manifests.append(manifest)
        result_paths.append(result_path)
    output = tmp_path / "program"
    inputs = tmp_path / "internal-inputs"
    profile = tmp_path / "profile.json"
    plan = tmp_path / "codegen.plan"
    plan.write_text(
        "pcc.frontend-codegen-plan.v2\n"
        + str(worker)
        + "\n"
        + str(output)
        + "\n"
        + str(runtime)
        + "\n"
        + str(profile)
        + "\n"
        + str(inputs)
        + "\n"
        + str(artifacts)
        + "\n2\n1\n1\n2\npidx-pco-v1\n"
        + str(manifests[0])
        + "\n"
        + str(manifests[1])
        + "\n",
        encoding="utf-8",
    )

    def fake_frontend_worker(_worker, manifest, *, indexed_sidecar):
        assert indexed_sidecar is True
        index = manifests.index(manifest)
        sidecar = artifacts / ("module_" + str(index) + ".direct.pidx")
        sidecar.write_bytes(b"indexed")
        result_paths[index].write_text(
            "OK\t"
            + str(index)
            + "\t"
            + ("entry" if index == 0 else "leaf")
            + "\t0\t0\t0\tunused\tPIDX\t"
            + str(sidecar)
            + "\n",
            encoding="utf-8",
        )
        return 0

    def fake_link(command, **kwargs):
        assert kwargs == {"check": False, "timeout": 40}
        if native:
            assert command[0] == str(native_linker)
            assert "--profile-json" not in command
        else:
            assert command[:2] == [tool.sys.executable, str(TOOL.with_name("pcc_link_macho.py"))]
            assert "--profile-json" in command
        output.write_bytes(b"executable")
        output.chmod(0o755)
        return subprocess.CompletedProcess(command, 0)

    class FakeProcess:
        _next_pid = 900_000

        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            FakeProcess._next_pid += 1
            self.pid = FakeProcess._next_pid

        def poll(self) -> int:
            return self.returncode

        def wait(self) -> int:
            return self.returncode

        def send_signal(self, _signal) -> None:
            return None

    def fake_popen(command, *, env):
        if command[1] == "--pcc-python-multi-codegen-worker":
            assert env["PCC_DIRECT_INDEXED_KERNEL_CAPTURE"] == "1"
            assert env["PCC_DIRECT_INDEXED_KERNEL_EMIT"] == "1"
            assert env["PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK"] == "1"
            return FakeProcess(
                fake_frontend_worker(
                    Path(command[0]),
                    Path(command[-1]),
                    indexed_sidecar=env["PCC_DIRECT_INDEXED_SIDECAR"] == "1",
                )
            )
        assert command[1] == "--pcc-self-backend-indexed-emit-worker"
        assert Path(command[2]).read_bytes() == b"indexed"
        if command[4] == "ASM":
            Path(command[3]).write_text(".text\n", encoding="utf-8")
        else:
            assert command[4] == "PCO"
            Path(command[3]).write_bytes(b"native-object")
        return FakeProcess(0)

    monkeypatch.setattr(tool.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tool.subprocess, "run", fake_link)

    receipt = tool.run_codegen_plan(
        plan, timeout_s=40, native_linker=native_linker if native else None,
    )

    assert receipt["worker_count"] == 2
    assert receipt["link_execution_owner"] == ("native-linker" if native else "host-cpython")
    assert bool(receipt["native_linker_sha256"]) is native
    assert receipt["profile"] == ("" if native else str(profile))
    assert receipt["indexed_process_split"] is True
    assert inputs.read_text(encoding="utf-8").splitlines()[2:] == [
        ("PCO\t" if native else "ASM\t")
        + str(artifacts / ("module_0.direct.pco" if native else "module_0.direct.s")),
        "PCO\t" + str(artifacts / "module_1.direct.pco"),
    ]
    assert receipt["schema"] == "pcc.frontend-codegen-result.v2"
    assert receipt["lanes"]["serial"]["artifact_kind"] == ("PCO" if native else "ASM")
    assert receipt["lanes"]["small"]["artifact_kind"] == "PCO"
    assert receipt["indexed_phases"]["frontend"]["launched"] == 2
    assert receipt["indexed_phases"]["asm_emit"]["launched"] == (0 if native else 1)
    assert receipt["indexed_phases"]["pco_emit"]["launched"] == (2 if native else 1)
    assert output.is_file()


def test_frontend_codegen_lanes_use_measured_ast_risk_bands(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    ast_dir = tmp_path / "ast"
    ast_dir.mkdir()
    manifests = []
    for index, ast_bytes in enumerate(
        (13_000_000, 7_000_000, 6_500_000, 4_000_000, 2_500_000, 1_000_000)
    ):
        (ast_dir / ("module_" + str(index) + ".json")).write_bytes(
            b"x" * ast_bytes
        )
        manifest = tmp_path / ("worker_" + str(index) + ".manifest")
        manifest.write_text(
            "pcc.frontends.python.codegen_worker.v4\nresult\nir\nexports\n"
            "codegen\n"
            + str(ast_dir)
            + "\nentry\noff\non\n0\n0\n0\n1\n"
            + str(index)
            + "\n",
            encoding="utf-8",
        )
        manifests.append(manifest)

    lanes = tool._partition_codegen_lanes(
        {"manifests": manifests, "oversized_count": 3}
    )

    assert lanes == {
        "serial": [manifests[0]],
        "paired_oversized": manifests[1:3],
        "heavy": [manifests[3]],
        "medium": [manifests[4]],
        "small": [manifests[5]],
    }


def test_frontend_codegen_v1_plan_keeps_the_legacy_single_process_mode(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    worker = tmp_path / "pcc1"
    worker.write_bytes(b"worker")
    worker.chmod(0o755)
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"archive")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    manifest = tmp_path / "worker.manifest"
    manifest.write_text("manifest\n", encoding="utf-8")
    plan = tmp_path / "legacy.plan"
    plan.write_text(
        "pcc.frontend-codegen-plan.v1\n"
        + str(worker)
        + "\n"
        + str(tmp_path / "program")
        + "\n"
        + str(runtime)
        + "\n"
        + str(tmp_path / "profile.json")
        + "\n"
        + str(tmp_path / "inputs")
        + "\n"
        + str(artifacts)
        + "\n1\n0\n1\n1\n"
        + str(manifest)
        + "\n",
        encoding="utf-8",
    )

    decoded = tool.read_codegen_plan(plan)

    assert decoded["indexed_process_split"] is False
    with pytest.raises(tool.DeferredLinkError, match="indexed v2 codegen plan"):
        tool.run_codegen_plan(plan, timeout_s=30, native_linker=worker)
