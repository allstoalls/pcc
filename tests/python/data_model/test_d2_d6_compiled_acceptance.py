from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def _compile_and_run(tmp_path: Path, source: str, *, backend: str = "0") -> list[str]:
    from tests.native_provisioning import native_test_runtime_options

    runtime_options = native_test_runtime_options()
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        **runtime_options,
    )
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = backend
    env["PCC_PYTHON_LIBPYTHON"] = "off"
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip().splitlines()


def test_d2_generator_yield_send_return_value_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        def gen():
            x = yield 10
            yield x + 1
            return 99

        g = gen()
        print(next(g))
        print(g.send(20))
        try:
            next(g)
        except StopIteration as e:
            print(e.value)
        ''',
    )
    assert lines == ["10", "21", "99"]


def test_d3_async_await_minimum_compiled(tmp_path):
    import sys

    source = textwrap.dedent('''
        async def value():
            return 41

        async def main():
            x = await value()
            return x + 1

        c = main()
        print(c.__class__.__name__)
        try:
            c.send(None)
        except StopIteration as error:
            print(type(error).__name__, error.value)
        else:
            print("coroutine returned without StopIteration")
        ''').lstrip()
    oracle = subprocess.run(
        [sys.executable, "-c", source],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert oracle.returncode == 0, oracle.stdout + oracle.stderr
    expected = ["coroutine", "StopIteration 42"]
    assert oracle.stdout.strip().splitlines() == expected
    assert _compile_and_run(tmp_path, source) == expected


def test_d4_context_manager_enter_exit_and_suppression_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        class CM:
            def __init__(self):
                self.exited = 0

            def __enter__(self):
                print("enter")
                return "value"

            def __exit__(self, exc_type, exc, tb):
                self.exited = 1
                print("exit")
                return True

        cm = CM()
        with cm as v:
            print(v)
            raise ValueError("suppressed")
        print(cm.exited)
        ''',
    )
    assert lines == ["enter", "value", "exit", "1"]


def test_d5_iteration_number_comparison_protocols_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        class Box:
            def __init__(self):
                self.data = [1, 2, 3]

            def __len__(self):
                return len(self.data)

            def __bool__(self):
                return len(self) > 0

            def __contains__(self, x):
                return x in self.data

            def __getitem__(self, i):
                return self.data[i]

            def __setitem__(self, i, v):
                self.data[i] = v

            def __delitem__(self, i):
                del self.data[i]

            def __lt__(self, other):
                return len(self) < len(other)

            def __eq__(self, other):
                return len(self) == len(other)

        b = Box()
        print(len(b))
        print(bool(b))
        print(2 in b)
        print(b[1])
        b[1] = 7
        print(b[1])
        del b[0]
        print(len(b))
        print(b == Box())
        print(b < Box())
        ''',
    )
    assert lines == ["3", "True", "True", "2", "7", "2", "False", "True"]


def test_d6_format_spec_and_user_format_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        '''
        class F:
            def __format__(self, spec):
                return "fmt:" + spec

        print(format(255, "x"))
        print(format(12, "d"))
        print(format(F(), "abc"))
        print(f"{255:x}")
        ''',
    )
    assert lines == ["ff", "12", "fmt:abc", "ff"]
