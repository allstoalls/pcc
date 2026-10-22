"""``==``/``!=`` follow the NotImplemented protocol for typed instances.

For an instance of a known class the comparison called its ``__eq__``
directly and truth-tested the answer, so ``NotImplemented`` (a truthy
object) made ``key == "a"`` True; ``key == 1`` did not compile at all
("cannot coerce ClassType to int"). A dunder that returns an object now
goes through the runtime protocol: the reflected dunder once, then
identity. A ``-> bool`` annotation does not narrow a method that returns
NotImplemented. Native output under every collector must match CPython.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SOURCE = """
class Key:
    def __init__(self, text):
        self.text = text

    def __eq__(self, other):
        if not isinstance(other, Key):
            return NotImplemented
        return self.text == other.text

    def __hash__(self):
        return hash(self.text)


class Loose:
    def __init__(self, text):
        self.text = text

    def __eq__(self, other):
        if isinstance(other, Key):
            return other.text == self.text
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return NotImplemented
        return not result


class Never:
    def __eq__(self, other):
        return NotImplemented


class Plain:
    def __eq__(self, other):
        return isinstance(other, Plain)


class Annotated:
    def __init__(self, text):
        self.text = text

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Annotated):
            return NotImplemented
        return self.text == other.text

    def __hash__(self) -> int:
        return hash(self.text)


def main():
    key = Key("a")
    print(key == Key("a"), key == Key("b"), key == "a", "a" == key, key != "a", key == key)
    loose = Loose("a")
    print(key == loose, loose == key, key != loose, loose != key)
    print(key == Loose("b"), key != Loose("b"))
    never = Never()
    print(never == never, never == Never(), never != never, never != Never())
    print(key == 1, 1 == key, key != 1, key == None, None == key)
    print(key in [1, "a", Key("a")], {key: 1}.get(Key("a")), {key: 1}.get("a"))
    print(Plain() == Plain(), Plain() == 1, Plain() != Plain())
    marked = Annotated("a")
    print(marked == Annotated("a"), marked == "a", "a" == marked, marked != "a",
          marked == Annotated("b"), {marked: 2}.get(Annotated("a")))


main()
"""


def test_typed_instance_equality_follows_notimplemented(tmp_path):
    source = tmp_path / "compare.py"
    source.write_text(_SOURCE, encoding="utf-8")
    exe = tmp_path / "compare_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(source), "-o", str(exe)],
        text=True, capture_output=True, timeout=600, env=env,
    )
    assert built.returncode == 0, built.stderr
    expected = subprocess.run([sys.executable, str(source)], text=True, capture_output=True,
                              timeout=60, env=env, cwd=tmp_path)
    assert expected.returncode == 0, expected.stderr
    for backend in range(5):
        native = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60,
                                env=dict(env, PCC_GC_BACKEND=str(backend)))
        assert native.returncode == 0, (backend, native.stderr)
        assert native.stdout == expected.stdout, backend
