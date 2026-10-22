"""May-park completion publishes from its authoritative frame cell."""

import os
import re
import subprocess

import pytest

from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.test_slot_call_operand_roots import _emit


@pytest.mark.parametrize("expression", (
    "slot_operand_probe(leaf())",
    "slot_args_probe(['before'], leaf())",
    "slot_operand_probe(vt.call(callback))",
    "slot_operand_probe(vt.call(callback, [1]))",
    "callback(['before'], leaf())",
    "slot_operand_probe(vt.call(callback, ['before'], leaf()))",
    "slot_operand_probe(vt.call(leaf))",
))
def test_may_park_object_completion_has_an_owned_consumer(expression):
    text = _emit(
        "import pcc.virtual_thread as vt\n"
        "def leaf():\n    vt.yield_now()\n    return [42]\n"
        "def plain():\n    return [41]\n"
        "def probe(callback):\n    return " + expression + "\n"
    )
    verify_parsed_module(parse_self_backend_module(text))
    body = re.search(
        r"^define [^\n]*@user_slot_operand_probe__gen_resume\([^\n]*\).*?^}",
        text,
        re.M | re.S,
    )
    assert body is not None
    body = body.group(0)
    assert "gen.operand.restore" in body
    assert "@pcc_gc_root_copy_lease(" in body
    assert "vthread.delegate.result.retain" not in body
    assert "vthread.call.direct.retain" not in body
    assert "vthread.result.current" in body


_MAY_PARK_NATIVE_PROGRAM = """import gc
import pcc.virtual_thread as vt
from pcc.extern import c_int64
from pcc.extern import extern
actual_backend = extern("pcc_gc_backend", (), c_int64)
events = []
class Token:
    def __init__(self):
        self.value = 40
    def __del__(self):
        events.append("drop")
class Combine:
    def __call__(self, token, value):
        return token.value + value[0]
def leaf():
    vt.yield_now()
    return [2]
def handler(callback):
    return callback(Token(), leaf())
def main():
    print(actual_backend())
    thread = vt.spawn(handler, Combine())
    vt.run(1, 32)
    print(vt.result(thread))
    gc.collect()
    print(events)
main()
"""


@pytest.mark.parametrize("call_kind", ("ordinary", "virtual_thread"))
def test_may_park_completion_native_preserves_prior_argument_and_finalizer(
    tmp_path,
    pcc_runtime_archive,
    call_kind,
):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "may_park_operands.py"
    executable = tmp_path / "may_park_operands"
    program = _MAY_PARK_NATIVE_PROGRAM
    if call_kind == "virtual_thread":
        program = program.replace(
            "return callback(Token(), leaf())",
            "return vt.call(callback, Token(), leaf())",
        )
    source.write_text(program, encoding="utf-8")
    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    for requested_backend in range(5):
        log = tmp_path / ("gc" + str(requested_backend) + ".jsonl")
        ran = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            timeout=20,
            env=dict(
                os.environ,
                PCC_GC_BACKEND=str(requested_backend),
                PCC_LOG="gc",
                PCC_LOG_FORMAT="json",
                PCC_LOG_FILE=str(log),
            ),
        )
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert ran.stdout.splitlines() == [str(requested_backend), "42", "['drop']"]
        assert log.is_file()
        observed_backends = {
            event.fields["value1"]
            for event in parse_log_lines(log.read_text(encoding="utf-8").splitlines())
            if event.fields.get("category") == "gc"
            and event.event in ("collect_start", "collect_stop", "collect_end")
        }
        assert observed_backends == {requested_backend}
