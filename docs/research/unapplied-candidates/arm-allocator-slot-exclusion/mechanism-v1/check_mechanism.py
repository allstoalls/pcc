"""Coordinator-only observation of one existing ARM indexed-module entry.

Counts successful ordinary allocator predecessor walks. No timing conclusion,
frontend generation, changed solver, runtime build, or native execution.
"""
from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import traceback

REGALLOC = "pcc/backend/self_backend_aarch64_darwin_regalloc.py"
COUNTERS = ("pending_pops", "predecessor_visits")
SCHEMA = "pcc.arm-allocator-slot-exclusion-mechanism.v1"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with path.open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def normalized(value):
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
    raise TypeError("unexpected contract type: " + type(value).__name__)


def contract_digest(value):
    state = hashlib.sha256(); size = 0
    for piece in json.JSONEncoder(sort_keys=True, indent=2).iterencode(value):
        raw = piece.encode("utf-8"); state.update(raw); size += len(raw)
    state.update(b"\n")
    return {"sha256": state.hexdigest(), "bytes": size + 1}


def module_cfg(module):
    from pcc.backend.self_backend_kernel import INLINE_ERROR_EDGE_WIDTH, get_indexed_function_kernel
    from pcc.ir.direct_indexed_kernel import direct_indexed_module_first_libpython_edge
    assert not direct_indexed_module_first_libpython_edge(module)
    rows = []
    for function in module.functions:
        kernel = get_indexed_function_kernel(function)
        assert "strict.nolib.stub" not in function.name
        assert all("strict.nolib.stub" not in name for name in kernel.block_names)
        rows.append({"name": function.name, "blocks": len(kernel.block_names),
                     "instructions": len(kernel.instruction_metadata) // 4,
                     "values": len(kernel.value_names),
                     "inline_error_edges": len(kernel.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH})
    assert len({row["name"] for row in rows}) == len(rows)
    return {"functions": len(rows), "blocks": sum(row["blocks"] for row in rows),
            "instructions": sum(row["instructions"] for row in rows), "rows": rows}


class ObservedPending(list):
    """Same list operations/order; only a successful pop records work."""
    def __init__(self, predecessors, row):
        super().__init__()
        self.predecessors = predecessors
        self.row = row

    def pop(self):
        block = list.pop(self)
        self.row["pending_pops"] += 1
        # The exact source always executes every entry of this built-in list
        # next. Counts are accepted only after the whole entry succeeds.
        self.row["predecessor_visits"] += len(self.predecessors[block])
        return block


class AllocatorObserver:
    def __init__(self, source):
        from pcc.backend import self_backend_aarch64_darwin_regalloc as regalloc
        from pcc.backend import self_backend_aarch64_darwin_prologue as prologue
        self.regalloc = regalloc; self.prologue = prologue
        self.real_facts = regalloc._function_level_facts
        self.real_allocate = prologue.allocate_aarch64_block_registers
        assert self.real_allocate is regalloc.allocate_aarch64_block_registers
        self.active = None; self.rows = []; self.restored = False
        tree = ast.parse((source / REGALLOC).read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "_function_level_facts"]
        assert len(functions) == 1
        function = copy.deepcopy(functions[0])
        original_dump = ast.dump(function, include_attributes=False)
        assert not function.decorator_list
        pending = [node for node in ast.walk(function) if isinstance(node, ast.AnnAssign)
                   and isinstance(node.target, ast.Name) and node.target.id == "pending"]
        assert len(pending) == 1 and isinstance(pending[0].value, ast.List)
        assert pending[0].value.elts == []
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "pending"]
        assert {node.func.attr for node in calls} <= {"append", "clear", "pop"}
        pops = [node for node in calls if node.func.attr == "pop"]
        assert len(pops) == 1 and not pops[0].args and not pops[0].keywords
        loops = [node for node in ast.walk(function) if isinstance(node, ast.While)
                 and isinstance(node.test, ast.Name) and node.test.id == "pending"]
        assert len(loops) == 1
        expected_loop = ast.parse("""while pending:
    block_id = pending.pop()
    base = block_base[block_id]
    if lowest < 0 or base < lowest:
        lowest = base
    for predecessor in predecessors[block_id]:
        end = block_end[predecessor]
        if end > highest:
            highest = end
        if predecessor != home and visited[predecessor] != value_id:
            visited[predecessor] = value_id
            pending.append(predecessor)
""").body[0]
        assert ast.dump(loops[0], include_attributes=False) == ast.dump(expected_loop, include_attributes=False)
        factory = "_pcc_allocator_pending_observer_factory"
        assert factory not in regalloc.__dict__
        assert all(not isinstance(node, ast.Name) or node.id != factory for node in ast.walk(function))
        original_value = pending[0].value
        pending[0].value = ast.Call(func=ast.Name(id=factory, ctx=ast.Load()),
                                    args=[ast.Name(id="predecessors", ctx=ast.Load())], keywords=[])
        restored = copy.deepcopy(function)
        restored_pending = [node for node in ast.walk(restored) if isinstance(node, ast.AnnAssign)
                            and isinstance(node.target, ast.Name) and node.target.id == "pending"]
        restored_pending[0].value = original_value
        assert ast.dump(restored, include_attributes=False) == original_dump
        self.original_ast_sha256 = hashlib.sha256(original_dump.encode()).hexdigest()
        namespace = dict(regalloc.__dict__)
        namespace[factory] = self.pending_factory
        isolated = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        # This compiles the single host observer function, not PCC IR or code.
        exec(compile(ast.fix_missing_locations(isolated), str(source / REGALLOC), "exec"), namespace)
        self.observed_facts = namespace["_function_level_facts"]

    def pending_factory(self, predecessors):
        assert self.active is not None
        row, _kernel = self.active
        assert row["pending_instances"] == 0
        row["pending_instances"] += 1
        return ObservedPending(predecessors, row)

    def facts(self, kernel, *args, **kwargs):
        assert self.active is not None
        row, expected_kernel = self.active
        assert kernel is expected_kernel and row["facts_calls"] == 0
        row["facts_calls"] += 1
        return self.observed_facts(kernel, *args, **kwargs)

    def allocate(self, function):
        assert self.active is None, "reentrant allocator owner"
        kernel = self.regalloc.get_indexed_function_kernel(function)
        function_level = self.regalloc.function_live_intervals_enabled()
        callee_saved = self.regalloc.callee_saved_registers_enabled() and bool(function.indexed_slot_projection)
        expected_facts = int(not function.is_vararg and (function_level or callee_saved))
        row = {"name": function.name, "blocks": len(kernel.block_names),
               "values": len(kernel.value_names), "is_vararg": bool(function.is_vararg),
               "indexed_slot_projection": bool(function.indexed_slot_projection),
               "expected_facts_calls": expected_facts, "facts_calls": 0,
               "pending_instances": 0, "pending_pops": 0, "predecessor_visits": 0}
        self.active = (row, kernel)
        try:
            returned = self.real_allocate(function)
        finally:
            self.active = None
        assert row["facts_calls"] == row["pending_instances"] == expected_facts
        self.rows.append(row)
        return returned

    def __enter__(self):
        self.regalloc._function_level_facts = self.facts
        self.prologue.allocate_aarch64_block_registers = self.allocate
        return self

    def __exit__(self, *_error):
        self.regalloc._function_level_facts = self.real_facts
        self.prologue.allocate_aarch64_block_registers = self.real_allocate
        self.restored = (self.regalloc._function_level_facts is self.real_facts
                         and self.prologue.allocate_aarch64_block_registers is self.real_allocate)


def compare(baseline, candidate):
    assert baseline["status"] == candidate["status"] == "PASS"
    assert baseline["arm"] == "baseline" and candidate["arm"] == "candidate"
    for key in ("schema", "manifest_sha256", "input_sha256", "reference_result_sha256",
                "target", "cfg", "object", "decoded_contract", "transport_counts", "stackmap_functions"):
        assert baseline[key] == candidate[key], key
    left = baseline["mechanism"]; right = candidate["mechanism"]
    assert len(left["functions"]) == len(right["functions"])
    nonincreasing = True
    for b, c in zip(left["functions"], right["functions"], strict=True):
        assert {k: v for k, v in b.items() if k not in COUNTERS} == {k: v for k, v in c.items() if k not in COUNTERS}
        nonincreasing &= all(c[key] <= b[key] for key in COUNTERS)
    before = left["totals"]["predecessor_visits"]
    after = right["totals"]["predecessor_visits"]
    threshold = before > 0 and after * 4 <= before * 3
    passed = threshold and nonincreasing
    return {"status": "PASS", "decision": "MECHANISM_THRESHOLD_MET_REVIEW_ONLY" if passed else "HOLD_NO_MATERIAL_PREDECESSOR_REDUCTION",
            "threshold_percent": 25, "threshold_met": passed,
            "per_function_pops_and_predecessors_nonincreasing": nonincreasing,
            "baseline": left["totals"], "candidate": right["totals"],
            "predecessor_reduction_percent": None if not before else 100 * (before - after) / before,
            "complete_object_and_decoded_contract_equal": True,
            "performance_claim": False, "historical_pre_outline_corpus": True}


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--baseline-result", type=Path)
    parser.add_argument("--baseline-result-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    assert sha(packet / "manifest.json") == args.manifest_sha256
    manifest = json.loads((packet / "manifest.json").read_text())
    for name, identity in manifest["artifacts"].items():
        assert sha(packet / name) == identity["sha256"] and (packet / name).stat().st_size == identity["bytes"]
    assert not sys.flags.optimize and sys.platform == "linux"
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)
    for name in ("PCC_DISABLE_ROADMAP_DEEPWIRE", "PCC_NO_AUTO_PCC1", "PCC_TEST_NO_NATIVE_PROVISIONING"):
        assert os.environ.get(name) == "1"
    reservation = {name: os.environ[name] for name in ("PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES")}
    assert reservation["PCC_WORKER_TREE_BUDGET_BYTES"] == "4294967296"
    for name in manifest["rejected_inherited_environment"]:
        assert name not in os.environ, "inherited experimental flag: " + name
    source = args.source.resolve(strict=True); reference = args.reference_root.resolve(strict=True)
    assert Path.cwd().resolve() == source
    inventory_path = source.parent / "source-manifest.json"
    assert sha(inventory_path) == manifest["source_inventories"][args.arm]
    inventory = {row["path"]: row for row in json.loads(inventory_path.read_text())["files"]}
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    assert all(not output.is_relative_to(path) and not path.is_relative_to(output) for path in (source, reference, packet))
    baseline = None
    if args.arm == "candidate":
        assert args.baseline_result is not None and args.baseline_result_sha256
        assert sha(args.baseline_result) == args.baseline_result_sha256
        baseline = json.loads(args.baseline_result.read_text())
        assert baseline["status"] == "PASS" and baseline["arm"] == "baseline"
        assert baseline["manifest_sha256"] == args.manifest_sha256
        assert baseline["source_inventory_sha256"] == manifest["source_inventories"]["baseline"]
    else:
        assert args.baseline_result is None and args.baseline_result_sha256 is None

    def check_pins():
        assert sha(source / REGALLOC) == manifest["regalloc_sha256"][args.arm]
        for relative, expected in manifest["shared_source_files"].items():
            assert sha(source / relative) == expected, relative
        for relative, identity in manifest["reference_inputs"].items():
            path = reference / relative
            assert path.stat().st_size == identity["bytes"] and sha(path) == identity["sha256"], relative

    check_pins()
    prepared = json.loads((reference / "prepare/payload/result.json").read_text())
    prior = json.loads((reference / "baseline/payload/result.json").read_text())
    assert prepared["status"] == prior["status"] == "PASS"
    assert prepared["target"] == prior["target"] == manifest["target"]
    assert prepared["fallback_records"] == 0
    assert prepared["indexed_input"]["sha256"] == prior["input_sha256"] == manifest["reference_inputs"]["prepare/payload/input.pidx"]["sha256"]
    output.mkdir(); sys.dont_write_bytecode = True; sys.path.insert(0, str(source))
    os.environ.update(manifest["effective_environment"])
    result = {"schema": SCHEMA, "status": "RUNNING", "arm": args.arm,
              "manifest_sha256": args.manifest_sha256, "target": manifest["target"],
              "source_inventory_sha256": manifest["source_inventories"][args.arm],
              "input_sha256": prior["input_sha256"],
              "reference_result_sha256": manifest["reference_inputs"]["baseline/payload/result.json"]["sha256"],
              "scope": "host ordinary Mac indexed transport of historical pre-outline c_ast; mechanism only"}

    def phase(name):
        result["phase"] = name; save(output / "result.json", result); print(name, flush=True)

    try:
        from pcc.backend.self_backend_indexed_codec import decode_indexed_module_file
        from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
        from pcc.backend.native_object import encode_native_object_from_sections, decode_native_object
        from pcc.backend.precise_stackmap import ARCH_AARCH64, decode_stack_map
        phase("decode-retained-targeted-input")
        module = decode_indexed_module_file(str(reference / "prepare/payload/input.pidx"))
        assert module.triple == manifest["target"]
        assert all(not function.blocks and not function.block_map for function in module.functions)
        result["cfg"] = module_cfg(module)
        assert result["cfg"] == prepared["cfg"] == prior["cfg"]
        phase("ordinary-entry-with-pending-pop-observer")
        with AllocatorObserver(source) as observer:
            transport = emit_aarch64_darwin_indexed_transport(
                module, optimize=False, structured_instructions=True, phase_timing=None)
        assert observer.restored and observer.active is None
        assert [row["name"] for row in observer.rows] == [row["name"] for row in result["cfg"]["rows"]]
        result["observer_restored"] = True
        result["observed_function_ast_sha256"] = observer.original_ast_sha256
        result["mechanism"] = {"functions": observer.rows,
                               "totals": {key: sum(row[key] for row in observer.rows) for key in COUNTERS},
                               "allocator_calls": len(observer.rows),
                               "facts_calls": sum(row["facts_calls"] for row in observer.rows)}
        assert transport.native_finalized and not transport.line_chunks
        assert transport.encoded_line_records is None
        assert transport.fallback_instruction_count == 0 and not transport.fallback_instruction_lines
        result["transport_counts"] = {name: getattr(transport, name) for name in prior["transport_counts"]}
        assert result["transport_counts"] == prior["transport_counts"]
        phase("ordinary-sections-and-packed-object")
        sections, undefined = transport.assemble_sections()
        encoded = encode_native_object_from_sections(sections, undefined=undefined)
        assert encoded == (reference / "baseline/payload/module.pco").read_bytes()
        (output / "module.pco").write_bytes(encoded)
        result["object"] = {"name": "module.pco", "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
        assert result["object"] == prior["object"]
        if baseline is not None:
            assert encoded == (args.baseline_result.parent / "module.pco").read_bytes()
        del transport, sections, undefined, module
        phase("complete-decoded-contract-and-comparison")
        obj = decode_native_object(encoded)
        stackmaps = [decode_stack_map(section.data, expected_arch=ARCH_AARCH64)
                     for section in obj.sections if section.sectname == "__pcc_stackmaps"]
        assert len(stackmaps) == 1
        assert any(section.sectname == "__compact_unwind" for section in obj.sections)
        result["decoded_contract"] = contract_digest(normalized({"object": obj, "stackmaps": stackmaps}))
        assert result["decoded_contract"] == prior["decoded_contract"]
        result["stackmap_functions"] = len(stackmaps[0].functions)
        assert result["stackmap_functions"] == result["cfg"]["functions"] == prior["stackmap_functions"]
        from pcc.tools.runtime_archive_provenance import codegen_checksum
        result["codegen_checksum"] = codegen_checksum()
        assert result["codegen_checksum"] == manifest["codegen_checksums"][args.arm]
        for name, imported in tuple(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc."):
                location = getattr(imported, "__file__", None)
                if location is not None:
                    path = Path(location).resolve(); relative = path.relative_to(source).as_posix()
                    assert sha(path) == inventory[relative]["sha256"], relative
        check_pins()
        assert sha(inventory_path) == manifest["source_inventories"][args.arm]
        assert all(os.environ.get(key) == value for key, value in reservation.items())
        assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
        assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
        result["status"] = "PASS"
        if baseline is not None:
            assert sha(args.baseline_result) == args.baseline_result_sha256
            comparison = compare(baseline, result)
            comparison["baseline_result_sha256"] = args.baseline_result_sha256
            save(output / "comparison.json", comparison)
        phase("complete")
    except BaseException as error:
        result["status"] = "FAIL"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        save(output / "result.json", result); traceback.print_exc(); raise


if __name__ == "__main__":
    main()
