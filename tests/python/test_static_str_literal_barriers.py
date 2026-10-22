"""String literals are static immortal objects: no pin, unpin or release.

A ``str`` literal lowers to a module-static object whose header carries
``PY_FLAG_IMMORTAL``; collectors never move or free it. Pinning, unpinning
and releasing it around every call were pure protocol (68 of 1,518 calls in
one small module). The literal is now recorded like the ``None`` singleton
and tagged int literals. The native program passes literals through calls
whose later operand raises, through methods, containers and attributes, and
churns allocations so every collector runs while literals are live.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

PROGRAM = '''class Box:
    def __init__(self, label, size):
        self.label = label
        self.size = size

    def join(self, left, middle, right):
        return left + middle + right + self.label


def explode(n):
    if n > 2:
        raise ValueError("boom " + str(n))
    return n


def three(a, b, c):
    return a + "|" + b + "|" + str(c)


def churn(n):
    out = []
    i = 0
    while i < n:
        out.append([i, str(i), {"k": i}])
        i += 1
    return len(out)


def main():
    lines = []
    box = Box("tail", 3)
    for n in range(5):
        try:
            lines.append(three("alpha", "beta", explode(n)))
        except ValueError as exc:
            lines.append("caught " + str(exc))
        lines.append(box.join("x", "y", "z"))
        churn(200)
    items = []
    for n in range(4):
        items.append("same")
        items.append(Box("inner", n).label)
    lines.append(str(items[0] is items[2]) + " " + str(len(items)))
    table = {"a": "one", "b": "two"}
    lines.append(table.get("a", "missing") + table.get("zz", "missing"))
    labels = [Box("k" + str(n), n) for n in range(3)]
    churn(500)
    lines.append(",".join([b.label for b in labels]))
    del labels
    churn(500)
    lines.append(f"{box.label}-{'lit'}-{box.size}")
    print("\\n".join(lines))


main()
'''


def test_str_literal_operands_emit_no_gc_protocol():
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    typed = infer_module(parse_and_lift(PROGRAM, "lits.py", "lits"))
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    text = str(codegen.generate(typed))
    assert "@.pystr.obj." in text
    protocol = re.findall(
        r"@pcc_gc_(?:pin|unpin|release|release_known)\(ptr @\.pystr\.obj\.\d+\)", text,
    )
    assert protocol == [], protocol[:5]


def test_str_literal_operands_match_cpython_on_five_collectors(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "lits.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "lits"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr[-2000:]
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
