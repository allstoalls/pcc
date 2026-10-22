"""Selection of owned runtime archives across semantic build configurations."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcc.frontends.python import owned_runtime_build as owned
from pcc.frontends.python import pipeline_runtime_archive as selection
from pcc.frontends.python import pipeline_targets
from pcc.tools import runtime_archive_provenance as provenance


TARGETS = ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc", "arm64-apple-darwin"]


def _forbidden(*_args, **_kwargs):
    raise AssertionError("unexpected old archive shortcut, subprocess, or rebuild")


def _manifest(runtime_dir, target, *, threads=True, refcount="atomic"):
    config = {"threads": threads, "refcount": refcount}
    return {"target_triple": target, "members": [
        {"member": name + ".o", "runtime_build_config": dict(config)}
        for name in owned.runtime_modules(str(runtime_dir), target, threads)
    ]}


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    monkeypatch.delenv("PCC_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.delenv("PCC_RUNTIME_BUILD", raising=False)
    monkeypatch.delenv("PCC_WITH_THREADS", raising=False)
    monkeypatch.delenv("PCC_REFCOUNT_KIND", raising=False)
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "Makefile").write_text(
        "PY_MODULES = py_obj py_threading\n"
        "PY_MODULES += freestanding_thread_kernel_pthread\n"
        "FREESTANDING_PY_MODULES = freestanding_mem_str\n", encoding="utf-8")
    return root


def _selection_options(root):
    callbacks = {
        name: _forbidden for name in (
            "archive_stale_check", "c_bundle_valid", "archive_requires_provenance",
            "archive_provenance_valid", "archive_codegen_stale", "archive_manifest",
            "archive_target_matches", "compiler_sources_newer", "resolve_pcc_binary",
            "runtime_host_python", "run_make", "write_archive_target_stamp",
        )
    }
    return dict(callbacks, needs_libpython=False, runtime_dir_default=str(root),
                archive_pcc_py=str(root / "libpy_runtime_pcc_py.a"),
                archive_pcc_py_libpython=str(root / "libpy_runtime_pcc_py_libpython.a"),
                wheel_matches=lambda _path: False, logger=lambda *_args: None)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("initial_refcount", ["atomic", "biased"])
def test_native_runtime_switches_configuration_in_both_directions(runtime_root, monkeypatch, target, initial_refcount):
    # Every runtime is threaded; the refcount strategy is the remaining
    # configuration axis, and the removed single-thread runtime fails.
    monkeypatch.setattr(selection, "sys", SimpleNamespace(platform="win32" if "windows" in target else "linux"))
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: target)
    monkeypatch.setattr(provenance, "manifest_codegen_staleness_reason", lambda _receipt: "")
    options = _selection_options(runtime_root)
    packaged = Path(options["archive_pcc_py"])
    packaged.write_bytes(b"pre-existing runtime")
    receipts = {str(packaged): _manifest(runtime_root, target, refcount=initial_refcount)}
    builds = []

    def verify(path, *, runtime_root):
        return receipts[str(path)]

    def build(root, output, selected_target):
        assert selected_target == target
        Path(output).write_bytes(b"new runtime")
        config = owned.runtime_build_config()
        receipts[output] = _manifest(Path(root), target, **config)
        builds.append(output)

    other = "biased" if initial_refcount == "atomic" else "atomic"
    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", verify)
    monkeypatch.setattr(owned, "build_runtime_archive", build)
    monkeypatch.setenv("PCC_REFCOUNT_KIND", initial_refcount)
    assert selection.ensure_runtime(False, **options) == str(packaged)
    assert builds == []
    monkeypatch.setenv("PCC_REFCOUNT_KIND", other)
    switched = selection.ensure_runtime(False, **options)
    assert switched != str(packaged)
    assert ("threads-" + other) in switched
    assert builds == [switched]
    assert selection.ensure_runtime(False, **options) == switched
    assert builds == [switched]
    monkeypatch.setenv("PCC_REFCOUNT_KIND", initial_refcount)
    assert selection.ensure_runtime(False, **options) == str(packaged)
    assert builds == [switched]
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    # Selection reports configuration errors as its archive error.
    with pytest.raises(Exception, match="single-thread runtime was removed"):
        selection.ensure_runtime(False, **options)


@pytest.mark.parametrize("mismatch", ["target", "threads", "refcount", "missing_config", "missing_module"])
def test_explicit_archive_rejects_mismatched_configuration(runtime_root, monkeypatch, mismatch):
    target = TARGETS[0]
    path = runtime_root / "explicit.a"
    path.write_bytes(b"explicit archive")
    receipt = _manifest(runtime_root, target)
    if mismatch == "target":
        receipt["target_triple"] = TARGETS[1]
    elif mismatch == "threads":
        # A stale archive from the removed single-thread configuration.
        for member in receipt["members"]:
            member["runtime_build_config"]["threads"] = False
    elif mismatch == "refcount":
        receipt = _manifest(runtime_root, target, refcount="local")
    elif mismatch == "missing_config":
        del receipt["members"][0]["runtime_build_config"]
    else:
        receipt["members"].pop()
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(path))
    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", lambda *_a, **_k: receipt)
    monkeypatch.setattr(provenance, "manifest_codegen_staleness_reason", lambda _receipt: "")
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    with pytest.raises(ValueError, match="target/source configuration"):
        owned.ensure_target_runtime(str(runtime_root), target)
    assert path.read_bytes() == b"explicit archive"


def _write_wheel_bundle(archive, receipt, target_id):
    archive.write_bytes(b"wheel runtime")
    manifest = Path(str(archive) + ".provenance.json")
    manifest.write_text(json.dumps(receipt), encoding="utf-8")
    inventory = Path(str(archive) + ".capi_syms")
    inventory.write_bytes(b"PyLong_FromLong\n")
    lines = ["pcc.runtime-wheel-artifact.v2", "target=" + target_id]
    for name, path in (("archive", archive), ("manifest", manifest), ("capi-inventory", inventory)):
        lines.append(name + "-sha256=" + hashlib.sha256(path.read_bytes()).hexdigest())
    Path(str(archive) + ".wheel").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("explicit", [False, True])
def test_verified_wheel_reuses_matching_config_without_source_codegen(runtime_root, monkeypatch, explicit):
    target = TARGETS[2]
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    target_id = "win32:x86_64:" + target
    _write_wheel_bundle(archive, _manifest(runtime_root, target), target_id)
    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", _forbidden)
    monkeypatch.setattr(provenance, "manifest_codegen_staleness_reason", _forbidden)
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    if explicit:
        monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    result = owned.ensure_target_runtime(
        str(runtime_root), target, packaged_archive=str(archive),
        wheel_matches=lambda candidate: selection.wheel_stamp_matches(candidate, target_id))
    assert result == str(archive)
    assert not (runtime_root / "build_owned").exists()


def test_verified_explicit_wheel_does_not_bypass_thread_config(runtime_root, monkeypatch):
    target = TARGETS[2]
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    target_id = "win32:x86_64:" + target
    # A wheel from the removed single-thread configuration.
    stale = _manifest(runtime_root, target)
    for member in stale["members"]:
        member["runtime_build_config"]["threads"] = False
    _write_wheel_bundle(archive, stale, target_id)
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", _forbidden)
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    with pytest.raises(ValueError, match="target/source configuration"):
        owned.ensure_target_runtime(
            str(runtime_root), target,
            wheel_matches=lambda candidate: selection.wheel_stamp_matches(candidate, target_id))


def test_runtime_dir_override_applies_to_cross_target_cache(runtime_root, tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_RUNTIME_DIR", str(runtime_root))
    target = TARGETS[1]
    expected = runtime_root / "build_owned" / target / "threads-atomic" / "libpy_runtime_pcc_py.a"
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"cached archive")
    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", lambda *_a, **_k: _manifest(runtime_root, target))
    monkeypatch.setattr(provenance, "manifest_codegen_staleness_reason", lambda _receipt: "")
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    assert owned.ensure_target_runtime(str(tmp_path / "absent-default"), target) == str(expected)


def test_fresh_owned_build_must_pass_current_codegen_admission(runtime_root, monkeypatch):
    target = TARGETS[3]
    builds = []

    def build(root, output, selected):
        Path(output).write_bytes(b"simulated stale publication")
        builds.append(output)

    monkeypatch.setattr(owned, "build_runtime_archive", build)
    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", lambda *_a, **_k: _manifest(runtime_root, target))
    monkeypatch.setattr(provenance, "manifest_is_stale_for_current_codegen", lambda _receipt: True)
    with pytest.raises(ValueError, match="stale codegen provenance"):
        owned.ensure_target_runtime(str(runtime_root), target)
    assert len(builds) == 1


def test_native_libpython_request_rejected_before_explicit_fast_path(runtime_root, monkeypatch):
    monkeypatch.setattr(selection, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(runtime_root / "arbitrary.a"))
    monkeypatch.setattr(owned, "ensure_target_runtime", _forbidden)
    options = _selection_options(runtime_root)
    options["needs_libpython"] = True
    with pytest.raises(selection.RuntimeArchiveError, match="does not include libpython"):
        selection.ensure_runtime(False, **options)


@pytest.mark.parametrize("target", TARGETS)
def test_facade_default_native_entry_selects_in_process_owned_builder(runtime_root, monkeypatch, target):
    from pcc.frontends.python import pipeline

    calls = []
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: target)
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(pipeline, "_run_runtime_make", _forbidden)
    monkeypatch.setattr(selection.subprocess, "run", _forbidden)

    def ensure(root, selected, **options):
        calls.append((root, selected, options))
        return str(runtime_root / "selected-owned.a")

    monkeypatch.setattr(owned, "ensure_target_runtime", ensure)
    assert pipeline._ensure_runtime(False) == str(runtime_root / "selected-owned.a")
    assert len(calls) == 1 and calls[0][0:2] == (str(runtime_root), target)
    assert calls[0][2]["packaged_archive"] == pipeline._PY_RUNTIME_ARCHIVE_PCC_PY
    assert calls[0][2]["wheel_matches"] is pipeline._runtime_archive_wheel_stamp_matches


@pytest.mark.parametrize("entry", ["default", "explicit"])
def test_native_make_oracle_rejected_before_runtime_commands(runtime_root, monkeypatch, entry):
    from pcc.frontends.python import pipeline

    monkeypatch.setenv("PCC_RUNTIME_BUILD", "make")
    monkeypatch.setattr(selection, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pcc")))
    monkeypatch.setattr(owned, "ensure_target_runtime", _forbidden)
    monkeypatch.setattr(pipeline, "_run_runtime_make", _forbidden)
    with pytest.raises(pipeline.PyPipelineError, match="native pcc1 requires the owned runtime builder"):
        if entry == "default":
            pipeline._ensure_runtime(False)
        else:
            pipeline._explicit_runtime_archive(str(runtime_root / "unused.a"))


def test_explicit_make_host_reference_is_labelled_and_selected_only_explicitly(runtime_root, monkeypatch, capsys):
    target = "arm64-apple-darwin"
    monkeypatch.setenv("PCC_RUNTIME_BUILD", "make")
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: target)
    monkeypatch.setattr(owned, "ensure_target_runtime", _forbidden)
    calls = []
    monkeypatch.setattr(selection, "_ensure_runtime_make_reference", lambda *args, **kwargs: calls.append(kwargs) or "reference.a")
    assert selection.ensure_runtime(False, **_selection_options(runtime_root)) == "reference.a"
    assert len(calls) == 1
    assert "explicit host runtime reference oracle PCC_RUNTIME_BUILD=make" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["cc", "fallback", "invalid"])
def test_runtime_builder_selector_rejects_unknown_modes_before_construction(runtime_root, monkeypatch, value):
    monkeypatch.setenv("PCC_RUNTIME_BUILD", value)
    monkeypatch.setattr(owned, "ensure_target_runtime", _forbidden)
    with pytest.raises(selection.RuntimeArchiveError, match="invalid PCC_RUNTIME_BUILD"):
        selection.ensure_runtime(False, **_selection_options(runtime_root))


def test_owned_runtime_frontend_uses_runtime_pass_policy_and_restores_application_env(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "application-only")
    monkeypatch.setenv("PCC_RUNTIME_PYTHON_IR_PASSES", "mem2reg,sroa")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    observed = []

    def compile_module(source, output, **options):
        import os
        observed.append((os.environ.get("PCC_PYTHON_IR_PASSES"),
                         os.environ.get("PCC_IMPLICIT_THREAD_SAFEPOINTS"),
                         os.environ.get("PCC_DIRECT_INDEXED_KERNEL_CAPTURE"), options))

    monkeypatch.setattr(pipeline, "compile_python", compile_module)
    owned._compile_runtime_module(owned._THREAD_KERNEL_MODULE, "source.py", str(tmp_path / "module.ll"), TARGETS[3])
    import os
    assert observed[0][0:3] == ("mem2reg,sroa", "0", None)
    assert observed[0][3] == dict(emit_llvm_only=True, python_library=True, libpython_mode="off", backend="self", target_triple=TARGETS[3])
    assert os.environ["PCC_PYTHON_IR_PASSES"] == "application-only"
    assert os.environ["PCC_WITH_THREADS"] == "1"
    assert os.environ["PCC_DIRECT_INDEXED_KERNEL_CAPTURE"] == "1"


def test_simulated_owned_darwin_builder_preserves_public_and_private_capi_inventory(runtime_root, monkeypatch):
    from pcc.backend.macho_obj import Section, TextSymbol, emit_object
    from pcc.backend import owned_object_emit
    from pcc.ir.optimization import driver as optimizer

    with (runtime_root / "Makefile").open("a") as stream:
        stream.write("PCC_RUNTIME_IR_PASSES ?= mem2reg,sroa\n")
    target = TARGETS[3]
    names = owned.runtime_modules(str(runtime_root), target)
    emitted = []

    def compile_member(name, source, output, selected_target):
        assert selected_target == target
        Path(source).parent.mkdir(exist_ok=True)
        Path(source).write_text("# simulated runtime frontend source\n")
        Path(output).write_text("; " + name + "\n")

    def emit_member(text, selected_target):
        assert selected_target == target
        emitted.append(text)
        return emit_object([Section(sectname="__data", segname="__DATA", data=bytes(16),
                                    symbols=(TextSymbol("_PyPublic", 0), TextSymbol("__PyPrivate", 8)))])

    # Parent-local mocks exercise the serial build, not worker processes.
    monkeypatch.setenv("PCC_RUNTIME_BUILD_JOBS", "1")
    monkeypatch.setattr(owned, "_compile_runtime_module", compile_member)
    monkeypatch.setattr(optimizer, "optimize_ir", lambda text, passes: text)
    monkeypatch.setattr(owned_object_emit, "emit_owned_object", emit_member)
    monkeypatch.setattr(selection.subprocess, "run", _forbidden)
    monkeypatch.setattr(provenance, "codegen_checksum", lambda: "0" * 64)
    archive = runtime_root / "simulated.a"
    owned.build_runtime_archive(str(runtime_root), str(archive), target)
    assert len(emitted) == len(names)
    assert Path(str(archive) + ".capi_syms").read_text() == "_PyPublic\n__PyPrivate\n"
    receipt = provenance.verify_runtime_archive_manifest(archive, runtime_root=runtime_root)
    assert owned._manifest_matches_config(receipt, str(runtime_root), target, owned.runtime_build_config())


@pytest.mark.parametrize("tamper", ["target_stamp", "archive", "manifest", "inventory"])
def test_darwin_wheel_shortcut_rejects_wrong_target_or_mutated_payload(runtime_root, monkeypatch, tamper):
    target = TARGETS[3]
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    target_id = "darwin:arm64:" + target
    _write_wheel_bundle(archive, _manifest(runtime_root, target), target_id)
    suffix = {"target_stamp": ".wheel", "archive": "", "manifest": ".provenance.json", "inventory": ".capi_syms"}[tamper]
    path = Path(str(archive) + suffix)
    if tamper == "target_stamp":
        lines = path.read_text().splitlines()
        lines[1] = "target=win32:x86_64:" + TARGETS[2]
        path.write_text("\n".join(lines) + "\n")
    else:
        path.write_bytes(path.read_bytes() + b"tampered")
    assert not selection.wheel_stamp_matches(str(archive), target_id)
    calls = []

    def reject(path, **options):
        calls.append(str(path))
        raise ValueError("unverified wheel must pass ordinary archive admission")

    monkeypatch.setattr(provenance, "verify_runtime_archive_manifest", reject)
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    with pytest.raises(ValueError, match="ordinary archive admission"):
        owned.ensure_target_runtime(str(runtime_root), target, explicit_archive=str(archive),
                                    wheel_matches=lambda candidate: selection.wheel_stamp_matches(candidate, target_id))
    assert calls == [str(archive)]


@pytest.mark.parametrize("config", [
    {"threads": "1", "refcount": "atomic"},
    {"threads": 1, "refcount": "atomic"},
    {"threads": True},
    {"threads": False, "refcount": "../atomic"},
])
def test_runtime_receipt_rejects_malformed_config_before_publication(tmp_path, monkeypatch, config):
    source = tmp_path / "py" / "member.py"
    source.parent.mkdir()
    source.write_text("def member() -> int:\n    return 1\n", encoding="utf-8")
    ir = tmp_path / "member.ll"
    ir.write_text("; diagnostic IR\n", encoding="utf-8")
    obj = tmp_path / "member.o"
    monkeypatch.setattr(provenance, "codegen_checksum", lambda: "0" * 64)
    with pytest.raises(provenance.ProvenanceError, match="runtime"):
        provenance.write_pcc_python_receipt(
            object_path=obj, ir_path=ir, source_path=source, runtime_root=tmp_path,
            target_triple=TARGETS[0], object_bytes=b"owned object",
            runtime_build_config=config)
    assert not Path(str(obj) + ".provenance.json").exists()


def test_runtime_receipt_copies_and_serializes_config(tmp_path, monkeypatch):
    source = tmp_path / "py" / "member.py"
    source.parent.mkdir()
    source.write_text("def member() -> int:\n    return 1\n", encoding="utf-8")
    ir = tmp_path / "member.ll"
    ir.write_text("; diagnostic IR\n", encoding="utf-8")
    obj = tmp_path / "member.o"
    config = {"threads": True, "refcount": "atomic"}
    monkeypatch.setattr(provenance, "codegen_checksum", lambda: "0" * 64)
    receipt = provenance.write_pcc_python_receipt(
        object_path=obj, ir_path=ir, source_path=source, runtime_root=tmp_path,
        target_triple=TARGETS[0], object_bytes=b"owned object",
        runtime_build_config=config)
    config["threads"] = False
    assert receipt["runtime_build_config"] == {"threads": True, "refcount": "atomic"}
    stored = json.loads(Path(str(obj) + ".provenance.json").read_text())
    assert stored["runtime_build_config"] == receipt["runtime_build_config"]


def _write_real_archive(root, target):
    """A small format/provenance-correct inventory; never a runnable runtime."""
    from pcc.backend.ar_writer import write_archive
    from pcc.backend.coff_x86_64 import CoffObject, CoffSection, CoffSymbol, emit_object
    from pcc.backend.elf_x86_64 import (
        ElfObject, ElfSection, ElfSymbol, SHT_PROGBITS, SHF_ALLOC, SHF_WRITE,
        STB_GLOBAL, STT_OBJECT, emit_relocatable,
    )

    objects = []
    members = []
    symbols = []
    config = owned.runtime_build_config()
    for name in owned.runtime_modules(str(root), target, config["threads"]):
        source = root / "py" / (name + ".py")
        source.parent.mkdir(exist_ok=True)
        source.write_text("# provenance fixture\n", encoding="utf-8")
        ir = root / (name + ".ll")
        ir.write_text("; provenance fixture\n", encoding="utf-8")
        symbol = "PyRuntime_" + name
        symbols.append(symbol)
        if "windows" in target:
            data = emit_object(CoffObject(
                (CoffSection(".data", bytes(8), 0xC0000040, align=8),),
                (CoffSymbol(symbol, 1),)))
        elif "darwin" in target:
            from pcc.backend.macho_obj import Section, TextSymbol, emit_object as emit_macho

            data = emit_macho([Section(sectname="__data", segname="__DATA", data=bytes(8),
                                       symbols=(TextSymbol("_" + symbol, 0),))])
            symbols[-1] = "_" + symbol
        else:
            data = emit_relocatable(ElfObject(
                (ElfSection(".data", SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, 8, data=bytes(8)),),
                (ElfSymbol.null(), ElfSymbol(symbol, 1, 0, 8, STB_GLOBAL, STT_OBJECT)),
                machine=183 if target.startswith("aarch64") else 62))
        obj = root / (name + ".o")
        obj.write_bytes(data)
        provenance.write_pcc_python_receipt(
            object_path=obj, ir_path=ir, source_path=source, runtime_root=root,
            target_triple=target, object_emitter="pcc", runtime_build_config=config)
        objects.append(obj)
        members.append((obj.name, data))
    archive = root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(write_archive(members))
    Path(str(archive) + ".capi_syms").write_text("\n".join(sorted(symbols)) + "\n", encoding="ascii")
    provenance.assemble_runtime_archive_manifest(archive, objects, runtime_root=root)
    return archive


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("entry", ["argument-native", "argument-cross", "env-native", "env-cross"])
def test_public_runtime_routes_validate_real_archive_config(runtime_root, monkeypatch, target, entry):
    from pcc.frontends.python import pipeline

    monkeypatch.setattr(provenance, "codegen_checksum", lambda: "0" * 64)
    archive = _write_real_archive(runtime_root, target)
    host = "arm64-apple-darwin" if entry.endswith("cross") else target
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: host)
    monkeypatch.setattr(selection, "sys", SimpleNamespace(platform="win32" if "windows" in host else "linux"))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(owned, "build_runtime_archive", _forbidden)
    if entry.startswith("argument"):
        # Explicit function arguments take precedence without mutating the
        # caller's environment (including a conflicting environment override).
        monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(runtime_root / "wrong-env-archive.a"))

        def select():
            return pipeline._explicit_runtime_archive(
                str(archive), target_triple=target if entry.endswith("cross") else None)
    else:
        monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))

        def select():
            return pipeline._ensure_runtime(
                False, target_triple=target if entry.endswith("cross") else None)
    import os
    previous = os.environ["PCC_RUNTIME_ARCHIVE"]
    assert select() == str(archive)
    # Threads are the one configuration; a different refcount strategy is
    # a configuration the archive was not built with.
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "biased")
    with pytest.raises(pipeline.PyPipelineError, match="target/source configuration"):
        select()
    assert os.environ["PCC_RUNTIME_ARCHIVE"] == previous
