"""Compile the preserved real callsite through the unchanged public pipeline.

This stage never executes emitted native code. A later independently admitted
known-PID launcher owns execution. No emitter threshold or fallback is changed.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import io
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
NATIVE_TEST = "tests/python/test_slot_call_status_reporting_native.py"
HOST_TEST = "tests/python/test_slot_call_status_reporting.py"
STATUS_OWNER = "pcc/frontends/python/codegen/call_object_lowering.py"
FIXED_ENV = {
    "PCC_DISABLE_ROADMAP_DEEPWIRE": "1", "PCC_NO_AUTO_PCC1": "1",
    "PCC_TEST_NO_NATIVE_PROVISIONING": "1", "PCC_WITH_THREADS": "1",
    "PCC_REFCOUNT_KIND": "atomic", "PCC_RUNTIME_CC": "pcc",
    "PCC_RUNTIME_HIGH": "py", "PCC_SELF_LINK": "pcc", "PCC_SELF_OBJ": "pcc",
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
    """Load exact reviewed test definitions without unrelated module imports."""
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in names]
    assert {node.name for node in nodes} == set(names) and len(nodes) == len(names)
    assert all(not node.decorator_list for node in nodes)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--runtime-output", type=Path, required=True)
    parser.add_argument("--runtime-result-sha256", required=True)
    parser.add_argument("--arm", choices=("candidate", "inline-reference"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest = json.loads((packet / "manifest.json").read_text())
    source = args.source.resolve(strict=True)
    canonical = args.canonical.resolve(strict=True)
    runtime_output = args.runtime_output.resolve(strict=True)
    runtime = runtime_output / "libpy_runtime_pcc_py.a"
    runtime_sources = runtime_output / "runtime-source"
    result_path = runtime_output / "result.json"
    program = packet / "program.py"
    assert digest(Path(__file__)) == manifest["driver_sha256"]
    assert not sys.flags.optimize and sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert resource.getrlimit(resource.RLIMIT_AS) == (4 * GiB, 4 * GiB)
    assert os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES") == str(4 * GiB)
    supervisor_environment = {name: os.environ[name] for name in SUPERVISOR_ENV}
    assert Path(supervisor_environment["PCC_WORKER_TREE_STATE_PATH"]).is_file()
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)

    def check_source_inputs():
        assert digest(source.parent / "source-manifest.json") == manifest["candidate_source_inventory_sha256"]
        assert digest(canonical.parent / "materialized-source-manifest.json") == manifest["canonical_source_inventory_sha256"]
        for relative, value in manifest["candidate_files"].items():
            assert digest(source / relative) == value, relative
        assert digest(canonical / STATUS_OWNER) == manifest["canonical_status_owner_sha256"]
        assert digest(program) == manifest["program_sha256"]
        assert args.runtime_result_sha256 == manifest["runtime_required"]["result_sha256"]
        assert digest(result_path) == args.runtime_result_sha256

    check_source_inputs()
    built = json.loads(result_path.read_text())
    assert built["status"] == "PASS" and built["member_count"] == 191
    for key in ("archive_sha256", "provenance_sha256", "codegen_checksum"):
        assert built[key] == manifest["runtime_required"][key], key
    assert built["candidate_source_inventory_sha256"] == manifest["candidate_source_inventory_sha256"]
    assert built["target"] == TARGET and built["runtime_build_config"] == {"threads": True, "refcount": "atomic"}
    assert built["source_after"]["status"] == built["runtime_source_after"]["status"] == "PASS"
    assert digest(runtime) == built["archive_sha256"]
    assert digest(Path(str(runtime) + ".provenance.json")) == built["provenance_sha256"]
    assert digest(Path(str(runtime) + ".capi_syms")) == built["capi_inventory_sha256"]
    assert status_method_body(source / HOST_TEST) == status_method_body(canonical / STATUS_OWNER)
    tree = ast.parse((source / NATIVE_TEST).read_text())
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "_PROGRAM" for target in node.targets)]
    assert len(assignments) == 1 and ast.literal_eval(assignments[0].value) == program.read_text()
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
    result = {"status": "RUNNING", "phase": "imports_and_runtime_admission", "arm": args.arm,
              "comparison_scope": "one-method inline reference versus candidate on the same candidate compiler/runtime",
              "candidate_source_inventory_sha256": manifest["candidate_source_inventory_sha256"],
              "canonical_status_owner_sha256": manifest["canonical_status_owner_sha256"],
              "runtime_result_sha256": args.runtime_result_sha256,
              "runtime_sha256": built["archive_sha256"], "program_sha256": digest(program),
              "target": TARGET, "bound_environment": FIXED_ENV, "native_execution": "UNRUN"}

    def phase(name):
        result.update(phase=name, elapsed_seconds=time.monotonic() - started)
        save_json(output / "result.json", result)

    try:
        phase("same_process_cpython_reference")
        reference_stdout, reference_stderr = io.StringIO(), io.StringIO()
        reference_globals = {"__name__": "__main__", "__file__": str(program)}
        with redirect_stdout(reference_stdout), redirect_stderr(reference_stderr):
            exec(compile(program.read_text(), str(program), "exec"), reference_globals)
        oracle = {"status": "OBSERVED", "mode": "same-process CPython reference",
                  "stdout": reference_stdout.getvalue(), "stderr": reference_stderr.getvalue()}
        save_json(output / "reference.json", oracle)
        assert (oracle["stdout"], oracle["stderr"]) == ("slot-status-owned-callsite-ok\n", "")
        oracle["status"] = "PASS"
        save_json(output / "reference.json", oracle)
        del reference_globals
        phase("imports_and_runtime_admission")
        from pcc.frontends.python import pipeline
        from pcc.frontends.python.codegen.layer1 import L1CodeGen
        from pcc.frontends.python.pipeline_targets import host_target_triple
        from pcc.ir.compat import ir
        from pcc.tools.runtime_archive_provenance import codegen_checksum
        from tests.runtime_fixture_provenance import _verified_test_runtime_archive
        from tests.owned_ir_validation import verify_ir_text
        assert host_target_triple() == TARGET and codegen_checksum() == built["codegen_checksum"]
        admitted, receipt = _verified_test_runtime_archive(runtime, threads=True, runtime_root=runtime_sources)
        assert admitted == runtime and receipt["member_count"] == 191
        namespace = {"ast": ast, "Path": Path, "L1CodeGen": L1CodeGen, "ir": ir,
                     "_I64": ir.IntType(64), "_PROGRAM": program.read_text()}
        load_definitions(source / HOST_TEST, {"_InlineReference"}, namespace)
        load_definitions(source / NATIVE_TEST,
                         {"_object_call_lines", "_capture_actual_status_edges", "_assert_actual_status_edges"}, namespace)
        closures = []
        generated = []
        raw_generate = L1CodeGen.generate
        raw_closure = pipeline._prepare_multi_source_compile_closure
        profile = {}
        with ExitStack() as stack:
            class PatchSet:
                def setattr(self, owner, name, value):
                    stack.enter_context(patch.object(owner, name, value))
            patches = PatchSet()

            def observe_closure(*positional, **keywords):
                paths, names = raw_closure(*positional, **keywords)
                row = {"sources": [str(Path(path).resolve()) for path in paths], "modules": list(names),
                       "recursive_stdlib": keywords["recursive_stdlib"]}
                closures.append(row)
                save_json(output / "discovered-closure.json", closures)
                assert row["sources"] == [str(program.resolve())] and row["modules"] == ["program"]
                assert row["recursive_stdlib"] is True
                return paths, names

            patches.setattr(pipeline, "_prepare_multi_source_compile_closure", observe_closure)
            if args.arm == "candidate":
                generated = namespace["_capture_actual_status_edges"](program, patches)
            else:
                patches.setattr(L1CodeGen, "_slot_call_check_status",
                                namespace["_InlineReference"]._slot_call_check_status)

                def capture_inline(self, module=None):
                    text = raw_generate(self, module)
                    if self._module_source_path == str(program.resolve()):
                        # Every completed reporter is published under this exact
                        # module-owned key; generated IR names are intentionally fresh.
                        reporter = self.runtime.get("__pcc_slot_call_status_report")
                        assert reporter is None, "Inline reference emitted a reporter"
                        generated.append({"owner": self.functions["main"].name,
                                          "text": str(text), "target_triple": str(self.module.triple),
                                          "runtime_reporter_key_absent": reporter is None})
                    return text
                patches.setattr(L1CodeGen, "generate", capture_inline)
            phase("unchanged_public_executable_compile")
            executable = output / "program.out"
            pipeline.compile_python(str(program), str(executable), backend="self", libpython_mode="off",
                                    ir_scaffold_mode="on", runtime_archive=str(runtime),
                                    target_triple=TARGET, profile=profile)
        phase("actual_owner_ir_contract")
        save_json(output / "compiler-profile.json", profile)
        assert len(closures) == 1 and len(generated) == 1
        capture = generated[0]
        assert capture["target_triple"] == TARGET
        text = capture["text"]
        (output / "actual-generated.ll").write_text(text)
        assert "strict.nolib.stub" not in text
        from pcc.backend.self_backend_parse import parse_self_backend_module
        from pcc.backend.self_backend_kernel import get_indexed_function_kernel
        parsed = parse_self_backend_module(text)
        function = next(function for function in parsed.functions if function.name == capture["owner"])
        kernel = get_indexed_function_kernel(function)
        object_calls = 0
        for block in range(len(kernel.block_names)):
            for index in range(kernel.instruction_count(block)):
                instruction = kernel.diagnostic_instruction(block, index)
                if instruction.kind == "call":
                    assert not instruction.data[2].startswith("py_cpy_")
                    object_calls += int(instruction.data[2] == "py_obj_call_slots")
        assert object_calls >= 3
        verify_ir_text(text)
        if args.arm == "candidate":
            save_json(output / "observed-status-edges.json", capture["witnesses"])
            edges = namespace["_assert_actual_status_edges"](capture)
            save_json(output / "verified-status-edges.json", edges)
            assert len(edges) == 3
            result["actual_candidate_edges"] = 3
        else:
            assert capture["runtime_reporter_key_absent"] is True
            result["actual_candidate_edges"] = 0
        assert executable.is_file() and executable.stat().st_mode & 0o111
        with executable.open("rb") as stream:
            assert stream.read(4) == b"\x7fELF"
        check_source_inputs()
        assert digest(runtime) == built["archive_sha256"]
        assert all(os.environ.get(name) == value for name, value in supervisor_environment.items())
        assert shutil.disk_usage(output).free >= 4 * GiB
        result.update(status="PASS", executable_sha256=digest(executable), executable_bytes=executable.stat().st_size,
                      generated_ir_sha256=digest(output / "actual-generated.ll"), generated_ir_bytes=len(text.encode()),
                      actual_owner=capture["owner"], object_slot_calls=object_calls, codegen_checksum=codegen_checksum(),
                      libpython="off", frontend_route="explicit-host-target/full-ordinary-import-discovery")
        phase("complete")
    except BaseException as exc:
        result.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc), elapsed_seconds=time.monotonic() - started)
        save_json(output / "result.json", result)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
