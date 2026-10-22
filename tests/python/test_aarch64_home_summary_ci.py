"""One CI-only finite helper A/B receipt; no project imports or subprocesses."""

import ast
import gc
import hashlib
import json
import sys
import time

from tests.python.test_aarch64_regalloc_single_seed import (
    FakeKernel, PRE_SUMMARY_AST_SHA256, SOURCE, canonical_facts_ast_dump, case, load_facts,
)


def test_home_summary_ci_same_input_abba(tmp_path):
    # Included only in the existing CI default invocation. This measures the
    # source-extracted helper with finite canonical fake CFGs, not a backend
    # build or native compiler. Both sides use the same accessor fallback.
    functions = {
        "before": load_facts(SOURCE, canonical=True, pre_summary=True)[0],
        "after": load_facts(SOURCE, canonical=True)[0],
    }
    chain = [[index + 1] for index in range(127)] + [[]]
    shapes = {
        "high-reuse": case(chain, {126: list(range(32)), 127: list(range(32))}),
        "zero-overlap": case([[index + 1] if index % 2 == 0 else [] for index in range(64)],
                             {index: [index // 2] for index in range(1, 64, 2)}),
        "low-reuse": case(chain, {42: [0], 127: [1]}),
    }
    source = SOURCE.read_bytes()
    function_ast = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                        and node.name == "_function_level_facts")
    report = {
        "schema": 1,
        "scope": "synthetic source-extracted function helper; no production wall-time claim",
        "baseline_commit": "7c1218dba0bfb8bbccc768259b15ff2891b08241",
        "ast_dump_convention": "full dump with empty fields/contexts; identity-only absent/eager Import.is_lazy equivalence",
        "baseline_function_ast_sha256": PRE_SUMMARY_AST_SHA256,
        "candidate_source_sha256": hashlib.sha256(source).hexdigest(),
        "candidate_function_ast_sha256": hashlib.sha256(canonical_facts_ast_dump(function_ast).encode()).hexdigest(),
        "python": sys.version,
        "gc_enabled": gc.isenabled(),
        "clock": "perf_counter_ns",
        "rounds": 3,
        "order": ["before", "after", "after", "before"],
        "preparation": "same loader; fresh kernel and full GC outside every single-call timer",
        "shapes": [],
    }
    for name, blocks in shapes.items():
        input_bytes = json.dumps(blocks, sort_keys=True, separators=(",", ":")).encode()
        values = {value for block in blocks for _, _, used in block["instructions"] for value in used}
        seed_traffic = sum(len({value for _, _, used in block["instructions"] for value in used})
                           for block in blocks)
        shape = {
            "name": name,
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "B": len(blocks), "E": sum(len(block["edges"]) for block in blocks),
            "V": len(values), "seed_traffic": seed_traffic,
            "samples": [],
        }
        expected = None
        for round_index in range(3):
            for slot, side in enumerate(report["order"]):
                function = functions[side]
                kernel = FakeKernel(blocks)
                gc.collect()
                start = time.perf_counter_ns()
                output = function(kernel)
                elapsed = time.perf_counter_ns() - start
                assert len(output) == 7
                normalized = tuple(tuple(value.items()) if isinstance(value, dict) else tuple(value)
                                   for value in output)
                if expected is None:
                    expected = normalized
                else:
                    assert normalized == expected
                shape["samples"].append({"round": round_index, "slot": slot,
                                          "side": side, "elapsed_ns": elapsed})
                # Do not carry transient output/kernel roots into the next
                # sample's full collection. Setup is symmetric in every slot.
                del output, normalized, kernel
        assert json.dumps(blocks, sort_keys=True, separators=(",", ":")).encode() == input_bytes
        shape["all_seven_outputs_equal"] = True
        report["shapes"].append(shape)
    assert sum(len(shape["samples"]) for shape in report["shapes"]) == 36
    # The existing build/worker-prior-preflight/**/*.json upload includes the
    # default invocation's pytest basetemp; no separate job/run is introduced.
    (tmp_path / "home-summary-abba.json").write_text(json.dumps(report, indent=2) + "\n")
