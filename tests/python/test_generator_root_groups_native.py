"""Compile the real repository fixture once; observe every independent GC case.

Host pcc0 emits self-backed/no-libpython native code using an explicitly matched
threaded/atomic runtime. No provisioning, relocation of source, or pcc1 claim.
This supplements the original aggregate/root-join and constructor regressions;
it neither replaces them nor proves actual relocation from an alias check.
Run with -m integration -x -n0 and retain --basetemp for all raw receipts.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sys

import pytest

from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.constructor_native_support import _same_source_imports
from tests.python.test_callable_construction_native import (
    _assert_integrity,
    _environment,
    _run,
    _save,
    _sha,
)


pytestmark = pytest.mark.integration
_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "tests/fixtures/native/constructor/generator_root_groups.py"
_CASES = (
    "globals", "normal", "early", "close", "exception", "weakref",
    "resurrection", "alias", "small",
)
_SUPPORT = (
    "tests/python/constructor_native_support.py",
    "tests/python/test_callable_construction_native.py",
    "tests/python/process_timeout.py",
    "tests/python/root_slot_contract.py",
)


def _source_contract(source):
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    sites = [node for node in ast.walk(functions["grouped"])
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "Witness"]
    assert sorted(node.args[0].value for node in sites) == list(range(1, 33))
    assert len({node.lineno for node in sites}) == 32
    calls = [node for node in ast.walk(functions["grouped"])
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "consume"]
    assert len(calls) == 32
    assert all(isinstance(node.args[0], ast.Call) and isinstance(node.args[1], ast.Yield)
               for node in calls), "Every owner must remain live across later-argument suspension"
    small = [node for node in ast.walk(functions["small"])
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "Witness"]
    assert len(small) == 1
    return {"distinct_grouped_witness_sites": 32, "small_witness_sites": 1}


def _actual_group_contract(link_inputs, target_triple):
    # Read only the retained SAME IR strings given to the real owned linker.
    # This does not recompile, rewrite, or substitute a synthetic IR module.
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import (
        parse_self_backend_module,
        parse_self_backend_target_triple,
    )
    from pcc.backend.self_backend_verify import verify_parsed_module
    from tests.python.root_slot_contract import RootSlotContract

    assert target_triple.split("-", 1)[0] in ("x86_64", "arm64", "aarch64"), (
        "RootSlotContract requires a supported 64-bit target; observed " + target_triple
    )
    result = {}
    for link in link_inputs:
        path = Path(link["path"])
        assert _sha(path) == link["sha256"]
        text = path.read_text(encoding="utf-8")
        if "grouped__gen_resume" not in text and "small__gen_resume" not in text:
            continue
        assert parse_self_backend_target_triple(text) == target_triple, "Actual linker target drift"
        module = parse_self_backend_module(text)
        verify_parsed_module(module)
        for function in module.functions:
            kind = next((name for name in ("grouped", "small")
                         if function.name.endswith("_" + name + "__gen_resume")), None)
            if kind is None:
                continue
            assert kind not in result, "Ambiguous fixture resume function"
            blocks = get_indexed_function_kernel(function).materialize_legacy_blocks(function)
            roots = RootSlotContract(blocks, module.globals_)
            groups = [name for name in roots.allocas if name.startswith("gen.operand.roots.")]
            operands = [name for name in roots.geps
                        if ".operand" in name and not name.startswith("gen.operand.empty.cell.")
                        and roots.slot(name)[0] in groups]
            cells = roots.require_distinct(operands)
            assert groups and cells, "Fixture did not exercise grouped owning operands"
            offsets = {base: sorted(offset for owner, offset in cells if owner == base)
                       for base in groups}
            for base, used in offsets.items():
                assert roots._pointer_cells(base) == 16
                width = roots.POINTER_BYTES
                assert used == list(range(0, len(used) * width, width))
                assert roots.require_owning((base, 0)) == (base, 0, 16)
            exits = roots.assert_frame_exits(groups)
            if kind == "grouped":
                assert len(cells) >= 32, "Need real owners at positions 16/17/31/32"
                assert sum(len(used) == 16 for used in offsets.values()) >= 2
            else:
                assert len(groups) == 1 and 0 < len(cells) <= 16, "Small control crossed a group"
            result[kind] = {
                "function": function.name, "actual_linker_input": str(path),
                "target_triple": target_triple, "pointer_bytes": roots.POINTER_BYTES,
                "linker_input_sha256": link["sha256"], "owning_cells": len(cells),
                "group_used_offsets": offsets, "fixed_group_size": 16,
                "reachable_return_count": len(exits),
                "all_reachable_frame_exits_checked": True,
            }
    assert set(result) == {"grouped", "small"}, "Missing real fixture resume definitions"
    return result


def test_generator_root_groups_repository_native(tmp_path):
    directory = tmp_path / "generator-root-groups"
    directory.mkdir()
    receipt_path = directory / "receipt.json"
    receipt = {
        "schema": "pcc.generator-root-groups-native.v1",
        "compiler": "host-pcc0", "native_pcc1": "UNRUN",
        "backend": "self", "libpython_mode": "off", "ir_scaffold_mode": "on",
        "source": str(_SOURCE), "source_relocated": False,
        "compile": {"status": "UNRUN"}, "actual_group_contract": {"status": "UNRUN"},
        "cases": {case: {"reference": {"status": "UNRUN"}, "executions": [
            {"requested_backend": backend, "status": "UNRUN", "observed_backends": []}
            for backend in range(5)]} for case in _CASES},
        "relocation_witness": {
            "status": "UNPROVEN",
            "reason": "Collector start/stop and alias survival do not identify an object relocation",
            "original_movement_gate": "STILL_REQUIRED; foreign-address-lease link blocker unchanged",
        },
        "performance": "UNMEASURED; small selector is a future same-input before/after control",
    }
    _save(receipt_path, receipt)
    failures = []
    archive = binary = environment = None
    try:
        assert not os.environ.get("PCC_TEST_COMPILER"), "This observer selects host pcc0 only"
        selected = os.environ.get("PCC_THREADED_RUNTIME_ARCHIVE", "").strip()
        assert selected, "Set PCC_THREADED_RUNTIME_ARCHIVE to a matched threaded/atomic archive"
        archive = Path(selected).resolve(strict=True)
        assert _SOURCE.absolute() == _SOURCE.resolve(strict=True), "Do not relocate/symlink the fixture"
        receipt.update(
            source_sha256=_sha(_SOURCE), source_contract=_source_contract(_SOURCE),
            runtime_archive=str(archive), runtime_sha256=_sha(archive),
            wrapper_sha256=_sha(Path(__file__)),
            support={name: _sha(_ROOT / name) for name in _SUPPORT},
            parent_imports=_same_source_imports(_ROOT),
        )
        environment = _environment(archive)
        receipt["effective_pcc_environment"] = {
            key: value for key, value in environment.items() if key.startswith("PCC_")
        }
        _save(receipt_path, receipt)
        # Reference failures block only their own native cases. Every remaining
        # case is still attempted after an independent semantic/runtime failure.
        for case in _CASES:
            case_dir = directory / case
            case_dir.mkdir()
            reference = receipt["cases"][case]["reference"]
            try:
                reference.update(_run(case_dir, "reference", [
                    sys.executable, "-E", "-B", str(_SOURCE), case,
                ], environment, 30))
                assert reference["returncode"] == 0, reference
                assert (case_dir / "reference.stdout").read_bytes() == (
                    "GENERATOR_ROOT_GROUPS_OK:" + case + "\n").encode()
                assert (case_dir / "reference.stderr").read_bytes() == b""
                reference["status"] = "PASS"
            except Exception as error:
                reference.update(status="FAILED", error=repr(error))
                failures.append(case + ": reference: " + repr(error))
            _save(receipt_path, receipt)
        binary = directory / ("program.exe" if os.name == "nt" else "program")
        receipt["compile"] = {"status": "RUNNING"}
        _save(receipt_path, receipt)
        result = _run(directory, "compile", [
            sys.executable, "-E", "-B", "-u", "-m", "tests.python.constructor_native_support",
            str(_SOURCE), str(binary), str(archive), str(directory), "0",
        ], environment, 300)
        worker_path = directory / "compile-receipt.json"
        worker = json.loads(worker_path.read_text(encoding="utf-8")) if worker_path.is_file() else {}
        receipt["compile"] = dict(result, worker=worker)
        receipt["compile"]["status"] = "READY" if (
            result["returncode"] == 0 and worker.get("status") == "READY"
        ) else "FAILED"
        _save(receipt_path, receipt)
        assert receipt["compile"]["status"] == "READY", receipt["compile"]
        receipt["binary_sha256"] = _sha(binary)
        try:
            receipt["actual_group_contract"] = {
                "status": "PASS", "functions": _actual_group_contract(
                    worker["link_inputs"], worker["target_triple"],
                ),
            }
        except Exception as error:
            receipt["actual_group_contract"] = {"status": "FAILED", "error": repr(error)}
            failures.append("actual linker group contract: " + repr(error))
    except Exception as error:
        failures.append("admission/compile: " + repr(error))
        receipt["setup_error"] = repr(error)
        if receipt["compile"]["status"] in ("UNRUN", "RUNNING"):
            receipt["compile"]["status"] = "ADMISSION_OR_SUPERVISOR_FAILED"
    finally:
        _save(receipt_path, receipt)

    for case in _CASES:
        case_row = receipt["cases"][case]
        case_dir = directory / case
        for execution in case_row["executions"]:
            if receipt["compile"]["status"] != "READY" or case_row["reference"]["status"] != "PASS":
                execution["blocked_by"] = "compile or this case's reference did not pass"
                _save(receipt_path, receipt)
                continue
            backend = execution["requested_backend"]
            label = "gc" + str(backend)
            log = case_dir / (label + ".gc.jsonl")
            execution.update(status="RUNNING", gc_log=str(log))
            _save(receipt_path, receipt)
            try:
                _assert_integrity(_SOURCE, binary, archive, receipt)
                native_env = dict(environment, PCC_GC_BACKEND=str(backend), PCC_LOG="gc",
                                  PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log))
                result = _run(case_dir, label, [str(binary), case], native_env, 60)
                execution.update(result, process_status=result["status"])
                events = parse_log_lines(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else []
                collections = [event for event in events if event.fields.get("category") == "gc"
                               and event.event in ("collect_start", "collect_stop", "collect_end")]
                starts = {event.fields.get("value1") for event in collections if event.event == "collect_start"}
                stops = {event.fields.get("value1") for event in collections
                         if event.event in ("collect_stop", "collect_end")}
                observed = starts | stops
                execution.update(observed_backends=sorted(observed, key=repr),
                                 observed_starts=sorted(starts, key=repr),
                                 observed_stops=sorted(stops, key=repr),
                                 collection_event_count=len(collections),
                                 gc_log_sha256=_sha(log) if log.is_file() else None)
                assert result["returncode"] == 0, execution
                for stream in ("stdout", "stderr"):
                    assert (case_dir / (label + "." + stream)).read_bytes() == (
                        case_dir / ("reference." + stream)).read_bytes(), execution
                assert all(type(value) is int for value in observed), execution
                assert starts == stops == {backend}, execution
                _assert_integrity(_SOURCE, binary, archive, receipt)
                execution["status"] = "PASS"
            except Exception as error:
                execution.update(status="TIMEOUT" if execution.get("returncode") == 124 else "FAILED",
                                 error=repr(error))
                failures.append(case + "/" + label + ": " + repr(error))
            finally:
                _save(receipt_path, receipt)
    receipt["status"] = "FAILED" if failures else "PASS_SEMANTICS_AND_COLLECTOR_ONLY"
    receipt["failures"] = failures
    _save(receipt_path, receipt)
    assert not failures, "\n".join(failures)
