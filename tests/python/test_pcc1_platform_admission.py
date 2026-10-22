"""Receipt/format models only: no compiler, runtime build or native execution.

The tiny ELF/archive fixtures exercise actual owned format/provenance writers.
Their contents are synthetic and never represent a successful native build.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend.ar_writer import _member, write_archive
from pcc.frontends.python import owned_runtime_build
from pcc.frontends.python import pipeline_targets
from pcc.tools import runtime_archive_provenance as provenance
from scripts import verify_pcc1_platform as admission
from scripts import bootstrap_platform as platform_producer


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def _object(name="entry"):
    return elf.emit_relocatable(elf.ElfObject((elf.ElfSection(
        ".text", elf.SHT_PROGBITS, elf.SHF_ALLOC | elf.SHF_EXECINSTR, 16, b"\xc3",
    ),), (elf.ElfSymbol.null(), elf.ElfSymbol(name, 1, 0, 1, elf.STB_GLOBAL, elf.STT_FUNC))))


def _executable():
    data = bytearray(128)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16,
                     2, 62, 1, 0x400078, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0x400000, 0, 128, 128, 4096)
    data[120] = 0xC3
    return bytes(data)


def _runtime_fixture(source, output, *, threads=True):
    runtime = source / "pcc/runtime"
    (runtime / "py").mkdir(parents=True, exist_ok=True)
    (runtime / "Makefile").write_text("PY_MODULES = model\nPCC_RUNTIME_IR_PASSES ?= default\n")
    output.mkdir()
    objects, members = [], []
    for name in owned_runtime_build.runtime_modules(str(runtime), admission.TARGET, threads):
        source_path = runtime / "py" / (name + ".py")
        source_path.write_text("# Synthetic receipt test input, never compiled.\n")
        ir = output / (name + ".ll")
        ir.write_text("; Synthetic receipt test input\n")
        obj = output / (name + ".o")
        obj.write_bytes(_object(name))
        provenance.write_pcc_python_receipt(
            object_path=obj, ir_path=ir, source_path=source_path, runtime_root=runtime,
            target_triple=admission.TARGET, runtime_build_config={"threads": threads, "refcount": "atomic"},
        )
        objects.append(obj)
        members.append((obj.name, obj.read_bytes()))
    archive = output / "libpy_runtime_pcc_py.a"
    archive.write_bytes(write_archive(members))
    Path(str(archive) + ".capi_syms").write_bytes(b"PyModel\n")
    provenance.assemble_runtime_archive_manifest(archive, objects, runtime_root=runtime)
    return archive


def test_runtime_fixture_inventory_is_canonical_with_windows_text_defaults(tmp_path, monkeypatch):
    write_text = Path.write_text

    def windows_write_text(path, data, encoding=None, errors=None, newline=None):
        return write_text(path, data, encoding=encoding, errors=errors,
                          newline="\r\n" if newline is None else newline)

    # Exercise Windows text translation on every host, including source/IR
    # receipts. The byte-defined inventory must not depend on text defaults.
    monkeypatch.setattr(Path, "write_text", windows_write_text)
    source = tmp_path / "source"
    archive = _runtime_fixture(source, tmp_path / "runtime")
    inventory = Path(str(archive) + ".capi_syms")
    assert inventory.read_bytes() == b"PyModel\n"
    runtime = source / "pcc/runtime"
    assert b"\r\n" in (runtime / "Makefile").read_bytes()
    manifest = provenance.verify_runtime_archive_manifest(archive, runtime_root=runtime)
    assert manifest["capi_symbols"] == ["PyModel"]
    assert manifest["capi_inventory_sha256"] == hashlib.sha256(b"PyModel\n").hexdigest()
    for malformed in (b"PyModel\r\n", b"PyModel"):
        inventory.write_bytes(malformed)
        with pytest.raises(provenance.ProvenanceError, match="not canonical newline-delimited content"):
            provenance.verify_runtime_archive_manifest(archive, runtime_root=runtime)


@pytest.fixture
def product(tmp_path, monkeypatch):
    """Run the real producer orchestration with synthetic external execution."""
    source = tmp_path / "core"
    (source / "scripts").mkdir(parents=True)
    (source / "pcc").mkdir()
    (source / "pcc/__main__.py").write_text("# Synthetic compiler source\n")
    for name in ("bootstrap_platform.py", "platform_process_watchdog.py"):
        shutil.copyfile(admission.CORE_ROOT / "scripts" / name, source / "scripts" / name)
    archive = _runtime_fixture(source, tmp_path / "runtime")
    monkeypatch.setattr(admission, "CORE_ROOT", source)
    monkeypatch.setattr(platform_producer, "ROOT", source)
    # The fixture source is intentionally tiny. Source hashing and the actual
    # runtime verifier execute; only checkout/host location is modeled here.
    monkeypatch.setattr(admission, "_host_source", lambda path: Path(path))
    # Execution is modeled below; the producer also models its host as the
    # admitted target so this contract runs on any machine.
    monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: admission.TARGET)
    for key, value in {"PCC_GC_BACKEND": "0", "PCC_WITH_THREADS": "1", "PCC_REFCOUNT_KIND": "atomic"}.items():
        monkeypatch.setenv(key, value)

    def git(command, **kwargs):
        assert command[:7] == ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--"]
        assert tuple(command[7:]) == admission.SOURCE_INPUTS
        assert Path(kwargs["cwd"]) == source
        names = sorted(str(path.relative_to(source)) for path in source.rglob("*") if path.is_file())
        return b"\0".join(name.encode() for name in names) + b"\0"

    monkeypatch.setattr(platform_producer.subprocess, "check_output", git)
    monkeypatch.setattr(platform_producer, "source_identity", lambda *args: {
        "commit": "f" * 40, "clean_commit": True, **platform_producer._source_snapshot(),
    })
    calls = []

    def modeled_execution(command, **kwargs):
        calls.append(command)
        log = Path(kwargs["log_path"])
        if "-o" in command:
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(_executable())
            output.chmod(0o755)
            text = ""
        else:
            text = "0\n" + str(2**80 + 42) + "\n"
        log.write_text(text)
        log.with_suffix(".resources.jsonl").write_text("{\"elapsed\":0,\"rss\":1,\"processes\":[]}\n")
        return {"seconds": 1.0, "peak_tree_rss": 1, "log": str(log),
                "command": command, "launched_command": command}

    monkeypatch.setattr(platform_producer, "run", modeled_execution)
    directory = tmp_path / "stage1"
    receipt = platform_producer.run_chain("0", directory, stage_limit=1,
                                         runtime_archive=archive, lock_held=True)
    assert len(calls) == 3  # modeled Stage1, native canary compile, canary run
    assert receipt["qualified"] is receipt["fixed_point"] is False
    return SimpleNamespace(source=source, directory=directory, receipt=directory / "receipt.json",
                           compiler=directory / "pcc1", runtime=archive)


def _admit(product, **overrides):
    args = dict(compiler=product.compiler, runtime_archive=product.runtime,
                source_root=product.source, gc_backend=0, threads=True)
    args.update(overrides)
    return admission.verify_platform_compiler(product.receipt, **args)


def _change(path, edit):
    receipt = json.loads(path.read_text())
    edit(receipt)
    _write(path, receipt)


def test_actual_platform_producer_and_consumer_contract(product, monkeypatch):
    monkeypatch.setattr(platform_producer, "run", lambda *args, **kwargs: pytest.fail("producer executed"))
    monkeypatch.setattr(owned_runtime_build, "ensure_target_runtime",
                        lambda *args, **kwargs: pytest.fail("runtime provisioning attempted"))
    before = {str(path): path.read_bytes() for path in product.directory.rglob("*") if path.is_file()}
    result = _admit(product)
    assert result["target"] == admission.TARGET
    assert result["source_files"]["scripts/bootstrap_platform.py"]
    assert result["runtime"]["runtime_build_config"] == {"threads": True, "refcount": "atomic"}
    assert str(product.directory / "canary1") in result["files"]
    assert "fixed point and application benchmark unproven" in result["qualification"]
    assert before == {str(path): path.read_bytes() for path in product.directory.rglob("*") if path.is_file()}


@pytest.mark.parametrize("profile", [
    {"threads": False}, {"gc_backend": 1}, {"refcount": "local"},
    {"gc_backend": True}, {"threads": 0}, {"gc_backend": 5},
])
def test_rejects_wrong_or_untyped_profile(product, profile):
    with pytest.raises(admission.AdmissionError):
        _admit(product, **profile)


@pytest.mark.parametrize("relative", ["pcc/__main__.py", "scripts/bootstrap_platform.py"])
def test_rejects_changed_source(product, relative):
    (product.source / relative).write_text("# changed\n")
    with pytest.raises(admission.AdmissionError, match="source"):
        _admit(product)


def test_rejects_added_source(product):
    (product.source / "pcc/extra.py").write_text("# extra\n")
    with pytest.raises(admission.AdmissionError, match="source"):
        _admit(product)


@pytest.mark.parametrize("artifact", ["pcc1", "canary.py", "canary1", "canary1-run.log"])
def test_rejects_tampered_compiler_or_canary(product, artifact):
    with (product.directory / artifact).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(admission.AdmissionError):
        _admit(product)


@pytest.mark.parametrize("field,value", [
    ("qualified", True), ("fixed_point", True), ("target", "aarch64-unknown-linux-gnu"),
    ("schema", "pcc.stage1-build-receipt.v3"), ("stages", []), ("gc", "4"),
])
def test_rejects_unrelated_or_promoted_receipts(product, field, value):
    _change(product.receipt, lambda receipt: receipt.update({field: value}))
    with pytest.raises(admission.AdmissionError):
        _admit(product)


@pytest.mark.parametrize("field", sorted(admission.RECEIPT_FIELDS))
def test_rejects_missing_producer_field(product, field):
    _change(product.receipt, lambda receipt: receipt.pop(field))
    with pytest.raises(admission.AdmissionError):
        _admit(product)


@pytest.mark.parametrize("field", sorted(admission.STAGE_FIELDS))
def test_rejects_missing_stage_field(product, field):
    _change(product.receipt, lambda receipt: receipt["stages"][0].pop(field))
    with pytest.raises(admission.AdmissionError):
        _admit(product)


@pytest.mark.parametrize("field", ["path", "sha256", "provenance_sha256", "capi_inventory_sha256"])
def test_rejects_missing_producer_runtime_field(product, field):
    _change(product.receipt, lambda receipt: receipt["runtime"].pop(field))
    with pytest.raises(admission.AdmissionError):
        _admit(product)


@pytest.mark.parametrize("field,value", [
    ("worker_jobs", True), ("worker_jobs", 0), ("tree_rss_limit", True), ("tree_rss_limit", 0),
    ("qualification_scope", "fully qualified"), ("execution_owner_evidence", {}), ("error", "failed"),
])
def test_rejects_incomplete_or_unsupported_top_level_evidence(product, field, value):
    _change(product.receipt, lambda receipt: receipt.update({field: value}))
    with pytest.raises(admission.AdmissionError):
        _admit(product)


@pytest.mark.parametrize("field,value", [
    ("reused", True), ("stage", True), ("seconds", -1), ("peak_tree_rss", True),
    ("sha256", "a" * 64), ("command", ["python", "other.py"]),
    ("input_compiler_sha256", "unknown"), ("input_compiler_sha256", "A" * 64),
    ("peak_tree_rss", 17179869185), ("canary_status", "pending"),
    ("seconds", 10**500),
])
def test_rejects_unbound_or_incomplete_stage(product, field, value):
    _change(product.receipt, lambda receipt: receipt["stages"][0].update({field: value}))
    with pytest.raises(admission.AdmissionError):
        _admit(product)


@pytest.mark.parametrize("phase", ["compile", "run"])
def test_rejects_wrong_native_canary_command(product, phase):
    _change(product.receipt, lambda receipt: receipt["stages"][0]["canary"][phase].update({"command": ["other"]}))
    with pytest.raises(admission.AdmissionError, match="execution record"):
        _admit(product)


def test_rejects_wrong_canary_output_even_with_matching_hash(product):
    log = product.directory / "canary1-run.log"
    log.write_text("0\n42\n")
    _change(product.receipt, lambda receipt: receipt["stages"][0]["canary"].update({
        "output_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
    }))
    with pytest.raises(admission.AdmissionError, match="output mismatch"):
        _admit(product)


def test_rejects_print_only_canary_even_with_matching_source_hash(product):
    source = product.directory / "canary.py"
    source.write_text('print("0\\n1208925819614629174706218")\n')
    _change(product.receipt, lambda receipt: receipt["stages"][0]["canary"].update({
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }))
    with pytest.raises(admission.AdmissionError, match="canonical barrier"):
        _admit(product)


@pytest.mark.parametrize("suffix", ["", ".provenance.json", ".capi_syms"])
def test_rejects_changed_runtime_bundle(product, suffix):
    with Path(str(product.runtime) + suffix).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(admission.AdmissionError):
        _admit(product)


def test_runtime_profile_verifier_checks_actual_manifest(product):
    with pytest.raises(admission.AdmissionError, match="thread/refcount profile"):
        admission.verify_runtime_archive(product.runtime, source_root=product.source,
                                         threads=True, refcount="local")
    result = admission.verify_runtime_archive(product.runtime, source_root=product.source, threads=True)
    assert result["runtime_build_config"] == {"threads": True, "refcount": "atomic"}
    assert result["source_identity"] == platform_producer.source_identity()
    assert "scripts/bootstrap_platform.py" in result["source_files"]
    assert str(product.source / "scripts/bootstrap_platform.py") in result["files"]


def test_runtime_admits_exact_threaded_member_profile(product, tmp_path):
    archive = _runtime_fixture(product.source, tmp_path / "threaded-runtime", threads=True)
    result = admission.verify_runtime_archive(archive, source_root=product.source, threads=True)
    assert result["runtime_build_config"] == {"threads": True, "refcount": "atomic"}
    with pytest.raises(admission.AdmissionError, match="single-thread runtime kernel was removed"):
        admission.verify_runtime_archive(archive, source_root=product.source, threads=False)


def test_runtime_rejects_stale_process_codegen_cache(product, monkeypatch):
    monkeypatch.setitem(provenance._CODEGEN_CHECKSUM_CACHE, "value", "a" * 64)
    with pytest.raises(admission.AdmissionError, match="cached codegen identity"):
        admission.verify_runtime_archive(product.runtime, source_root=product.source, threads=True)


@pytest.mark.parametrize("truncated", [b"\x7fELF\x02\x01\x01", b"\x7fELF\x02\x01\x01" + b"\0" * 57])
def test_runtime_rejects_self_consistent_malformed_elf_member(product, truncated):
    payloads = elf.read_archive_payloads(product.runtime.read_bytes())
    name, _ = payloads[0]
    # The production archive writer rightly rejects malformed objects; use
    # only its framing primitive to construct this deliberate negative model.
    archive = b"!<arch>\n"
    for member_name, payload in [(name, truncated), *payloads[1:]]:
        archive += _member(member_name, payload, len(archive))
    product.runtime.write_bytes(archive)
    path = Path(str(product.runtime) + ".provenance.json")
    manifest = json.loads(path.read_text())
    manifest["members"][0]["object_sha256"] = hashlib.sha256(truncated).hexdigest()
    manifest["members_sha256"] = provenance._members_sha256(manifest["members"])
    _write(path, manifest)
    with pytest.raises(admission.AdmissionError, match="ELF"):
        admission.verify_runtime_archive(product.runtime, source_root=product.source, threads=True)


def test_runtime_rejects_dirty_source_checkout(product, monkeypatch):
    source = platform_producer.source_identity()
    monkeypatch.setattr(platform_producer, "source_identity", lambda: {**source, "clean_commit": False})
    with pytest.raises(admission.AdmissionError, match="not clean"):
        admission.verify_runtime_archive(product.runtime, source_root=product.source, threads=True)


def test_runtime_rejects_source_changed_during_admission(product, monkeypatch):
    original = admission._runtime

    def mutate(*args):
        result = original(*args)
        (product.source / "pcc/__main__.py").write_text("# changed after initial capture\n")
        return result

    monkeypatch.setattr(admission, "_runtime", mutate)
    with pytest.raises(admission.AdmissionError, match="source changed"):
        admission.verify_runtime_archive(product.runtime, source_root=product.source, threads=True)


def test_runtime_rejects_duplicate_manifest_fields(product):
    path = Path(str(product.runtime) + ".provenance.json")
    text = path.read_text()
    path.write_text(text.replace('"schema":', '"schema":"ignored","schema":', 1))
    with pytest.raises(admission.AdmissionError, match="duplicate"):
        admission.verify_runtime_archive(product.runtime, source_root=product.source, threads=True)


@pytest.mark.parametrize("field,value,diagnostic", [
    ("codegen_checksum", "f" * 64, "stale for current codegen"),
    ("target_triple", "aarch64-unknown-linux-gnu", "target, member inventory"),
])
def test_runtime_rejects_self_consistent_wrong_producer_or_target(product, field, value, diagnostic):
    path = Path(str(product.runtime) + ".provenance.json")
    manifest = json.loads(path.read_text())
    for member in manifest["members"]:
        member[field] = value
    if field == "target_triple":
        manifest[field] = value
    manifest["members_sha256"] = provenance._members_sha256(manifest["members"])
    _write(path, manifest)
    with pytest.raises(admission.AdmissionError, match=diagnostic):
        admission.verify_runtime_archive(product.runtime, source_root=product.source, threads=True)


def test_rejects_missing_canary_artifact(product):
    (product.directory / "canary1").unlink()
    with pytest.raises(admission.AdmissionError):
        _admit(product)


def test_rejects_symlinked_receipt(product):
    moved = product.directory / "original-receipt.json"
    product.receipt.rename(moved)
    product.receipt.symlink_to(moved)
    with pytest.raises(admission.AdmissionError, match="symlink"):
        _admit(product)


def test_rejects_duplicate_json_fields(product):
    text = product.receipt.read_text()
    product.receipt.write_text(text.replace('"gc":', '"gc":"0","gc":', 1))
    with pytest.raises(admission.AdmissionError, match="duplicate"):
        _admit(product)


def test_artifacts_can_be_relocated_without_rewriting_receipt(product, tmp_path):
    moved = tmp_path / "copied-product"
    shutil.copytree(product.directory, moved / "stage1")
    shutil.copytree(product.runtime.parent, moved / "runtime")
    product.directory = moved / "stage1"
    product.receipt = product.directory / "receipt.json"
    product.compiler = product.directory / "pcc1"
    product.runtime = moved / "runtime/libpy_runtime_pcc_py.a"
    assert _admit(product)["compiler"]["path"] == str(product.compiler)


@pytest.mark.parametrize("offset,format,value", [
    (4, "B", 1), (5, "B", 2), (7, "B", 9), (8, "B", 1),
    (16, "H", 1), (18, "H", 183), (20, "I", 0), (48, "I", 1),
    (54, "H", 55), (56, "H", 65535), (32, "Q", 127),
    (64, "I", 2), (64, "I", 3), (96, "Q", 129), (104, "Q", 127),
    (112, "Q", 3), (24, "Q", 0x500000), (68, "I", 6),
])
def test_elf_machine_abi_and_bounds_fail_closed(offset, format, value):
    data = bytearray(_executable())
    struct.pack_into("<" + format, data, offset, value)
    with pytest.raises(admission.AdmissionError):
        admission._static_elf64(data)


@pytest.mark.parametrize("system,machine", [("darwin", "arm64"), ("linux", "aarch64"), ("win32", "AMD64")])
def test_admission_does_not_broaden_other_platforms(monkeypatch, system, machine):
    monkeypatch.setattr(admission.sys, "platform", system)
    monkeypatch.setattr(admission.platform, "machine", lambda: machine)
    with pytest.raises(admission.AdmissionError, match="host ABI"):
        admission._host_source(admission.CORE_ROOT)


def test_capture_rejects_input_changed_between_checks(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"old")
    files = admission._Files()
    files.add(path)
    path.write_bytes(b"new")
    with pytest.raises(admission.AdmissionError, match="changed"):
        files.verify()
