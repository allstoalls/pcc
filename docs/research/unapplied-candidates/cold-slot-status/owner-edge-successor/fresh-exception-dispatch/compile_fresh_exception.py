"""Compile the two-call test-only dispatch successor through ordinary CEvaluator.

Emitted code is never executed here. The reviewed fixture, generated probes,
production C frontend, owned emitter/linker and strict runtime admission remain
the owners of their respective stages.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sys
import tempfile
import time
import traceback
from unittest.mock import patch


GiB = 1024 ** 3
TARGET = "x86_64-unknown-linux-gnu"
HOST_TEST = "tests/python/test_slot_call_status_reporting.py"
NATIVE_TEST = "tests/python/test_slot_call_status_reporting_native.py"
STATUS_OWNER = "pcc/frontends/python/codegen/call_object_lowering.py"
FIXED_ENV = {
    "PCC_DISABLE_ROADMAP_DEEPWIRE": "1", "PCC_NO_AUTO_PCC1": "1",
    "PCC_TEST_NO_NATIVE_PROVISIONING": "1", "PCC_WITH_THREADS": "1",
    "PCC_REFCOUNT_KIND": "atomic", "PCC_RUNTIME_BUILD": "owned",
    "PCC_RUNTIME_CC": "pcc", "PCC_RUNTIME_HIGH": "py",
    "PCC_SELF_LINK": "pcc", "PCC_SELF_OBJ": "pcc",
    "PCC_IR_TO_OBJ_EMITTER": "pcc", "PCC_PYTHON_IR_PASSES": "off",
    "PCC_DISABLE_PY_RUN_CACHE": "1", "PCC_HOST_PYTHON": "/nonexistent/host-python",
    "PCC_HOST_PCC": "/nonexistent/host-pcc", "PCC_IR_SCAFFOLD": "on",
    "PCC_PYTHON_LIBPYTHON": "off", "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "0",
    "PCC_DIRECT_INDEXED_KERNEL_EMIT": "0", "PCC_SELF_TARGET_PASSES": "off",
}
SUPERVISOR_ENV = {"PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def status_method_body(path):
    tree = ast.parse(path.read_text())
    methods = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == "_slot_call_check_status"]
    assert len(methods) == 1
    return ast.dump(ast.Module(body=methods[0].body, type_ignores=[]), include_attributes=False)


def load_definitions(path, names, namespace):
    """Load exact frozen test definitions without unrelated pytest imports."""
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in names]
    assert {node.name for node in nodes} == set(names) and len(nodes) == len(names)
    assert all(not node.decorator_list for node in nodes)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--runtime-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest = json.loads((packet / "manifest.json").read_text())
    source = args.source.resolve(strict=True)
    canonical = args.canonical.resolve(strict=True)
    runtime_output = args.runtime_output.resolve(strict=True)
    runtime = runtime_output / "libpy_runtime_pcc_py.a"
    runtime_sources = runtime_output / "runtime-source"
    runtime_result = runtime_output / "result.json"
    harness = packet / "status.c"
    native_test = packet / "test_slot_call_status_reporting_native.py"
    assert digest(Path(__file__)) == manifest["driver_sha256"]
    assert sys.flags.isolated and sys.flags.no_site and not sys.flags.optimize and sys.dont_write_bytecode
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert resource.getrlimit(resource.RLIMIT_AS) == (4 * GiB, 4 * GiB)
    assert os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES") == str(4 * GiB)
    supervisor_environment = {name: os.environ[name] for name in SUPERVISOR_ENV}
    assert Path(supervisor_environment["PCC_WORKER_TREE_STATE_PATH"]).is_file()
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)

    def check_inputs():
        assert digest(source.parent / "source-manifest.json") == manifest["candidate_source_inventory_sha256"]
        assert digest(canonical.parent / "materialized-source-manifest.json") == manifest["canonical_source_inventory_sha256"]
        for relative, expected in manifest["candidate_files"].items():
            assert digest(source / relative) == expected, relative
        assert digest(canonical / STATUS_OWNER) == manifest["canonical_status_owner_sha256"]
        assert digest(harness) == manifest["harness_sha256"]
        assert digest(native_test) == manifest["test_only_successor"]["postimage_sha256"]
        assert digest(packet / "test-only-dispatch.patch") == manifest["test_only_successor"]["patch_sha256"]
        assert digest(runtime_result) == manifest["runtime_required"]["result_sha256"]

    check_inputs()
    built = json.loads(runtime_result.read_text())
    assert built["status"] == "PASS" and built["member_count"] == 191
    for key in ("archive_sha256", "provenance_sha256", "codegen_checksum"):
        assert built[key] == manifest["runtime_required"][key], key
    assert built["candidate_source_inventory_sha256"] == manifest["candidate_source_inventory_sha256"]
    assert built["target"] == TARGET and built["runtime_build_config"] == {"threads": True, "refcount": "atomic"}
    assert built["source_after"]["status"] == built["runtime_source_after"]["status"] == "PASS"
    runtime_identities = {
        runtime: built["archive_sha256"],
        Path(str(runtime) + ".provenance.json"): built["provenance_sha256"],
        Path(str(runtime) + ".capi_syms"): built["capi_inventory_sha256"],
    }
    assert all(digest(path) == expected for path, expected in runtime_identities.items())
    assert status_method_body(source / HOST_TEST) == status_method_body(canonical / STATUS_OWNER)
    # Compiler imports remain on the old frozen source. The separately bound
    # test postimage is an explicit input, never a production-source overlay.
    original_test = (source / NATIVE_TEST).read_text()
    assert digest(source / NATIVE_TEST) == manifest["test_only_successor"]["preimage_sha256"]
    assert original_test.count("py_gc_collect();") == 2
    assert native_test.read_text() == original_test.replace("py_gc_collect();", "pcc_gc_collect(-1);")
    tree = ast.parse(native_test.read_text())
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "_HARNESS" for target in node.targets)]
    assert len(values) == 1 and ast.literal_eval(values[0]) == harness.read_text()
    extra = {"PCC_RUNTIME_DIR": str(runtime_sources), "PCC_RUNTIME_ARCHIVE": str(runtime)}
    allowed = set(FIXED_ENV) | set(extra) | SUPERVISOR_ENV
    assert not [name for name in os.environ if name.startswith("PCC_") and name not in allowed]
    for name, value in {**FIXED_ENV, **extra}.items():
        assert name not in os.environ or os.environ[name] == value, name
        os.environ[name] = value
    os.environ["PATH"] = ""
    output = args.output.resolve()
    assert all(not output.is_relative_to(root) and not root.is_relative_to(output)
               for root in (source, canonical, packet, runtime_output))
    assert output.parent.is_dir() and not output.exists()
    assert shutil.disk_usage(output.parent).free >= 4 * GiB + 512 * 1024 ** 2
    output.mkdir()
    temporary = output / "tmp"
    temporary.mkdir()
    os.environ["TMPDIR"] = str(temporary)
    tempfile.tempdir = str(temporary)
    sys.path.insert(0, str(source))
    started = time.monotonic()
    result = {"status": "RUNNING", "phase": "imports_and_runtime_admission",
              "comparison_scope": "same frozen candidate compiler/runtime; generated former-inline and candidate probes in the explicit two-call test-only dispatch successor",
              "candidate_source_inventory_sha256": manifest["candidate_source_inventory_sha256"],
              "canonical_status_owner_sha256": manifest["canonical_status_owner_sha256"],
              "runtime_result_sha256": digest(runtime_result), "runtime_sha256": built["archive_sha256"],
              "harness_sha256": digest(harness), "test_only_successor": manifest["test_only_successor"],
              "target": TARGET, "bound_environment": FIXED_ENV,
              "native_execution": "UNRUN", "proposed_native_gc": [0, 1]}

    def phase(name):
        result.update(phase=name, elapsed_seconds=time.monotonic() - started)
        save_json(output / "result.json", result)

    try:
        phase("imports_and_runtime_admission")
        from pcc.driver.project import TranslationUnit
        from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
        from pcc.frontends.python import pipeline
        from pcc.frontends.python.codegen.layer1 import L1CodeGen
        from pcc.frontends.python.pipeline_targets import host_target_triple
        from pcc.frontends.python.py_ast import FuncDef, Module, SourceSpan
        from pcc.ir.compat import ir
        from pcc.tools.runtime_archive_provenance import codegen_checksum
        from tests.runtime_fixture_provenance import _verified_test_runtime_archive
        from tests.owned_ir_validation import verify_ir_text
        from pcc.backend.self_backend_kernel import get_indexed_function_kernel
        assert host_target_triple() == TARGET and codegen_checksum() == built["codegen_checksum"]
        admitted, receipt = _verified_test_runtime_archive(runtime, threads=True, runtime_root=runtime_sources)
        assert admitted == runtime and receipt["member_count"] == 191
        namespace = {"L1CodeGen": L1CodeGen, "ir": ir, "_I64": ir.IntType(64),
                     "Module": Module, "FuncDef": FuncDef, "SourceSpan": SourceSpan}
        load_definitions(source / HOST_TEST, {"_InlineReference", "_sites"}, namespace)
        generated = []
        generated_rows = []
        phase("unchanged_generated_probes")
        for cls, symbol in ((namespace["_InlineReference"], "baseline_probe"), (L1CodeGen, "candidate_probe")):
            owner, function = namespace["_sites"](cls, spans=(None, SourceSpan("source.py", 1, 0, 1, 2)), symbol=symbol)
            owner.module.triple = TARGET
            text = str(owner.module)
            path = output / (symbol + ".ll")
            path.write_text(text)
            verified = verify_ir_text(text)
            assert verified.triple == TARGET and function.name == symbol
            reporter = owner.runtime.get("__pcc_slot_call_status_report")
            assert (reporter is not None) == (symbol == "candidate_probe")
            assert "strict.nolib.stub" not in text
            # Runtime declarations are not executed calls. Inspect every actual
            # instruction in the owned parsed definitions, not module text.
            for definition in verified.functions:
                kernel = get_indexed_function_kernel(definition)
                for block_id in range(len(kernel.block_names)):
                    for index in range(kernel.instruction_count(block_id)):
                        instruction = kernel.diagnostic_instruction(block_id, index)
                        if instruction.kind == "call":
                            assert not instruction.data[2].startswith("py_cpy_")
            generated.append((path.name, text, None, ()))
            generated_rows.append({"symbol": symbol, "ir_sha256": digest(path), "ir_bytes": path.stat().st_size,
                                   "reporter": None if reporter is None else reporter.name,
                                   "owned_verification": "PASS", "status_sites": 2})
        save_json(output / "generated-probes.json", generated_rows)
        c_source = output / "status.c"
        c_source.write_text(harness.read_text())
        executable = output / "status-runtime"
        evaluator = CEvaluator(backend="self", target_triple=TARGET)
        runtime_selections = []
        raw_ensure = pipeline._ensure_runtime

        def observe_runtime(*positional, **keywords):
            selected = raw_ensure(*positional, **keywords)
            assert Path(selected).resolve(strict=True) == runtime
            runtime_selections.append({"archive_sha256": digest(Path(selected)), "same_sealed_runtime": True})
            return selected

        phase("ordinary_c_frontend")
        with patch("subprocess.Popen", side_effect=AssertionError("external build process")) as forbidden_popen:
            units = evaluator.compile_translation_units(
                [TranslationUnit(c_source.name, str(c_source), harness.read_text())],
                use_system_cpp=False, use_compile_cache=False,
                include_dirs=[str(source / "pcc/runtime/include"), str(source / "utils/fake_libc_include")],
            )
            assert len(units) == 1
            (output / "status-c.ll").write_text(units[0][1])
            c_module = verify_ir_text(units[0][1])
            collection_calls = []
            for definition in c_module.functions:
                kernel = get_indexed_function_kernel(definition)
                for block_id in range(len(kernel.block_names)):
                    for index in range(kernel.instruction_count(block_id)):
                        instruction = kernel.diagnostic_instruction(block_id, index)
                        if instruction.kind != "call":
                            continue
                        call = instruction.data
                        assert call[2] != "py_gc_collect", "Raw backend-0 collector remains reachable"
                        if call[2] == "pcc_gc_collect":
                            assert not call[3] and call[1].is_int and call[1].width == 64
                            assert len(call[4]) == 1 and call[4][0][0].is_int and call[4][0][0].width == 32
                            collection_calls.append({"owner": definition.name, "callee": call[2],
                                                     "reason_ir": call[4][0][1], "abi": "i64(i32)"})
            assert len(collection_calls) == 2
            save_json(output / "collection-call-edges.json", collection_calls)
            phase("ordinary_owned_object_and_executable")
            with patch.object(pipeline, "_ensure_runtime", observe_runtime):
                evaluator.emit_executable(units + generated, str(executable), optimize=False,
                                          link_args=[str(runtime)])
        assert forbidden_popen.call_count == 0, "A compiler stage attempted external Popen"
        assert len(runtime_selections) == 1
        save_json(output / "runtime-selections.json", runtime_selections)
        assert executable.is_file() and executable.stat().st_mode & 0o111
        with executable.open("rb") as stream:
            assert stream.read(4) == b"\x7fELF"
        for name, module in tuple(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc."):
                origin = getattr(module, "__file__", None)
                if origin is not None:
                    assert Path(origin).resolve().is_relative_to(source), (name, origin)
        check_inputs()
        assert all(digest(path) == expected for path, expected in runtime_identities.items())
        assert all(os.environ.get(name) == value for name, value in supervisor_environment.items())
        assert shutil.disk_usage(output).free >= 4 * GiB
        result.update(status="PASS", executable_sha256=digest(executable), executable_bytes=executable.stat().st_size,
                      c_ir_sha256=digest(output / "status-c.ll"), generated_probes=generated_rows,
                      codegen_checksum=codegen_checksum(), imported_source_closure="PASS",
                      c_frontend="ordinary CEvaluator, one translation unit, default jobs=1, internal preprocessor, normal C frontend passes",
                      emitter="owned self backend and ELF linker", program_backend_optimization=False,
                      runtime_selections=runtime_selections, collection_calls=collection_calls,
                      expected_native_stdout="slot-status-runtime-equal\n", expected_native_stderr="")
        phase("complete")
    except BaseException as exc:
        result.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc), elapsed_seconds=time.monotonic() - started)
        save_json(output / "result.json", result)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
