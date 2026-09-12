from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

from llvmlite import binding as llvm
import pytest

import pcc.tools.ir_to_obj as ir_to_obj_module
import pcc.tools.runtime_archive_provenance as provenance_module
from pcc.tools.ir_to_obj import emit_object, main as ir_to_obj_main
from pcc.tools.runtime_archive_provenance import (
    MANIFEST_SCHEMA,
    PRODUCTION_POLICY,
    RECEIPT_SCHEMA,
    ProvenanceError,
    assemble_runtime_archive_manifest,
    capi_inventory_path_for_archive,
    manifest_is_stale_for_current_codegen,
    receipt_path_for_object,
    verify_runtime_archive_manifest,
    write_pcc_python_receipt,
)

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "pcc" / "py_runtime"
_TEST_CAPI_SYMBOLS = ["PyRuntime_TestAnchor", "_PyRuntime_TestInternal"]


def _write_test_capi_inventory(archive: Path, symbols=None) -> Path:
    inventory = capi_inventory_path_for_archive(archive)
    values = _TEST_CAPI_SYMBOLS if symbols is None else list(symbols)
    inventory.write_text("\n".join(values) + "\n", encoding="ascii")
    return inventory


def _test_capi_manifest_fields() -> dict[str, object]:
    content = ("\n".join(_TEST_CAPI_SYMBOLS) + "\n").encode("ascii")
    return {
        "capi_symbol_count": len(_TEST_CAPI_SYMBOLS),
        "capi_inventory_sha256": hashlib.sha256(content).hexdigest(),
        "capi_symbols": list(_TEST_CAPI_SYMBOLS),
    }


def test_runtime_cache_identity_distinguishes_owned_and_oracle_emitters(monkeypatch):
    from tests import runtime_build_cache

    compiler = REPO / ".venv/bin/pcc"
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "pcc")
    owned = runtime_build_cache._pcc_runtime_cache_key(compiler, variant="pcc-py")
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "llvmlite")
    oracle = runtime_build_cache._pcc_runtime_cache_key(compiler, variant="pcc-py")
    assert owned != oracle


def test_ir_to_obj_initializes_native_inline_asm_parser(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "llvmlite")
    target_triple = llvm.get_default_triple()
    ir_path = tmp_path / "native-inline-asm.ll"
    object_path = tmp_path / "native-inline-asm.o"
    ir_path.write_text(
        f'target triple = "{target_triple}"\n'
        "define void @native_inline_asm_probe() {\n"
        '  call void asm sideeffect "nop", "~{memory}"()\n'
        "  ret void\n"
        "}\n",
        encoding="utf-8",
    )

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcc.tools.ir_to_obj",
            str(ir_path),
            str(object_path),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert process.returncode == 0, process.stdout + process.stderr
    assert object_path.stat().st_size > 0


def _foreign_target_triple() -> str:
    native_arch = llvm.get_triple_parts(llvm.get_default_triple()).Arch
    if native_arch in ("x86", "x86_64"):
        return "aarch64-unknown-linux-gnu"
    return "x86_64-unknown-linux-gnu"


def _run_ir_to_obj(
    ir_path: Path,
    object_path: Path,
    *,
    target: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "pcc.tools.ir_to_obj",
    ]
    if target is not None:
        command.extend(("--target", target))
    command.extend((str(ir_path), str(object_path)))
    return subprocess.run(
        command,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_ir_to_obj_rejects_explicit_target_module_triple_mismatch(
    tmp_path: Path,
) -> None:
    module_triple = llvm.get_default_triple()
    requested_triple = _foreign_target_triple()
    ir_path = tmp_path / "target-mismatch.ll"
    object_path = tmp_path / "target-mismatch.o"
    ir_path.write_text(
        f'target triple = "{module_triple}"\n'
        "define i32 @target_mismatch_probe() {\n"
        "  ret i32 7\n"
        "}\n",
        encoding="utf-8",
    )

    process = _run_ir_to_obj(
        ir_path,
        object_path,
        target=requested_triple,
    )

    assert process.returncode == 1
    assert "ir_to_obj: target triple mismatch:" in process.stderr
    assert module_triple in process.stderr
    assert requested_triple in process.stderr
    assert not object_path.exists()


def test_ir_to_obj_rejects_target_data_layout_mismatch(tmp_path: Path) -> None:
    target_triple = _foreign_target_triple()
    ir_path = tmp_path / "data-layout-mismatch.ll"
    object_path = tmp_path / "data-layout-mismatch.o"
    ir_path.write_text(
        'target datalayout = "e-p:32:32"\n'
        f'target triple = "{target_triple}"\n'
        "define i32 @data_layout_mismatch_probe() {\n"
        "  ret i32 11\n"
        "}\n",
        encoding="utf-8",
    )

    process = _run_ir_to_obj(
        ir_path,
        object_path,
        target=target_triple,
    )

    assert process.returncode == 1
    assert "ir_to_obj: target data layout mismatch" in process.stderr
    assert target_triple in process.stderr
    assert "e-p:32:32" in process.stderr
    assert not object_path.exists()


def test_ir_to_obj_rejects_foreign_target_inline_asm_before_llvm_emission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # This parser-registration limit belongs to the explicit LLVM oracle.
    # Owned cross-target assembly does not consult LLVM's native parser.
    monkeypatch.setenv("PCC_IR_TO_OBJ_EMITTER", "llvmlite")
    target_triple = _foreign_target_triple()
    ir_path = tmp_path / "foreign-inline-asm.ll"
    object_path = tmp_path / "foreign-inline-asm.o"
    ir_path.write_text(
        f'target triple = "{target_triple}"\n'
        "define void @foreign_inline_asm_probe() {\n"
        '  call void asm sideeffect "nop", "~{memory}"()\n'
        "  ret void\n"
        "}\n",
        encoding="utf-8",
    )

    process = _run_ir_to_obj(
        ir_path,
        object_path,
        target=target_triple,
    )

    assert process.returncode == 1
    assert (
        "ir_to_obj: foreign-target inline assembly is unsupported:"
        in process.stderr
    )
    assert target_triple in process.stderr
    assert llvm.get_default_triple() in process.stderr
    assert not object_path.exists()


def test_ir_to_obj_allows_ordinary_foreign_target_ir_without_inline_asm(
    tmp_path: Path,
) -> None:
    target_triple = _foreign_target_triple()
    ir_path = tmp_path / "foreign-ordinary.ll"
    object_path = tmp_path / "foreign-ordinary.o"
    ir_path.write_text(
        f'target triple = "{target_triple}"\n'
        "define i32 @foreign_ordinary_probe() {\n"
        "  ret i32 13\n"
        "}\n",
        encoding="utf-8",
    )

    process = _run_ir_to_obj(
        ir_path,
        object_path,
        target=target_triple,
    )

    assert process.returncode == 0, process.stdout + process.stderr
    assert object_path.stat().st_size > 0


def _archive(path: Path, *objects: Path) -> None:
    process = subprocess.run(
        ["ar", "rcs", str(path), *(str(obj) for obj in objects)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stdout + process.stderr


def _fake_ar_listing(
    executable: Path,
    *,
    member: str,
    invocation_log: Path,
) -> None:
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$1\" >> {shlex.quote(str(invocation_log))}\n"
        'if [ "$1" = "t" ]; then\n'
        f"  printf '%s\\n' {shlex.quote(member)}\n"
        "  exit 0\n"
        "fi\n"
        "exit 97\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _build_provenanced_archive(
    root: Path,
    *,
    member_stems: tuple[str, ...] = ("member",),
) -> tuple[Path, Path, list[Path], dict[str, object]]:
    runtime_root = root / "pcc" / "py_runtime"
    source_root = runtime_root / "py"
    build_root = runtime_root / "build_py"
    source_root.mkdir(parents=True)
    build_root.mkdir(parents=True)
    target_triple = llvm.get_default_triple()
    objects: list[Path] = []
    for value, stem in enumerate(member_stems, start=1):
        source = source_root / f"{stem}.py"
        ir_path = build_root / f"{stem}.ll"
        object_path = build_root / f"{stem}.o"
        source.write_text(
            f"def {stem}() -> int:\n    return {value}\n",
            encoding="utf-8",
        )
        ir_text = (
            f'target triple = "{target_triple}"\n'
            f"define i32 @{stem}() {{\n  ret i32 {value}\n}}\n"
        )
        ir_path.write_text(ir_text, encoding="utf-8")
        object_bytes = emit_object(ir_text)
        object_path.write_bytes(object_bytes)
        write_pcc_python_receipt(
            object_path=object_path,
            ir_path=ir_path,
            source_path=source,
            runtime_root=runtime_root,
            target_triple=target_triple,
            object_bytes=object_bytes,
        )
        objects.append(object_path)
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    _archive(archive, *objects)
    _write_test_capi_inventory(archive)
    manifest = assemble_runtime_archive_manifest(
        archive,
        objects,
        runtime_root=runtime_root,
    )
    return runtime_root, archive, objects, manifest


def test_manifest_validation_does_not_start_external_tools(tmp_path, monkeypatch):
    runtime_root, archive, objects, expected = _build_provenanced_archive(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("runtime archive validation started an external tool")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert assemble_runtime_archive_manifest(
        archive, objects, runtime_root=runtime_root,
    ) == expected
    assert verify_runtime_archive_manifest(
        archive, runtime_root=runtime_root,
    ) == expected


def test_runtime_selection_checks_do_not_resolve_host_python(tmp_path):
    from pcc.py_frontend.pipeline_runtime_archive import (
        provenance_codegen_stale, provenance_valid,
    )

    runtime_root, archive, objects, expected = _build_provenanced_archive(tmp_path)

    def forbidden():
        pytest.fail("runtime validation tried to resolve a host Python")

    assert provenance_valid(
        str(archive), runtime_dir=str(runtime_root),
        pcc_source_root=forbidden, host_python_command=forbidden,
    )
    assert not provenance_codegen_stale(
        str(archive), pcc_source_root=forbidden, host_python_command=forbidden,
    )


def test_codegen_freshness_is_fail_closed_and_aggregates_all_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "a" * 64
    monkeypatch.setattr(provenance_module, "codegen_checksum", lambda: current)

    assert not manifest_is_stale_for_current_codegen(
        {
            "members": [
                {"codegen_checksum": current},
                {"codegen_checksum": current},
            ]
        }
    )
    assert manifest_is_stale_for_current_codegen(
        {
            "members": [
                {"codegen_checksum": current},
                {"codegen_checksum": "b" * 64},
            ]
        }
    )
    assert manifest_is_stale_for_current_codegen(
        {"members": [{"codegen_checksum": "unknown"}]}
    )
    assert manifest_is_stale_for_current_codegen({"members": [{}]})
    assert manifest_is_stale_for_current_codegen({"members": []})
    assert manifest_is_stale_for_current_codegen(None)


def test_codegen_freshness_is_separate_from_manifest_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, archive, _objects, manifest = _build_provenanced_archive(tmp_path)
    manifest_path = Path(str(archive) + ".provenance.json")
    monkeypatch.setattr(
        provenance_module,
        "codegen_checksum",
        lambda: "a" * 64,
    )

    manifest["members"][0]["codegen_checksum"] = "b" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    verified = verify_runtime_archive_manifest(archive, runtime_root=runtime_root)
    assert manifest_is_stale_for_current_codegen(verified)

    del manifest["members"][0]["codegen_checksum"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    verified = verify_runtime_archive_manifest(archive, runtime_root=runtime_root)
    assert manifest_is_stale_for_current_codegen(verified)


@pytest.mark.parametrize("archive_header", [b"not-an-archive", b"!<thin>\n"])
def test_non_regular_archive_is_rejected_before_ar_is_invoked(
    tmp_path: Path,
    archive_header: bytes,
) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    runtime_root.mkdir(parents=True)
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(archive_header)

    with pytest.raises(ProvenanceError, match="regular ar archive"):
        assemble_runtime_archive_manifest(
            archive,
            [runtime_root / "build_py" / "member.o"],
            runtime_root=runtime_root,
            ar=str(tmp_path / "ar-must-not-run"),
        )


@pytest.mark.parametrize(
    "unsafe_member",
    ["../escape.o", "/tmp/escape.o", "nested/member.o", r"nested\member.o"],
)
def test_unsafe_archive_member_is_rejected_before_extraction(
    tmp_path: Path,
    unsafe_member: str,
) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    runtime_root.mkdir(parents=True)
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    name = unsafe_member.encode()
    payload = name + b"object"
    header = (
        ("#1/" + str(len(name))).encode().ljust(16)
        + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
        + b"100644".ljust(8) + str(len(payload)).encode().ljust(10) + b"`\n"
    )
    archive.write_bytes(
        b"!<arch>\n" + header + payload + (b"\n" if len(payload) % 2 else b"")
    )
    invocation_log = tmp_path / "ar-invocations.txt"
    fake_ar = tmp_path / "fake-ar"
    _fake_ar_listing(fake_ar, member=unsafe_member, invocation_log=invocation_log)
    target_triple = llvm.get_default_triple()
    record = {
        "schema": RECEIPT_SCHEMA,
        "member": unsafe_member,
        "object_sha256": "0" * 64,
        "ir_sha256": "0" * 64,
        "source": "pcc/py_runtime/py/member.py",
        "source_sha256": "0" * 64,
        "source_kind": "pcc-python",
        "producer_kind": "pcc-python-library-ir-to-obj",
        "object_emitter": "llvmlite-target-machine",
        "uses_host_cc": False,
        "target_triple": target_triple,
    }
    receipt_path = Path(str(archive) + ".provenance.json")
    _write_test_capi_inventory(archive)
    receipt_path.write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "archive": archive.name,
                "policy": PRODUCTION_POLICY,
                "target_triple": target_triple,
                "member_count": 1,
                "members_sha256": "0" * 64,
                "members": [record],
                **_test_capi_manifest_fields(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="unsafe archive member"):
        verify_runtime_archive_manifest(
            archive,
            runtime_root=runtime_root,
            ar=str(fake_ar),
        )

    assert not invocation_log.exists()


def test_pcc_python_archive_manifest_round_trips_without_build_paths(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "checkout" / "pcc" / "py_runtime"
    source = runtime_root / "py" / "demo_runtime.py"
    object_path = runtime_root / "build_py" / "demo_runtime.o"
    ir_path = runtime_root / "build_py" / "demo_runtime.ll"
    source.parent.mkdir(parents=True)
    object_path.parent.mkdir(parents=True)
    source.write_text("def demo() -> int:\n    return 7\n", encoding="utf-8")
    target_triple = llvm.get_default_triple()
    ir_text = (
        'source_filename = "<string>"\n'
        f'target triple = "{target_triple}"\n'
        "define i32 @demo() {\n"
        "  ret i32 7\n"
        "}\n"
    )
    ir_path.write_text(ir_text, encoding="utf-8")
    object_bytes = emit_object(ir_text)
    object_path.write_bytes(object_bytes)

    write_pcc_python_receipt(
        object_path=object_path,
        ir_path=ir_path,
        source_path=source,
        runtime_root=runtime_root,
        target_triple=target_triple,
        object_bytes=object_bytes,
    )
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    _archive(archive, object_path)
    _write_test_capi_inventory(archive)

    manifest = assemble_runtime_archive_manifest(
        archive,
        [object_path],
        runtime_root=runtime_root,
    )
    verified = verify_runtime_archive_manifest(
        archive,
        runtime_root=runtime_root,
    )

    assert verified == manifest
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["archive"] == "libpy_runtime_pcc_py.a"
    assert manifest["target_triple"] == target_triple
    assert manifest["member_count"] == 1
    assert manifest["capi_symbol_count"] == len(_TEST_CAPI_SYMBOLS)
    assert manifest["capi_symbols"] == _TEST_CAPI_SYMBOLS
    assert manifest["members"][0]["member"] == "demo_runtime.o"
    assert manifest["members"][0]["source"] == "pcc/py_runtime/py/demo_runtime.py"
    serialized = json.dumps(manifest, sort_keys=True)
    assert str(tmp_path) not in serialized


@pytest.mark.parametrize(
    ("damage", "replacement"),
    [
        ("stale", "PyRuntime_StaleAnchor\n_PyRuntime_StaleInternal\n"),
        ("truncated", "PyRuntime_TestAnchor\n"),
        ("replaced", "PyRuntime_ReplacedAnchor\n_PyRuntime_ReplacedInternal\n"),
    ],
)
def test_verified_archive_rejects_changed_capi_inventory(
    tmp_path: Path,
    damage: str,
    replacement: str,
) -> None:
    runtime_root, archive, _, _ = _build_provenanced_archive(tmp_path)
    capi_inventory_path_for_archive(archive).write_text(
        replacement,
        encoding="ascii",
    )

    with pytest.raises(ProvenanceError, match="C-API anchor inventory"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_verified_archive_rejects_missing_capi_inventory(tmp_path: Path) -> None:
    runtime_root, archive, _, _ = _build_provenanced_archive(tmp_path)
    capi_inventory_path_for_archive(archive).unlink()

    with pytest.raises(ProvenanceError, match="cannot read C-API anchor inventory"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_manifest_assembly_refuses_partial_capi_inventory_publication(
    tmp_path: Path,
) -> None:
    runtime_root, archive, objects, _ = _build_provenanced_archive(tmp_path)
    capi_inventory_path_for_archive(archive).unlink()

    with pytest.raises(ProvenanceError, match="cannot read C-API anchor inventory"):
        assemble_runtime_archive_manifest(
            archive,
            objects,
            runtime_root=runtime_root,
        )


def test_manifest_assembly_rejects_noncanonical_capi_inventory_order(
    tmp_path: Path,
) -> None:
    runtime_root, archive, objects, _ = _build_provenanced_archive(tmp_path)
    capi_inventory_path_for_archive(archive).write_text(
        "_PyRuntime_TestInternal\nPyRuntime_TestAnchor\n",
        encoding="ascii",
    )

    with pytest.raises(ProvenanceError, match="bytewise symbol order"):
        assemble_runtime_archive_manifest(
            archive,
            objects,
            runtime_root=runtime_root,
        )


def test_manifest_cannot_independently_replace_capi_inventory_contract(
    tmp_path: Path,
) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    replacement_symbols = ["PyRuntime_ManifestReplacement"]
    replacement = ("\n".join(replacement_symbols) + "\n").encode("ascii")
    manifest["capi_symbol_count"] = len(replacement_symbols)
    manifest["capi_inventory_sha256"] = hashlib.sha256(replacement).hexdigest()
    manifest["capi_symbols"] = replacement_symbols
    manifest_path = Path(str(archive) + ".provenance.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="differs from its manifest"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_receipt_schema_rejects_unrecognized_fields(tmp_path: Path) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    manifest["members"][0]["cwd"] = str(tmp_path)
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="receipt fields"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_manifest_schema_rejects_unrecognized_fields(tmp_path: Path) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    manifest["build_root"] = str(tmp_path)
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="manifest fields"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_manifest_member_count_requires_an_integer(tmp_path: Path) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    manifest["member_count"] = True
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ProvenanceError, match="member_count must be a positive integer"
    ):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


@pytest.mark.parametrize("invalid_digest", ["g" * 64, "A" * 64])
def test_receipt_hashes_require_lowercase_sha256_hex(
    tmp_path: Path,
    invalid_digest: str,
) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    manifest["members"][0]["ir_sha256"] = invalid_digest
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="ir_sha256 must be SHA-256"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


@pytest.mark.parametrize(
    "noncanonical_source",
    [
        "pcc/py_runtime/py/./member.py",
        "pcc/py_runtime//py/member.py",
        "pcc/py_runtime/py/member.py/",
        r"pcc\py_runtime\py\member.py",
    ],
)
def test_receipt_source_must_be_a_canonical_logical_path(
    tmp_path: Path,
    noncanonical_source: str,
) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    manifest["members"][0]["source"] = noncanonical_source
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="normalized relative path"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_production_manifest_requires_at_least_one_member(tmp_path: Path) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    runtime_root.mkdir(parents=True)
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"!<arch>\n")

    with pytest.raises(ProvenanceError, match="at least one member"):
        assemble_runtime_archive_manifest(
            archive,
            [],
            runtime_root=runtime_root,
            ar=str(tmp_path / "ar-must-not-run"),
        )


def test_verifier_rejects_an_empty_production_manifest(tmp_path: Path) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    runtime_root.mkdir(parents=True)
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"!<arch>\n")
    _write_test_capi_inventory(archive)
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(
            {
                "schema": MANIFEST_SCHEMA,
                "archive": archive.name,
                "policy": PRODUCTION_POLICY,
                "target_triple": llvm.get_default_triple(),
                "member_count": 1,
                "members_sha256": (
                    "e3b0c44298fc1c149afbf4c8996fb924"
                    "27ae41e4649b934ca495991b7852b855"
                ),
                "members": [],
                **_test_capi_manifest_fields(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="at least one member"):
        verify_runtime_archive_manifest(
            archive,
            runtime_root=runtime_root,
            ar=str(tmp_path / "ar-must-not-run"),
        )


def test_receipt_target_triple_must_be_nonempty(tmp_path: Path) -> None:
    runtime_root, _, objects, _ = _build_provenanced_archive(tmp_path)
    object_path = objects[0]

    with pytest.raises(ProvenanceError, match="target triple must be non-empty"):
        write_pcc_python_receipt(
            object_path=object_path,
            ir_path=object_path.with_suffix(".ll"),
            source_path=runtime_root / "py" / "member.py",
            runtime_root=runtime_root,
            target_triple=" \t ",
        )


def test_explicit_archive_member_name_must_be_nonempty(tmp_path: Path) -> None:
    runtime_root, _, objects, _ = _build_provenanced_archive(tmp_path)
    object_path = objects[0]

    with pytest.raises(ProvenanceError, match="unsafe archive member"):
        write_pcc_python_receipt(
            object_path=object_path,
            ir_path=object_path.with_suffix(".ll"),
            source_path=runtime_root / "py" / "member.py",
            runtime_root=runtime_root,
            target_triple=llvm.get_default_triple(),
            member="",
        )


def test_verifier_rejects_a_blank_receipt_target(tmp_path: Path) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    manifest["members"][0]["target_triple"] = "   "
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="target triple is missing"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_archive_manifest_rejects_mixed_member_targets(tmp_path: Path) -> None:
    runtime_root, archive, objects, _ = _build_provenanced_archive(
        tmp_path,
        member_stems=("first", "second"),
    )
    second_receipt_path = receipt_path_for_object(objects[1])
    second_receipt = json.loads(second_receipt_path.read_text(encoding="utf-8"))
    second_receipt["target_triple"] = "different-unknown-target"
    second_receipt_path.write_text(
        json.dumps(second_receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="mixed target triples"):
        assemble_runtime_archive_manifest(
            archive,
            objects,
            runtime_root=runtime_root,
        )


def test_manifest_target_must_match_every_member_target(tmp_path: Path) -> None:
    runtime_root, archive, _, manifest = _build_provenanced_archive(tmp_path)
    manifest["target_triple"] = "different-unknown-target"
    Path(str(archive) + ".provenance.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProvenanceError, match="differs from its members"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_manifest_binds_the_order_of_two_archive_members(tmp_path: Path) -> None:
    runtime_root, archive, objects, _ = _build_provenanced_archive(
        tmp_path,
        member_stems=("first", "second"),
    )
    archive.unlink()
    _archive(archive, objects[1], objects[0])

    with pytest.raises(ProvenanceError, match="archive inventory mismatch"):
        verify_runtime_archive_manifest(archive, runtime_root=runtime_root)


def test_receipt_publish_does_not_reuse_a_fixed_temp_name(tmp_path: Path) -> None:
    runtime_root, _, objects, _ = _build_provenanced_archive(tmp_path)
    object_path = objects[0]
    receipt_path = receipt_path_for_object(object_path)
    legacy_temp = Path(str(receipt_path) + ".tmp")
    legacy_temp.write_bytes(b"unrelated-writer")

    write_pcc_python_receipt(
        object_path=object_path,
        ir_path=object_path.with_suffix(".ll"),
        source_path=runtime_root / "py" / "member.py",
        runtime_root=runtime_root,
        target_triple=llvm.get_default_triple(),
    )

    assert legacy_temp.read_bytes() == b"unrelated-writer"
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["member"] == "member.o"


def test_ir_to_obj_does_not_reuse_fixed_publish_temp_names(tmp_path: Path) -> None:
    runtime_root, _, objects, _ = _build_provenanced_archive(tmp_path)
    object_path = objects[0]
    receipt_path = receipt_path_for_object(object_path)
    sentinels = [
        Path(str(object_path) + ".tmp"),
        Path(str(receipt_path) + ".pending"),
        Path(str(receipt_path) + ".pending.tmp"),
    ]
    for sentinel in sentinels:
        sentinel.write_bytes(b"unrelated-writer")

    result = ir_to_obj_main(
        [
            str(object_path.with_suffix(".ll")),
            str(object_path),
            "--provenance",
            str(receipt_path),
            "--source",
            str(runtime_root / "py" / "member.py"),
            "--runtime-root",
            str(runtime_root),
            "--member",
            object_path.name,
        ]
    )

    assert result == 0
    assert [path.read_bytes() for path in sentinels] == [
        b"unrelated-writer",
        b"unrelated-writer",
        b"unrelated-writer",
    ]


def test_receipt_publish_failure_removes_the_new_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root, _, objects, _ = _build_provenanced_archive(tmp_path)
    object_path = objects[0]
    receipt_path = receipt_path_for_object(object_path)
    previous_receipt = receipt_path.read_bytes()
    real_replace = ir_to_obj_module.os.replace

    def fail_final_receipt_publish(source: Path, destination: Path) -> None:
        if Path(destination) == receipt_path:
            raise OSError("simulated receipt publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(ir_to_obj_module.os, "replace", fail_final_receipt_publish)

    result = ir_to_obj_main(
        [
            str(object_path.with_suffix(".ll")),
            str(object_path),
            "--provenance",
            str(receipt_path),
            "--source",
            str(runtime_root / "py" / "member.py"),
            "--runtime-root",
            str(runtime_root),
            "--member",
            object_path.name,
        ]
    )

    assert result == 1
    assert not object_path.exists()
    assert receipt_path.read_bytes() == previous_receipt


def test_ir_to_obj_emits_a_build_authoritative_pcc_python_receipt(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "copy" / "pcc" / "py_runtime"
    source = runtime_root / "py" / "emitted_runtime.py"
    ir_path = runtime_root / "build_py" / "emitted_runtime.ll"
    object_path = runtime_root / "build_py" / "emitted_runtime.o"
    source.parent.mkdir(parents=True)
    ir_path.parent.mkdir(parents=True)
    source.write_text("def emitted() -> int:\n    return 11\n", encoding="utf-8")
    target_triple = llvm.get_default_triple()
    ir_path.write_text(
        f'target triple = "{target_triple}"\n'
        "define i32 @emitted() {\n"
        "  ret i32 11\n"
        "}\n",
        encoding="utf-8",
    )

    result = ir_to_obj_main(
        [
            str(ir_path),
            str(object_path),
            "--provenance",
            str(receipt_path_for_object(object_path)),
            "--source",
            str(source),
            "--runtime-root",
            str(runtime_root),
            "--member",
            object_path.name,
        ]
    )

    assert result == 0
    receipt = json.loads(
        receipt_path_for_object(object_path).read_text(encoding="utf-8")
    )
    assert receipt["member"] == "emitted_runtime.o"
    assert receipt["source"] == "pcc/py_runtime/py/emitted_runtime.py"
    assert receipt["target_triple"] == target_triple
    assert receipt["uses_host_cc"] is False
    assert str(tmp_path) not in json.dumps(receipt, sort_keys=True)


def test_ir_to_obj_receipt_failure_does_not_publish_new_object(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    invalid_source = runtime_root / "src" / "not_python.c"
    ir_path = runtime_root / "build_py" / "not_python.ll"
    object_path = runtime_root / "build_py" / "not_python.o"
    receipt_path = receipt_path_for_object(object_path)
    invalid_source.parent.mkdir(parents=True)
    ir_path.parent.mkdir(parents=True)
    invalid_source.write_text("int value(void) { return 9; }\n", encoding="utf-8")
    target_triple = llvm.get_default_triple()
    ir_path.write_text(
        f'target triple = "{target_triple}"\n'
        "define i32 @value() {\n  ret i32 9\n}\n",
        encoding="utf-8",
    )
    object_path.write_bytes(b"previous-object")
    receipt_path.write_bytes(b"previous-receipt")

    result = ir_to_obj_main(
        [
            str(ir_path),
            str(object_path),
            "--provenance",
            str(receipt_path),
            "--source",
            str(invalid_source),
            "--runtime-root",
            str(runtime_root),
            "--member",
            object_path.name,
        ]
    )

    assert result == 1
    assert object_path.read_bytes() == b"previous-object"
    assert receipt_path.read_bytes() == b"previous-receipt"


def test_same_named_host_cc_object_without_emitter_receipt_is_rejected(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    python_source = runtime_root / "py" / "looks_python.py"
    c_source = runtime_root / "src" / "looks_python.c"
    object_path = runtime_root / "build_py" / "looks_python.o"
    python_source.parent.mkdir(parents=True)
    c_source.parent.mkdir(parents=True)
    object_path.parent.mkdir(parents=True)
    python_source.write_text("def answer() -> int:\n    return 42\n", encoding="utf-8")
    c_source.write_text("int answer(void) { return 42; }\n", encoding="utf-8")
    compile_result = subprocess.run(
        ["cc", "-c", str(c_source), "-o", str(object_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    _archive(archive, object_path)

    with pytest.raises(ProvenanceError, match="cannot read provenance JSON"):
        assemble_runtime_archive_manifest(
            archive,
            [object_path],
            runtime_root=runtime_root,
        )


def test_receipt_explicitly_labeled_host_cc_is_rejected(tmp_path: Path) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    source = runtime_root / "py" / "host_labeled.py"
    ir_path = runtime_root / "build_py" / "host_labeled.ll"
    object_path = runtime_root / "build_py" / "host_labeled.o"
    source.parent.mkdir(parents=True)
    ir_path.parent.mkdir(parents=True)
    source.write_text("def value() -> int:\n    return 5\n", encoding="utf-8")
    target_triple = llvm.get_default_triple()
    ir_text = (
        f'target triple = "{target_triple}"\n' "define i32 @value() {\n  ret i32 5\n}\n"
    )
    ir_path.write_text(ir_text, encoding="utf-8")
    object_bytes = emit_object(ir_text)
    object_path.write_bytes(object_bytes)
    receipt = write_pcc_python_receipt(
        object_path=object_path,
        ir_path=ir_path,
        source_path=source,
        runtime_root=runtime_root,
        target_triple=target_triple,
        object_bytes=object_bytes,
    )
    receipt["uses_host_cc"] = True
    receipt_path_for_object(object_path).write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    _archive(archive, object_path)

    with pytest.raises(ProvenanceError, match="host-CC objects are forbidden"):
        assemble_runtime_archive_manifest(
            archive,
            [object_path],
            runtime_root=runtime_root,
        )


def test_stale_receipt_cannot_authorize_replaced_object_bytes(tmp_path: Path) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    source = runtime_root / "py" / "replace_me.py"
    ir_path = runtime_root / "build_py" / "replace_me.ll"
    object_path = runtime_root / "build_py" / "replace_me.o"
    source.parent.mkdir(parents=True)
    ir_path.parent.mkdir(parents=True)
    source.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    target_triple = llvm.get_default_triple()
    original_ir = (
        f'target triple = "{target_triple}"\n' "define i32 @value() {\n  ret i32 1\n}\n"
    )
    ir_path.write_text(original_ir, encoding="utf-8")
    original_object = emit_object(original_ir)
    object_path.write_bytes(original_object)
    write_pcc_python_receipt(
        object_path=object_path,
        ir_path=ir_path,
        source_path=source,
        runtime_root=runtime_root,
        target_triple=target_triple,
        object_bytes=original_object,
    )

    replacement_ir = (
        f'target triple = "{target_triple}"\n' "define i32 @value() {\n  ret i32 2\n}\n"
    )
    object_path.write_bytes(emit_object(replacement_ir))
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    _archive(archive, object_path)

    with pytest.raises(ProvenanceError, match="does not match its receipt"):
        assemble_runtime_archive_manifest(
            archive,
            [object_path],
            runtime_root=runtime_root,
        )


def test_unmanifested_archive_member_is_rejected(tmp_path: Path) -> None:
    runtime_root = tmp_path / "pcc" / "py_runtime"
    source = runtime_root / "py" / "owned.py"
    ir_path = runtime_root / "build_py" / "owned.ll"
    owned_object = runtime_root / "build_py" / "owned.o"
    extra_object = runtime_root / "build_py" / "renamed_extra.o"
    source.parent.mkdir(parents=True)
    ir_path.parent.mkdir(parents=True)
    source.write_text("def owned() -> int:\n    return 1\n", encoding="utf-8")
    target_triple = llvm.get_default_triple()
    ir_text = (
        f'target triple = "{target_triple}"\n' "define i32 @owned() {\n  ret i32 1\n}\n"
    )
    ir_path.write_text(ir_text, encoding="utf-8")
    object_bytes = emit_object(ir_text)
    owned_object.write_bytes(object_bytes)
    extra_object.write_bytes(object_bytes)
    write_pcc_python_receipt(
        object_path=owned_object,
        ir_path=ir_path,
        source_path=source,
        runtime_root=runtime_root,
        target_triple=target_triple,
        object_bytes=object_bytes,
    )
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    _archive(archive, owned_object, extra_object)

    with pytest.raises(ProvenanceError, match="archive inventory mismatch"):
        assemble_runtime_archive_manifest(
            archive,
            [owned_object],
            runtime_root=runtime_root,
        )


def test_manifest_is_deterministic_across_checkout_and_cache_roots(
    tmp_path: Path,
) -> None:
    target_triple = llvm.get_default_triple()
    ir_text = (
        f'target triple = "{target_triple}"\n'
        "define i32 @stable() {\n  ret i32 3\n}\n"
    )
    object_bytes = emit_object(ir_text)

    def build_under(root: Path) -> str:
        runtime_root = root / "pcc" / "py_runtime"
        source = runtime_root / "py" / "stable.py"
        ir_path = runtime_root / "build_py" / "stable.ll"
        object_path = runtime_root / "build_py" / "stable.o"
        source.parent.mkdir(parents=True)
        ir_path.parent.mkdir(parents=True)
        source.write_text("def stable() -> int:\n    return 3\n", encoding="utf-8")
        ir_path.write_text(ir_text, encoding="utf-8")
        object_path.write_bytes(object_bytes)
        write_pcc_python_receipt(
            object_path=object_path,
            ir_path=ir_path,
            source_path=source,
            runtime_root=runtime_root,
            target_triple=target_triple,
            object_bytes=object_bytes,
        )
        archive = runtime_root / "libpy_runtime_pcc_py.a"
        _archive(archive, object_path)
        _write_test_capi_inventory(archive)
        manifest = assemble_runtime_archive_manifest(
            archive,
            [object_path],
            runtime_root=runtime_root,
        )
        return json.dumps(manifest, indent=2, sort_keys=True)

    first = build_under(tmp_path / "checkout-a")
    second = build_under(tmp_path / "unrelated" / "cache-b")

    assert first == second
    assert str(tmp_path) not in first


def test_makefile_requests_emitter_receipts_for_pcc_python_objects() -> None:
    plan = subprocess.run(
        ["make", "-B", "-n", "build_py/py_tuple.o"],
        cwd=RUNTIME,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert plan.returncode == 0, plan.stdout + plan.stderr
    emitter_line = next(
        line for line in plan.stdout.splitlines() if "pcc.tools.ir_to_obj" in line
    )
    assert "--provenance build_py/py_tuple.o.provenance.json" in emitter_line
    assert "--source py/py_tuple.py" in emitter_line
    assert f"--runtime-root {RUNTIME}" in emitter_line
    assert "--member py_tuple.o" in emitter_line

    threaded_plan = subprocess.run(
        [
            "make",
            "-B",
            "-n",
            "PCC_WITH_THREADS=1",
            "build_py/freestanding_thread_kernel_pthread.o",
        ],
        cwd=RUNTIME,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert threaded_plan.returncode == 0, threaded_plan.stdout + threaded_plan.stderr
    threaded_emitter = next(
        line
        for line in threaded_plan.stdout.splitlines()
        if "pcc.tools.ir_to_obj" in line
    )
    assert (
        "--provenance "
        "build_py/freestanding_thread_kernel_pthread.o.provenance.json"
        in threaded_emitter
    )
    assert "--source py/freestanding_thread_kernel_pthread.py" in threaded_emitter
