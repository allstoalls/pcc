"""One counter-free host emission/object arm over the sealed c_ast input.

The coordinator owns process supervision, source inventories and arm order.
No PCC monkeypatch, forced collection, runtime, link or native execution.
"""
from __future__ import annotations

import argparse
import dataclasses
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
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def normalized(value):
    # Same complete dataclass contract as the passed real-module gate.
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if dataclasses.is_dataclass(value):
        return {field.name: normalized(getattr(value, field.name))
                for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    if value is None or type(value) in (str, int, bool, float):
        return value
    raise TypeError("unexpected decoded contract type: " + type(value).__name__)


def contract_digest(value):
    # json.dump uses this encoder too. Hash every byte, including its final
    # newline, without retaining another 220 MB redundant contract file.
    state = hashlib.sha256()
    size = 0
    for piece in json.JSONEncoder(sort_keys=True, indent=2).iterencode(value):
        raw = piece.encode("utf-8")
        state.update(raw)
        size += len(raw)
    state.update(b"\n")
    return {"sha256": state.hexdigest(), "bytes": size + 1}


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--slot", type=int, choices=(1, 2, 3, 4), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--reference-result", type=Path, required=True)
    parser.add_argument("--reference-object", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest_path = packet / "manifest.json"
    assert sha(manifest_path) == args.manifest_sha256
    manifest = json.loads(manifest_path.read_text())
    assert sha(Path(__file__)) == manifest["driver_sha256"]
    assert not sys.flags.optimize and sys.platform == "linux"
    assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)
    for name in ("PCC_DISABLE_ROADMAP_DEEPWIRE", "PCC_NO_AUTO_PCC1", "PCC_TEST_NO_NATIVE_PROVISIONING"):
        assert os.environ.get(name) == "1"
    reservation = {key: os.environ[key] for key in
                   ("PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES")}
    assert reservation["PCC_WORKER_TREE_BUDGET_BYTES"] == "4294967296"
    for key in manifest["effective_environment"]:
        assert key not in os.environ, "inherited experimental flag: " + key

    slot = manifest["schedule"][args.slot - 1]
    arm = slot["arm"]
    source = args.source.resolve(strict=True)
    input_path = args.input.resolve(strict=True)
    reference_path = args.reference_result.resolve(strict=True)
    reference_object = args.reference_object.resolve(strict=True)
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    assert not output.is_relative_to(source) and not source.is_relative_to(output)
    assert not output.is_relative_to(packet)
    assert all(not path.is_relative_to(output) for path in
               (input_path, reference_path, reference_object, packet))
    assert sha(source.parent / "source-manifest.json") == manifest["source_inventories"][arm]
    assert sha(input_path) == manifest["input"]["sha256"]
    assert input_path.stat().st_size == manifest["input"]["bytes"]
    assert sha(reference_path) == manifest["reference_result_sha256"]
    assert sha(reference_object) == manifest["reference_object"]["sha256"]
    assert reference_object.stat().st_size == manifest["reference_object"]["bytes"]
    reference = json.loads(reference_path.read_text())
    assert reference["status"] == "PASS" and reference["target"] == manifest["target"]
    assert reference["input_sha256"] == manifest["input"]["sha256"]
    assert reference["object"]["sha256"] == manifest["reference_object"]["sha256"]

    def source_pins():
        for relative, expected in manifest["shared_source_files"].items():
            assert sha(source / relative) == expected, relative
        assert sha(source / "pcc/backend/self_backend_x86_64_linux.py") == manifest["emitter_sha256"][arm]

    source_pins()
    output.mkdir()
    result = {"schema": "pcc.x86-bridge-uncounted-arm.v1", "status": "RUNNING",
              "slot": args.slot, "arm": arm, "target": manifest["target"],
              "manifest_sha256": args.manifest_sha256,
              "input_sha256": manifest["input"]["sha256"],
              "source_inventory_sha256": manifest["source_inventories"][arm],
              "emitter_sha256": manifest["emitter_sha256"][arm],
              "scope": "host indexed emission plus object encoding; no frontend, native or full Stage1 timing"}
    save(output / "result.json", result)
    try:
        os.environ.update(manifest["effective_environment"])
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source))
        from pcc.backend.self_backend_indexed_codec import decode_indexed_module_file
        from pcc.backend.self_backend_kernel import INLINE_ERROR_EDGE_WIDTH, get_indexed_function_kernel
        from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
        from pcc.backend.elf_x86_64 import parse_relocatable
        from pcc.backend.precise_stackmap import ARCH_X86_64, decode_stack_map
        from pcc.ir.direct_indexed_kernel import direct_indexed_module_first_libpython_edge

        module = decode_indexed_module_file(str(input_path))
        assert module.triple == manifest["target"]
        assert not direct_indexed_module_first_libpython_edge(module)
        rows = []
        for function in module.functions:
            assert not function.blocks and not function.block_map
            assert "strict.nolib.stub" not in function.name
            kernel = get_indexed_function_kernel(function)
            assert all("strict.nolib.stub" not in name for name in kernel.block_names)
            rows.append({"name": function.name, "blocks": len(kernel.block_names),
                         "instructions": len(kernel.instruction_metadata) // 4,
                         "values": len(kernel.value_names),
                         "inline_error_edges": len(kernel.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH})
        cfg = {"functions": len(rows), "blocks": sum(row["blocks"] for row in rows),
               "instructions": sum(row["instructions"] for row in rows), "rows": rows}
        assert cfg == reference["cfg"]
        plans = []
        result["phase"] = "uncounted-emission-and-object-encoding"
        save(output / "result.json", result)
        print(result["phase"], flush=True)
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

        # Only six clocks surround the unchanged production calls. No hooks,
        # counters, hashing, progress output, serialization or forced GC occur
        # in this measured interval. Automatic GC retains its ordinary policy.
        cpu_start = time.process_time_ns()
        wall_start = time.perf_counter_ns()
        assembly = emit_indexed_assembly(module, optimize=False, stack_map_plans_out=plans)
        wall_emit_end = time.perf_counter_ns()
        cpu_emit_end = time.process_time_ns()
        encoded = encode_assembly_object(assembly, manifest["target"], stack_map_plans=plans)
        wall_end = time.perf_counter_ns()
        cpu_end = time.process_time_ns()

        result["timing_ns"] = {
            "emission_wall": wall_emit_end - wall_start,
            "encoding_wall": wall_end - wall_emit_end,
            "combined_wall": wall_end - wall_start,
            "emission_cpu": cpu_emit_end - cpu_start,
            "encoding_cpu": cpu_end - cpu_emit_end,
            "combined_cpu": cpu_end - cpu_start,
        }
        result["rss_high_water_bytes"] = {
            "before_timed_pipeline": rss_before,
            "through_timed_pipeline": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        }
        result["rss_scope"] = "Linux process-lifetime high-water marks; input/import/preflight included; not isolated phase RSS and not additive"
        result["phase"] = "outside-timing-complete-contract-checks"
        save(output / "result.json", result)
        assert hashlib.sha256(assembly.encode()).hexdigest() == reference["assembly_sha256"]
        assert len(assembly.encode()) == reference["assembly_bytes"]
        assert hashlib.sha256(encoded).hexdigest() == manifest["reference_object"]["sha256"]
        assert len(encoded) == manifest["reference_object"]["bytes"]
        assert encoded == reference_object.read_bytes()
        obj = parse_relocatable(encoded)
        stackmaps = [decode_stack_map(section.data, expected_arch=ARCH_X86_64)
                     for section in obj.sections if section.name == ".pcc_stackmaps"]
        assert stackmaps
        contract = contract_digest({"object": normalized(obj), "stackmaps": normalized(stackmaps)})
        assert contract == manifest["reference_decoded_contract"]
        (output / "module.s").write_text(assembly)
        (output / "module.o").write_bytes(encoded)
        result["cfg"] = {key: value for key, value in cfg.items() if key != "rows"}
        result["cfg_rows_equal_reference"] = True
        result["assembly_sha256"] = reference["assembly_sha256"]
        result["assembly_bytes"] = reference["assembly_bytes"]
        result["object"] = manifest["reference_object"]
        result["decoded_contract"] = contract
        result["exact_reference_equality"] = True
        from pcc.tools.runtime_archive_provenance import codegen_checksum
        result["codegen_checksum"] = codegen_checksum()
        assert result["codegen_checksum"] == manifest["codegen_checksum"][arm]
        for name, imported in tuple(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc."):
                location = getattr(imported, "__file__", None)
                if location is not None:
                    assert Path(location).resolve().is_relative_to(source), (name, location)
        source_pins()
        assert sha(source.parent / "source-manifest.json") == manifest["source_inventories"][arm]
        assert sha(input_path) == manifest["input"]["sha256"]
        assert sha(reference_path) == manifest["reference_result_sha256"]
        assert sha(reference_object) == manifest["reference_object"]["sha256"]
        assert sha(manifest_path) == args.manifest_sha256
        assert all(os.environ.get(key) == value for key, value in reservation.items())
        assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
        assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
        result["status"] = "PASS"
        result["phase"] = "complete"
        save(output / "result.json", result)
        print(json.dumps(result, sort_keys=True))
    except BaseException as error:
        result["status"] = "FAIL"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        save(output / "result.json", result)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
