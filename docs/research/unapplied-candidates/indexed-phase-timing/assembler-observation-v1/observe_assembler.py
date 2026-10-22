"""One hash-bound, counted host observation of unchanged x86 COFF assembly.

The coordinator owns admission, the exclusive lock, hard resource limits and
full source inventories. This driver does not compile a frontend, build a
runtime, execute native code, or implement a sizing optimization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time
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
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--assembly", type=Path, required=True)
    parser.add_argument("--reference-object", type=Path, required=True)
    parser.add_argument("--reference-result", type=Path, required=True)
    parser.add_argument("--reference-qualification", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest_path = packet / "manifest.json"
    assert sha(manifest_path) == args.manifest_sha256
    manifest = json.loads(manifest_path.read_text())
    assert sha(Path(__file__)) == manifest["driver_sha256"]
    assert sha(packet / "README.md") == manifest["readme_sha256"]
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
    assembly_path = args.assembly.resolve(strict=True)
    object_path = args.reference_object.resolve(strict=True)
    reference_path = args.reference_result.resolve(strict=True)
    qualification_path = args.reference_qualification.resolve(strict=True)
    source_manifest = source.parent / "source-manifest.json"
    assert sha(source_manifest) == manifest["source_inventory_sha256"]
    inventory = {row["path"]: row for row in
                 json.loads(source_manifest.read_text())["files"]}
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    assert not output.is_relative_to(source) and not source.is_relative_to(output)
    assert not output.is_relative_to(packet) and not packet.is_relative_to(output)
    inputs = (assembly_path, object_path, reference_path, qualification_path,
              source_manifest)
    assert all(not path.is_relative_to(output) for path in inputs)

    def check_pins():
        assert sha(source_manifest) == manifest["source_inventory_sha256"]
        for relative, row in manifest["source_relation"].items():
            assert sha(source / relative) == row["current_source_sha256"], relative
            assert (source / relative).stat().st_size == row["current_bytes"]
        for path, row in ((assembly_path, manifest["assembly"]),
                          (object_path, manifest["reference_object"])):
            assert path.stat().st_size == row["bytes"] and sha(path) == row["sha256"]
        assert sha(reference_path) == manifest["reference_result_sha256"]
        assert sha(qualification_path) == manifest["reference_qualification_sha256"]
        assert sha(manifest_path) == args.manifest_sha256
        assert sha(Path(__file__)) == manifest["driver_sha256"]
        assert sha(packet / "README.md") == manifest["readme_sha256"]

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
    reference = json.loads(reference_path.read_text())
    qualification = json.loads(qualification_path.read_text())
    assert reference["status"] == qualification["status"] == "PASS"
    assert reference["target"] == qualification["target"] == manifest["target"]
    assert reference["assembly_sha256"] == qualification["assembly_sha256"] == manifest["assembly"]["sha256"]
    assert reference["object"]["sha256"] == qualification["object"]["sha256"] == manifest["reference_object"]["sha256"]
    assert qualification["codegen_checksums"]["baseline"] == manifest["historical_compiler_checksum"]
    assert qualification["cfg"] == manifest["historical_input_cfg"]
    assert qualification["fallback_capture_records"] == 0
    output.mkdir()
    result = {
        "schema": "pcc.x86-assembler-observation-result.v1",
        "status": "RUNNING", "operation": "one-unchanged-host-COFF-assembly",
        "manifest_sha256": args.manifest_sha256, "target": manifest["target"],
        "source_inventory_sha256": manifest["source_inventory_sha256"],
        "source_commit": manifest["source_commit"],
        "assembly_sha256": manifest["assembly"]["sha256"],
        "input_scope": manifest["input_scope"],
        "performance_claim": False,
    }
    save(output / "result.json", result)
    try:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source))
        from pcc.backend import coff_x86_64 as coff
        from pcc.backend import x86_64_asm_driver as asm
        from pcc.backend import x86_64_encode as encoder
        from pcc.backend.target_objects import encode_assembly_object

        result["imported_source_files_before"] = check_imports()
        # Loading, hashing and reference checks are outside the timed entry.
        assembly = assembly_path.read_text(encoding="utf-8")
        expected_bytes = object_path.read_bytes()
        durations = {}
        calls = {}
        counts = {
            "layout_size_cache_misses": 0,
            "layout_relocation_bearing": 0,
            "layout_no_relocation_control_flow": 0,
            "layout_other_non_admitted_mnemonic": 0,
            "layout_cache_admitted_shape": 0,
            "final_instruction_calls": 0,
            "unexpected_phase_calls": 0,
        }
        state = {"phase": "outside"}
        patches = []

        def time_function(module, name, key, phase=None):
            original = getattr(module, name)

            def observed(*values, **options):
                previous = state["phase"]
                if phase is not None:
                    state["phase"] = phase
                started = time.perf_counter_ns()
                calls[key] = calls.get(key, 0) + 1
                try:
                    return original(*values, **options)
                finally:
                    elapsed = time.perf_counter_ns() - started
                    assert elapsed >= 0
                    durations[key] = durations.get(key, 0) + elapsed
                    state["phase"] = previous

            patches.append((module, name, original))
            setattr(module, name, observed)

        original_encode = asm.encode_instruction

        def observed_encode(line, **options):
            encoded = original_encode(line, **options)
            if state["phase"] == "layout":
                counts["layout_size_cache_misses"] += 1
                assert type(line) is str
                mnemonic = line.strip().partition(" ")[0].lower()
                if encoded.relocations:
                    counts["layout_relocation_bearing"] += 1
                elif mnemonic in ("call", "jmp") or (
                    mnemonic.startswith("j") and mnemonic[1:] in encoder._SET_CONDITIONS
                ):
                    counts["layout_no_relocation_control_flow"] += 1
                elif mnemonic not in encoder._PC_INDEPENDENT_INSTRUCTION_MNEMONICS:
                    counts["layout_other_non_admitted_mnemonic"] += 1
                else:
                    counts["layout_cache_admitted_shape"] += 1
            elif state["phase"] == "shared-assembler":
                counts["final_instruction_calls"] += 1
            else:
                counts["unexpected_phase_calls"] += 1
            return encoded

        try:
            time_function(asm, "_parse_file", "parse_ns", "parse")
            time_function(asm, "_measure_sections", "layout_ns", "layout")
            time_function(asm, "_assemble_file", "shared_assembler_inclusive_ns", "shared-assembler")
            time_function(coff, "_unwind_source", "seh_text_rewrite_ns")
            time_function(coff, "_append_unwind", "unwind_metadata_ns")
            time_function(coff, "assemble", "coff_assembly_inclusive_ns")
            time_function(coff, "emit_object", "coff_serialization_ns")
            patches.append((asm, "encode_instruction", original_encode))
            asm.encode_instruction = observed_encode
            started = time.perf_counter_ns()
            encoded = encode_assembly_object(assembly, manifest["target"])
            durations["complete_object_entry_ns"] = time.perf_counter_ns() - started
        finally:
            for module, name, original in reversed(patches):
                setattr(module, name, original)
            assert all(getattr(module, name) is original for module, name, original in patches)
            result["observers_restored"] = True

        result["durations_ns"] = durations
        result["coarse_calls"] = calls
        result["counts"] = counts
        assert state["phase"] == "outside"
        assert set(calls) == {
            "parse_ns", "layout_ns", "shared_assembler_inclusive_ns",
            "seh_text_rewrite_ns", "unwind_metadata_ns",
            "coff_assembly_inclusive_ns", "coff_serialization_ns",
        }
        assert all(value == 1 for value in calls.values())
        assert all(value > 0 for value in durations.values())
        assert counts["unexpected_phase_calls"] == 0
        misses = counts["layout_size_cache_misses"]
        final = counts["final_instruction_calls"]
        opportunity = counts["layout_relocation_bearing"] + counts["layout_no_relocation_control_flow"]
        assert sum(counts[key] for key in (
            "layout_relocation_bearing", "layout_no_relocation_control_flow",
            "layout_other_non_admitted_mnemonic", "layout_cache_admitted_shape",
        )) == misses
        assert 0 <= opportunity <= misses <= final and final > 0
        durations["shared_payload_symbols_relocations_and_teardown_ns"] = (
            durations["shared_assembler_inclusive_ns"] - durations["parse_ns"] - durations["layout_ns"]
        )
        durations["coff_adaptation_and_other_ns"] = (
            durations["coff_assembly_inclusive_ns"] - durations["shared_assembler_inclusive_ns"]
            - durations["seh_text_rewrite_ns"] - durations["unwind_metadata_ns"]
        )
        durations["entry_other_ns"] = (
            durations["complete_object_entry_ns"] - durations["coff_assembly_inclusive_ns"]
            - durations["coff_serialization_ns"]
        )
        assert all(value >= 0 for value in durations.values())
        disjoint_names = (
            "seh_text_rewrite_ns", "parse_ns", "layout_ns",
            "shared_payload_symbols_relocations_and_teardown_ns", "unwind_metadata_ns",
            "coff_adaptation_and_other_ns", "coff_serialization_ns", "entry_other_ns",
        )
        assert sum(durations[key] for key in disjoint_names) == durations["complete_object_entry_ns"]
        result["disjoint_durations_ns"] = {key: durations[key] for key in disjoint_names}
        # Full byte equality preserves every section, relocation, symbol,
        # symbolic stackmap and unwind byte, not merely executable text.
        assert encoded == expected_bytes
        assert len(encoded) == manifest["reference_object"]["bytes"]
        assert hashlib.sha256(encoded).hexdigest() == manifest["reference_object"]["sha256"]
        obj = coff.parse_object(encoded)
        sections = {section.name: section for section in obj.sections}
        assert len(sections) == len(obj.sections)
        assert all(name in sections and sections[name].data for name in
                   (".text", ".pcc_stackmaps", ".pdata", ".xdata"))
        result["object_contract"] = {
            "complete_byte_equality": True, "symbols": len(obj.symbols),
            "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
            "sections": [{"name": section.name, "bytes": len(section.data),
                          "sha256": hashlib.sha256(section.data).hexdigest(),
                          "flags": section.flags, "align": section.align,
                          "relocations": len(section.relocations)} for section in obj.sections],
        }
        with (output / "module.obj").open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        threshold = manifest["screening_thresholds"]
        assert threshold == {"minimum_layout_share_percent": 25,
                             "minimum_opportunity_per_final_instruction_percent": 10}
        layout_ok = durations["layout_ns"] * 100 >= durations["complete_object_entry_ns"] * 25
        coverage_ok = opportunity * 100 >= final * 10
        result["screen"] = {
            "layout_share_percent": 100 * durations["layout_ns"] / durations["complete_object_entry_ns"],
            "size_misses_per_final_instruction_percent": 100 * misses / final,
            "selected_opportunity_per_final_instruction_percent": 100 * opportunity / final,
            "selected_opportunity_share_of_size_misses_percent": 100 * opportunity / misses if misses else 0,
            "layout_threshold_met": layout_ok, "coverage_threshold_met": coverage_ok,
            "decision": ("SIZE_ONLY_OPPORTUNITY_SCREEN_MET_REVIEW_ONLY" if layout_ok and coverage_ok
                         else "HOLD_INSUFFICIENT_LAYOUT_OR_MISS_COVERAGE"),
            "counts_are_not_time_weights": True,
            "observer_overhead_not_subtracted": True,
            "automatic_implementation_or_more_runs": False,
        }
        result["peak_process_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        result["rss_scope"] = "process lifetime including imports, observation and post-timing decode; not phase RSS"
        result["imported_source_files_after"] = check_imports()
        check_pins()
        assert reservation == {key: os.environ[key] for key in reservation}
        result["source_and_input_postseals"] = "PASS"
        result["status"] = "PASS"
    except BaseException as exc:
        result["status"] = "FAIL"
        result["error"] = type(exc).__name__ + ": " + str(exc)
        result["traceback"] = traceback.format_exc()
        save(output / "result.json", result)
        raise
    save(output / "result.json", result)


if __name__ == "__main__":
    main()
