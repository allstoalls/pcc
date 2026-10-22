"""Signal APIs use the owned runtime and validate before entering the OS."""

from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python, count_py_cpy_fallback_calls


def test_os_kill_uses_owned_integer_protocol(tmp_path):
    source = tmp_path / "signal_probe.py"
    output = tmp_path / "signal_probe.ll"
    source.write_text(
        "import os\n"
        "def probe(pid, signal_number):\n"
        "    return os.kill(pid, signal_number)\n",
        encoding="utf-8",
    )
    compile_python(
        str(source), str(output), emit_llvm_only=True,
        backend="self", libpython_mode="off", ir_scaffold_mode="on",
    )
    text = output.read_text(encoding="utf-8")
    assert "call ptr (ptr, ptr) @py_os_kill(" in text
    assert count_py_cpy_fallback_calls(text) == 0


# POSIX pid_t and signal-zero contract
@pytest.mark.pcc_gate(probe=lambda: not (os.name == "nt"))
def test_os_kill_native_zero_signal_and_checked_arguments(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    monkeypatch.setenv("PCC_RUNTIME_CC", "pcc")
    source = tmp_path / "signal_native.py"
    output = tmp_path / "signal_native"
    source.write_text(textwrap.dedent('''
        import gc
        import os

        class Pid:
            def __index__(self):
                gc.collect()
                return os.getpid()

        class Signal:
            def __index__(self):
                gc.collect()
                return 0

        def probe(pid, signal_number):
            try:
                result = os.kill(pid, signal_number)
                print(result is None)
            except ProcessLookupError:
                print("missing")
            except OverflowError:
                print("overflow")
            except TypeError:
                print("type")

        probe(os.getpid(), 0)
        probe(Pid(), Signal())
        probe(os.getpid(), False)
        probe(2147483647, 0)
        probe(1 << 80, 0)
        probe(1 << 40, 0)
        probe(os.getpid(), 1 << 80)
        probe(os.getpid(), 1 << 40)
        probe(1.5, 0)
        probe(os.getpid(), 0.5)
    ''').lstrip(), encoding="utf-8")
    compile_python(
        str(source), str(output), backend="self",
        libpython_mode="off", ir_scaffold_mode="on",
    )
    expected = "True\nTrue\nTrue\nmissing\noverflow\noverflow\noverflow\noverflow\ntype\ntype\n"
    for gc in range(5):
        result = subprocess.run(
            [str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == expected, (gc, result.stdout, result.stderr)


# POSIX signal-zero contract
@pytest.mark.pcc_gate(probe=lambda: not (os.name == "nt"))
def test_os_kill_roots_direct_temporaries_and_releases_error_operands(
    tmp_path, pcc_runtime_archive,
):
    source = tmp_path / "signal_operand_lifetime.py"
    output = tmp_path / "signal_operand_lifetime"
    source.write_text(textwrap.dedent('''
        import gc
        import os

        released = []
        errors = []

        class Pid:
            def __init__(self, name: str):
                self.name = name
            def __index__(self):
                gc.collect()
                return os.getpid()
            def __del__(self):
                released.append(self.name)
                gc.collect()

        class Signal:
            def __init__(self, name: str, fail: bool):
                self.name = name
                self.fail = fail
            def __index__(self):
                gc.collect()
                if self.fail:
                    raise ValueError("bad-signal")
                return 0
            def __del__(self):
                released.append(self.name)
                gc.collect()

        class NotSignal:
            def __del__(self):
                released.append("type-signal")
                gc.collect()

        def make_signal():
            gc.collect()
            return Signal("good-signal", False)

        def fail_signal():
            gc.collect()
            raise ValueError("signal-factory")

        def exercise():
            # The first object is an SSA temporary while make_signal collects;
            # a wrapper accepting pre-evaluated pid/signal locals misses this.
            print(os.kill(Pid("good-pid"), make_signal()) is None)
            try:
                os.kill(Pid("bad-pid"), Signal("bad-signal", True))
            except ValueError as error:
                errors.append(str(error))
            try:
                os.kill(Pid("factory-pid"), fail_signal())
            except ValueError as error:
                errors.append(str(error))
            try:
                os.kill(Pid("type-pid"), NotSignal())
            except TypeError:
                errors.append("type")

        def main():
            exercise()
            gc.collect()
            gc.collect()
            print(errors)
            print(sorted(released))

        main()
    ''').lstrip(), encoding="utf-8")
    compile_python(
        str(source), str(output), backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
    )
    expected = (
        "True\n"
        "['bad-signal', 'signal-factory', 'type']\n"
        "['bad-pid', 'bad-signal', 'factory-pid', 'good-pid', "
        "'good-signal', 'type-pid', 'type-signal']\n"
    )
    for gc in range(5):
        result = subprocess.run(
            [str(output)], env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == expected, (gc, result.stdout, result.stderr)
