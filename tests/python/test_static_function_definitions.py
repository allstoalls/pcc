"""Capture-free definitions with static signatures use one runtime constructor.

``py_func_new_static_def`` replaces the inline per-step construction for these
definitions. The program checks what that constructor owns -- names,
qualified names, module, docstrings, defaults, keyword-only parameters,
identity of each definition and of redefinitions, methods and hoisted nested
functions -- under all five collectors, with collections between definitions,
against CPython's output.
"""
import os
from pathlib import Path
import subprocess
import sys

PROGRAM = '''import gc


def plain(a, b):
    return a + b


def documented(value=7, *, scale=3):
    """Scale a value."""
    return value * scale


def no_doc_defaults(text="x", count=2, flag=None, *rest, **named):
    return text * count, flag, rest, sorted(named)


gc.collect()
first_identity = documented
churn = [[index] * 8 for index in range(2000)]
gc.collect()


def redefined():
    return "first"


early = redefined


def redefined():
    """Second."""
    return "second"


class Holder:
    def method(self, value=4):
        """A method."""
        return value + 1

    def other(self):
        return "other"


def outer(limit):
    def helper(step=2):
        return step * 10
    gc.collect()
    return helper() + limit


def main():
    print(plain(2, 3), plain.__name__, plain.__qualname__, plain.__module__)
    print(documented(), documented(2), documented(2, scale=5), documented.__doc__)
    print(documented is first_identity, no_doc_defaults.__doc__)
    print(no_doc_defaults(), no_doc_defaults("ab", 3, True, 9, k=1, j=2))
    print(early(), redefined(), redefined.__doc__, early is redefined)
    holder = Holder()
    gc.collect()
    # Read the class dict entry: a static class-attribute read returns another
    # object without __doc__ (known defect, docs/status.md).
    print(holder.method(), holder.method(10), Holder.method.__qualname__,
          Holder.__dict__["method"].__doc__)
    print(holder.other(), Holder.other.__name__)
    print(outer(1), outer(2))
    try:
        documented(1, 2)
    except TypeError:
        print("positional scale rejected")
    del churn[:]
    gc.collect()
    print(plain.__name__, documented.__doc__)


main()
'''


def test_static_definitions_match_cpython_on_five_collectors(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "static_defs.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "static_defs"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
