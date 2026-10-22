"""Callback cleanup preserves the selecting exception through real disposal."""

import os
import subprocess

import pytest


# Suspend before creating disposable operands. The leaf itself cannot park,
# so saved generator cells cannot postpone their finalizers until completion.
# The original callback lifetime fixture separately retains its may-park leaf.
PROGRAM = '''import gc
import weakref
import pcc.virtual_thread as vt
from pcc.extern import c_int64, extern
actual_backend = extern("pcc_gc_backend", (), c_int64)
selected = ValueError("selected callback failure")
events = []
references = []
class Token:
    def __del__(self):
        events.append("token-drop")
        gc.collect()
        raise RuntimeError("unraisable token disposal")
class Callback:
    def __call__(self, token, value):
        events.append("callback-enter")
        gc.collect()
        assert value == 2
        raise selected
    def __del__(self):
        events.append("callback-drop")
        gc.collect()
        raise RuntimeError("unraisable callback disposal")
def on_weakref(reference):
    events.append("weakref-drop")
    gc.collect()
    raise RuntimeError("unraisable weakref disposal")
def make_token():
    token = Token()
    references.append(weakref.ref(token, on_weakref))
    return token
def leaf(failing_argument):
    gc.collect()
    assert events == []
    if failing_argument:
        raise selected
    return 2
def handler(failing_argument):
    vt.yield_now()
    try:
        vt.call(Callback(), make_token(), leaf(failing_argument))
    except ValueError as caught:
        assert caught is selected
        assert events.count("token-drop") == 1
        assert events.count("callback-drop") == 1
        assert events.count("weakref-drop") == 1
        gc.collect()
        assert caught is selected
        events.append("caught-selected")
        return 17
    raise AssertionError("callback did not raise the selecting exception")
def main():
    print(actual_backend())
    for failing_argument in (False, True):
        events.clear()
        references.clear()
        thread = vt.spawn(handler, failing_argument)
        vt.run(1, 128)
        assert vt.outcome(thread) == vt.OUTCOME_RETURNED
        assert vt.result(thread) == 17
        thread = None
        gc.collect()
        assert events.count("token-drop") == 1
        assert events.count("callback-drop") == 1
        assert events.count("weakref-drop") == 1
        assert events.count("caught-selected") == 1
        assert references[0]() is None
        if failing_argument:
            assert events.count("callback-enter") == 0
        else:
            assert events.count("callback-enter") == 1
    print("PCC_CALLBACK_EXCEPTION_DISPOSAL_OK")
main()
'''


def test_callback_exception_disposal_program_has_complete_ir(tmp_path):
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_verify import verify_parsed_module
    from tests.python.test_slot_call_operand_roots import _emit

    text = _emit(PROGRAM)
    (tmp_path / "callback_exception_disposal.py").write_text(PROGRAM, encoding="utf-8")
    (tmp_path / "callback_exception_disposal.ll").write_text(text, encoding="utf-8")
    assert "strict.nolib.stub:" not in text
    verify_parsed_module(parse_self_backend_module(text))


@pytest.mark.integration
def test_callback_exception_disposal_native_all_gc(tmp_path, pcc_runtime_archive):
    from pcc.diagnostics.gc_log import parse_log_lines
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "callback_exception_disposal.py"
    executable = tmp_path / "callback_exception_disposal"
    source.write_text(PROGRAM, encoding="utf-8")
    compile_python(str(source), str(executable), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        log = tmp_path / ("gc" + str(backend) + ".jsonl")
        ran = subprocess.run(
            [str(executable)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend),
                     PCC_LOG="gc", PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log)),
        )
        assert ran.returncode == 0, (backend, ran.stdout, ran.stderr)
        assert ran.stdout.splitlines() == [str(backend), "PCC_CALLBACK_EXCEPTION_DISPOSAL_OK"]
        # Finalizer/weakref failures are deliberately unraisable; diagnostics
        # may be printed, while the exact selecting owner must survive.
        assert log.is_file()
        observed = {event.fields["value1"] for event in parse_log_lines(log.read_text().splitlines())
                    if event.fields.get("category") == "gc"
                    and event.event in ("collect_start", "collect_stop", "collect_end")}
        assert observed == {backend}
