import os
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python import native_deferred as driver


def test_native_entry_rejects_host_python():
    with pytest.raises(RuntimeError, match="host Python is not supported"):
        driver.main()


def test_cli_dispatch_checks_native_owner_and_forwards_plan(monkeypatch, capsys):
    from types import SimpleNamespace
    from pcc.driver import cli_bootstrap as cli

    calls = []
    monkeypatch.setattr(driver, "run", calls.append)
    assert cli.bootstrap_cli_main(["--pcc-native-deferred-worker", "--check"]) == 2
    assert "requires pcc1" in capsys.readouterr().err
    monkeypatch.setattr(cli, "sys", SimpleNamespace(
        implementation=SimpleNamespace(name="pcc"),
    ))
    assert cli.bootstrap_cli_main(["--pcc-native-deferred-worker", "--check"]) == 0
    assert calls == []
    assert cli.bootstrap_cli_main(["--pcc-native-deferred-worker", "/plan with spaces"]) == 0
    assert calls == ["/plan with spaces"]


def test_wrapper_rejects_old_compiler_before_starting_compile(tmp_path):
    touched = tmp_path / "started"
    script = (
        Path(driver.__file__).parents[2] / "scripts" / "run_pcc_native_deferred.py"
    )
    # ``sys.executable`` answers ``--pcc-native-deferred-worker --check`` with
    # exit 2 ("Unknown option"), which is exactly an old compiler that cannot
    # run the continuation.
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            sys.executable,
            "codegen",
            "link",
            "--",
            sys.executable,
            "-c",
            f"open({str(touched)!r}, 'w').close()",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert "host Python fallback is forbidden" in result.stderr
    assert not touched.exists()


def test_bootstrap_rejects_missing_deferred_runtime_before_compilation(tmp_path):
    environment = dict(
        os.environ,
        PCC_RUNTIME_ARCHIVE=str(tmp_path / "absent-runtime.a"),
        PCC_PY_FRONTEND_IR_CACHE_IDENTITY="test",
        PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY="test",
        PCC_BOOTSTRAP_DEFER_FRONTEND_CODEGEN="1",
        PCC_BOOTSTRAP_DEFER_SELF_LINK="1",
    )
    environment.pop("LC_ALL", None)
    script = Path(driver.__file__).parents[2] / "scripts" / "bootstrap.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--backend",
            "self",
            "--from-stage",
            "2",
            "--stage",
            "2",
            "--out-dir",
            str(tmp_path / "out"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
    assert "requires a runtime archive before compilation" in result.stderr
    assert not (tmp_path / "out" / "pcc2").exists()


def test_stage_continuation_inherits_effective_stage_environment(tmp_path, monkeypatch):
    """The deferred continuation sees the coordinator's effective settings.

    The shell version of this test sliced ``run_stage()`` out of
    ``bootstrap.sh``; the driver exposes the same contract as a function, so
    this drives it directly and inspects the environment handed to the
    wrapper process.
    """

    from scripts import bootstrap

    runtime = tmp_path / "runtime.a"
    runtime.touch()
    options = bootstrap.validate_settings(
        bootstrap.Options(
            {
                "PCC_BOOTSTRAP_OUT_DIR": str(tmp_path / "out"),
                "PCC_BOOTSTRAP_PYTHON_IR_PASSES": "off",
                "PCC_PYTHON_IR_PASSES": "default",
                "PCC_BOOTSTRAP_SELF_BACKEND_JOBS": "2",
                "PCC_SELF_BACKEND_JOBS": "1",
                "PCC_RUNTIME_ARCHIVE": str(runtime),
                "PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD": "1",
            }
        )
    )
    options.out_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_guarded(target, opts, stage, environment):
        captured["target"] = list(target)
        captured["environment"] = dict(environment)
        return 127, None

    monkeypatch.setattr(bootstrap, "_run_guarded", fake_guarded)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.run_stage(2, options.stage_output(2), ["compiler"], options)

    environment = captured["environment"]
    assert environment["PCC_PYTHON_IR_PASSES"] == "off"
    assert environment["PCC_SELF_BACKEND_JOBS"] == "2"
    assert environment["PCC_RUNTIME_ARCHIVE"] == str(runtime)
    assert environment["PCC_DIRECT_INDEXED_KERNEL_CAPTURE"] == "1"
    assert environment["PCC_DIRECT_INDEXED_KERNEL_EMIT"] == "1"
    target = captured["target"]
    assert target[0] == sys.executable
    assert str(target[1]).endswith("run_pcc_native_deferred.py")


@pytest.mark.parametrize("auto_pco", [False, True])
def test_native_codegen_orders_results_and_rejects_stale_artifacts(tmp_path, monkeypatch, auto_pco):
    manifests = []
    for index in (1, 0):
        manifest = tmp_path / ("worker" + str(index))
        result = tmp_path / ("result" + str(index))
        manifest.write_text("\n".join([
            "pcc.frontends.python.codegen_worker.v4", str(result),
            str(tmp_path), "", "", "", "", "", "", "", "1", str(index),
        ]) + "\n")
        result.write_text("stale result")
        stem = tmp_path / ("module_" + str(index) + ".direct")
        stem.with_suffix(".pco").write_bytes(b"stale pco")
        stem.with_suffix(".pidx").write_bytes(b"stale sidecar")
        manifests.append(str(manifest))
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"archive")
    plan = [
        "pcc.frontend-codegen-plan.v2", "/native/pcc1", str(tmp_path / "output"),
        str(runtime), str(tmp_path / "profile"), str(tmp_path / "inputs"),
        str(tmp_path), "2", "1", "2", "2", "pidx-pco-v1",
    ] + manifests
    calls = []

    def run_one(command):
        import shlex
        arguments = shlex.split(command)
        if "--pcc-python-multi-codegen-worker" in arguments:
            path = arguments[-1]
            index = int(Path(path).read_text().splitlines()[-1])
            result = tmp_path / ("result" + str(index))
            assert not result.exists()
            sidecar = tmp_path / ("module_" + str(index) + ".direct.pidx")
            assert not sidecar.exists()
            sidecar.write_bytes(b"sidecar")
            result.write_text("OK\t" + str(index) + "\tmodule\t0\t0\t0\tunused\tPIDX\t" + str(sidecar) + "\n")
        else:
            assert Path(arguments[-3]).read_bytes() == b"sidecar"
            packed = Path(arguments[-2])
            assert not packed.exists()
            packed.write_bytes(b"packed")

    def run_commands(commands, width):
        calls.append((len(commands), width))
        for command in commands:
            run_one(command)

    def run_chained(commands, reservations, followups, paths, floor, width, budget):
        calls.append(("chained", len(commands), width))
        for command, followup, path in zip(commands, followups, paths):
            run_one(command)
            assert Path(path).is_file()
            run_one(followup)

    linked = []
    monkeypatch.setattr(driver, "_native_worker", lambda _path: None)
    from pcc.frontends.python import deferred_frontend_schedule
    monkeypatch.setattr(deferred_frontend_schedule, "run_worker_processes", run_commands)
    monkeypatch.setattr(deferred_frontend_schedule, "run_chained_worker_processes", run_chained)
    monkeypatch.setattr(
        deferred_frontend_schedule, "_frontend_floors_and_order",
        lambda commands, _manifests: ([1] * len(commands), list(range(len(commands)))),
    )
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "auto" if auto_pco else "2")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(6 * 1073741824))
    monkeypatch.setattr(deferred_frontend_schedule, "parallel_cpu_budget", lambda: 12)
    # Memory admission reads this process's RSS; ordering is the contract here.
    monkeypatch.setattr(deferred_frontend_schedule, "compiled_native_auto_jobs", lambda jobs: jobs)
    monkeypatch.setattr(driver, "_link", lambda *args: linked.append(args))
    driver._codegen(plan)
    assert calls == ([("chained", 2, 12)] if auto_pco else [(1, 1), (1, 2), (1, 1), (1, 2)])
    assert Path(linked[0][1]).read_text().splitlines()[2:] == [
        "PCO\t" + str(tmp_path / "module_0.direct.pco"),
        "PCO\t" + str(tmp_path / "module_1.direct.pco"),
    ]


def test_native_codegen_rejects_a_sidecar_other_than_the_scheduled_one(tmp_path, monkeypatch):
    manifest = tmp_path / "worker0"
    result = tmp_path / "result0"
    manifest.write_text("\n".join([
        "pcc.frontends.python.codegen_worker.v4", str(result),
        str(tmp_path), "", "", "", "", "", "", "", "1", "0",
    ]) + "\n")
    runtime = tmp_path / "runtime.a"
    runtime.write_bytes(b"archive")
    plan = [
        "pcc.frontend-codegen-plan.v2", "/native/pcc1", str(tmp_path / "output"),
        str(runtime), str(tmp_path / "profile"), str(tmp_path / "inputs"),
        str(tmp_path), "1", "0", "1", "1", "pidx-pco-v1", str(manifest),
    ]

    def run_commands(commands, _width):
        import shlex
        for command in commands:
            arguments = shlex.split(command)
            if "--pcc-python-multi-codegen-worker" in arguments:
                other = tmp_path / "elsewhere.pidx"
                other.write_bytes(b"sidecar")
                result.write_text("OK\t0\tmodule\t0\t0\t0\tunused\tPIDX\t" + str(other) + "\n")
                (tmp_path / "module_0.direct.pidx").write_bytes(b"sidecar")
            else:
                Path(arguments[-2]).write_bytes(b"packed")

    monkeypatch.setattr(driver, "_native_worker", lambda _path: None)
    from pcc.frontends.python import deferred_frontend_schedule
    monkeypatch.setattr(deferred_frontend_schedule, "run_worker_processes", run_commands)
    monkeypatch.setenv("PCC_PY_FRONTEND_JOBS", "1")
    monkeypatch.setattr(driver, "_link", lambda *args: None)
    with pytest.raises(ValueError, match="not the scheduled PCO input"):
        driver._codegen(plan)


def test_native_codegen_refuses_script_worker(tmp_path):
    worker = tmp_path / "python launcher"
    worker.write_text("#!/usr/bin/python3\n")
    worker.chmod(0o755)
    with pytest.raises(ValueError, match="must be a native executable"):
        driver._native_worker(str(worker))


def test_native_unsupported_link_surface_never_resolves_host_python(monkeypatch):
    from types import SimpleNamespace
    from pcc.frontends.python import pipeline_self_backend_link as linker

    monkeypatch.setattr(linker, "sys", SimpleNamespace(
        platform="linux", implementation=SimpleNamespace(name="pcc"),
    ))
    def forbidden():
        pytest.fail("native compiler requested host Python")

    # Plain internal inputs link in process; semantic layout is a surface the
    # in-process owner does not cover, so it reaches the native refusal.
    with pytest.raises(linker.SelfBackendLinkError, match="host Python fallback is forbidden"):
        linker.run_link_command(
            [], "input.s", "out", None, (), False,
            semantic_layout_policy="policy.json",
            target_triple="arm64-apple-darwin23.6.0",
            resolve_self_link_mode=lambda **_kwargs: "pcc",
            validate_pcc_self_link_surface=lambda **kwargs: None,
            repo_root_for_link=forbidden, host_python_command=forbidden,
            build_pcc_link_command=None, log=None, join_strings=None,
        )


@pytest.mark.integration
def test_native_deferred_link_plan_executes_under_all_collectors(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.native_object import NativeObject, encode_native_object

    helper = tmp_path / "native-deferred"
    python_program_compiler(
        str(Path(driver.__file__)), str(helper), backend="self",
        libpython_mode="off", runtime_archive=str(pcc_runtime_archive),
    )
    sections, undefined = assemble_file(
        ".section __TEXT,__text,regular,pure_instructions\n"
        ".globl _main\n.p2align 2\n_main:\n movz w0, #42\n ret\n"
    )
    packed = tmp_path / "main.pco"
    packed.write_bytes(encode_native_object(
        NativeObject.from_sections(sections, undefined=undefined),
    ))
    inputs = tmp_path / "inputs"
    inputs.write_text("pcc.macho-internal-inputs.v1\n1\nPCO\t" + str(packed) + "\n")
    for gc in range(5):
        output = tmp_path / ("out" + str(gc))
        plan = tmp_path / ("plan" + str(gc))
        plan.write_text("\n".join([
            "pcc.deferred-self-link.v1", str(output), "", str(inputs),
            str(tmp_path / "profile"), "", "0",
        ]) + "\n")
        env = dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(gc),
                   PCC_HOST_PYTHON="/usr/bin/false", PCC_HOST_PCC="/usr/bin/false")
        linked = subprocess.run([str(helper), str(plan)], env=env, capture_output=True, timeout=30)
        assert linked.returncode == 0, linked.stderr
        executed = subprocess.run([str(output)], env=env, capture_output=True, timeout=10)
        assert executed.returncode == 42, executed.stderr


@pytest.mark.integration
def test_pcc1_native_deferred_cli_compiles_and_executes_two_modules(
    tmp_path, native_pcc1_compiler, pcc_runtime_archive,
):
    from tests.python.process_timeout import run_process_group_timeout

    package = tmp_path / "two_modules"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "lib.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    source = package / "__main__.py"
    source.write_text("from two_modules.lib import add\nprint(add(20, 22))\n")
    tools = tmp_path / "file-tools"
    tools.mkdir()
    for name in ("ls", "mkdir", "cp", "rm", "sh", "cat"):
        (tools / name).symlink_to("/bin/" + name)
    output = tmp_path / "native output"
    codegen = tmp_path / "codegen plan"
    link = tmp_path / "link plan"
    env = dict(os.environ, PATH=str(tools), PYTHONPATH=str(tmp_path),
               PCC_RUNTIME_ARCHIVE=str(pcc_runtime_archive),
               PCC_RUNTIME_CC="/usr/bin/false", PCC_HOST_PYTHON="/usr/bin/false",
               PCC_HOST_PCC="/usr/bin/false", PCC_PY_FRONTEND_JOBS="auto",
               PCC_WORKER_TREE_BUDGET_BYTES="4294967296",
               PCC_SELF_BACKEND_JOBS="2", PCC_PYTHON_IR_PASSES="off",
               PCC_DIRECT_INDEXED_KERNEL_CAPTURE="1", PCC_DIRECT_INDEXED_KERNEL_EMIT="1",
               PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK="1",
               PCC_DIRECT_INDEXED_KERNEL_FUSE_USES="1",
               PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND="1",
               PCC_DIRECT_INDEXED_NATIVE_OBJECT="1",
               PCC_DEFER_FRONTEND_CODEGEN_PLAN=str(codegen),
               PCC_DEFER_FRONTEND_OUTPUT=str(output), PCC_DEFER_SELF_LINK_PLAN=str(link))
    env.pop("LC_ALL", None)
    script = (
        Path(driver.__file__).parents[2] / "scripts" / "run_pcc_native_deferred.py"
    )
    result = run_process_group_timeout(
        [sys.executable, str(script), str(native_pcc1_compiler), str(codegen), str(link), "--",
         str(native_pcc1_compiler), "--backend", "self", "--python-libpython", "off",
         str(source), "-o", str(output)],
        env=env, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert codegen.read_text().splitlines()[0] == "pcc.frontend-codegen-plan.v2"
    inputs = Path(str(codegen) + ".internal-inputs").read_text().splitlines()[2:]
    assert len(inputs) == 2 and all(item.startswith("PCO\t") for item in inputs)
    executed = subprocess.run([str(output)], env=env, capture_output=True, text=True, timeout=10)
    assert executed.returncode == 0, executed.stderr
    assert executed.stdout == "42\n"


@pytest.mark.integration
@pytest.mark.parametrize("gc_backend", range(5))
def test_pcc1_native_deferred_link_entry_executes(
    tmp_path, native_pcc1_compiler, gc_backend,
):
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.native_object import NativeObject, encode_native_object
    from tests.python.process_timeout import run_process_group_timeout

    sections, undefined = assemble_file(
        ".section __TEXT,__text,regular,pure_instructions\n"
        ".globl _main\n.p2align 2\n_main:\n movz w0, #42\n ret\n"
    )
    packed = tmp_path / "main.pco"
    packed.write_bytes(encode_native_object(NativeObject.from_sections(sections, undefined=undefined)))
    inputs = tmp_path / "inputs"
    inputs.write_text("pcc.macho-internal-inputs.v1\n1\nPCO\t" + str(packed) + "\n")
    output = tmp_path / "out"
    plan = tmp_path / "link.plan"
    plan.write_text("\n".join([
        "pcc.deferred-self-link.v1", str(output), "", str(inputs),
        str(tmp_path / "profile"), "", "0",
    ]) + "\n")
    env = dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(gc_backend),
               PCC_HOST_PYTHON="/usr/bin/false", PCC_HOST_PCC="/usr/bin/false")
    linked = run_process_group_timeout(
        [str(native_pcc1_compiler), "--pcc-native-deferred-worker", str(plan)],
        env=env, timeout=30,
    )
    assert linked.returncode == 0, linked.stderr
    executed = subprocess.run([str(output)], env=env, capture_output=True, timeout=10)
    assert executed.returncode == 42, executed.stderr
