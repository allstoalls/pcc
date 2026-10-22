"""One correctly targeted host c_ast input or native-transport mechanism arm.

Coordinator-only. This observes ordinary production functions without changing
roots, provenance, their returned owners, row results, or emitted instructions.
No subprocess, native execution, runtime build, full Stage1, or timing claim.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import gc
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time
import traceback

MODULE = "pcc.frontends.c.ast.c_ast"
FIXTURE = "pcc/frontends/c/ast/c_ast.py"
CHANGED = "pcc/backend/self_backend_precise_stackmaps.py"


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
    state = hashlib.sha256()
    size = 0
    for piece in json.JSONEncoder(sort_keys=True, indent=2).iterencode(value):
        raw = piece.encode("utf-8")
        state.update(raw)
        size += len(raw)
    state.update(b"\n")
    return {"sha256": state.hexdigest(), "bytes": size + 1}


def arena_contract(arena):
    # Scalar reads only: never retain an interior address or close an owner.
    state = hashlib.sha256()
    count = len(arena)
    for index in range(count):
        state.update((str(arena.get_unchecked(index)) + "\n").encode())
    return {"scalars": count, "sha256": state.hexdigest()}


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


class LivenessObserver:
    def __init__(self, arm, stream):
        from pcc.backend import self_backend_precise_stackmaps as stackmaps
        from pcc.backend.self_backend_value_arena import CompilerIntArena
        self.stackmaps = stackmaps
        self.arena_type = CompilerIntArena
        self.real_solver = stackmaps._native_managed_liveness
        self.real_row = CompilerIntArena.converge_liveness_row_unchecked
        self.arm = arm
        self.stream = stream
        self.active = None
        self.rows = []

    def __enter__(self):
        def observed_row(scratch, uses, definitions, live_out, live_in, start, count, mask):
            return self.observe_row(scratch, uses, definitions, live_out, live_in, start, count, mask)
        self.stackmaps._native_managed_liveness = self.observe_solver
        # A plain function on the arena class receives the actual arena self.
        self.arena_type.converge_liveness_row_unchecked = observed_row
        return self

    def __exit__(self, *_error):
        self.stackmaps._native_managed_liveness = self.real_solver
        self.arena_type.converge_liveness_row_unchecked = self.real_row

    @staticmethod
    def calls(kernel):
        from pcc.backend.self_backend_ir import PARSED_INSTRUCTION_KIND_CALL
        for block_id in range(len(kernel.block_names)):
            block = kernel.block_fact(block_id)
            for position in range(block.second):
                instruction_id = block.first + position
                metadata = kernel.instruction_metadata_by_id(instruction_id)
                if metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                    yield block_id, position, instruction_id, metadata.second

    @staticmethod
    def protocol_contract(kernel):
        from pcc.backend.self_backend_call_flags import CALL_FLAG_FRAME_PROTOCOL
        state = hashlib.sha256()
        count = 0
        for block_id, position, instruction_id, call_id in LivenessObserver.calls(kernel):
            flags = kernel.call_flags(call_id)
            if flags & CALL_FLAG_FRAME_PROTOCOL:
                row = [block_id, position, instruction_id, call_id, flags,
                       kernel.call_aux_state_id(call_id)]
                state.update((json.dumps(row, separators=(",", ":")) + "\n").encode())
                count += 1
            else:
                # Fresh pidx, no prior liveness publication or reused function.
                assert kernel.call_aux_state_id(call_id) == 0
        return {"calls": count, "sha256": state.hexdigest()}

    def observe_solver(self, function, origins, kernel):
        from pcc.backend.self_backend_call_flags import CALL_FLAG_FRAME_PROTOCOL
        assert self.active is None, "reentrant liveness owner"
        blocks = len(kernel.block_names)
        degrees = [kernel.cfg_successor_count(block) for block in range(blocks)]
        successors = [[kernel.cfg_successor_id(block, edge) for edge in range(degrees[block])]
                      for block in range(blocks)]
        assert all(0 <= successor < blocks for row in successors for successor in row)
        tracked = [value for value in range(len(kernel.value_names)) if origins.is_tracked(value)]
        words = (len(tracked) + 29) // 30
        origin_before = {name: arena_contract(getattr(origins, name))
                         for name in ("states", "transfers", "transfer_ids", "proposal")}
        protocol_before = self.protocol_contract(kernel)
        row = {"invocation": len(self.rows), "name": function.name,
               "B": blocks, "E": sum(degrees), "T": len(tracked), "W": words,
               "frame_size": function.frame_size, "outdegrees": degrees,
               "value_names": contract_digest(kernel.value_names),
               "prepared_cfg": contract_digest({
                   "block_names": kernel.block_names, "successors": successors,
                   "terminators": arena_contract(kernel.terminator_scalars),
                   "cases": arena_contract(kernel.terminator_case_scalars),
                   "phis": arena_contract(kernel.phi_scalars),
                   "phi_incoming": arena_contract(kernel.phi_incoming_scalars),
                   "inline_error_edges": arena_contract(kernel.error_edge_scalars)}),
               "origins": origin_before, "tracked_ids": contract_digest(tracked),
               "protocol_before": protocol_before, "row_visits": 0,
               "positive_word_row_visits": 0, "dense_successor_visits": 0,
               "weighted_word_units": 0, "changed_rows": 0,
               "per_block_visits": [0] * blocks if words else None,
               "first_reverse_sweep_checked": words > 0,
               "baseline_K": None, "last_baseline_sweep_changes": None}
        self.active = (row, degrees)
        try:
            result = self.real_solver(function, origins, kernel)
        finally:
            self.active = None
        assert row["row_visits"] >= blocks
        if words == 0:
            assert row["row_visits"] == blocks
            # All row_start arguments are zero. Do not claim observed row IDs
            # or K for this domain; its production path remains unchanged.
        elif self.arm == "baseline" and blocks:
            assert row["row_visits"] % blocks == 0
            row["baseline_K"] = row["row_visits"] // blocks
            assert row["last_baseline_sweep_changes"] == 0
        assert row["weighted_word_units"] == words * (
            2 * row["row_visits"] + row["dense_successor_visits"])
        assert origin_before == {name: arena_contract(getattr(origins, name))
                                 for name in origin_before}
        protocol_after = hashlib.sha256()
        protocol_count = live_count = nonempty_count = 0
        assert len(result.states) % 4 == 0
        state_count = len(result.states) // 4
        for block_id, position, instruction_id, call_id in self.calls(kernel):
            flags = kernel.call_flags(call_id)
            state_id = kernel.call_aux_state_id(call_id)
            header = kernel.call_header(call_id)
            assert 0 <= header.second < len(kernel.call_texts)
            callee = kernel.call_texts[header.second]
            if flags & CALL_FLAG_FRAME_PROTOCOL:
                protocol_after.update((json.dumps(
                    [block_id, position, instruction_id, call_id, flags, state_id],
                    separators=(",", ":")) + "\n").encode())
                protocol_count += 1
                kind = "root-state"
                values = None
            else:
                assert 0 <= state_id < state_count
                state = result.record(state_id)
                count = state.first
                assert count >= 0
                values = []
                if count:
                    values.append(state.second)
                if count == 2:
                    values.append(state.third)
                elif count > 2:
                    start = -state.third - 2
                    assert 0 <= start <= len(result.overflow_ids) - (count - 1)
                    values.extend(result.overflow_ids.get_unchecked(start + index)
                                  for index in range(count - 1))
                assert len(values) == count and values == sorted(set(values))
                assert all(origins.is_tracked(value) for value in values)
                kind = "managed-live-after"
                live_count += 1
                nonempty_count += bool(values)
            # Covers every call, including nonprotocol calls later stackmap-
            # skipped. Frame-protocol IDs are never read as liveness records.
            self.stream.write(json.dumps(
                [row["invocation"], function.name, block_id, position, instruction_id,
                 call_id, callee, flags, kind, state_id, values,
                 None if values is None else [kernel.value_name(value) for value in values]],
                separators=(",", ":")) + "\n")
        assert protocol_before == {"calls": protocol_count, "sha256": protocol_after.hexdigest()}
        row["protocol_calls"] = protocol_count
        row["nonprotocol_calls"] = live_count
        row["nonempty_live_calls"] = nonempty_count
        row["live_state_storage"] = {"states": arena_contract(result.states),
                                     "overflow": arena_contract(result.overflow_ids)}
        self.rows.append(row)
        # Same owner/result: the ordinary planner, not this observer, closes it.
        return result

    def observe_row(self, scratch, uses, definitions, live_out, live_in, start, count, mask):
        assert self.active is not None, "row helper outside its bound production owner"
        row, degrees = self.active
        assert count == row["W"] and mask == (1 << 30) - 1
        index = row["row_visits"]
        if count:
            assert start % count == 0
            block = start // count
            assert 0 <= block < row["B"]
            if self.arm == "baseline" or index < row["B"]:
                assert block == row["B"] - 1 - index % row["B"]
            if self.arm == "baseline" and index % row["B"] == 0:
                row["last_baseline_sweep_changes"] = 0
            row["per_block_visits"][block] += 1
            row["positive_word_row_visits"] += 1
            row["dense_successor_visits"] += degrees[block]
            row["weighted_word_units"] += count * (2 + degrees[block])
        else:
            assert start == 0
        row["row_visits"] += 1
        changed = self.real_row(scratch, uses, definitions, live_out, live_in, start, count, mask)
        assert type(changed) is bool
        row["changed_rows"] += changed
        if self.arm == "baseline" and count:
            row["last_baseline_sweep_changes"] += changed
        return changed


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--operation", choices=("prepare", "emit"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--prepared-result", type=Path)
    parser.add_argument("--prepared-result-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    assert sha(packet / "manifest.json") == args.manifest_sha256
    manifest = json.loads((packet / "manifest.json").read_text())
    assert sha(Path(__file__)) == manifest["driver_sha256"]
    assert not sys.flags.optimize and sys.platform == "linux"
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)
    for name in ("PCC_DISABLE_ROADMAP_DEEPWIRE", "PCC_NO_AUTO_PCC1", "PCC_TEST_NO_NATIVE_PROVISIONING"):
        assert os.environ.get(name) == "1"
    reservation = {name: os.environ[name] for name in
                   ("PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES")}
    assert reservation["PCC_WORKER_TREE_BUDGET_BYTES"] == "4294967296"
    for name in manifest["effective_environment"]:
        assert name not in os.environ, "inherited experimental flag: " + name
    source = args.source.resolve(strict=True)
    input_path = args.input.resolve(strict=True)
    assert sha(source.parent / "source-manifest.json") == manifest["source_inventories"][args.arm]
    assert sha(input_path) == args.input_sha256
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    assert all(not output.is_relative_to(path) and not path.is_relative_to(output)
               for path in (source, packet))
    assert not input_path.is_relative_to(output)

    def check_sources():
        for relative, expected in manifest["shared_source_files"].items():
            assert sha(source / relative) == expected, relative
        assert sha(source / CHANGED) == manifest["solver_sha256"][args.arm]

    check_sources()
    target = manifest["target"]
    output.mkdir()
    os.environ.update(manifest["effective_environment"])
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    result = {"schema": "pcc.arm-managed-liveness-real-module.v1", "status": "RUNNING",
              "operation": args.operation, "arm": args.arm, "target": target,
              "manifest_sha256": args.manifest_sha256,
              "source_inventory_sha256": manifest["source_inventories"][args.arm],
              "input_sha256": args.input_sha256,
              "solver_sha256": manifest["solver_sha256"][args.arm],
              "scope": "host c_ast production native transport; mechanism-only, no speed/native execution claim"}

    def phase(name):
        result["phase"] = name
        save(output / "result.json", result)
        print(name, flush=True)

    try:
        if args.operation == "prepare":
            assert args.arm == "baseline"
            assert args.prepared_result is None and args.prepared_result_sha256 is None
            assert input_path == (source / FIXTURE).resolve()
            assert args.input_sha256 == manifest["shared_source_files"][FIXTURE]
            syntax = ast.parse(input_path.read_text())
            imports = [node for node in ast.walk(syntax) if isinstance(node, (ast.Import, ast.ImportFrom))]
            assert len(imports) == 1 and isinstance(imports[0], ast.Import)
            assert [(item.name, item.asname) for item in imports[0].names] == [("sys", None)]
            assert {node.attr for node in ast.walk(syntax)
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "sys"} == {"stdout"}
            from pcc.frontends.python.codegen.layer1 import L1CodeGen
            from pcc.frontends.python.pipeline_import_policy import NATIVE_BUILTIN_IMPORTS, REQUIRED_COMPILED_STDLIB_PROVIDERS
            from pcc.frontends.python.py_lift import parse_and_lift
            from pcc.frontends.python.type_infer import infer_module
            from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
            from pcc.backend.self_backend_kernel import get_indexed_function_kernel
            assert "sys" in NATIVE_BUILTIN_IMPORTS and "sys" not in REQUIRED_COMPILED_STDLIB_PROVIDERS
            phase("normal-targeted-library-parse-infer-generate")
            typed = infer_module(parse_and_lift(input_path.read_text(), str(input_path), MODULE))
            generator = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
            generator._strict_no_libpython = True
            generator._prefer_native_callable_values = True
            generator._module_source_path = str(input_path)
            generator._target_triple = target
            generator.module.triple = target
            generator._python_library = True
            generator._skip_program_main = True
            assert generator._native_module_exports is None and not generator._sibling_module_inits
            assert not generator._suppress_implicit_gc_roots
            assert not generator._suppress_borrowed_return_retain
            generated_text = generator.generate(typed)
            assert generated_text == ""
            direct = generator._direct_indexed_module
            assert direct is not None and direct.triple == target
            result["supported_records"] = generator.module._direct_indexed_supported_records
            result["fallback_records"] = generator.module._direct_indexed_fallback_records
            assert result["supported_records"] > 0 and result["fallback_records"] == 0
            assert all(not function.blocks for function in direct.functions)
            result["cfg"] = module_cfg(direct)
            phase("owned-indexed-codec")
            indexed_path = output / "input.pidx"
            encode_indexed_module_file(str(indexed_path), direct)
            result["indexed_input"] = {"name": indexed_path.name, "sha256": sha(indexed_path),
                                       "bytes": indexed_path.stat().st_size}
            for function in direct.functions:
                get_indexed_function_kernel(function).close_native_tables()
            del generator, generated_text, typed, direct
            gc.collect()
        else:
            assert args.prepared_result is not None and args.prepared_result_sha256
            prepared_path = args.prepared_result.resolve(strict=True)
            assert not prepared_path.is_relative_to(output)
            assert sha(prepared_path) == args.prepared_result_sha256
            prepared = json.loads(prepared_path.read_text())
            assert prepared["status"] == "PASS" and prepared["operation"] == "prepare"
            assert prepared["arm"] == "baseline" and prepared["target"] == target
            assert prepared["manifest_sha256"] == args.manifest_sha256
            assert prepared["indexed_input"]["sha256"] == args.input_sha256
            assert input_path.stat().st_size == prepared["indexed_input"]["bytes"]
            assert prepared["fallback_records"] == 0
            result["prepared_result_sha256"] = args.prepared_result_sha256
            from pcc.backend.self_backend_indexed_codec import decode_indexed_module_file
            from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
            from pcc.backend.native_object import encode_native_object_from_sections, decode_native_object
            from pcc.backend.precise_stackmap import ARCH_AARCH64, decode_stack_map
            phase("owned-indexed-decode")
            module = decode_indexed_module_file(str(input_path))
            assert module.triple == target
            assert all(not function.blocks and not function.block_map for function in module.functions)
            result["cfg"] = module_cfg(module)
            assert result["cfg"] == prepared["cfg"]
            states_path = output / "call-states.jsonl"
            phase("native-transport-with-owner-bound-row-observer")
            with states_path.open("x") as states_stream:
                with LivenessObserver(args.arm, states_stream) as observer:
                    started = time.monotonic_ns()
                    transport = emit_aarch64_darwin_indexed_transport(
                        module, optimize=False, structured_instructions=True)
                    result["observer_weighted_transport_ns"] = time.monotonic_ns() - started
                states_stream.flush()
                os.fsync(states_stream.fileno())
            assert observer.active is None
            assert len(observer.rows) == result["cfg"]["functions"]
            assert len({row["name"] for row in observer.rows}) == len(observer.rows)
            assert {row["name"] for row in observer.rows} == {row["name"] for row in result["cfg"]["rows"]}
            result["mechanism"] = {
                "functions": observer.rows,
                "totals": {key: sum(row[key] for row in observer.rows) for key in
                           ("B", "E", "T", "row_visits", "positive_word_row_visits",
                            "dense_successor_visits", "weighted_word_units", "changed_rows",
                            "protocol_calls", "nonprotocol_calls", "nonempty_live_calls")},
                "positive_word_functions": sum(row["W"] > 0 for row in observer.rows)}
            result["call_states"] = {"name": states_path.name, "bytes": states_path.stat().st_size,
                                     "sha256": sha(states_path)}
            assert transport.native_finalized and not transport.line_chunks
            assert transport.encoded_line_records is None
            assert transport.fallback_instruction_count == 0 and not transport.fallback_instruction_lines
            result["transport_counts"] = {name: getattr(transport, name) for name in
                ("structured_instruction_count", "structured_unscaled_count", "structured_move_count",
                 "structured_call_count", "direct_instruction_count", "native_fragment_record_count")}
            phase("ordinary-final-sections-and-packed-object")
            sections, undefined = transport.assemble_sections()
            encoded = encode_native_object_from_sections(sections, undefined=undefined)
            del transport, sections, undefined, module
            object_path = output / "module.pco"
            object_path.write_bytes(encoded)
            result["object"] = {"name": object_path.name, "bytes": len(encoded), "sha256": sha(object_path)}
            phase("complete-object-and-stackmap-contract")
            obj = decode_native_object(encoded)
            stackmaps = [decode_stack_map(section.data, expected_arch=ARCH_AARCH64)
                         for section in obj.sections if section.sectname == "__pcc_stackmaps"]
            assert len(stackmaps) == 1
            assert any(section.sectname == "__compact_unwind" for section in obj.sections)
            result["decoded_contract"] = contract_digest(normalized({"object": obj, "stackmaps": stackmaps}))
            result["stackmap_functions"] = len(stackmaps[0].functions)
            assert result["stackmap_functions"] == result["cfg"]["functions"]
            assert sha(prepared_path) == args.prepared_result_sha256
        from pcc.tools.runtime_archive_provenance import codegen_checksum
        result["codegen_checksum"] = codegen_checksum()
        assert result["codegen_checksum"] == manifest["codegen_checksums"][args.arm]
        for name, imported in tuple(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc."):
                location = getattr(imported, "__file__", None)
                if location is not None:
                    assert Path(location).resolve().is_relative_to(source), (name, location)
        check_sources()
        assert sha(input_path) == args.input_sha256
        assert sha(source.parent / "source-manifest.json") == manifest["source_inventories"][args.arm]
        assert all(os.environ.get(key) == value for key, value in reservation.items())
        assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
        assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
        result["status"] = "PASS"
        phase("complete")
    except BaseException as error:
        result["status"] = "FAIL"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        save(output / "result.json", result)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
