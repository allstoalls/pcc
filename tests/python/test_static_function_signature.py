"""Constant function signatures are built from one static descriptor.

Every ``def`` built its five-tuple signature (magic, names, kinds,
has-defaults, defaults) at run time: four ``py_tuple_new`` calls, a
``py_tuple_set_item`` per field, and the pin/lease/status protocol around
each. A module-level ``def f(a, b=1)`` cost about 373 IR instructions (186
calls) in the module initialiser, and module initialisers were 103 MB of a
430 MB pcc1 text segment. When every default is a compile-time constant (str,
small int, None or bool) the whole signature is now one read-only descriptor
plus one ``py_static_aggregate_build`` call. Other defaults keep the general
path, which evaluates them in order at definition time.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from pcc.frontends.python import pipeline


_PROGRAM = '''
def plain(a, b):
    return a + b


def defaults(a, b=2, c="three", d=None, e=True, f=False, g=-7):
    return (a, b, c, d, e, f, g)


def kinds(a, /, b, *rest, c, d=4, **extra):
    return (a, b, rest, c, d, sorted(extra.items()))


BIG = 2 ** 70


def big_default(x=2 ** 70, y=1.5):
    return (x == BIG, y)


def computed_default(x=len("abcd")):
    return x


class Box:
    def __init__(self, value=None, label="box"):
        self.value = value
        self.label = label

    def show(self, prefix="<", suffix=">"):
        return prefix + self.label + ":" + str(self.value) + suffix


def make_adders():
    adders = []
    for step in range(3):
        def add(x, by=10):
            return x + by + step
        adders.append(add)
    return adders


def main() -> None:
    print(plain(1, 2))
    print(defaults(1))
    print(defaults(1, 5, "x", [1], False, True, 0))
    print(defaults(a=9, g=3))
    print(kinds(1, 2, 3, 4, c=5, z=6, y=7))
    print(big_default())
    print(computed_default())
    print(Box().show())
    print(Box(3, "b").show(suffix="]"))
    print([add(1) for add in make_adders()], [add(1, by=0) for add in make_adders()])
    try:
        defaults()
    except TypeError:
        print("missing argument rejected")
    table = [(None, True), (False, 1), ("s", None)]
    print(table, table[0][0] is None, table[0][1] is True, table[1][0] is False)


main()
'''


def _function_body(text, name):
    match = re.search(r"^define [^\n]*@\"?" + re.escape(name) + r"\"?\([^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, name
    return match.group(1)


def _main_instructions(tmp_path, source_text, name):
    source = tmp_path / (name + ".py")
    source.write_text(source_text, encoding="utf-8")
    output = tmp_path / (name + ".ll")
    pipeline.compile_python(str(source), str(output), emit_llvm_only=True, libpython_mode="off")
    text = output.read_text(encoding="utf-8")
    body = _function_body(text, "main")
    return text, [line for line in body.splitlines() if line.startswith("  ")]


def _defs(count):
    return "".join(
        f'def f{i}(a, b=1, c=None, d="x"):\n    return a\n\n\n' for i in range(count)
    ) + "print(f0(1))\n"


def test_constant_signatures_use_one_static_descriptor_per_def(tmp_path):
    text, _ = _main_instructions(tmp_path, _defs(3), "three_defs")
    main = _function_body(text, "main")
    builds = re.findall(r"getelementptr inbounds \[\d+ x ptr\], ptr @\"?(\.pcc\.static\.agg\.func\.sig\.\d+)", main)
    assert len(builds) == 3, builds
    # py_func_new_static_def builds the aggregate from each descriptor.
    assert main.count("@py_func_new_static_def(") == 3
    descriptor = re.search(re.escape(builds[0]) + r"\"? = internal constant \[(\d+) x ptr\]", text)
    assert descriptor is not None
    # magic + 4 header/count pairs per tuple + 4 fields x 4 parameters + outer pair
    assert int(descriptor.group(1)) == 2 + 1 + 4 * (2 + 4)
    assert "@py_tuple_set_item" not in main


def test_static_signature_cost_per_def(tmp_path):
    _, ten = _main_instructions(tmp_path, _defs(10), "ten_defs")
    _, forty = _main_instructions(tmp_path, _defs(40), "forty_defs")
    per_def = (len(forty) - len(ten)) / 30
    # 373 per def before; the descriptor path leaves the function object and
    # its module binding.
    assert per_def < 200, per_def


def test_non_constant_defaults_keep_the_general_path(tmp_path):
    text, _ = _main_instructions(
        tmp_path,
        "def f(x=2 ** 70, y=1.5):\n    return x\n\n\nprint(f())\n",
        "general_defaults",
    )
    main = _function_body(text, "main")
    assert ".pcc.static.agg.func.sig." not in main
    assert "@py_tuple_set_item" in main


def test_static_signatures_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "signatures.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "signatures"
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
