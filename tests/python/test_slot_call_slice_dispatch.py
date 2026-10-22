"""Owned slice dispatch, including emitted native behavior on every GC."""

import json
import os
import re
import subprocess
import sys

import pytest

from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.test_slot_call_operand_roots import (
    _emit,
    _probe_function,
)


@pytest.mark.parametrize("value", (
    "b'abcdef'",
    "'abcdef'",
    "[0, 1, 2, 3, 4, 5]",
    "(0, 1, 2, 3, 4, 5)",
    "value",
))
@pytest.mark.parametrize("bounds", (":-4", "::-1", "1:5:2"))
def test_slice_operand_uses_sequence_or_mapping_abi(value, bounds):
    text = _emit(
        "def probe(value):\n    return slot_operand_probe("
        + value + "[" + bounds + "])\n"
    )
    verify_parsed_module(parse_self_backend_module(text))
    body = _probe_function(text)
    assert "@py_obj_type_tag(" in body
    assert "@py_obj_slice(" in body
    assert "@py_obj_subscript(" in body
    assert "call.slice.sequence" in body and "call.slice.mapping" in body
    for helper in ("py_obj_slice", "py_obj_subscript"):
        result = re.search(
            r"(%[^ ]+) = call ptr[^\n]*@" + helper + r"\([^\n]*\n([^\n]+)",
            body,
        )
        assert result is not None
        assert "store ptr " + result.group(1) in result.group(2)


_SLICE_BEHAVIOR_PROGRAM = r"""import gc

class SliceReceiver:
    def __getitem__(self, key):
        print(key.start, key.stop, key.step)
        return b'custom'

def probe():
    lines = b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\nProxy-Connection: Keep-Alive\r\n\r\n"
    print(lines[:-4].decode().split("\r\n"))
    print(lines[::-1].decode())
    print(SliceReceiver()[1:4:2].decode())
    gc.collect()

probe()
"""


def test_slice_operands_match_cpython_on_all_gc_backends(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    from pcc.frontends.python.pipeline import compile_python

    # The fixture strictly verifies an explicit archive's source, compiler,
    # target, configuration, and inventory before native compilation.
    source = tmp_path / "slice_operands.py"
    executable = tmp_path / "slice_operands"
    source.write_text(_SLICE_BEHAVIOR_PROGRAM, encoding="utf-8")
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
