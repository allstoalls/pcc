"""A local rebound inside ``try``/``if`` keeps every type that reaches the join.

Type inference had no join for ``try`` and did not count a nested ``def`` as an
``if``-arm binding. ``x = 37`` followed by a rebinding to a function in either
statement left ``x`` typed by its last binding after the statement, so
``x == 37`` failed with "cannot coerce FuncType to int". A handler that returns
does not carry the pre-``try`` binding forward and needs no widening.
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROGRAM = '''
def fail():
    raise ValueError("fail")


def produce():
    return 5


def assigned_function(flag):
    previous = 37
    try:
        if flag:
            fail()
        previous = produce
    except ValueError:
        pass
    return previous == 37, previous is produce


def failing_default():
    previous = 41
    try:
        def previous(first=fail()):
            return first
    except ValueError:
        pass
    return previous == 41


def conditional_def(flag):
    previous = 37
    if flag:
        def previous():
            return 5
    if flag:
        return previous()
    return previous + 1


def handler_returns(text):
    try:
        value = int(text, 0)
    except ValueError:
        return None
    return value * 2


def partial_body(stage):
    value = "start"
    try:
        value = 1.5
        if stage == 1:
            fail()
        value = 2
        if stage == 2:
            fail()
        value = (3,)
    except ValueError:
        pass
    return value


def main() -> None:
    print(assigned_function(True), assigned_function(False))
    print(failing_default())
    print(conditional_def(True), conditional_def(False))
    print(handler_returns("0x10"), handler_returns("bad"))
    print(partial_body(1), partial_body(2), partial_body(3))


main()
'''


def test_try_and_if_rebindings_join_every_reaching_type(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "try_rebinding.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "try_rebinding"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
