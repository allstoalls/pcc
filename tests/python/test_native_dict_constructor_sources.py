"""``dict(x)`` for any source, wherever a call operand needs its result.

The owned dict() producer, which publishes into the caller's output root,
took only a dict or dynamic source. A list or tuple of pairs or an instance
fell back to an older builder that returns its result after cleanup, so as a
call or operator operand (``OrderedDict(dict(pairs))``, ``cm1 | dict(cm2)``)
the compile failed with "slot-call Call lacks an immediate owned-result
handoff". Every other source now goes through the owned update transaction,
which resolves keys()/__getitem__ or iterates pairs, as dict(x) does.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
class Mapping:
    def __init__(self, data):
        self.data = data

    def keys(self):
        return list(self.data)

    def __getitem__(self, key):
        return self.data[key] * 10


class Pairs:
    def __iter__(self):
        return iter([("p", 1), ("q", 2)])


def take(value):
    return sorted(value.items())


def errors(source):
    try:
        dict(source)
    except (TypeError, ValueError) as caught:
        return type(caught).__name__ + ": " + str(caught)
    return "no error"


def main():
    pairs = [("a", 1), ("b", 2)]
    print(take(dict(pairs)), take(dict(tuple(pairs))), take(dict(Pairs())))
    print(take(dict(Mapping({"x": 1, "y": 2}))), {"z": 0} | dict(pairs))
    print(take(dict(pairs, c=3)), take(dict((k, v) for k, v in pairs)))
    print(errors(5), errors(["ab", "c"]), errors([(1, 2, 3)]))
    print(dict([]), len(dict(pairs * 3)))


main()
'''


@pytest.mark.integration
def test_dict_constructor_sources_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "dict_sources.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "dict_sources"
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
