"""Context managers stay alive across yield and exit on resume/close/throw."""
import json
import os
import subprocess
import sys

import pytest

from pcc.diagnostics.gc_log import parse_log_lines


GENERATOR_CONTEXT_PROGRAMS = {
    "control_and_gc": '''from contextlib import contextmanager
import gc
events = []
released = 0
class Previous:
    def __del__(self):
        global released
        released += 1
        pressure = []
        for index in range(12):
            pressure.append(str(index) * 128)
        gc.collect()
@contextmanager
def managed(value, label):
    events.append("enter-" + label)
    try:
        yield value
    finally:
        events.append("exit-" + label)
        gc.collect()
def take(*, value, **extras):
    return value
def early(value):
    selected = Previous()
    with managed(value, "return") as selected:
        return take(value=selected)
def fresh():
    with managed(None, "fresh"):
        return ["fresh-owner"]
def suspended(value):
    with managed(value, "suspend") as selected:
        yield selected
def main():
    payload = ["alive"]
    assert early(payload) is payload
    gc.collect()
    assert released == 1
    assert fresh() == ["fresh-owner"]
    for value in [None, False, 0, "", []]:
        with managed(value, "falsey") as selected:
            assert selected is value
    for index in range(3):
        with managed(payload, "loop") as selected:
            assert selected is payload
            if index == 0:
                continue
            break
    with managed(payload, "outer") as selected:
        with managed(selected, "inner") as other:
            assert other is payload
    iterator = suspended(payload)
    assert next(iterator) is payload
    gc.collect()
    iterator.close()
    iterator = suspended(payload)
    next(iterator)
    error = ValueError("thrown")
    try:
        iterator.throw(error)
    except ValueError as found:
        assert found is error
    iterator = suspended(payload)
    next(iterator)
    iterator = None
    gc.collect()
    assert events == ["enter-return", "exit-return", "enter-fresh", "exit-fresh",
        "enter-falsey", "exit-falsey", "enter-falsey", "exit-falsey",
        "enter-falsey", "exit-falsey", "enter-falsey", "exit-falsey",
        "enter-falsey", "exit-falsey", "enter-loop", "exit-loop",
        "enter-loop", "exit-loop", "enter-outer", "enter-inner", "exit-inner",
        "exit-outer", "enter-suspend", "exit-suspend", "enter-suspend",
        "exit-suspend", "enter-suspend", "exit-suspend"]
    print("context-control-gc-ok")
main()
''',
    "protocol": '''from contextlib import contextmanager
import gc
events = []
@contextmanager
def empty():
    if False:
        yield 1
@contextmanager
def multiple():
    try:
        yield 1
        yield 2
    finally:
        events.append("closed")
@contextmanager
def thrown_multiple():
    try:
        try:
            yield 1
        except ValueError:
            yield 2
    finally:
        events.append("throw-closed")
@contextmanager
def passthru():
    yield 1
@contextmanager
def swallowed():
    try:
        yield 1
    except ValueError:
        gc.collect()
@contextmanager
def replaced(error):
    try:
        yield 1
    finally:
        raise error
@contextmanager
def wrapped_stop():
    try:
        yield 1
    except StopIteration as error:
        raise RuntimeError("generator raised StopIteration") from error
@contextmanager
def close_failure(error):
    try:
        yield 1
        yield 2
    finally:
        raise error
def plain():
    yield 1
def cancelled_return(error):
    with replaced(error):
        return ["abandoned-return"]
def main():
    try:
        with empty():
            assert False
    except RuntimeError as error:
        assert str(error) == "generator didn't yield"
    try:
        with multiple():
            pass
    except RuntimeError as error:
        assert str(error) == "generator didn't stop"
    try:
        with thrown_multiple():
            raise ValueError("body")
    except RuntimeError as error:
        assert str(error) == "generator didn't stop after throw()"
    error = ValueError("identity")
    try:
        with passthru():
            raise error
    except ValueError as found:
        assert found is error
    stop = StopIteration("same-stop")
    try:
        with passthru():
            raise stop
    except StopIteration as found:
        assert found is stop
    try:
        with wrapped_stop():
            raise stop
    except StopIteration as found:
        assert found is stop
    with swallowed():
        raise ValueError("suppress")
    cleanup = LookupError("cleanup")
    try:
        with replaced(cleanup):
            raise ValueError("body")
    except LookupError as found:
        assert found is cleanup
    try:
        cancelled_return(cleanup)
    except LookupError as found:
        assert found is cleanup
    try:
        with close_failure(cleanup):
            pass
    except LookupError as found:
        assert found is cleanup
    try:
        with plain():
            assert False
    except (TypeError, AttributeError):
        pass
    assert events == ["closed", "throw-closed"]
    print("context-protocol-ok")
main()
''',
}


@pytest.mark.parametrize("case", tuple(GENERATOR_CONTEXT_PROGRAMS))
def test_declared_generator_context_protocol_on_every_gc(
    case, tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / (case + ".py")
    source.write_text(GENERATOR_CONTEXT_PROGRAMS[case], encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=20)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / case
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    receipts = []
    for backend in range(5):
        log = tmp_path / ("gc" + str(backend) + ".jsonl")
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                         PCC_LOG="gc", PCC_LOG_FORMAT="json",
                                         PCC_LOG_FILE=str(log)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected.stdout, (backend, result.stdout, expected.stdout)
        assert result.stderr == expected.stderr, (backend, result.stderr, expected.stderr)
        assert log.is_file(), "native collection must emit a collector witness"
        observed = sorted({
            event.fields["value1"]
            for event in parse_log_lines(log.read_text(encoding="utf-8").splitlines())
            if event.fields.get("category") == "gc"
            and event.event in ("collect_start", "collect_stop", "collect_end")
        })
        receipts.append({"requested_backend": backend, "observed_backends": observed})
        (tmp_path / "gc-backends.json").write_text(json.dumps(receipts, indent=2) + "\n")
        assert observed == [backend], receipts[-1]


def test_generator_with_context_lifetime_and_cleanup(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / 'generator_context.py'
    source.write_text('''
import os
import gc
import tempfile
events = []
class Manager:
    def __enter__(self):
        events.append("enter")
        return 7
    def __exit__(self, et, ev, tb):
        events.append("exit")
        return False

def temporary(other):
    with tempfile.TemporaryDirectory(prefix="pcc_gen_ctx_") as path:
        yield path
        path = other
        yield "again"
        return 42

def ordinary():
    with Manager(), Manager() as value:
        yield value
    yield 8

def main():
    with tempfile.TemporaryDirectory(prefix="pcc_gen_other_") as other:
        iterator = temporary(other)
        path = next(iterator)
        gc.collect()
        print(os.path.isdir(path), next(iterator))
        try:
            next(iterator)
        except StopIteration as exc:
            print(exc.value)
        print(os.path.exists(path), os.path.isdir(other))
        iterator = temporary(other)
        path = next(iterator)
        iterator.close()
        print(os.path.exists(path))
        iterator = temporary(other)
        path = next(iterator)
        try:
            iterator.throw(ValueError("stop"))
        except ValueError:
            print(os.path.exists(path))
    iterator = ordinary()
    print(next(iterator))
    gc.collect()
    print(next(iterator), events)
    iterator.close()
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'generator_context'
    python_program_compiler(str(source), str(binary), backend='self', libpython_mode='off',
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}; {result.stdout}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'
