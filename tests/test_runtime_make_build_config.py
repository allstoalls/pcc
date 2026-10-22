"""Make producer plumbing uses owned target/configuration admission.

The controlled frontend supplies tiny IR to the real Make rules and owned
emitter. It does not compile the full runtime or prove native runtime execution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import textwrap

import pytest

from pcc.backend.ar import read_members
from pcc.backend.ar_writer import _defined_symbols, write_archive
from pcc.frontends.python import owned_runtime_build as owned
from pcc.frontends.python.pipeline_targets import host_target_triple
from pcc.tools import ir_to_obj, runtime_archive_provenance as provenance
from pcc.tools import runtime_module_inventory


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "pcc" / "runtime"
TARGETS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "arm64-apple-darwin",
    "x86_64-pc-windows-msvc",
)


def _forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected external compiler, subprocess or runtime rebuild")


@pytest.fixture
def emitter_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv("PCC_IR_TO_OBJ_EMITTER", raising=False)
    source = tmp_path / "py" / "probe.py"
    source.parent.mkdir()
    source.write_text("def probe() -> int:\n    return 7\n", encoding="utf-8")
    ir = tmp_path / "probe.ll"
    ir.write_text(
        f'target triple = "{TARGETS[0]}"\n'
        "define i32 @probe() {\nentry:\n  ret i32 7\n}\n",
        encoding="utf-8",
    )
    obj = tmp_path / "probe.o"
    receipt = provenance.receipt_path_for_object(obj)
    return [str(ir), str(obj), "--provenance", str(receipt),
            "--source", str(source), "--runtime-root", str(tmp_path)], obj, receipt


@pytest.mark.parametrize("threads, refcount, expected", [
    ("0", "atomic", {"threads": False, "refcount": "atomic"}),
    ("1", "local", {"threads": True, "refcount": "local"}),
    ("1", " ATOMIC ", {"threads": True, "refcount": "atomic"}),
    ("0", "", {"threads": False, "refcount": "atomic"}),
])
def test_emitter_records_explicit_configuration(
    emitter_inputs, monkeypatch, threads, refcount, expected,
):
    args, obj, receipt = emitter_inputs
    monkeypatch.setenv("PCC_WITH_THREADS", "1" if threads == "0" else "0")
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "ambient-other")
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    assert ir_to_obj.main(args + ["--runtime-threads", threads,
                                 "--runtime-refcount", refcount]) == 0
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["runtime_build_config"] == expected
    assert record["target_triple"] == TARGETS[0]
    assert record["uses_host_cc"] is False
    assert obj.read_bytes().startswith(b"\x7fELF")


@pytest.mark.parametrize("options, error", [
    (["--runtime-threads", "1"], "must be used together"),
    (["--runtime-refcount", "atomic"], "must be used together"),
    (["--runtime-threads", "0", "--runtime-refcount", "../atomic"], "refcount"),
    (["--runtime-threads", "0", "--runtime-refcount", "atomic/"], "refcount"),
    (["--runtime-threads", "0", "--runtime-refcount", "not a mode"], "refcount"),
])
def test_invalid_configuration_preserves_published_pair(
    emitter_inputs, monkeypatch, capsys, options, error,
):
    args, obj, receipt = emitter_inputs
    obj.write_bytes(b"previous object")
    receipt.write_bytes(b"previous receipt")
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    assert ir_to_obj.main(args + options) == 1
    assert error in capsys.readouterr().err
    assert obj.read_bytes() == b"previous object"
    assert receipt.read_bytes() == b"previous receipt"
    assert not list(obj.parent.glob(".*.tmp"))
    assert not list(obj.parent.glob(".*.pending"))


def test_runtime_configuration_requires_provenance(emitter_inputs, capsys):
    args, obj, receipt = emitter_inputs
    assert ir_to_obj.main(args[:2] + [
        "--runtime-threads", "0", "--runtime-refcount", "atomic",
    ]) == 1
    assert "--provenance, --source, and --runtime-root" in capsys.readouterr().err
    assert not obj.exists()
    assert not receipt.exists()


def test_generic_receipt_does_not_invent_runtime_configuration(emitter_inputs):
    args, _, receipt = emitter_inputs
    assert ir_to_obj.main(args) == 0
    assert "runtime_build_config" not in json.loads(receipt.read_text())


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("threads", [True])
def test_make_inventory_matches_owned_target_membership(tmp_path, target, threads):
    assert shutil.which("make"), "Make is required for its production graph test"
    inspection = tmp_path / "inspection.mk"
    inspection.write_text(
        f"include {RUNTIME / 'Makefile'}\n"
        ".PHONY: inspect-runtime-inventory\n"
        "inspect-runtime-inventory:\n"
        "\t@printf '%s\\n' '$(PCC_RUNTIME_TARGET)' '$(PCC_PY_OBJECTS)'\n",
        encoding="utf-8",
    )
    result = subprocess.run([
        "make", "--no-print-directory", "-rR", "-f", str(inspection),
        f"PYTHON={sys.executable}", f"PCC_REPO_ROOT={ROOT}",
        f"PCC_RUNTIME_TARGET={target}", f"PCC_WITH_THREADS={int(threads)}",
        "inspect-runtime-inventory",
    ], cwd=RUNTIME, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    selected_target, object_line = result.stdout.splitlines()
    names = owned.runtime_modules(str(RUNTIME), target, threads)
    assert selected_target == target
    assert object_line.split() == ["build_py/" + name + ".o" for name in names]
    assert len(names) == len(set(names))
    assert "freestanding_thread_kernel_pthread" in names
    assert "freestanding_thread_kernel" not in names
    assert ("freestanding_c_linux_start" in names) == ("linux" in target)
    assert ("freestanding_linux_aarch64" in names) == target.startswith("aarch64")
    assert ("freestanding_linux_threads" in names) == ("linux" in target)
    assert ("freestanding_windows_threads" in names) == ("windows" in target)


@pytest.mark.parametrize("flags", [
    ("-B", "-n"),
    ("-Bn",),
    ("--always-make", "--just-print"),
    ("--always-make", "--dry-run"),
    ("--always-make", "--recon"),
    ("-Bns",),
])
@pytest.mark.parametrize("selection", ["default", "environment", "command"])
def test_make_dry_run_resolves_inventory_only_for_archive_graph(
    tmp_path, flags, selection,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    shutil.copyfile(RUNTIME / "Makefile", runtime / "Makefile")
    (runtime / "py").symlink_to(RUNTIME / "py", target_is_directory=True)
    calls = tmp_path / "inventory.jsonl"
    python = tmp_path / "inventory-python"
    python.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "with open(" + repr(str(calls)) + ", 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("PCC_") and key not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    }
    options = []
    target = host_target_triple()
    if selection != "default":
        environment["PCC_RUNTIME_TARGET"] = target = TARGETS[2]
    if selection == "command":
        target = TARGETS[3]
        options.append("PCC_RUNTIME_TARGET=" + target)
    before = sorted(str(path.relative_to(runtime)) for path in runtime.rglob("*"))
    result = subprocess.run([
        "make", "--no-print-directory", *flags,
        f"PYTHON={python}", f"PCC_REPO_ROOT={ROOT}", *options,
        "libpy_runtime_pcc_py.a",
    ], cwd=runtime, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = [json.loads(line) for line in calls.read_text().splitlines()]
    # The public archive and its locked sub-Make each select inventory once.
    # Missing receipts in a dry run must not start one sub-Make per object.
    expected_targets = 2 if selection == "default" else 0
    assert len(observed) == expected_targets + 2
    assert sum("--field" in args for args in observed) == expected_targets
    modules = [args for args in observed if "--threads" in args]
    assert len(modules) == 2
    assert all(args[args.index("--target") + 1] == target for args in modules)
    archive_line = next(line for line in result.stdout.splitlines()
                        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line)
    objects = re.findall(r"build_py/[^ ;]+\.o(?= |;|$)", archive_line)
    assert objects == ["build_py/" + name + ".o"
                       for name in owned.runtime_modules(str(runtime), target, True)]
    assert sorted(str(path.relative_to(runtime)) for path in runtime.rglob("*")) == before


@pytest.mark.parametrize("field", ["modules", "target"])
def test_inventory_rejects_unknown_target_without_partial_output(capsys, field):
    assert runtime_module_inventory.main([
        "--runtime-root", str(RUNTIME), "--target", "unsupported-unknown-none",
        "--field", field,
    ]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "no emitter" in output.err


def _controlled_make_runtime(
    tmp_path, target, threads, refcount, *, archive_tools=None, recover_flags=None,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # Retain the actual production recipes and configuration selection. Reduce
    # only the common fixture inventory; the target module additions stay real.
    makefile = (RUNTIME / "Makefile").read_text(encoding="utf-8")
    makefile = re.sub(r"^PY_MODULES =.*$", "PY_MODULES = probe py_runtime_log", makefile, flags=re.M)
    makefile = re.sub(r"^PY_MODULES \+=.*$", "", makefile, flags=re.M)
    makefile = re.sub(r"^FREESTANDING_PY_MODULES (?:\+)?=.*$", "", makefile, flags=re.M)
    (runtime / "Makefile").write_text(makefile, encoding="utf-8")
    (runtime / "py").mkdir()
    for name in owned.runtime_modules(str(runtime), target, threads):
        (runtime / "py" / (name + ".py")).write_text(
            "# Controlled frontend input; not a functional runtime.\n", encoding="utf-8")
    frontend = tmp_path / "controlled_frontend.py"
    frontend.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "target = next(value.split('=', 1)[1] for value in args if value.startswith('--target='))\n"
        "out = Path(next(value.split('=', 1)[1] for value in args if value.startswith('--emit-llvm=')))\n"
        "name = Path(args[-1]).stem\n"
        "with open(os.environ['FRONTEND_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'name': name, 'target': target, 'threads': os.environ.get('PCC_WITH_THREADS'), 'polls': os.environ.get('PCC_IMPLICIT_THREAD_SAFEPOINTS'), 'refcount': os.environ.get('PCC_REFCOUNT_KIND')}) + '\\n')\n"
        "out.write_text('target triple = \"' + target + '\"\\ndefine i32 @PyProbe_' + name + '() {\\nentry:\\n  ret i32 7\\n}\\n')\n",
        encoding="utf-8",
    )
    host_cc = tmp_path / "forbidden-host-cc"
    host_cc.write_text("#!/bin/sh\necho unexpected-host-cc >&2\nexit 99\n", encoding="utf-8")
    host_cc.chmod(0o755)
    log = tmp_path / "frontend.jsonl"
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("PCC_") or key in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}:
            environment.pop(key)
    environment.update(FRONTEND_LOG=str(log), CC=str(host_cc),
                       PCC_WITH_THREADS="0" if threads else "1",
                       PCC_REFCOUNT_KIND="ambient-other")
    tools = archive_tools or {"AR": "ar", "RANLIB": "ranlib", "NM": "nm"}
    options = [
        f"PYTHON={sys.executable}", f"PCC_REPO_ROOT={ROOT}",
        f"PCC={shlex.quote(sys.executable)} {shlex.quote(str(frontend))}",
        f"PCC_RUNTIME_TARGET={target}", f"PCC_WITH_THREADS={int(threads)}",
        f"PCC_REFCOUNT_KIND={refcount}", f"CC={host_cc}",
        *(name + "=" + str(tool) for name, tool in tools.items()),
        "libpy_runtime_pcc_py.a",
    ]
    result = subprocess.run([
        "make", "--no-print-directory", "-rR", "-j1", *options,
    ], cwd=runtime, env=environment, capture_output=True, text=True, timeout=90)
    (tmp_path / "make.stdout").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "make.stderr").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    if recover_flags is not None:
        # Retain the current object: only its lost receipt should force a rebuild.
        receipt = runtime / "build_py" / "probe.o.provenance.json"
        receipt.unlink()
        result = subprocess.run([
            "make", *recover_flags, "NOTE=contains-n", *options,
        ], cwd=runtime, env=environment, capture_output=True, text=True, timeout=90)
        (tmp_path / "recovery.stdout").write_text(result.stdout, encoding="utf-8")
        (tmp_path / "recovery.stderr").write_text(result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        assert receipt.is_file()
    return runtime, [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("flags", [(), ("--no-print-directory",)])
def test_make_recovers_missing_receipt_with_non_dry_run_flags(tmp_path, flags):
    target = host_target_triple()
    runtime, calls = _controlled_make_runtime(
        tmp_path, target, True, "atomic", recover_flags=flags,
    )
    names = owned.runtime_modules(str(runtime), target, True)
    assert [call["name"] for call in calls] == [*names, "probe"]
    manifest = provenance.verify_runtime_archive_manifest(
        runtime / "libpy_runtime_pcc_py.a", runtime_root=runtime,
    )
    assert owned._manifest_matches_config(
        manifest, str(runtime), target, {"threads": True, "refcount": "atomic"},
    )


@pytest.mark.parametrize("threads, refcount", [(True, "atomic"), (True, "local")])
def test_real_make_recipes_record_archive_configuration(tmp_path, monkeypatch, threads, refcount):
    # Native archive tools consume native objects, including Mach-O on macOS.
    target = host_target_triple()
    runtime, calls = _controlled_make_runtime(tmp_path, target, threads, refcount)
    archive = runtime / "libpy_runtime_pcc_py.a"
    config = {"threads": threads, "refcount": refcount}
    names = owned.runtime_modules(str(runtime), target, threads)
    assert [call["name"] for call in calls] == names
    for call in calls:
        assert call["target"] == target
        assert call["refcount"] == refcount
        assert call["threads"] == "1"
        assert call["polls"] == ("0" if call["name"] in owned._NO_IMPLICIT_POLL_MODULES else None)
    manifest = provenance.verify_runtime_archive_manifest(archive, runtime_root=runtime)
    assert [row["member"] for row in manifest["members"]] == [name + ".o" for name in names]
    assert all(row["runtime_build_config"] == config for row in manifest["members"])
    assert owned._manifest_matches_config(manifest, str(runtime), target, config)
    for key in ("PCC_RUNTIME_DIR", "PCC_RUNTIME_ARCHIVE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PCC_WITH_THREADS", str(int(threads)))
    monkeypatch.setenv("PCC_REFCOUNT_KIND", refcount)
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    assert owned.ensure_target_runtime(str(runtime), target, explicit_archive=str(archive)) == str(archive)

    # Preserve valid payload/source hashes to exercise configuration admission,
    # rather than letting a generic integrity mismatch conceal the producer gap.
    manifest_path = provenance.manifest_path_for_archive(archive)
    original = manifest_path.read_bytes()
    for tamper in ("absent", "null", "threads", "refcount", "codegen"):
        changed = json.loads(original)
        member = changed["members"][0]
        if tamper == "absent":
            del member["runtime_build_config"]
        elif tamper == "null":
            member["runtime_build_config"] = None
        elif tamper == "threads":
            member["runtime_build_config"]["threads"] = not threads
        elif tamper == "refcount":
            member["runtime_build_config"]["refcount"] = "different"
        else:
            member["codegen_checksum"] = "0" * 64
        changed["members_sha256"] = provenance._members_sha256(changed["members"])
        manifest_path.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError, match="runtime build configuration|target/source configuration"):
            owned.ensure_target_runtime(str(runtime), target, explicit_archive=str(archive))
    manifest_path.write_bytes(original)
    source = runtime / "py" / "probe.py"
    source.write_text(source.read_text() + "# changed source\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        owned.ensure_target_runtime(str(runtime), target, explicit_archive=str(archive))


# These adapters exercise the real Make tool-selection contract using PCC's
# existing object readers and indexed archive writer. They do not emulate a
# native Darwin toolchain or certify execution on another operating system.
def _owned_archive_tools(tmp_path, *, required_format=None):
    tools_dir = tmp_path / "archive-tools"
    tools_dir.mkdir()
    log = tools_dir / "calls.jsonl"
    program = (
        "#!" + sys.executable + "\n"
        "import json, sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
        "from pcc.backend.ar import read_members\n"
        "from pcc.backend.ar_writer import _defined_symbols, write_archive\n"
        "required_format = " + repr(required_format) + "\n"
        "log = Path(" + repr(str(log)) + ")\n"
        + textwrap.dedent("""\
        role = Path(sys.argv[0]).name
        args = sys.argv[1:]
        with log.open('a') as stream:
            stream.write(json.dumps({'role': role, 'args': args}) + chr(10))
        if role == 'ar':
            assert args[0] == 'rcs' and len(args) >= 3
            members = [(Path(path).name, Path(path).read_bytes()) for path in args[2:]]
            Path(args[1]).write_bytes(write_archive(members))
        else:
            assert (role == 'ranlib' and len(args) == 1) or (role == 'nm' and args[:1] == ['-g'] and len(args) == 2)
            payload = Path(args[-1]).read_bytes()
            assert b'__.SYMDEF SORTED' in payload, 'owned archive lacks index'
            members = read_members(payload)
            assert members, 'owned archive lacks members'
            for name, data in members:
                kind, symbols = _defined_symbols(data)
                if required_format is not None and kind != required_format:
                    raise SystemExit('target archive tool requires ' + required_format + ', got ' + kind + ': ' + name)
                if role == 'nm':
                    for symbol in symbols:
                        print('00000000 T ' + symbol)
        """)
    )
    result = {}
    for role in ('ar', 'ranlib', 'nm'):
        tool = tools_dir / role
        tool.write_text(program, encoding="utf-8")
        tool.chmod(0o755)
        result[role.upper()] = tool
    return result, log


def _emit_probe_object(tmp_path, target):
    source = tmp_path / "py" / "probe.py"
    source.parent.mkdir()
    source.write_text("def probe() -> int:\n    return 7\n", encoding="utf-8")
    ir = tmp_path / "probe.ll"
    ir.write_text(
        f'target triple = "{target}"\n'
        "define i32 @PyProbe() {\nentry:\n  ret i32 7\n}\n",
        encoding="utf-8",
    )
    obj = tmp_path / "probe.o"
    assert ir_to_obj.main([
        str(ir), str(obj), "--target", target,
        "--source", str(source), "--runtime-root", str(tmp_path),
        "--provenance", str(provenance.receipt_path_for_object(obj)),
        "--runtime-threads", "1", "--runtime-refcount", "local",
    ]) == 0
    return obj


@pytest.mark.parametrize("target, kind, machine", [
    (TARGETS[0], "elf", 62),
    (TARGETS[1], "elf", 183),
    (TARGETS[2], "macho", 0x0100000c),
    (TARGETS[3], "coff", 0x8664),
])
def test_explicit_target_object_archive_shape(tmp_path, monkeypatch, target, kind, machine):
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "pcc")
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    obj = _emit_probe_object(tmp_path, target)
    data = obj.read_bytes()
    actual_kind, symbols = _defined_symbols(data)
    assert actual_kind == kind
    offset, size = {"elf": (18, 2), "macho": (4, 4), "coff": (0, 2)}[kind]
    assert int.from_bytes(data[offset:offset + size], "little") == machine
    assert symbols == (["_PyProbe"] if kind == "macho" else ["PyProbe"])
    archive = tmp_path / "probe.a"
    archive.write_bytes(write_archive([(obj.name, data)]))
    assert read_members(archive.read_bytes()) == [(obj.name, data)]
    provenance.capi_inventory_path_for_archive(archive).write_text("\n".join(symbols) + "\n")
    provenance.assemble_runtime_archive_manifest(archive, [obj], runtime_root=tmp_path)
    manifest = provenance.verify_runtime_archive_manifest(archive, runtime_root=tmp_path)
    assert manifest["target_triple"] == target
    assert manifest["member_count"] == 1
    assert manifest["members"][0]["runtime_build_config"] == {"threads": True, "refcount": "local"}
    assert manifest["capi_symbols"] == symbols


def test_archive_tool_rejects_incompatible_member_format(tmp_path, monkeypatch):
    # Model the reported ELF-to-Mach-O tool mismatch with actual owned bytes.
    # The diagnostic is ours; this is not an execution of Apple's ranlib.
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "pcc")
    obj = _emit_probe_object(tmp_path, TARGETS[0])
    tools, _ = _owned_archive_tools(tmp_path, required_format="macho")
    archive = tmp_path / "wrong-target.a"
    result = subprocess.run([str(tools["AR"]), "rcs", str(archive), str(obj)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(tools["RANLIB"]), str(archive)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "target archive tool requires macho, got elf: probe.o" in result.stderr
    assert not provenance.manifest_path_for_archive(archive).exists()


def test_make_explicit_cross_target_uses_selected_archive_tools(tmp_path):
    target = TARGETS[2]
    tools, log = _owned_archive_tools(tmp_path, required_format="macho")
    runtime, _ = _controlled_make_runtime(tmp_path, target, True, "local", archive_tools=tools)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["role"] for call in calls] == ["ar", "ranlib", "nm"]
    archive = runtime / "libpy_runtime_pcc_py.a"
    manifest = provenance.verify_runtime_archive_manifest(archive, runtime_root=runtime)
    config = {"threads": True, "refcount": "local"}
    assert owned._manifest_matches_config(manifest, str(runtime), target, config)
    assert all(_defined_symbols(data)[0] == "macho" for _, data in read_members(archive.read_bytes()))
    assert manifest["capi_symbols"] == sorted("_PyProbe_" + name for name in owned.runtime_modules(str(runtime), target, True))
