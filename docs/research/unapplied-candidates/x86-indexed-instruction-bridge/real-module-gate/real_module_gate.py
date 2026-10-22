"""Hash-bound real c_ast indexed-input preparation or one x86 object arm.

Coordinator-only, single-process host production components. No runtime,
linker, native execution, subprocess, text-triple rewrite or fake exports.
The object-arm counters are observers, not a performance measurement.
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
EMITTER = "pcc/backend/self_backend_x86_64_linux.py"
TARGETS = ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
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
    raise TypeError("unexpected decoded contract type: " + type(value).__name__)


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--operation", choices=("prepare", "emit"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest = json.loads((packet / "manifest.json").read_text())
    assert digest(Path(__file__)) == manifest["driver_sha256"]
    assert not sys.flags.optimize, "assertions required"
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
    assert os.environ.get("PCC_DISABLE_ROADMAP_DEEPWIRE") == "1"
    assert os.environ.get("PCC_NO_AUTO_PCC1") == "1"
    assert os.environ.get("PCC_TEST_NO_NATIVE_PROVISIONING") == "1"
    # Leave live supervisor reservation/state variables untouched.
    assert os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES") == "4294967296"
    assert os.environ.get("PCC_WORKER_TREE_STATE_PATH")
    source = args.source.resolve(strict=True)
    input_path = args.input.resolve(strict=True)
    assert digest(input_path) == args.input_sha256
    output = args.output.resolve()
    assert not output.is_relative_to(source)
    assert not input_path.is_relative_to(output)
    output.mkdir(parents=True, exist_ok=False)

    def check_sources():
        for relative, expected in manifest["shared_source_files"].items():
            assert digest(source / relative) == expected, relative
        assert digest(source / EMITTER) == manifest["emitter_sha256"][args.arm]

    check_sources()
    for key, value in manifest["effective_environment"].items():
        os.environ[key] = value
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    result = {"schema": "pcc.x86-indexed-bridge-real-module.v1", "status": "RUNNING",
              "operation": args.operation, "arm": args.arm, "target": args.target,
              "source": str(source), "input": str(input_path),
              "input_sha256": args.input_sha256,
              "emitter_sha256": manifest["emitter_sha256"][args.arm],
              "manifest_sha256": digest(packet / "manifest.json"),
              "scope": "host owned indexed emission/object identity; observer-weighted, no speed or native claim"}

    def phase(name):
        result["phase"] = name
        write_json(output / "result.json", result)
        print(name, flush=True)

    def imported_sources():
        for name, module in tuple(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc."):
                location = getattr(module, "__file__", None)
                if location is not None:
                    assert Path(location).resolve().is_relative_to(source), (name, location)

    def cfg(module):
        from pcc.backend.self_backend_kernel import (
            INLINE_ERROR_EDGE_WIDTH, get_indexed_function_kernel,
        )
        from pcc.ir.direct_indexed_kernel import direct_indexed_module_first_libpython_edge
        assert not direct_indexed_module_first_libpython_edge(module)
        functions = []
        for function in module.functions:
            kernel = get_indexed_function_kernel(function)
            assert "strict.nolib.stub" not in function.name
            assert all("strict.nolib.stub" not in name for name in kernel.block_names)
            functions.append({"name": function.name,
                              "blocks": len(kernel.block_names),
                              "instructions": len(kernel.instruction_metadata) // 4,
                              "values": len(kernel.value_names),
                              "inline_error_edges": len(kernel.error_edge_scalars) // INLINE_ERROR_EDGE_WIDTH})
        return {"functions": len(functions),
                "blocks": sum(row["blocks"] for row in functions),
                "instructions": sum(row["instructions"] for row in functions),
                "rows": functions}

    try:
        if args.operation == "prepare":
            assert args.arm == "baseline"
            assert args.input_sha256 == manifest["shared_source_files"][FIXTURE]
            syntax = ast.parse(input_path.read_text())
            imports = [node for node in ast.walk(syntax)
                       if isinstance(node, (ast.Import, ast.ImportFrom))]
            assert len(imports) == 1 and isinstance(imports[0], ast.Import)
            assert [(item.name, item.asname) for item in imports[0].names] == [("sys", None)]
            assert {node.attr for node in ast.walk(syntax)
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "sys"} == {"stdout"}
            from pcc.frontends.python.codegen.layer1 import L1CodeGen
            from pcc.frontends.python.pipeline_import_policy import (
                NATIVE_BUILTIN_IMPORTS, REQUIRED_COMPILED_STDLIB_PROVIDERS,
            )
            from pcc.frontends.python.py_lift import parse_and_lift
            from pcc.frontends.python.type_infer import infer_module
            from pcc.backend.self_backend_indexed_codec import encode_indexed_module_file
            from pcc.backend.self_backend_kernel import get_indexed_function_kernel

            assert "sys" in NATIVE_BUILTIN_IMPORTS
            assert "sys" not in REQUIRED_COMPILED_STDLIB_PROVIDERS
            phase("normal-library-parse-infer-generate")
            started = time.monotonic_ns()
            typed = infer_module(parse_and_lift(input_path.read_text(), str(input_path), MODULE))
            generator = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
            generator._strict_no_libpython = True
            generator._prefer_native_callable_values = True
            generator._module_source_path = str(input_path)
            generator._target_triple = args.target
            generator.module.triple = args.target
            generator._python_library = True
            generator._skip_program_main = True
            assert generator._native_module_exports is None and not generator._sibling_module_inits
            assert not generator._suppress_implicit_gc_roots
            assert not generator._suppress_borrowed_return_retain
            generated_text = generator.generate(typed)
            # Normal generation owns capture. In direct-only mode its return
            # value is empty text; consume the same captured module as workers.
            assert generated_text == ""
            direct = generator._direct_indexed_module
            assert direct is not None and direct.triple == args.target
            result["generation_and_capture_ns"] = time.monotonic_ns() - started
            result["supported_records"] = generator.module._direct_indexed_supported_records
            result["fallback_records"] = generator.module._direct_indexed_fallback_records
            assert result["supported_records"] > 0 and result["fallback_records"] == 0
            assert all(not function.blocks for function in direct.functions)
            result["cfg"] = cfg(direct)
            phase("owned-indexed-codec")
            indexed_path = output / "input.pidx"
            encode_indexed_module_file(str(indexed_path), direct)
            result["indexed_input"] = {"name": indexed_path.name,
                                       "sha256": digest(indexed_path),
                                       "bytes": indexed_path.stat().st_size}
            for function in direct.functions:
                get_indexed_function_kernel(function).close_native_tables()
            del generator, generated_text, typed, direct
            gc.collect()
        else:
            from pcc.backend import self_backend_x86_64_linux as emitter
            from pcc.backend import self_backend_ir as parsed_ir
            from pcc.backend.self_backend_indexed_codec import decode_indexed_module_file
            from pcc.backend.self_backend_kernel import IndexedFunctionKernel, get_indexed_function_kernel
            from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
            from pcc.backend.precise_stackmap import ARCH_X86_64, decode_stack_map

            phase("owned-indexed-decode")
            module = decode_indexed_module_file(str(input_path))
            assert module.triple == args.target
            assert all(not function.blocks and not function.block_map for function in module.functions)
            result["cfg"] = cfg(module)
            kernels = [get_indexed_function_kernel(function) for function in module.functions]
            total_instructions = result["cfg"]["instructions"]
            counters = {"emitted_functions": 0, "diagnostic_instruction_calls": 0,
                        "operand_intern_calls": 0, "instruction_data_calls": 0,
                        "arithmetic_flag_reads": 0}
            active = False
            real_emit = emitter._emit_function
            real_diagnostic = IndexedFunctionKernel.diagnostic_instruction
            real_intern = parsed_ir._interned_operands
            real_data = IndexedFunctionKernel.instruction_data
            real_flags = IndexedFunctionKernel.instruction_arithmetic_flags

            def observed_emit(function, plan):
                nonlocal active
                assert not active
                active = True
                counters["emitted_functions"] += 1
                try:
                    return real_emit(function, plan)
                finally:
                    active = False

            def observed_diagnostic(self, *values):
                if active:
                    counters["diagnostic_instruction_calls"] += 1
                return real_diagnostic(self, *values)

            def observed_intern(*values):
                if active:
                    counters["operand_intern_calls"] += 1
                return real_intern(*values)

            def observed_data(self, *values):
                if active:
                    counters["instruction_data_calls"] += 1
                return real_data(self, *values)

            def observed_flags(self, *values):
                if active:
                    counters["arithmetic_flag_reads"] += 1
                return real_flags(self, *values)

            emitter._emit_function = observed_emit
            IndexedFunctionKernel.diagnostic_instruction = observed_diagnostic
            parsed_ir._interned_operands = observed_intern
            IndexedFunctionKernel.instruction_data = observed_data
            IndexedFunctionKernel.instruction_arithmetic_flags = observed_flags
            plans = [] if args.target == TARGETS[0] else None
            try:
                phase("indexed-emission-with-counters")
                started = time.monotonic_ns()
                assembly = emit_indexed_assembly(module, optimize=False, stack_map_plans_out=plans)
                result["observed_emit_ns"] = time.monotonic_ns() - started
            finally:
                emitter._emit_function = real_emit
                IndexedFunctionKernel.diagnostic_instruction = real_diagnostic
                parsed_ir._interned_operands = real_intern
                IndexedFunctionKernel.instruction_data = real_data
                IndexedFunctionKernel.instruction_arithmetic_flags = real_flags
                for function in module.functions:
                    get_indexed_function_kernel(function).close_native_tables()
            assert counters["emitted_functions"] == result["cfg"]["functions"]
            assert counters["instruction_data_calls"] == total_instructions
            assert counters["arithmetic_flag_reads"] == total_instructions
            expected_bridge = total_instructions if args.arm == "baseline" else 0
            assert counters["diagnostic_instruction_calls"] == expected_bridge
            assert counters["operand_intern_calls"] == expected_bridge
            counters["instruction_arena_projections"] = sum(
                kernel.instruction_arena_diagnostic_projections for kernel in kernels)
            assert counters["instruction_arena_projections"] == (
                result["cfg"]["blocks"] if args.arm == "baseline" else 0)
            result["mechanism_counts"] = counters
            result["assembly_sha256"] = hashlib.sha256(assembly.encode()).hexdigest()
            result["assembly_bytes"] = len(assembly.encode())
            (output / "module.s").write_text(assembly)
            del module, kernels
            gc.collect()
            phase("owned-object-encoding")
            started = time.monotonic_ns()
            encoded = encode_assembly_object(assembly, args.target, stack_map_plans=plans)
            result["observed_object_ns"] = time.monotonic_ns() - started
            object_path = output / ("module.obj" if args.target == TARGETS[1] else "module.o")
            object_path.write_bytes(encoded)
            result["object"] = {"name": object_path.name, "bytes": len(encoded), "sha256": digest(object_path)}
            phase("complete-decoded-contract")
            if args.target == TARGETS[1]:
                from pcc.backend.coff_x86_64 import parse_object
                obj = parse_object(encoded)
                names = {section.name for section in obj.sections}
                assert {".pdata", ".xdata"} <= names
            else:
                from pcc.backend.elf_x86_64 import parse_relocatable
                obj = parse_relocatable(encoded)
            stackmaps = [decode_stack_map(section.data, expected_arch=ARCH_X86_64)
                         for section in obj.sections if section.name == ".pcc_stackmaps"]
            assert stackmaps
            contract_path = output / "decoded-contract.json"
            write_json(contract_path, {"object": normalized(obj), "stackmaps": normalized(stackmaps)})
            result["decoded_contract_sha256"] = digest(contract_path)
        imported_sources()
        check_sources()
        assert digest(input_path) == args.input_sha256
        assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
        assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
        result["status"] = "PASS"
        phase("complete")
    except BaseException as error:
        result["status"] = "FAIL"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        write_json(output / "result.json", result)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
