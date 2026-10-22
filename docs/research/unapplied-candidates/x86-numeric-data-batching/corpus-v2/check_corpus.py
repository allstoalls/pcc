"""One counted ordinary COFF re-encode; no timing or native execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import traceback


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with path.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    for option in ("source", "assembly", "reference-object", "reference-result",
                   "reference-qualification", "host-result", "host-qualification",
                   "output"):
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest_path = packet / "manifest.json"
    assert sha(manifest_path) == args.manifest_sha256
    manifest = json.loads(manifest_path.read_text())
    assert not sys.flags.optimize and sys.platform == "linux"
    assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)
    for key in ("PCC_DISABLE_ROADMAP_DEEPWIRE", "PCC_NO_AUTO_PCC1",
                "PCC_TEST_NO_NATIVE_PROVISIONING"):
        assert os.environ.get(key) == "1"
    reservation = {key: os.environ[key] for key in
                   ("PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES")}
    assert reservation["PCC_WORKER_TREE_STATE_PATH"]
    assert reservation["PCC_WORKER_TREE_BUDGET_BYTES"] == "4294967296"

    source = args.source.resolve(strict=True)
    assert Path.cwd().resolve() == source
    source_manifest = source.parent / "source-manifest.json"
    assert sha(source_manifest) == manifest["source_inventory_sha256"]
    inventory_document = json.loads(source_manifest.read_text())
    inventory = {row["path"]: row for row in inventory_document["files"]}
    assert inventory_document["candidate_pcc_git_tree"] == manifest["candidate_pcc_tree"]
    assert inventory_document["base_manifest_sha256"] == manifest["base_source_inventory_sha256"]
    assert inventory_document["packet_manifest_sha256"] == manifest["source_packet_manifest_sha256"]
    assert set(inventory_document["changed_paths"]) == {
        "pcc/backend/x86_64_asm_driver.py",
        "tests/python/test_x86_64_numeric_data_batching.py",
    }
    paths = {name: getattr(args, name).resolve(strict=True) for name in (
        "assembly", "reference_object", "reference_result", "reference_qualification",
        "host_result", "host_qualification",
    )}
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    assert all(not output.is_relative_to(root) and not root.is_relative_to(output)
               for root in (source, packet))
    assert all(not path.is_relative_to(output) for path in paths.values())
    assert not source_manifest.is_relative_to(output)

    def check_pins():
        assert sha(source_manifest) == manifest["source_inventory_sha256"]
        assert sha(manifest_path) == args.manifest_sha256
        assert sha(Path(__file__)) == manifest["driver_sha256"]
        assert sha(packet / "README.md") == manifest["readme_sha256"]
        for name, path in paths.items():
            row = manifest["inputs"][name]
            assert path.stat().st_size == row["bytes"] and sha(path) == row["sha256"], name
        for relative, expected in manifest["source_file_sha256"].items():
            assert sha(source / relative) == expected, relative

    def check_imports():
        imported = []
        for name, module in sorted(sys.modules.items()):
            if name != "pcc" and not name.startswith("pcc."):
                continue
            path = Path(module.__file__).resolve(strict=True)
            assert path.is_relative_to(source), name
            relative = path.relative_to(source).as_posix()
            assert relative in inventory and sha(path) == inventory[relative]["sha256"]
            imported.append(relative)
        assert imported
        return sorted(set(imported))

    check_pins()
    reference = json.loads(paths["reference_result"].read_text())
    qualification = json.loads(paths["reference_qualification"].read_text())
    assert reference["status"] == qualification["status"] == "PASS"
    assert reference["target"] == qualification["target"] == manifest["target"]
    assert reference["assembly_sha256"] == qualification["assembly_sha256"] == manifest["inputs"]["assembly"]["sha256"]
    assert reference["object"]["sha256"] == qualification["object"]["sha256"] == manifest["inputs"]["reference_object"]["sha256"]
    assert qualification["codegen_checksums"]["baseline"] == manifest["historical_compiler_checksum"]
    assert qualification["cfg"] == manifest["historical_input_cfg"]
    assert qualification["fallback_capture_records"] == 0
    host = json.loads(paths["host_result"].read_text())
    assert host["status"] == "PASS" and host["counts"] == {
        "collected": 46, "passed": 46, "failed": 0, "skipped": 0,
        "xfailed": 0, "xpassed": 0, "errors": 0,
    }
    assert host["deselected"] == []
    assert host["source_manifest_sha256"] == manifest["source_inventory_sha256"]
    assert host["codegen_checksum"] == manifest["codegen_checksum"]
    host_seal = json.loads(paths["host_qualification"].read_text())
    assert host_seal["status"] == "PASS" and host_seal["counts"] == host["counts"]
    assert host_seal["candidate_source_manifest_sha256"] == manifest["source_inventory_sha256"]
    assert host_seal["artifacts"]["host-payload/result.json"]["sha256"] == manifest["inputs"]["host_result"]["sha256"]
    assert host_seal["stages"]["host"]["denied_audit_events"] == 0
    assert host_seal["lock_released"] is True
    output.mkdir()
    result = {
        "schema": "pcc.x86-numeric-batching-corpus-result.v1",
        "status": "RUNNING", "operation": "one-counted-ordinary-host-COFF-reencode",
        "manifest_sha256": args.manifest_sha256, "target": manifest["target"],
        "source_inventory_sha256": manifest["source_inventory_sha256"],
        "assembly_sha256": manifest["inputs"]["assembly"]["sha256"],
        "input_scope": manifest["input_scope"], "performance_claim": False,
    }
    save(output / "result.json", result)
    try:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source))
        from pcc.backend import coff_x86_64 as coff
        from pcc.backend import x86_64_asm_driver as asm
        from pcc.backend.target_objects import encode_assembly_object
        from pcc.tools.runtime_archive_provenance import codegen_checksum

        assert codegen_checksum() == manifest["codegen_checksum"]
        result["imported_source_files_before"] = check_imports()
        assert asm._NUMERIC_DATA_BATCH_BYTES == 8192
        assembly = paths["assembly"].read_text(encoding="utf-8")
        expected_bytes = paths["reference_object"].read_bytes()
        counts = {"parser_calls": 0, "integer_payload_calls": 0,
                  "float_payload_calls": 0, "scalar_payload_bytes": 0,
                  "data_records": 0, "data_payload_bytes": 0,
                  "maximum_data_payload_bytes": 0}
        by_section = {}
        active = {"parse": False}
        original_parse = asm._parse_file
        original_integer = asm._integer_payload
        struct_module = asm.struct
        original_pack = struct_module.pack

        def observed_integer(value, width, *, owner):
            payload = original_integer(value, width, owner=owner)
            if active["parse"]:
                assert owner in (".byte", ".short", ".long", ".quad")
                assert type(payload) is bytes and len(payload) == width
                counts["integer_payload_calls"] += 1
                counts["scalar_payload_bytes"] += len(payload)
            return payload

        def observed_pack(fmt, *values):
            payload = original_pack(fmt, *values)
            if active["parse"]:
                # The exact shared parser uses module-level pack only for
                # scalar float directives; packed instruction spans use a
                # preconstructed Struct. Fail closed if that source changes.
                assert fmt in ("<f", "<d") and len(values) == 1
                assert type(payload) is bytes
                counts["float_payload_calls"] += 1
                counts["scalar_payload_bytes"] += len(payload)
            return payload

        def observed_parse(*values, **options):
            assert not active["parse"] and counts["parser_calls"] == 0
            assert len(values) == 1 and options == {"compact_instructions": True}
            counts["parser_calls"] += 1
            active["parse"] = True
            try:
                parsed = original_parse(*values, **options)
            finally:
                active["parse"] = False
            plans, order, _symbols = parsed
            assert set(plans) == set(order) and len(plans) == len(order)
            for name in order:
                records = 0
                payload_bytes = 0
                for entry in plans[name].entries:
                    if type(entry) is asm._Data:
                        assert type(entry.payload) is bytes
                        assert 0 < len(entry.payload) <= asm._NUMERIC_DATA_BATCH_BYTES
                        records += 1
                        payload_bytes += len(entry.payload)
                        counts["maximum_data_payload_bytes"] = max(
                            counts["maximum_data_payload_bytes"], len(entry.payload),
                        )
                by_section[name] = {"data_records": records, "data_payload_bytes": payload_bytes}
                counts["data_records"] += records
                counts["data_payload_bytes"] += payload_bytes
            return parsed

        try:
            asm._integer_payload = observed_integer
            struct_module.pack = observed_pack
            asm._parse_file = observed_parse
            encoded = encode_assembly_object(assembly, manifest["target"])
        finally:
            asm._parse_file = original_parse
            struct_module.pack = original_pack
            asm._integer_payload = original_integer
            assert asm._parse_file is original_parse
            assert struct_module.pack is original_pack and asm.struct is struct_module
            assert asm._integer_payload is original_integer
            result["observers_restored"] = True

        assert not active["parse"] and counts["parser_calls"] == 1
        scalar_records = counts["integer_payload_calls"] + counts["float_payload_calls"]
        assert scalar_records > 0 and 0 < counts["data_records"] <= scalar_records
        assert counts["scalar_payload_bytes"] == counts["data_payload_bytes"]
        result["counts"] = counts
        result["data_by_section"] = by_section
        result["legacy_per_scalar_equivalent_records"] = scalar_records
        result["legacy_count_proof"] = (
            "Actual successful scalar payload callbacks in this invocation; the pinned "
            "base parser creates exactly one _Data for each callback. The baseline "
            "was not reparsed or re-encoded by this gate."
        )
        result["record_reduction_fraction"] = (scalar_records - counts["data_records"]) / scalar_records
        assert type(encoded) is bytes and encoded == expected_bytes
        assert len(encoded) == manifest["inputs"]["reference_object"]["bytes"]
        assert hashlib.sha256(encoded).hexdigest() == manifest["inputs"]["reference_object"]["sha256"]
        decoded = coff.parse_object(encoded)
        assert decoded == coff.parse_object(expected_bytes)
        sections = {section.name: section for section in decoded.sections}
        for name in (".text", ".pcc_stackmaps", ".pdata", ".xdata"):
            assert name in sections and sections[name].data
        object_output = output / "module.obj"
        with object_output.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        result["object"] = {"bytes": len(encoded), "sha256": sha(object_output)}
        result["byte_and_complete_decoded_contract_equal"] = True
        result["sections"] = [
            {"name": section.name, "bytes": len(section.data),
             "relocations": len(section.relocations), "align": section.align}
            for section in decoded.sections
        ]
        minimum = manifest["screening_thresholds"]["minimum_record_reduction_percent"]
        assert minimum == 50
        meets_threshold = 100 * (scalar_records - counts["data_records"]) >= minimum * scalar_records
        result["decision"] = (
            "MECHANISM_THRESHOLD_MET_REVIEW_ONLY" if meets_threshold else
            "HOLD_INSUFFICIENT_DATA_RECORD_REDUCTION"
        )
        result["timing_evidence"] = "NONE: scalar callbacks and plan census are counted; no performance timers."
        result["imported_source_files_after"] = check_imports()
        check_pins()
        assert all(os.environ.get(key) == value for key, value in reservation.items())
        result["status"] = "PASS"
    except BaseException:
        result["status"] = "FAIL"
        result["error"] = traceback.format_exc()
        save(output / "result.json", result)
        raise
    save(output / "result.json", result)
    print(json.dumps({key: result[key] for key in
                     ("status", "decision", "counts", "object", "record_reduction_fraction")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
