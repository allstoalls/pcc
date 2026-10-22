"""Range/reversed roots survive backedges and emitted execution on every GC."""

import json
import os
import subprocess
import sys

import pytest

from tests.python.test_slot_call_lexical_roots import _emit
from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
from pcc.backend.elf_x86_64 import parse_relocatable
from pcc.diagnostics.gc_log import parse_log_lines


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


def _owned_object(text):
    plans = []
    assembly = emit_indexed_assembly(parse_self_backend_module(text), stack_map_plans_out=plans)
    encoded = encode_assembly_object(assembly, 'x86_64-unknown-linux-gnu', stack_map_plans=plans)
    obj = parse_relocatable(encoded)
    assert any(section.name == '.text' and section.data for section in obj.sections)
    assert any(section.name == '.pcc_stackmaps' and section.data for section in obj.sections)


@pytest.mark.parametrize('body', [
    '    return sorted(range(len(commands)), key=lambda index: (-sizes[index], index))\n',
    '    values = range(count)\n    return values\n',
    '    return range(5, 0, -2)\n',
    '    return range(0)\n',
    '    return take(values=range(count))\n',
    '    try:\n        return range(count)\n    except ValueError:\n        return []\n',
], ids=['scheduler-sorted-lambda', 'assigned', 'negative-step', 'empty', 'argument', 'handler'])
def test_range_item_frame_survives_backedge(body):
    source = 'def take(*, values, **extras):\n    return values\n'
    source += 'def probe(commands, sizes, count):\n' + body
    codegen, text = _emit(source, 'range_root')
    prepared = prepare_module_for_target(
        text, aggregate_returned_indirect=_aggregate_returned_indirect,
    )
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)
    _owned_object(text)


@pytest.mark.parametrize('expression', ['reversed(commands)', 'reversed([])', 'reversed({"a": 1, "b": 2})', 'take(values=reversed(commands))'])
def test_reversed_item_frames_survive_backedge(expression):
    source = 'def take(*, values, **extras):\n    return values\n'
    source += 'def probe(commands):\n    return ' + expression + '\n'
    _codegen, text = _emit(source, 'reversed_root')
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    _owned_object(text)


_RANGE_REVERSED_BEHAVIOR_PROGRAM = """import gc

def take(*, values, **extras):
    return values

def probe(commands, sizes, count):
    values = range(count)
    empty = range(0)
    negative = range(5, 0, -2)
    ordered = sorted(range(len(commands)), key=lambda index: (-sizes[index], index))
    backwards = reversed(commands)
    keys = reversed({"a": 1, "b": 2, "c": 3})
    gc.collect()
    print(list(values), list(empty), list(negative))
    print(ordered, list(backwards), list(keys))
    print(list(take(values=range(count))))
    print(list(take(values=reversed(commands))), list(reversed([])))

probe(["a", "b", "c", "d"], [7, 2, 7, 9], 4)
probe([], [], 0)
probe(["x", "y", "z"], [2, 5, 2], 5)
gc.collect()
"""


def test_range_and_reversed_match_cpython_on_all_gc_backends(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    from pcc.frontends.python.pipeline import compile_python

    # Admit the source-matched runtime through the strict shared fixture.
    # Keep values live across collection and repeat the same generated loops.
    source = tmp_path / "range_reversed.py"
    executable = tmp_path / "range_reversed"
    source.write_text(_RANGE_REVERSED_BEHAVIOR_PROGRAM, encoding="utf-8")
    oracle = subprocess.run(
        [sys.executable, "-B", str(source)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert oracle.returncode == 0, oracle.stdout + oracle.stderr
    monkeypatch.setenv("PCC_HOST_PYTHON", sys.executable)
    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    receipts = []
    for requested_backend in range(5):
        log = tmp_path / ("gc" + str(requested_backend) + ".jsonl")
        environment = dict(
            os.environ,
            PCC_GC_BACKEND=str(requested_backend),
            PCC_LOG="gc",
            PCC_LOG_FORMAT="json",
            PCC_LOG_FILE=str(log),
        )
        ran = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )
        assert ran.returncode == oracle.returncode, ran.stdout + ran.stderr
        assert ran.stdout == oracle.stdout, "GC" + str(requested_backend)
        assert ran.stderr == oracle.stderr
        assert log.is_file(), "native collection must emit a backend witness"
        events = parse_log_lines(log.read_text(encoding="utf-8").splitlines())
        observed_backends = sorted({
            event.fields["value1"]
            for event in events
            if event.fields.get("category") == "gc"
            and event.event in ("collect_start", "collect_stop", "collect_end")
        })
        receipts.append({
            "requested_backend": requested_backend,
            "observed_backends": observed_backends,
        })
        (tmp_path / "gc_backend_receipt.json").write_text(
            json.dumps(receipts, indent=2) + "\n",
            encoding="utf-8",
        )
        assert observed_backends == [requested_backend], receipts[-1]
