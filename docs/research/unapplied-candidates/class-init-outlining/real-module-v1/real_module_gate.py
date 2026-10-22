"""Coordinator-only real c_ast outlining structure gate; no native emission."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import gc
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
FIXTURE = "pcc/frontends/c/ast/c_ast.py"
PREFIX = "__pcc_class_init_body_"
CALLEE = re.compile(r"\bcall\b[^\n]*?@([\w.$-]+)\(")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with Path(path).open("w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def function_cfg(function):
    blocks = {block.name: block for block in function.blocks}
    assert len(blocks) == len(function.blocks) and function.blocks
    successors = {}
    for name, block in blocks.items():
        assert block.terminator is not None
        successors[name] = re.findall(r"label %([-\w.$]+)", block.terminator.text)
        assert all(target in blocks for target in successors[name])
    return blocks, successors


def reachable(successors, start, omitted=()):
    found, pending = set(), [start]
    while pending:
        name = pending.pop()
        if name in found or name in omitted:
            continue
        found.add(name); pending.extend(successors[name])
    return found


def ordered_sites(function, predicates):
    blocks, successors = function_cfg(function)
    entry = function.blocks[0].name
    live = reachable(successors, entry)
    sites = []
    for label, predicate in predicates:
        matches = [(name, index) for name, block in blocks.items()
                   for index, instruction in enumerate(block.instructions)
                   if predicate(instruction.text)]
        assert len(matches) == 1 and matches[0][0] in live, (function.name, label, matches)
        sites.append((label, *matches[0]))
    for earlier, later in zip(sites, sites[1:]):
        if earlier[1] == later[1]:
            assert earlier[2] < later[2], (function.name, earlier, later)
        else:
            assert later[1] not in reachable(successors, entry, (earlier[1],)), (function.name, earlier, later)
    return [{"event": label, "block": block, "instruction": index} for label, block, index in sites]


def helper_status_contract(function, helper):
    blocks, successors = function_cfg(function)
    matches = [(block.name, index, item) for block in function.blocks
               for index, item in enumerate(block.instructions)
               if item.opcode == "call" and "@" + helper + "(" in item.text]
    assert len(matches) == 1, (function.name, helper, matches)
    owner, index, call = matches[0]
    assert call.result_name is not None
    result_name = "%" + call.result_name.removeprefix("%")
    remaining = blocks[owner].instructions[index + 1:]
    assert remaining
    branch = re.fullmatch(r"\s*br i1 (%[-\w.$]+), label %([-\w.$]+), label %([-\w.$]+)\s*", remaining[-1].text)
    assert branch, (function.name, helper, remaining[-1].text)
    comparison = branch.group(1) + " = icmp eq i32 " + result_name + ", 1"
    assert any(item.text.strip() == comparison for item in remaining)
    assert all(item.opcode != "call" for item in remaining)
    success, failure = branch.group(2), branch.group(3)
    assert success != failure and failure == "err.exit"
    failure_calls = [CALLEE.search(item.text).group(1) for name in reachable(successors, failure)
                     for item in blocks[name].instructions if CALLEE.search(item.text)]
    assert not any(name.startswith(PREFIX) for name in failure_calls), (function.name, helper, failure_calls)
    assert success not in reachable(successors, failure)
    return {"helper": helper, "owner_block": owner, "success": success, "failure": failure}


def inspect_text(text, *, arm, phase, classes, admitted, suffix, output):
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_prepare import prepare_parsed_module_for_target
    from pcc.backend.self_backend_aarch64_darwin_abi import aggregate_returned_indirect, aggregate_returned_indirect_indexed
    from pcc.backend.self_backend_aarch64_darwin_symbols import asm_symbol
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend import self_backend_precise_stackmaps as stackmaps
    from pcc.frontends.python.compiled_default_passes import _has_py_cpy_call
    from pcc.ir.optimization.ir_mutator import MutableModule
    from root_contract import RootContractObserver

    assert not _has_py_cpy_call(text) and "strict.nolib.stub:" not in text
    mutable = MutableModule.parse(text)
    functions = {function.name: function for function in mutable.functions}
    assert len(functions) == len(mutable.functions)
    helpers = {name: function for name, function in functions.items() if name.startswith(PREFIX)}
    expected_helpers = {PREFIX + suffix + "_" + str(ordinal): name for name, ordinal in classes if name in admitted}
    assert set(helpers) == set(expected_helpers)
    calls_by_function = {name: Counter(CALLEE.findall(function.serialize())) for name, function in functions.items()}
    helper_callers = {name: Counter() for name in helpers}
    for owner, calls in calls_by_function.items():
        for callee, count in calls.items():
            if callee in helper_callers:
                helper_callers[callee][owner] += count
    init, top = "_pcc_py_module_init_" + suffix, "_pcc_py_module_top_" + suffix
    for name in (init, top):
        assert name in functions
        assert re.search(r"^define external void @" + re.escape(name) + r"\(\)", functions[name].header_line)
    guard = ".pcc.module.init." + suffix
    assert guard in functions[top].serialize()
    assert guard not in functions[init].serialize()
    statuses = []
    for name, function in helpers.items():
        header = function.header_line
        assert re.search(r"^define internal i32 @" + re.escape(name) + r"\(\).*\bnoinline\b", header), header
        assert "noreturn" not in header and "alwaysinline" not in header
        body = function.serialize()
        assert guard not in body
        calls = Counter(CALLEE.findall(body))
        assert calls["py_class_new"] == 1
        assert calls["py_recursion_enter"] == calls["py_recursion_leave"] == 0
        class_name = expected_helpers[name]
        assert re.search(r"\bstore ptr [^\n]*, ptr @\.class\." + re.escape(suffix + "." + class_name) + r"\b", body)
        returns = set(re.findall(r"\bret i32 (-?\d+)\b", body))
        assert returns == {"0", "1"}, (name, returns)
        if class_name != "NodeVisitor":
            assert re.search(r"\bload ptr, ptr @\.class\." + re.escape(suffix) + r"\.Node\b", body)
        assert helper_callers[name] == Counter((init, top)), (name, helper_callers[name])
        for owner in (init, top):
            statuses.append(helper_status_contract(functions[owner], name))
    class_order = {}
    for owner in (init, top):
        predicates = []
        for name, ordinal in classes:
            if arm == "candidate" and name in admitted:
                helper = PREFIX + suffix + "_" + str(ordinal)
                predicates.append((name, lambda line, helper=helper: bool(CALLEE.search(line)) and "@" + helper + "(" in line))
            else:
                pattern = re.compile(r"\bstore ptr [^\n]*, ptr @\.class\." + re.escape(suffix + "." + name) + r"\b")
                predicates.append((name, lambda line, pattern=pattern: bool(pattern.search(line))))
        class_order[owner] = ordered_sites(functions[owner], predicates)
    # The actual c_ast top-level function binding lies between Node and NodeVisitor.
    binding = lambda line: "@py_module_attr_set(" in line and "@.pyattr._build_visitor_dispatch," in line
    node = lambda line: bool(re.search(r"\bstore ptr [^\n]*, ptr @\.class\." + re.escape(suffix) + r"\.Node\b", line))
    visitor_helper = PREFIX + suffix + "_" + str(dict(classes)["NodeVisitor"])
    visitor = (lambda line: "@" + visitor_helper + "(" in line) if arm == "candidate" and "NodeVisitor" in admitted else (lambda line: bool(re.search(r"\bstore ptr [^\n]*, ptr @\.class\." + re.escape(suffix) + r"\.NodeVisitor\b", line)))
    binding_order = ordered_sites(functions[top], [("Node", node), ("_build_visitor_dispatch", binding), ("NodeVisitor", visitor)])
    public = {name: {"prefix": function.header_line.split("@", 1)[0].strip(),
                     "args": [arg.ty for arg in function.args], "trailing": function.trailing.strip()}
              for name, function in functions.items()
              if not re.search(r"\b(internal|private)\b", function.header_line.split("@", 1)[0])}
    weighted = Counter()
    for name, calls in calls_by_function.items():
        factor = 2 if name in helpers else 1
        for callee, count in calls.items():
            if callee.startswith(("py_", "pcc_gc_", "pcc_thread_")):
                weighted[callee] += factor * count
    textual_rows = {name: {"text_instructions_including_terminators": sum(len(block.instructions) for block in function.blocks),
                           "blocks": len(function.blocks)} for name, function in functions.items()}
    del mutable, functions
    gc.collect()
    parsed = parse_self_backend_module(text)
    assert parsed.triple == "arm64-apple-darwin23.6.0"
    public_globals = {item.name: {"type": str(item.type), "initializer": item.initializer,
        "constant": item.is_constant, "tls_model": item.tls_model, "alignment": item.alignment,
        "prefix": item.ir_prefix, "attributes": list(item.trailing_attributes)}
        for item in parsed.globals_ if not item.is_internal}
    rows, plans = [], []
    closed_kernels = set()
    observer = RootContractObserver(stackmaps, PREFIX)
    try:
        prepared = prepare_parsed_module_for_target(parsed,
            aggregate_returned_indirect=aggregate_returned_indirect,
            aggregate_returned_indirect_indexed=aggregate_returned_indirect_indexed,
            materialize_legacy_slots=False)
        with observer:
            for function in prepared.functions:
                kernel = get_indexed_function_kernel(function)
                row = {"name": function.name, "blocks": len(kernel.block_names),
                       "indexed_instructions": len(kernel.instruction_metadata) // 4,
                       "phis": len(kernel.phi_scalars) // 4,
                       "terminators": len(kernel.block_names), **textual_rows[function.name]}
                rows.append(row)
                plan = None
                try:
                    plan = stackmaps.build_function_stack_map_plan(function, prepared.globals_, target="aarch64-darwin",
                        identity_name=asm_symbol(function.name, prepared.module_symbols))
                    assert plan.packed_records is not None
                    plans.append({"function": function.name, "frame_size": plan.frame_size,
                                  "records": len(plan.packed_records)})
                finally:
                    if plan is not None and plan.packed_records is not None:
                        plan.packed_records.close()
                    kernel.close_native_tables()
                    closed_kernels.add(id(kernel))
    finally:
        observer.restore()
        # Preparation eagerly publishes kernels for every function. Retire
        # unvisited published owners too if preparation or planning raises.
        for function in parsed.functions:
            kernel = function.indexed_kernel
            if kernel is not None and id(kernel) not in closed_kernels:
                kernel.close_native_tables()
                closed_kernels.add(id(kernel))
    if helpers:
        observer.assert_complete(expected_helpers=len(helpers))
    else:
        assert not observer.rows and not observer.failures and not observer._installed
    assert len(plans) == len(rows) and {row["name"] for row in rows} == set(textual_rows)
    assert {row["function"] for row in observer.rows} == set(helpers)
    summary = {"phase": phase, "target": parsed.triple, "functions": len(rows), "public_functions": len(public),
               "helpers": len(helpers), "blocks": sum(row["blocks"] for row in rows),
               "indexed_instructions": sum(row["indexed_instructions"] for row in rows),
               "phis": sum(row["phis"] for row in rows), "terminators": sum(row["terminators"] for row in rows),
               "text_instructions_including_terminators": sum(row["text_instructions_including_terminators"] for row in rows),
               "max_function_instructions": max(row["indexed_instructions"] for row in rows),
               "max_function_text_instructions": max(row["text_instructions_including_terminators"] for row in rows),
               "largest_functions": sorted(rows, key=lambda row: (-row["indexed_instructions"], row["name"]))[:5],
               "text_bytes": len(text.encode()), "zero_libpython_calls": True, "zero_strict_stubs": True,
               "owned_ssa_cfg_verification": "PASS", "all_function_arm_root_planning": "PASS"}
    contract = {"summary": summary, "functions": rows, "public_signatures": public,
                "public_globals": public_globals,
                "expanded_runtime_call_counts": dict(sorted(weighted.items())), "class_order": class_order,
                "top_function_binding_order": binding_order, "helper_status_edges": statuses,
                "root_plans": plans, "helper_root_lease_contracts": observer.rows}
    save(output / (phase + "-contract.json"), contract)
    return summary


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    assert sha(packet / "manifest.json") == args.manifest_sha256
    manifest = json.loads((packet / "manifest.json").read_text())
    for name, row in manifest["artifacts"].items():
        assert sha(packet / name) == row["sha256"]
    assert not sys.flags.optimize and resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)
    for name in ("PCC_DISABLE_ROADMAP_DEEPWIRE", "PCC_NO_AUTO_PCC1", "PCC_TEST_NO_NATIVE_PROVISIONING"):
        assert os.environ.get(name) == "1"
    guard = {name: os.environ[name] for name in ("PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES")}
    assert guard["PCC_WORKER_TREE_BUDGET_BYTES"] == "4294967296"
    for name in manifest["effective_environment"]:
        assert name not in os.environ, "unadmitted inherited flag: " + name
    source = args.source.resolve(strict=True); output = args.output.resolve()
    assert sha(source.parent / "source-manifest.json") == manifest["sources"][args.arm]["inventory_sha256"]
    assert not output.exists() and output.parent.is_dir()
    assert all(not output.is_relative_to(path) and not path.is_relative_to(output) for path in (source, packet))
    assert Path.cwd().resolve() == source

    def source_checks():
        for path, expected in manifest["shared_source_files"].items():
            assert sha(source / path) == expected, path
        for path, expected in manifest["sources"][args.arm]["changed_files"].items():
            assert sha(source / path) == expected, path
    source_checks()
    syntax = ast.parse((source / FIXTURE).read_text())
    imports = [node for node in ast.walk(syntax) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert len(imports) == 1 and isinstance(imports[0], ast.Import)
    assert [(item.name, item.asname) for item in imports[0].names] == [("sys", None)]
    assert {node.attr for node in ast.walk(syntax) if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "sys"} == {"stdout"}
    classes = [(node.name, ordinal) for ordinal, node in enumerate(syntax.body) if isinstance(node, ast.ClassDef)]
    assert len(classes) == 56
    output.mkdir(); sys.dont_write_bytecode = True
    os.environ.update(manifest["effective_environment"])
    sys.path[:0] = [str(source), str(packet)]
    from pcc.frontends.python.codegen.class_gen import ClassLowering
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.stmt_dispatch_lowering import StmtDispatchLoweringMixin
    from pcc.frontends.python.compiled_owned_passes import run_owned_passes
    from pcc.frontends.python.pipeline_import_policy import NATIVE_BUILTIN_IMPORTS, REQUIRED_COMPILED_STDLIB_PROVIDERS
    from pcc.frontends.python.pipeline_pass_config import PYTHON_IR_PASS_DEFAULT_TIER, resolve_python_ir_pass_names, python_ir_pass_should_skip_module
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.codegen.module_name_lowering import module_symbol_suffix
    from pcc.tools.runtime_archive_provenance import codegen_checksum
    assert "sys" in NATIVE_BUILTIN_IMPORTS and "sys" not in REQUIRED_COMPILED_STDLIB_PROVIDERS
    passes = resolve_python_ir_pass_names(default_raw="default")
    assert tuple(passes) == PYTHON_IR_PASS_DEFAULT_TIER == ("mem2reg", "sroa")
    assert not python_ir_pass_should_skip_module(MODULE)
    assert codegen_checksum() == manifest["sources"][args.arm]["codegen_checksum"]
    suffix = module_symbol_suffix(MODULE)
    init, top = "_pcc_py_module_init_" + suffix, "_pcc_py_module_top_" + suffix
    result = {"schema": "pcc.class-init-outline-real-module.v1", "status": "RUNNING", "arm": args.arm,
              "manifest_sha256": args.manifest_sha256, "source_inventory_sha256": manifest["sources"][args.arm]["inventory_sha256"],
              "input_sha256": sha(source / FIXTURE), "target": manifest["target"], "passes": passes,
              "scope": "fresh complete c_ast library frontend; ARM owned root planning, no native emission or timing claim"}
    bodies, admissions, dispatched = [], [], []
    originals = [(ClassLowering, "_emit_class_init", ClassLowering._emit_class_init),
                 (StmtDispatchLoweringMixin, "_emit_stmt_impl", StmtDispatchLoweringMixin._emit_stmt_impl)]
    body_method, dispatch_method = originals[0][2], originals[1][2]
    def observe_body(self, cd, info, **kwargs):
        bodies.append({"class": cd.name, "owner": self.parent.current_function.name})
        return body_method(self, cd, info, **kwargs)
    def observe_dispatch(self, statement):
        if self.current_function is not None and self.current_function.name == top:
            dispatched.append((type(statement).__name__, getattr(statement, "name", ""), statement.span.line))
        return dispatch_method(self, statement)
    ClassLowering._emit_class_init = observe_body
    StmtDispatchLoweringMixin._emit_stmt_impl = observe_dispatch
    if args.arm == "candidate":
        original = ClassLowering._maybe_emit_class_init_outline
        originals.append((ClassLowering, "_maybe_emit_class_init_outline", original))
        def observe_admission(self, cd, info):
            owner = self.parent.current_function.name
            admitted = original(self, cd, info)
            assert type(admitted) is bool and self.parent.current_function.name == owner
            admissions.append({"class": cd.name, "owner": owner, "admitted": admitted})
            return admitted
        ClassLowering._maybe_emit_class_init_outline = observe_admission
    def phase(name):
        result["phase"] = name; save(output / "result.json", result); print(name, flush=True)
    try:
        phase("fresh-parse-infer-generate")
        started = time.monotonic_ns()
        parsed = parse_and_lift((source / FIXTURE).read_text(), str(source / FIXTURE), MODULE)
        expected_dispatch = [(type(item).__name__, getattr(item, "name", ""), item.span.line) for item in parsed.body]
        typed = infer_module(parsed)
        generator = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        generator._strict_no_libpython = True; generator._prefer_native_callable_values = True
        generator._module_source_path = str(source / FIXTURE)
        generator._target_triple = manifest["target"]; generator.module.triple = manifest["target"]
        generator._python_library = True; generator._skip_program_main = True
        assert generator._native_module_exports is None and not generator._sibling_module_inits
        assert not generator._suppress_implicit_gc_roots and not generator._suppress_borrowed_return_retain
        raw = str(generator.generate(typed))
        assert generator._direct_indexed_module is None
        assert dispatched == expected_dispatch
        result["observed_generation_ns"] = time.monotonic_ns() - started
        result["effective_runtime_threads"] = generator._runtime_threads_enabled
        assert generator._runtime_threads_enabled == manifest["runtime_threads"]
        # Check the original generated objects before reparsing their rendered text.
        for function in generator.module.functions:
            for block in function.blocks:
                assert block.parent is function and block.function is function
                assert all(record.block is block for record in block._instrs)
        result["owned_record_containers"] = "PASS"
        admitted = set()
        names = [name for name, _ in classes]
        if args.arm == "baseline":
            assert not admissions
            for owner in (init, top):
                assert [row["class"] for row in bodies if row["owner"] == owner] == names
        else:
            for owner in (init, top):
                selected = [row for row in admissions if row["owner"] == owner]
                assert [row["class"] for row in selected] == names
                selected_admitted = {row["class"] for row in selected if row["admitted"]}
                if owner == init: admitted = selected_admitted
                else: assert selected_admitted == admitted
            assert len(admissions) == 2 * len(classes)
            for name, ordinal in classes:
                owners = [row["owner"] for row in bodies if row["class"] == name]
                expected = [PREFIX + suffix + "_" + str(ordinal)] if name in admitted else [init, top]
                assert owners == expected, (name, owners, expected)
        result["admitted_classes"] = [name for name, _ in classes if name in admitted]
        result["inline_classes"] = [name for name, _ in classes if name not in admitted]
        result["source_class_order"] = names
        result["generation_witnesses"] = {"body_emissions": bodies, "admissions": admissions, "top_dispatch": dispatched}
        del generator, typed, parsed
        gc.collect()
        (output / "raw.ll").write_text(raw)
        phase("raw-owned-verification-and-arm-root-contract")
        result["raw"] = inspect_text(raw, arm=args.arm, phase="raw", classes=classes, admitted=admitted, suffix=suffix, output=output)
        phase("default-owned-passes")
        post = run_owned_passes(raw, passes, strict_no_libpython=True)
        del raw; gc.collect()
        (output / "postpasses.ll").write_text(post)
        phase("post-owned-verification-and-arm-root-contract")
        result["post"] = inspect_text(post, arm=args.arm, phase="post", classes=classes, admitted=admitted, suffix=suffix, output=output)
        del post; gc.collect()
        result["artifacts"] = {name: {"sha256": sha(output / name), "bytes": (output / name).stat().st_size}
                               for name in ("raw.ll", "postpasses.ll", "raw-contract.json", "post-contract.json")}
        source_checks()
        assert sha(source.parent / "source-manifest.json") == manifest["sources"][args.arm]["inventory_sha256"]
        assert all(os.environ.get(name) == value for name, value in guard.items())
        for name, module in tuple(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc."):
                location = getattr(module, "__file__", None)
                if location is not None: assert Path(location).resolve().is_relative_to(source), (name, location)
        result["import_source_checks"] = "PASS"; result["status"] = "PASS"
    except BaseException as exc:
        result["status"] = "FAIL"; result["exception"] = {"type": type(exc).__name__, "message": str(exc)}
        save(output / "result.json", result); traceback.print_exc(); raise
    finally:
        for owner, name, original in reversed(originals): setattr(owner, name, original)
    save(output / "result.json", result)
    print(json.dumps({key: value for key, value in result.items() if key != "generation_witnesses"}, sort_keys=True))


if __name__ == "__main__":
    main()
