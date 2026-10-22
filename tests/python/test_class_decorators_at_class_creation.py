"""A class statement's decorators are any expressions, applied at creation.

class_gen applied a class decorator only when it was a bare name of a
function defined in the same module, through a direct native call; any
other decorator -- @unittest.skip("..."), @support.cpython_only,
@registry.add, @table["key"] -- stopped compilation with "Layer 1 does not
handle class decorator expression", which kept many CPython Lib/test modules
from compiling. Now every decorator expression is evaluated, top to bottom,
before the bases and the body, and applied bottom-up with the ordinary call
protocol when the body has run; the name binds whatever the outermost
decorator returns, and an exception from a decorator propagates.

pcc's unittest.skip raised NotImplementedError for a class; it now sets
CPython's __unittest_skip__/__unittest_skip_why__ marks on it, and the static
unittest.main() runner leaves out a marked class and counts a SkipTest as a
skip instead of letting it escape.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import unittest

events = []


def tagged(label):
    events.append("evaluate " + label)

    def apply(cls):
        events.append("apply " + label + " to " + cls.__name__)
        cls.tags = getattr(cls, "tags", ()) + (label,)
        return cls
    return apply


class Registry:
    def __init__(self):
        self.names = []

    def add(self, cls):
        self.names.append(cls.__name__)
        return cls


registry = Registry()
decorators = {"mark": tagged("subscript")}


@tagged("outer")
@registry.add
@tagged("inner")
class Point:
    events.append("body of Point")

    def __init__(self, x):
        self.x = x

    def double(self):
        return self.x * 2


@decorators["mark"]
class Marked:
    pass


def replace_with_factory(cls):
    def factory(value):
        return cls.__name__ + " made " + str(value)
    return factory


@replace_with_factory
class Replaced:
    pass


class Holder:
    def __init__(self, cls):
        self.held = cls


@Holder
class Held:
    kind = "held"


def local_class(label):
    def stamp(cls):
        cls.stamp = label
        return cls

    @stamp
    @tagged("local " + label)
    class Local:
        def describe(self):
            return self.stamp + ":" + str(self.tags)
    return Local


def refusing(error):
    def refuse(cls):
        events.append("refuse " + cls.__name__)
        raise error

    @refuse
    class Never:
        pass
    return Never


@unittest.skip("whole class")
class SkippedCase(unittest.TestCase):
    def test_never(self):
        raise AssertionError("not run")


@unittest.skipIf(False, "kept")
class KeptCase(unittest.TestCase):
    def test_kept(self):
        pass


@unittest.skipUnless(False, "unless reason")
class UnlessCase(unittest.TestCase):
    pass


def main():
    print(Point(21).double(), Point.tags, registry.names)
    print(Marked.tags)
    print(Replaced(7), type(Replaced).__name__)
    print(type(Held).__name__, Held.held.__name__, Held.held.kind)
    first = local_class("a")
    second = local_class("b")
    print(first().describe(), second().describe(), first is second)
    try:
        refusing(ValueError("refused"))
    except ValueError as caught:
        print("caught", caught)
    print(SkippedCase.__unittest_skip__, SkippedCase.__unittest_skip_why__)
    print(getattr(KeptCase, "__unittest_skip__", False))
    print(UnlessCase.__unittest_skip__, UnlessCase.__unittest_skip_why__)
    for event in events:
        print(event)


main()
'''


RUNNER_PROGRAM = r'''
import unittest


class Passing(unittest.TestCase):
    def test_one(self):
        self.assertEqual(1 + 1, 2)

    @unittest.skip("method reason")
    def test_skipped_method(self):
        raise AssertionError("not run")

    def test_failing(self):
        self.assertEqual(1, 2)


@unittest.skip("class reason")
class Skipped(unittest.TestCase):
    def test_a(self):
        raise AssertionError("not run")

    def test_b(self):
        raise AssertionError("not run")


@unittest.skipIf(False, "kept")
class Kept(unittest.TestCase):
    def test_kept(self):
        self.assertTrue(True)


unittest.main()
'''


def _environment(backend):
    environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
    environment.pop("LC_ALL", None)
    return environment


@pytest.mark.integration
def test_class_decorators_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "class_decorators.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "class_decorators"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=60,
            env=_environment(backend),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"


@pytest.mark.integration
def test_static_unittest_runner_honours_skips(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "unittest_skips.py"
    source.write_text(RUNNER_PROGRAM, encoding="utf-8")
    binary = tmp_path / "unittest_skips"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=60,
            env=_environment(backend),
        )
        # The failing test is the runner's exit status, as before.
        assert ran.returncode == 1, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == "2 passed, 1 failed, 3 skipped\n", f"GC{backend}: {ran.stderr}"
