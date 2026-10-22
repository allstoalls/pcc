"""Explicit raw fields keep their native view and reject managed publication."""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from pcc.diagnostics.gc_log import parse_log_lines
from pcc.frontends.python.codegen.errors import L1CodegenError
from tests.python.owned_regression_support import _record_execution, explicit_owned_runtime
from tests.python.test_shared_call_binding import _emit


PREFIX = '''import pcc
from pcc.extern import c_ptr
@pcc.valueclass
class RawView:
    address: c_ptr

def project(view: RawView) -> c_ptr:
    return view.address
'''

PROGRAM = PREFIX + '''
import gc
from pcc.unsafe import stack_alloc, store_i64, load_i64

def main():
    address = stack_alloc(16)
    store_i64(address, 0, 713)
    store_i64(address, 8, 991)
    view = RawView(address)
    alias = view
    gc.collect()
    assert load_i64(project(alias), 0) == 713
    assert load_i64(project(view), 8) == 991
    store_i64(project(alias), 0, 827)
    gc.collect()
    assert load_i64(project(view), 0) == 827
    print('VALUECLASS_RAW_FIELD_OK')

main()
'''


def test_explicit_raw_field_can_cross_its_native_return_abi():
    _emit(PROGRAM)


@pytest.mark.parametrize("operand", ("view", "view.address"))
def test_raw_payload_or_field_cannot_be_published_as_a_managed_operand(operand):
    source = PREFIX + "def take(*, value, **extras):\n    return value\ndef probe(view: RawView):\n    return take(value=" + operand + ")\n"
    with pytest.raises(L1CodegenError, match="raw"):
        _emit(source)


@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_raw_field_native_five_gc(python_program_compiler, explicit_owned_runtime, tmp_path):
    # stack_alloc is an explicit native intrinsic, with no CPython execution
    # counterpart. The memory canaries and native ABI calls are its oracle.
    source = tmp_path / "program.py"
    source.write_text(PROGRAM)
    binary = tmp_path / "program.out"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime))
    assert binary.read_bytes()[:4] == b"\x7fELF" or binary.read_bytes()[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe") or binary.read_bytes()[:2] == b"MZ"
    rows = []
    for collector in range(5):
        log = tmp_path / ("gc" + str(collector) + ".gc.jsonl")
        environment = dict(os.environ, PCC_GC_BACKEND=str(collector), PCC_LOG="gc",
                           PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log), PATH="",
                           PCC_HOST_PYTHON="/nonexistent/host-python", PCC_HOST_PCC="/nonexistent/host-pcc")
        result = _record_execution(tmp_path, "gc" + str(collector), [str(binary)], environment)
        events = parse_log_lines(log.read_text().splitlines()) if log.exists() else []
        observed = sorted({event.fields["value1"] for event in events if event.fields.get("category") == "gc"
                           and event.event in ("collect_start", "collect_stop", "collect_end")})
        rows.append({"requested_gc": collector, "observed_gc": observed, **result})
        (tmp_path / "raw-field-receipt.json").write_text(json.dumps({
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "executions": rows,
        }, indent=2) + "\n")
    assert all(row["returncode"] == 0 and row["stdout"] == "VALUECLASS_RAW_FIELD_OK\n"
               and row["stderr"] == "" and row["observed_gc"] == [row["requested_gc"]] for row in rows), rows
