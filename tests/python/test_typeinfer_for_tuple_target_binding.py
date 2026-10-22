"""For-loop tuple-unpack targets must be bound into the inference scope.

They never were: each element name resolved through the enclosing chain,
usually falling to dyn by accident — but a method whose OWN name matched an
element found the recursion seed (`param_scope.define(fn.name, ft)`) and
inferred 'callable', rejecting legal Python with
"return type mismatch: expected 'int', got 'callable'".

Task: PY-P1-TYPEINFER-LOOP-VAR-SHADOWS-METHOD.
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest


_SHADOW = """
class Table:
    def __init__(self) -> None:
        self.rows = [("a", 1), ("b", 2)]

    def value_id(self, name: str) -> int:
        for existing_name, value_id in self.rows:
            if existing_name == name:
                return value_id
        return -1

    def block_id(self, name: str) -> int:
        for existing_name, block_id in self.rows:
            if existing_name == name:
                return block_id
        return -1


def main() -> None:
    t = Table()
    print(t.value_id("b"))
    print(t.value_id("zz"))
    print(t.block_id("a"))


main()
"""

_TYPED_SLOTS = """
def main() -> None:
    pairs: list = [(1, "one"), (2, "two")]
    total: int = 0
    words = ""
    for number, word in pairs:
        total = total + number
        words = words + word
    print(total)
    print(words)
    # nested unpack
    rows = [((1, 2), "x"), ((3, 4), "y")]
    acc: int = 0
    tail = ""
    for (a, b), label in rows:
        acc = acc + a + b
        tail = tail + label
    print(acc)
    print(tail)
    # pre-loop binding of a DIFFERENT type must join to dyn, not crash
    joined = 7
    for joined, _ in pairs:
        pass
    print(joined)


main()
"""


@pytest.mark.parametrize(
    ("name", "program", "expected"),
    [
        ("shadow", _SHADOW, ["2", "-1", "1"]),
        ("typed_slots", _TYPED_SLOTS, ["3", "onetwo", "10", "xy", "2"]),
    ],
)
def test_for_tuple_targets_bind_and_match_cpython(tmp_path, name, program, expected):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    exe = tmp_path / f"{name}.out"
    src.write_text(textwrap.dedent(program).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    native = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120,
    )
    assert native.returncode == 0, (name, native.stderr)
    assert native.stdout.split() == expected, (name, native.stdout)
