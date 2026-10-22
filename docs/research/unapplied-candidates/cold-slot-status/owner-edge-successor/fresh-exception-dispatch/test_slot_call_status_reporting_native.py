"""Actual generated status helper versus its former inline runtime protocol.

Host pcc0 owns IR construction, C compilation, object emission and linking.
This exercises the real runtime, not a modeled exception provider or pcc1.
An explicit source-matched runtime is mandatory; this test never builds one.
"""
from __future__ import annotations

import ast
import json
import os
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from pcc.diagnostics.gc_log import parse_log_lines
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_targets import host_target_triple
from pcc.frontends.python.py_ast import SourceSpan
from tests.python.owned_regression_support import (
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.process_timeout import run_process_group_timeout
from tests.python.test_slot_call_status_reporting import _InlineReference, _sites


_HARNESS = r'''
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
extern int64_t baseline_probe(int64_t, int64_t);
extern int64_t candidate_probe(int64_t, int64_t);
extern int64_t pcc_refcount_load(void *);

static int64_t invoke(int arm, int64_t first, int64_t second) {
    if (arm == 0) return baseline_probe(first, second);
    return candidate_probe(first, second);
}

int main(int argc, char **argv) {
    if (argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 1;
    if (pcc_gc_backend() != atoi(argv[1])) return 16;
    PyObject *roots[4] = {NULL, NULL, NULL, NULL};
    int32_t map[1] = {4};
    pcc_gc_frame_enter(map, roots);
    int64_t root_count = pcc_gc_frame_root_slot_count();
    int64_t owners[2] = {0, 0};

    for (int mode = 0; mode < 2; ++mode) {
        for (int arm = 0; arm < 2; ++arm) {
            py_clear_exception();
            if (invoke(arm, 0, 0) != 17 || py_err_occurred()) return 2;
            int64_t first = mode == 0 ? -1 : 0;
            int64_t second = mode == 0 ? 0 : -1;
            if (invoke(arm, first, second) != -mode - 1) return 3;
            pcc_gc_store_root(&roots[0], py_current_exception());
            if (!roots[0]) return 4;
            if (!py_exc_matches(roots[0], (PyObject *)py_exc_builtin_class(PY_EXC_RUNTIMEERROR))) return 5;
            if (py_exc_traceback_len(roots[0]) != mode) return 6;
            owners[arm] = pcc_refcount_load(roots[0]);
            pcc_gc_store_root_take(&roots[arm + 1], py_exc_traceback_format_exc(roots[0]));
            if (!roots[arm + 1]) return 7;
            py_clear_exception();
            pcc_gc_store_root(&roots[0], NULL);
            pcc_gc_collect(-1);
            if (pcc_gc_frame_root_slot_count() != root_count) return 8;
        }
        /* Exact message and source-frame order, including no helper frame. */
        if (!py_str_eq(roots[1], roots[2])) return 9;
        /* Preserve the existing borrowed py_raise ownership convention. */
        if (owners[0] != owners[1]) return 10;
        pcc_gc_store_root(&roots[1], NULL);
        pcc_gc_store_root(&roots[2], NULL);
    }

    pcc_gc_store_root_take(&roots[0], py_exc_new(PY_EXC_VALUEERROR, "original"));
    py_exc_append_frame_source(roots[0], "original_owner", "original.py", "raise original", 23);
    py_raise(roots[0]);
    for (int arm = 0; arm < 2; ++arm) {
        if (invoke(arm, 0, 0) != 17 || py_current_exception() != roots[0]) return 11;
        if (invoke(arm, -1, 0) != -1 || py_current_exception() != roots[0]) return 12;
        if (invoke(arm, 0, -1) != -2 || py_current_exception() != roots[0]) return 13;
        if (py_exc_traceback_len(roots[0]) != 1) return 14;
        if (pcc_gc_frame_root_slot_count() != root_count) return 15;
    }
    py_clear_exception();
    pcc_gc_store_root(&roots[0], NULL);
    pcc_gc_collect(-1);
    pcc_gc_frame_leave(roots);
    puts("slot-status-runtime-equal");
    return 0;
}
'''


def _run_native(executable, arguments, tmp_path, backend, expected):
    log = tmp_path / ("gc" + str(backend) + ".gc.jsonl")
    environment = dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_LOG="gc",
                       PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log), PATH="")
    result = run_process_group_timeout([str(executable)] + arguments, env=environment, timeout=10)
    (tmp_path / ("gc" + str(backend) + ".stdout")).write_text(result.stdout)
    (tmp_path / ("gc" + str(backend) + ".stderr")).write_text(result.stderr)
    events = parse_log_lines(log.read_text().splitlines()) if log.exists() else []
    observed = sorted({event.fields["value1"] for event in events
                       if event.fields.get("category") == "gc"
                       and event.event in ("collect_start", "collect_stop", "collect_end")})
    row = {"requested_gc": backend, "observed_gc": observed,
           "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    (tmp_path / ("gc" + str(backend) + ".json")).write_text(json.dumps(row, indent=2) + "\n")
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, ""), row
    assert observed == [backend], row


@pytest.mark.integration
def test_generated_status_helper_matches_inline_real_runtime_all_gc(
    tmp_path, explicit_owned_runtime, monkeypatch,
):
    root = Path(__file__).resolve().parents[2]
    target = host_target_triple()
    for key in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "off")
    spans = (None, SourceSpan("source.py", 1, 0, 1, 2))
    generated = []
    for cls, symbol in ((_InlineReference, "baseline_probe"), (L1CodeGen, "candidate_probe")):
        owner, _ = _sites(cls, spans=spans, symbol=symbol)
        owner.module.triple = target
        text = str(owner.module)
        path = tmp_path / (symbol + ".ll")
        path.write_text(text)
        generated.append((path.name, text, None, ()))
    source = tmp_path / "status.c"
    source.write_text(_HARNESS)
    executable = tmp_path / "status-runtime"
    evaluator = CEvaluator(backend="self", target_triple=target)
    with patch("subprocess.Popen", side_effect=AssertionError("external build process")):
        units = evaluator.compile_translation_units(
            [TranslationUnit(source.name, str(source), _HARNESS)],
            use_system_cpp=False, use_compile_cache=False,
            include_dirs=[str(root / "pcc/runtime/include"), str(root / "utils/fake_libc_include")],
        )
        evaluator.emit_executable(
            units + generated, str(executable), optimize=False,
            link_args=[str(explicit_owned_runtime)],
        )
    assert executable.is_file()
    (tmp_path / "identity.json").write_text(json.dumps({
        "compiler_mode": "host-pcc0-generated-IR/owned-C",
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "baseline_ir_sha256": hashlib.sha256((tmp_path / "baseline_probe.ll").read_bytes()).hexdigest(),
        "candidate_ir_sha256": hashlib.sha256((tmp_path / "candidate_probe.ll").read_bytes()).hexdigest(),
    }, indent=2) + "\n")
    for backend in range(5):
        _run_native(executable, [str(backend)], tmp_path, backend, "slot-status-runtime-equal\n")


_PROGRAM = '''import gc
events = []
original = ValueError('original')

class Temporary:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        events.append(self.label)

def fails(*, value):
    gc.collect()
    assert value == [1, 2, 3]
    raise original

def takes(*, value):
    gc.collect()
    return value[0]

def main():
    assert takes(value=[11]) == 11
    try:
        fails(value=[1, 2, 3])
    except ValueError as caught:
        assert caught is original
        assert str(caught) == 'original'
    else:
        raise AssertionError('missing original exception')
    try:
        takes(unexpected=Temporary('argument'))
    except TypeError:
        pass
    else:
        raise AssertionError('missing binding exception')
    gc.collect()
    assert events == ['argument']
    print('slot-status-owned-callsite-ok')

main()
'''


def _object_call_lines():
    # Derive the three sites from the unchanged source rather than generated
    # temporary names. A nested Temporary constructor is a different call.
    tree = ast.parse(_PROGRAM)
    main = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    lines = {node.lineno for node in ast.walk(main)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id in {"takes", "fails"} and node.keywords}
    assert len(lines) == 3
    return lines


def _capture_actual_status_edges(source, monkeypatch):
    """Observe existing lowering without replacing its call or cleanup logic."""
    from pcc.backend.self_backend_parse import decode_ssa_name

    generated = []
    witnesses = []
    expected_lines = _object_call_lines()
    source = source.resolve()
    original_generate = L1CodeGen.generate
    original_check = L1CodeGen._slot_call_check_status
    key = "__pcc_slot_call_status_report"

    def observe_check(self, status, operation, span=None):
        witness = None
        definition = self.current_func_def
        if (operation == "object call" and definition is not None
                and definition.name == "main" and span is not None
                and span.line in expected_lines and Path(span.file).resolve() == source):
            target = self._current_try_err_block()
            assert target is not None, "Expected the actual operand-cleanup block"
            before = tuple(str(instruction) for instruction in target.instructions)
            witness = {
                "instance": self,
                "owner": self.current_function.name,
                "line": span.line,
                "status": decode_ssa_name(str(status)),
                "status_block": self.builder.block.name,
                "cleanup": target.name,
            }
        result = original_check(self, status, operation, span)
        if witness is not None:
            assert tuple(str(instruction) for instruction in target.instructions) == before
            witness["helper"] = self.runtime[key].name
            witness["ready"] = self.builder.block.name
            witnesses.append(witness)
        return result

    def capture(self, module=None):
        text = original_generate(self, module)
        local = [dict(witness) for witness in witnesses if witness["instance"] is self]
        if local:
            owner = self.functions["main"].name
            assert all(witness["owner"] == owner for witness in local)
            for witness in local:
                del witness["instance"]
            generated.append({"owner": owner, "text": str(text), "witnesses": local,
                              "target_triple": str(self.module.triple)})
        return text

    monkeypatch.setattr(L1CodeGen, "_slot_call_check_status", observe_check)
    monkeypatch.setattr(L1CodeGen, "generate", capture)
    return generated


def _assert_actual_status_edges(capture):
    """Prove actual instructions in main, never declarations or other owners."""
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_self_backend_module

    parsed = parse_self_backend_module(capture["text"])
    assert parsed.triple == capture["target_triple"]
    owners = [function for function in parsed.functions if function.name == capture["owner"]]
    assert len(owners) == 1
    kernel = get_indexed_function_kernel(owners[0])
    blocks = {
        name: ([kernel.diagnostic_instruction(block_id, index)
                for index in range(kernel.instruction_count(block_id))],
               kernel.diagnostic_terminator(block_id))
        for block_id, name in enumerate(kernel.block_names)
    }
    witnesses = capture["witnesses"]
    assert len(witnesses) == 3
    assert {witness["line"] for witness in witnesses} == _object_call_lines()
    rows = []
    for witness in witnesses:
        assert witness["owner"] == capture["owner"]
        instructions, branch = blocks[witness["status_block"]]
        calls = [instruction.data for instruction in instructions
                 if instruction.kind == "call" and instruction.data[0] == witness["status"]]
        assert len(calls) == 1
        call = calls[0]
        assert call[1].is_int and call[1].width == 64
        assert call[2:4] == ("py_obj_call_slots", False)
        assert len(call[4]) == 4
        assert all(ty.is_ptr and value != "null" for ty, value in call[4])
        comparisons = [instruction.data for instruction in instructions
                       if instruction.kind == "icmp" and instruction.data[0] == "slt"
                       and instruction.data[3:] == (witness["status"], "0")]
        assert len(comparisons) == 1
        comparison = comparisons[0]
        assert comparison[2].is_int and comparison[2].width == 64
        assert branch.kind == "br_cond" and branch.data[0] == comparison[1]
        error_name, ready_name = branch.data[1:]
        assert error_name != ready_name and ready_name == witness["ready"]
        error_instructions, error_branch = blocks[error_name]
        reporters = [instruction.data for instruction in error_instructions
                     if instruction.kind == "call" and instruction.data[2] == witness["helper"]]
        assert len(reporters) == 1
        reporter = reporters[0]
        assert reporter[0] is None and reporter[1].is_void and not reporter[3]
        assert len(reporter[4]) == 6
        assert all(ty.is_ptr for ty, _ in reporter[4][:4])
        line_type, line = reporter[4][4]
        frame_type, frame = reporter[4][5]
        assert line_type.is_int and line_type.width == 32 and line == str(witness["line"])
        assert frame_type.is_int and frame_type.width == 1 and frame == "1"
        assert error_branch.kind == "br" and error_branch.data == (witness["cleanup"],)
        ready_instructions, _ = blocks[ready_name]
        assert not any(instruction.kind == "call" and instruction.data[2] == witness["helper"]
                       for instruction in ready_instructions)
        cleanup_instructions, cleanup_branch = blocks[witness["cleanup"]]
        cleanup_calls = [instruction.data for instruction in cleanup_instructions
                         if instruction.kind == "call"]
        cleanup_names = [call[2] for call in cleanup_calls]
        clears = [call for call in cleanup_calls if call[2] == "pcc_gc_store_root"]
        # Callable, positional tuple and kwargs are owned caller slots; an
        # output sink may stay owned by the enclosing operation instead.
        assert len(clears) >= 3
        assert all(len(call[4]) == 2 and call[4][1][1] == "null" for call in clears)
        assert cleanup_names == (
            ["pcc_gc_frame_enter_lifo", "py_tls_exc_swap_slot"]
            + ["pcc_gc_store_root"] * len(clears)
            + ["py_clear_exception", "py_tls_exc_swap_slot"]
            + ["pcc_gc_frame_leave_lifo"] * (len(clears) + 1)
        )
        aliases = {instruction.data[1]: instruction.data[3]
                   for instruction in cleanup_instructions
                   if instruction.kind == "cast" and instruction.data[0] == "bitcast"
                   and instruction.data[2].is_ptr and instruction.data[4].is_ptr}

        def root_slot(value):
            seen = set()
            while value in aliases:
                assert value not in seen, "Cyclic pointer aliases in cleanup"
                seen.add(value)
                value = aliases[value]
            return value

        swaps = [call for call in cleanup_calls if call[2] == "py_tls_exc_swap_slot"]
        assert all(len(call[4]) == 1 and call[4][0][0].is_ptr for call in swaps)
        exception_slot = root_slot(swaps[0][4][0][1])
        assert root_slot(swaps[1][4][0][1]) == exception_slot
        assert root_slot(cleanup_calls[0][4][1][1]) == exception_slot
        leaves = [call for call in cleanup_calls if call[2] == "pcc_gc_frame_leave_lifo"]
        assert root_slot(leaves[0][4][0][1]) == exception_slot
        assert [root_slot(call[4][0][1]) for call in leaves[1:]] == [
            root_slot(call[4][0][1]) for call in clears
        ]
        assert cleanup_branch.kind == "br"
        rows.append(dict(witness, error=error_name, cleanup_calls=cleanup_names,
                         cleanup_successor=cleanup_branch.data[0]))
    return rows


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0",), indirect=True)
def test_production_callsite_temporaries_exception_and_gc(
    tmp_path, explicit_owned_runtime, python_program_compiler, monkeypatch, capfd,
):
    source, _ = assert_reference_program(_PROGRAM, "slot-status-owned-callsite-ok\n", tmp_path)
    for key in ("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT"):
        monkeypatch.setenv(key, "0")
    target = host_target_triple()
    generated = _capture_actual_status_edges(source, monkeypatch)
    executable = tmp_path / "ordinary-callsite"
    try:
        # An explicit host target selects the supported in-process frontend
        # branch for the full ordinary import closure. Default multi-module
        # worker subprocesses would not inherit these observation wrappers.
        python_program_compiler(str(source), str(executable), backend="self", libpython_mode="off",
                                ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
                                target_triple=target)
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compiler.stdout").write_text(captured.out)
        (tmp_path / "compiler.stderr").write_text(captured.err)
    assert len(generated) == 1, "Expected one actual program-main lowering witness"
    capture = generated[0]
    assert capture["target_triple"] == target
    text = capture["text"]
    (tmp_path / "actual-generated.ll").write_text(text)
    # Persist observations before checking them, including a failed edge proof.
    (tmp_path / "observed-status-edges.json").write_text(
        json.dumps(capture["witnesses"], indent=2) + "\n"
    )
    edges = _assert_actual_status_edges(capture)
    (tmp_path / "verified-status-edges.json").write_text(json.dumps(edges, indent=2) + "\n")
    assert executable.is_file()
    (tmp_path / "identity.json").write_text(json.dumps({
        "compiler_mode": "host-pcc0-production-pipeline",
        "frontend_route": "explicit-host-target/in-process/full-import-closure",
        "target_triple": target,
        "program_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "generated_ir_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }, indent=2) + "\n")
    for backend in range(5):
        _run_native(executable, [], tmp_path, backend, "slot-status-owned-callsite-ok\n")
