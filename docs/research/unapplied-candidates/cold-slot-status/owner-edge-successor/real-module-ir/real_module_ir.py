"""Bounded real c_ast host IR experiment, run only by the coordinator.

No native emission, runtime, FFI, discovery subprocess or reconstructed Stage1
context. The real module imports only the compiler-owned sys builtin. The
baseline restores the reviewed former inline method in the same frontend.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import sys
import time
import traceback


MODULE = "pcc.frontends.c.ast.c_ast"
INPUT = "pcc/frontends/c/ast/c_ast.py"
REPORTER_KEY = "__pcc_slot_call_status_report"
STATUS_METHOD = "_slot_call_check_status"
REPORT_CALLS = {"py_err_occurred", "py_exc_new", "py_raise",
                "py_current_exception", "py_exc_append_frame_source"}
SSA = re.compile(r'%[\w.$-]+')
CALLEE = re.compile(r'@([\w.$-]+)\(')
CONDITIONAL = re.compile(r'br i1 (%[\w.$-]+), label %([\w.$-]+), label %([\w.$-]+)')
UNCONDITIONAL = re.compile(r'br label %([\w.$-]+)')


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def save_json(path, value):
    with path.open("wb") as stream:
        stream.write(json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def normalized_instructions(instructions):
    """Local alpha-renaming preserves repeated-value relationships in a block."""
    names = {}

    def rename(match):
        token = match.group()
        if token not in names:
            names[token] = "%v" + str(len(names))
        return names[token]

    return [SSA.sub(rename, text.strip()) for text in instructions]


def method_body(path, class_name=None):
    tree = ast.parse(path.read_text())
    roots = tree.body if class_name is None else next(
        node.body for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = [node for root in roots for node in ast.walk(root)
               if isinstance(node, ast.FunctionDef) and node.name == STATUS_METHOD]
    assert len(methods) == 1
    return ast.dump(ast.Module(body=methods[0].body, type_ignores=[]), include_attributes=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--canonical", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    canonical = args.canonical.resolve(strict=True)
    packet = Path(__file__).resolve().parent
    manifest = json.loads((packet / "manifest.json").read_text())
    assert digest(Path(__file__)) == manifest["driver_sha256"]
    assert not sys.flags.optimize, "Assertions are required"
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    address_limit = resource.getrlimit(resource.RLIMIT_AS)
    assert 0 < address_limit[0] <= address_limit[1] <= 4 * 1024**3
    assert source != canonical
    output = args.output.resolve()
    assert not output.is_relative_to(source) and not output.is_relative_to(canonical)
    output.mkdir(parents=True, exist_ok=False)

    def check_inputs():
        assert digest(source.parent / "source-manifest.json") == manifest["candidate_source_inventory_sha256"]
        assert digest(canonical.parent / "materialized-source-manifest.json") == manifest["canonical_source_inventory_sha256"]
        for relative, expected in manifest["candidate_files"].items():
            assert digest(source / relative) == expected, relative
        for relative, expected in manifest["canonical_files"].items():
            assert digest(canonical / relative) == expected, relative

    check_inputs()
    fixture = source / INPUT
    fixture_lines = fixture.read_text().splitlines()
    syntax = ast.parse(fixture.read_text())
    imports = [node for node in ast.walk(syntax) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 1 and isinstance(imports[0], ast.Import)
    assert [(item.name, item.asname) for item in imports[0].names] == [("sys", None)]
    assert {node.attr for node in ast.walk(syntax) if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "sys"} == {"stdout"}
    reference_path = source / "tests/python/test_slot_call_status_reporting.py"
    baseline_path = canonical / "pcc/frontends/python/codegen/call_object_lowering.py"
    assert method_body(reference_path, "_InlineReference") == method_body(baseline_path)

    for key in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT"):
        os.environ[key] = "0"
    os.environ["PCC_SELF_TARGET_PASSES"] = "off"
    os.environ["PCC_PYTHON_IR_PASSES"] = "default"
    os.environ["PCC_DISABLE_ROADMAP_DEEPWIRE"] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))

    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.compiled_default_passes import _has_py_cpy_call
    from pcc.frontends.python.compiled_owned_passes import run_owned_passes
    from pcc.frontends.python.pipeline_import_policy import NATIVE_BUILTIN_IMPORTS, REQUIRED_COMPILED_STDLIB_PROVIDERS
    from pcc.frontends.python.pipeline_pass_config import PYTHON_IR_PASS_DEFAULT_TIER, python_ir_pass_should_skip_module, resolve_python_ir_pass_names
    from pcc.frontends.python.pipeline_targets import host_target_triple
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.ir.optimization.ir_mutator import MutableModule
    from tests.owned_ir_validation import verify_ir_text
    from tests.python.test_slot_call_status_reporting import _InlineReference

    assert "sys" in NATIVE_BUILTIN_IMPORTS and "sys" not in REQUIRED_COMPILED_STDLIB_PROVIDERS
    passes = resolve_python_ir_pass_names(default_raw="default")
    assert tuple(passes) == PYTHON_IR_PASS_DEFAULT_TIER == ("mem2reg", "sroa")
    assert not python_ir_pass_should_skip_module(MODULE)
    for name, module in tuple(sys.modules.items()):
        if name == "pcc" or name.startswith("pcc.") or name == "tests" or name.startswith("tests."):
            location = getattr(module, "__file__", None)
            if location is not None:
                assert Path(location).resolve().is_relative_to(source), (name, location)

    result = {"schema": "pcc.cold-slot-real-module-ir.v1", "status": "RUNNING",
              "scope": "host single-source library IR; no native execution or Stage1 timing claim",
              "input_module": MODULE, "input_sha256": digest(fixture),
              "source_base_commit": manifest["production_base_commit"],
              "candidate_source_inventory_sha256": manifest["candidate_source_inventory_sha256"],
              "canonical_source_inventory_sha256": manifest["canonical_source_inventory_sha256"],
              "source": str(source), "canonical": str(canonical),
              "manifest_sha256": digest(packet / "manifest.json"),
              "target": host_target_triple(), "passes": passes,
              "source_classes": sum(isinstance(node, ast.ClassDef) for node in ast.walk(syntax)),
              "source_functions": sum(isinstance(node, ast.FunctionDef) for node in ast.walk(syntax)),
              "address_space_limits": list(address_limit), "arms": {}}
    result_path = output / "result.json"

    def phase(name):
        result["phase"] = name
        save_json(result_path, result)
        print(name, flush=True)

    def observed_class(base, witnesses):
        class Observed(base):
            def _slot_call_check_status(self, status, operation, span=None):
                owner = self.current_function
                definition = self.current_func_def
                target = self._current_try_err_block()
                before = None if target is None else tuple(str(item) for item in target.instructions)
                row = {"owner": owner.name, "status": str(status),
                       "status_block": self.builder.block.name, "operation": operation,
                       "source_function": None if definition is None else definition.name,
                       "span": None if span is None else [span.file, span.line, span.col],
                       "original_cleanup": None if target is None else target.name}
                answer = base._slot_call_check_status(self, status, operation, span)
                assert self.current_function is owner and self.current_func_def is definition
                assert self._current_try_err_block() is target
                if target is not None:
                    assert tuple(str(item) for item in target.instructions) == before
                row["ready"] = self.builder.block.name
                witnesses.append(row)
                return answer
        return Observed

    def inspect(text, witnesses, reporter, *, after_default_passes=False):
        assert not _has_py_cpy_call(text), "Actual libpython fallback call"
        assert "strict.nolib.stub" not in text, "Strict no-libpython replaced an input body"
        owned = verify_ir_text(text)
        assert owned.triple == result["target"]
        del owned
        parsed = MutableModule.parse(text)
        functions = {function.name: function for function in parsed.functions}
        blocks = {name: {block.name: block for block in function.blocks}
                  for name, function in functions.items()}
        strings = {}
        for line in parsed.globals_:
            match = re.fullmatch(r'@([\w.$-]+) = internal constant \[\d+ x i8\] c"((?:\\[0-9A-Fa-f]{2})+)"', line.strip())
            if match:
                strings["@" + match.group(1)] = bytes.fromhex(match.group(2).replace("\\", "")).decode().removesuffix("\0")

        def call_args(instruction, callee):
            match = re.search(r'@' + re.escape(callee) + r'\((.*)\)$', instruction.strip())
            assert match, instruction
            return [] if not match.group(1) else [item.split(" ", 1) for item in match.group(1).split(", ")]

        def text_arg(argument):
            assert argument[0] == "ptr"
            return None if argument[1] == "null" else strings[argument[1]]

        public = {name: {"prefix": function.header_line.split("@", 1)[0].strip(),
                         "args": [arg.ty for arg in function.args], "trailing": function.trailing.strip()}
                  for name, function in functions.items()
                  if not re.search(r'\b(internal|private)\b', function.header_line.split("@", 1)[0])}
        calls = Counter()
        noreturn = []
        reporter_blocks = []
        definitions = {}
        for function in parsed.functions:
            local_definitions = {}
            definitions[function.name] = local_definitions
            if "noreturn" in function.header_line:
                noreturn.append(function.header_line.strip())
            for block in function.blocks:
                for instruction in block.instructions:
                    if instruction.result_name is not None:
                        assert instruction.result_name not in local_definitions, (function.name, instruction.result_name)
                        local_definitions[instruction.result_name] = (block.name, instruction)
                    if instruction.opcode == "call":
                        match = CALLEE.search(instruction.text)
                        if match:
                            calls[match.group(1)] += 1
                            if match.group(1) == reporter:
                                reporter_blocks.append((function.name, block.name))
        # Baseline report blocks and their corresponding pending-error blocks
        # have the same cleanup successor. Full mem2reg may name both edges in
        # a cleanup PHI. Candidate outlining legitimately removes one edge.
        report_predecessors = {}
        if reporter is None:
            for row in witnesses:
                local = blocks[row["owner"]]
                branch = CONDITIONAL.fullmatch(local[row["status_block"]].terminator.text.strip())
                assert branch, row
                error = branch.group(2)
                report_branch = CONDITIONAL.fullmatch(local[error].terminator.text.strip())
                assert report_branch, row
                report = report_branch.group(3)
                local_aliases = report_predecessors.setdefault(row["owner"], {})
                assert report not in local_aliases, row
                local_aliases[report] = error

        cleanup_cache = {}
        cleanup_programs = {}
        phi_edges_collapsed = 0

        def cleanup_contract(owner, name):
            nonlocal phi_edges_collapsed
            key = (owner, name)
            if key in cleanup_cache:
                return cleanup_cache[key]
            block = blocks[owner][name]
            lines = []
            aliases = report_predecessors.get(owner, {}) if after_default_passes else {}
            for item in block.instructions:
                line = item.text.strip()
                if item.opcode == "phi" and aliases:
                    first = line.index("[")
                    prefix, incoming = line[:first], line[first:]
                    pattern = r'\[\s*([^\[\]]+)\s*,\s*%([\w.$-]+)\s*\]'
                    pairs = [(match.group(1).strip(), match.group(2))
                             for match in re.finditer(pattern, incoming)]
                    assert pairs and not re.sub(pattern, "", incoming).strip(" ,")
                    values = {pred: value for value, pred in pairs}
                    assert len(values) == len(pairs)
                    retained = []
                    for value, pred in pairs:
                        if pred in aliases:
                            original = aliases[pred]
                            # Do not weaken a changed ownership value: the
                            # surviving pending-error edge must be present
                            # and carry exactly the same scalar SSA/constant.
                            assert original in values and values[original] == value, (owner, name, line)
                            phi_edges_collapsed += 1
                        else:
                            retained.append((value, pred))
                    if len(retained) != len(pairs):
                        line = prefix + ", ".join("[ " + value + ", %" + pred + " ]" for value, pred in retained)
                lines.append(line)
            program = normalized_instructions(lines)
            program_digest = hashlib.sha256(json_bytes(program)).hexdigest()
            if program_digest in cleanup_programs:
                assert cleanup_programs[program_digest] == program
            else:
                cleanup_programs[program_digest] = program
            call_names = [CALLEE.search(item.text).group(1) for item in block.instructions
                          if item.opcode == "call" and CALLEE.search(item.text)]
            row = {"cleanup_shape_sha256": program_digest, "cleanup_calls": call_names,
                   "cleanup_terminator": block.terminator.opcode}
            cleanup_cache[key] = row
            return row

        edges = []
        error_blocks = []
        for row in witnesses:
            local = blocks[row["owner"]]
            block = local[row["status_block"]]
            status_name = row["status"].removeprefix("%")
            # A production call can be followed by lease-cleanup branches
            # before its scalar status is checked. Require the unique producer
            # in this exact function, not necessarily in the checking block.
            producer_block, producer = definitions[row["owner"]][status_name]
            assert producer.opcode == "call", row
            status_call = producer.text.strip()
            assert re.search(r'= call i64(?:\s|\()', status_call), status_call
            checks = [item for item in block.instructions if item.opcode == "icmp"
                      and re.fullmatch(r'%[\w.$-]+ = icmp slt i64 ' + re.escape(row["status"]) + r', 0', item.text.strip())]
            assert len(checks) == 1, row
            branch = CONDITIONAL.fullmatch(block.terminator.text.strip())
            assert branch and branch.group(1) == "%" + checks[0].result_name, row
            error, ready = branch.group(2, 3)
            assert ready == row["ready"] and error != ready and error.startswith("call.slot.error")
            error_block = local[error]
            error_blocks.append((row["owner"], error))
            expected_message = "slot-call " + row["operation"] + " failed"
            expected_frame = None
            if row["span"] is not None:
                filename, line_number, _column = row["span"]
                assert filename == str(fixture), row
                source_line = fixture_lines[line_number - 1].strip() if 0 < line_number <= len(fixture_lines) else ""
                expected_frame = [row["source_function"] or "<module>", filename, source_line, line_number]
            if reporter is not None:
                error_calls = [item for item in error_block.instructions if item.opcode == "call"]
                assert len(error_calls) == 1 and CALLEE.search(error_calls[0].text).group(1) == reporter
                arguments = call_args(error_calls[0].text, reporter)
                assert len(arguments) == 6 and text_arg(arguments[0]) == expected_message
                assert arguments[4][0] == "i32" and arguments[5][0] == "i1"
                if expected_frame is None:
                    assert [text_arg(argument) for argument in arguments[1:4]] == [None] * 3
                    assert arguments[4:] == [["i32", "0"], ["i1", "0"]]
                else:
                    assert [text_arg(argument) for argument in arguments[1:4]] + [int(arguments[4][1])] == expected_frame
                    assert arguments[5][1] == "1"
                finish = UNCONDITIONAL.fullmatch(error_block.terminator.text.strip())
                assert finish, row
                cleanup = finish.group(1)
                assert not any(reporter in item.text for item in local[ready].instructions)
            else:
                pending = [item for item in error_block.instructions if item.opcode == "call"]
                assert len(pending) == 1 and CALLEE.search(pending[0].text).group(1) == "py_err_occurred"
                pending_checks = [item for item in error_block.instructions if item.opcode == "icmp"
                                  and re.fullmatch(r'%[\w.$-]+ = icmp ne i64 %' + re.escape(pending[0].result_name) + r', 0', item.text.strip())]
                assert len(pending_checks) == 1
                finish = CONDITIONAL.fullmatch(error_block.terminator.text.strip())
                assert finish and finish.group(1) == "%" + pending_checks[0].result_name, row
                cleanup, report = finish.group(2, 3)
                assert report.startswith("call.slot.report")
                assert UNCONDITIONAL.fullmatch(local[report].terminator.text.strip()).group(1) == cleanup
                report_calls = [item for item in local[report].instructions if item.opcode == "call"]
                assert [CALLEE.search(item.text).group(1) for item in report_calls] == (
                    ["py_exc_new", "py_raise", "py_current_exception"]
                    + ([] if expected_frame is None else ["py_exc_append_frame_source"]))
                arguments = call_args(report_calls[0].text, "py_exc_new")
                assert arguments[0] == ["i64", "7"] and text_arg(arguments[1]) == expected_message
                if expected_frame is not None:
                    arguments = call_args(report_calls[-1].text, "py_exc_append_frame_source")
                    assert [text_arg(argument) for argument in arguments[1:4]] + [int(arguments[4][1])] == expected_frame
            assert row["original_cleanup"] is None or cleanup == row["original_cleanup"], row
            assert cleanup != error and cleanup != ready
            edges.append({"source_function": row["source_function"], "span": row["span"],
                          "operation": row["operation"],
                          "message": expected_message, "frame": expected_frame,
                          "status_call": normalized_instructions([status_call])[0],
                          **cleanup_contract(row["owner"], cleanup)})
        assert witnesses and len(error_blocks) == len(set(error_blocks))
        if reporter is not None:
            helper = functions[reporter]
            assert helper.header_line.startswith("define internal void @")
            assert [arg.ty for arg in helper.args] == ["ptr"] * 4 + ["i32", "i1"]
            assert "noinline" in helper.trailing and "noreturn" not in helper.trailing
            assert sorted(reporter_blocks) == sorted(error_blocks)
            assert [block.name for block in helper.blocks] == ["entry", "report", "frame", "done"]
            assert helper.blocks[-1].terminator.text.strip() == "ret void"
            assert all(item.opcode != "unreachable" for block in helper.blocks for item in block.instructions)
        counts = {"functions": len(functions), "public_functions": len(public),
                  "blocks": sum(len(function.blocks) for function in parsed.functions),
                  "instructions": sum(len(block.instructions) for function in parsed.functions for block in function.blocks),
                  "text_bytes": len(text.encode()), "status_sites": len(witnesses),
                  "object_slot_calls": calls["py_obj_call_slots"],
                  "reporter_calls": 0 if reporter is None else calls[reporter],
                  "unreachable": sum(item.opcode == "unreachable" for function in parsed.functions
                                     for block in function.blocks for item in block.instructions),
                  "strict_no_libpython_stubs": 0, "actual_libpython_calls": 0}
        runtime_calls = {name: count for name, count in calls.items()
                         if (name.startswith("py_") or name.startswith("pcc_gc_") or name.startswith("pcc_thread_"))
                         and name not in REPORT_CALLS}
        return {"counts": counts, "public_signatures": public,
                "noreturn_definitions": sorted(noreturn),
                "noreturn_declarations": sorted(line.strip() for line in parsed.declarations if "noreturn" in line),
                "runtime_calls_excluding_reporter_protocol": runtime_calls,
                "report_protocol_calls": {name: calls[name] for name in sorted(REPORT_CALLS)},
                "cleanup_programs": cleanup_programs,
                "postpass_equal_value_report_phi_edges_collapsed": phi_edges_collapsed,
                "edges": edges}

    try:
        for label, base in (("baseline", _InlineReference), ("candidate", L1CodeGen)):
            phase(label + ":parse-infer-generate")
            witnesses = []
            started = time.monotonic_ns()
            module = infer_module(parse_and_lift(fixture.read_text(), str(fixture), MODULE))
            codegen = observed_class(base, witnesses)(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
            codegen._strict_no_libpython = True
            codegen._prefer_native_callable_values = True
            codegen._module_source_path = str(fixture)
            codegen._target_triple = result["target"]
            codegen.module.triple = result["target"]
            codegen._python_library = True
            codegen._skip_program_main = True
            assert codegen._native_module_exports is None and not codegen._sibling_module_inits
            assert not codegen._suppress_implicit_gc_roots and not codegen._suppress_borrowed_return_retain
            raw = str(codegen.generate(module))
            generated_ns = time.monotonic_ns() - started
            reporter = codegen.runtime[REPORTER_KEY].name if label == "candidate" else None
            del codegen, module
            (output / (label + ".ll")).write_text(raw)
            save_json(output / (label + ".witnesses.json"), witnesses)
            phase(label + ":raw-contract")
            raw_contract = inspect(raw, witnesses, reporter)
            save_json(output / (label + ".raw-contract.json"), raw_contract)
            phase(label + ":default-owned-passes")
            started = time.monotonic_ns()
            processed = run_owned_passes(raw, passes, True)
            passes_ns = time.monotonic_ns() - started
            (output / (label + ".postpasses.ll")).write_text(processed)
            phase(label + ":post-contract")
            post_contract = inspect(processed, witnesses, reporter, after_default_passes=True)
            save_json(output / (label + ".post-contract.json"), post_contract)
            result["arms"][label] = {"raw": raw_contract["counts"], "postpasses": post_contract["counts"],
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "postpasses_sha256": hashlib.sha256(processed.encode()).hexdigest(),
                "observed_generation_ns": generated_ns, "owned_passes_ns": passes_ns,
                "timing_scope": "single order with observation/verification; not a performance benchmark"}
            del raw, processed, raw_contract, post_contract, witnesses
            phase(label + ":complete")
        phase("compare")
        for name in ("raw", "post"):
            baseline = json.loads((output / ("baseline." + name + "-contract.json")).read_text())
            candidate = json.loads((output / ("candidate." + name + "-contract.json")).read_text())
            for key in ("public_signatures", "noreturn_definitions", "noreturn_declarations",
                        "runtime_calls_excluding_reporter_protocol", "cleanup_programs", "edges"):
                assert baseline[key] == candidate[key], (name, key)
            sites = baseline["counts"]["status_sites"]
            assert candidate["counts"]["status_sites"] == sites > 0
            assert candidate["counts"]["reporter_calls"] == sites
            assert baseline["counts"]["object_slot_calls"] == candidate["counts"]["object_slot_calls"]
            assert baseline["counts"]["unreachable"] - candidate["counts"]["unreachable"] == sites
            assert baseline["counts"]["blocks"] - candidate["counts"]["blocks"] == 2 * sites - 4
            assert candidate["counts"]["functions"] == baseline["counts"]["functions"] + 1
            for callee in REPORT_CALLS - {"py_exc_append_frame_source"}:
                assert baseline["report_protocol_calls"][callee] - candidate["report_protocol_calls"][callee] == sites - 1
            frame_sites = sum(edge["frame"] is not None for edge in baseline["edges"])
            assert baseline["report_protocol_calls"]["py_exc_append_frame_source"] - candidate["report_protocol_calls"]["py_exc_append_frame_source"] == frame_sites - 1
            assert candidate["counts"]["instructions"] < baseline["counts"]["instructions"]
            assert candidate["counts"]["blocks"] < baseline["counts"]["blocks"]
        check_inputs()
        result["status"] = "PASS"
        phase("complete")
    except BaseException as error:
        result["status"] = "FAIL"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        save_json(result_path, result)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
