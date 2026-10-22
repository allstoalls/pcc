"""An unpacked argument list reaches a method whose receiver is dynamic.

A call ``obj.join(*parts)`` on a value of unknown type took the native str
``join`` shortcut, which lowers a fixed operand list: the ``*parts`` marker
was compiled as an ordinary call, and the program raised
``NameError: name '*' is not defined`` (``self.pathmodule.join(*parts)``,
with a path module held in a class attribute, as test_genericpath does).
The str and bytes shortcuts now leave unpacked arguments to the generic
method call. (A str or bytes receiver itself still needs native str/bytes
method attributes, which only count/startswith/endswith have.)
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import posixpath

module = posixpath


class Holder:
    pathmodule = posixpath

    def join(self, *parts):
        return self.pathmodule.join(*parts)


class Codec:
    def join(self, *parts):
        return "+".join(parts)

    def decode(self, *options, **settings):
        return (options, sorted(settings.items()))


def through_name(*parts):
    return module.join(*parts)


def call_join(target, *parts):
    return target.join(*parts)


def call_decode(target, *options, **settings):
    return target.decode(*options, **settings)


def main():
    print(Holder().join("e", "f", "g"), through_name("c", "d"))
    print(call_join(Codec(), "x", "y"), call_join(module, "p", "q"))
    print(call_decode(Codec(), "utf-8", errors="strict"))


main()
'''


@pytest.mark.integration
def test_unpacked_arguments_reach_dynamic_receivers_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "dyn_unpacked.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "dyn_unpacked"
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
