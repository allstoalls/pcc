"""pickle.dumps()/loads() results are owned values for any consumer.

Both returned the runtime's NEW object built from unrooted operands and
without an owner record, so as a call operand
(``"".join(pickle.loads(pickle.dumps(it, proto)))``, as test_str does) the
compile failed with "slot-call operand has no authoritative source or owned
result: Call". They now own their operands and publish the result before
cleanup, as copy.copy() does.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import pickle


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def show(value):
    return repr(value)


def main():
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        print(proto, show(pickle.loads(pickle.dumps([1, "a", (2, 3.5)], proto))))
    print("".join(pickle.loads(pickle.dumps(["x", "y", "z"]))))
    print(sorted(pickle.loads(pickle.dumps({"k": [1, 2], "j": None})).items()))
    point = pickle.loads(pickle.dumps(Point(3, 4), 2))
    print(type(point).__name__, point.x + point.y, len(pickle.dumps("text")) > 0)


main()
'''


@pytest.mark.integration
def test_pickle_results_as_operands_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "pickle_operands.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "pickle_operands"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
