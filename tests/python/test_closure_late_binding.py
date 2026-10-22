"""Lambdas see a captured variable's value at call time, as in CPython.

A pcc lambda copies each captured value when it is created; a CPython
closure reads the variable's shared cell when it is called.  They differ
when the variable is bound again while the lambda is alive: a later
assignment, a loop that creates the lambda and rebinds the name, or a
comprehension target (one cell per comprehension run, so every
[lambda: v for v in xs] entry returns xs[-1]).  Such names are boxed
into one-element cell lists; loop, with and except targets bind
through fresh names and then fill the cell.  The program also pins the
scoping the rewrite must respect: lambda parameters and comprehension
targets that shadow a boxed name, defaults evaluated at creation, and a
comprehension's first iterable evaluated in the enclosing scope.
"""

import os
import subprocess
import sys


PROGRAM = '''class Ctx:
    def __init__(self, v):
        self.v = v

    def __enter__(self):
        return self.v

    def __exit__(self, a, b, c):
        return False


def comp(values):
    output = [lambda: value for value in values]
    return [f() for f in output]


def comp_enumerate(values):
    output = [lambda: value for i, value in enumerate(values)]
    return [f() is values[-1] for f in output]


def comp_default(values):
    output = [lambda value=value: value for i, value in enumerate(values)]
    return [f() for f in output]


def loop(values):
    output = []
    for value in values:
        output.append(lambda: value)
    return [f() for f in output]


def nested(values):
    def make():
        return [lambda: v for v in values]
    return [f() for f in make()]


def rebind_after_def():
    x = 1
    def f():
        return x
    x = 2
    return f()


def rebind_after_lambda():
    x = 1
    f = lambda: x
    x = 2
    return f()


def counter():
    n = 0
    def inc():
        nonlocal n
        n += 1
        return n
    inc()
    inc()
    return n


def named_lambda_returned():
    x = 1
    f = lambda: x
    x = 2
    return f


def defs_in_loop(values):
    out = []
    for v in values:
        def g():
            return v
        out.append(g)
    return [f() for f in out]


def expr_lambda_rebound_later():
    x = 1
    fs = [lambda: x]
    x = 2
    return fs[0]()


def expr_lambda_param_rebound():
    def make(x):
        fs = (lambda: x,)
        x = x + 1
        return fs
    return make(10)[0]()


def two_comprehension_runs():
    out = []
    for j in range(2):
        out += [lambda: v for v in (j * 10, j * 10 + 1)]
    return [g() for g in out]


def sort_key_in_loop(rows):
    res = []
    for col in range(2):
        res.append(sorted(rows, key=lambda r: r[col]))
    return res


def with_capture():
    fs = []
    for i in range(3):
        with Ctx(i * 10) as v:
            fs.append(lambda: v)
    return [f() for f in fs]


def with_multi():
    fs = []
    for i in range(2):
        with Ctx(i) as a, Ctx(a + 10) as b:
            fs.append(lambda: (a, b))
    return [f() for f in fs]


def except_rebound():
    fs = []
    for i in range(2):
        try:
            raise ValueError(str(i))
        except ValueError as e:
            err = e
            fs.append(lambda: str(err))
    return [f() for f in fs]


def except_name_capture():
    out = []
    for i in range(2):
        try:
            raise ValueError(str(i))
        except ValueError as e:
            g = lambda: str(e)
            out.append(g())
    return out


def del_capture():
    x = 1
    f = lambda: x
    del x
    x = 5
    return f()


def comp_multi():
    fs = [lambda: (a, b) for a in range(2) for b in range(2) if a != b]
    return [f() for f in fs]


def comp_shadow_iter():
    i = [5, 6]
    fs = [lambda: i for i in i]
    return [f() for f in fs]


def comp_shadow_enumerate():
    k = [7, 8]
    fs = [lambda: v for k, v in enumerate(k)]
    return [f() for f in fs]


def dict_comp():
    d = {k: (lambda: k) for k in "ab"}
    out = []
    for key in sorted(d):
        out.append((key, d[key]()))
    return out


def set_comp():
    fs = {(lambda: n) for n in range(3)}
    return sorted([f() for f in fs])


def gen_comp():
    return [f() for f in list(lambda: n for n in range(3))]


def nested_comp():
    fs = [[lambda: (x, y) for y in range(2)] for x in range(2)]
    return [[f() for f in row] for row in fs]


def param_shadow():
    x = 1
    fs = [lambda: x]
    g = lambda x: x + 100
    x = 2
    return fs[0](), g(5)


def comp_shadow_boxed():
    x = 1
    f = lambda: x
    ys = [x * 2 for x in range(3)]
    x = 10
    return f(), ys


class Reader:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, a, b, c):
        return False

    def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        return b""


def argsort_shadow(data, rows, cols):
    out = []
    for col in range(cols):
        out.append(sorted(range(rows), key=lambda row: data[row * cols + col]))
    for row in range(rows):
        start = row * cols
        out.append(sorted(range(cols), key=lambda col: data[start + col]))
    return out


def with_iter_lambda():
    out = []
    with Reader([b"ab", b"cd"]) as f:
        for chunk in iter(lambda: f.read(2), b""):
            out.append(chunk)
    for name in ("x", "y"):
        with Reader([name.encode()]) as f:
            for chunk in iter(lambda: f.read(1), b""):
                out.append(chunk)
    return out


def tuple_target_boxed():
    plan = {"f": 1, "g": 2}
    out = []
    for fn_name, dead in plan.items():
        out.append(fn_name + str(dead))
    subs = []
    for fn_name in ("h", "k"):
        subs.append(lambda s: s + fn_name)
    return out, [g("<") for g in subs]


def comp_reads_boxed(members):
    best = []
    for target in members:
        candidates = [c for c in members if c != target]
        best.append(max(candidates, key=lambda c: abs(c - target)))
    return best


def main():
    print("comp", comp([1, 2, 3]))
    print("comp_enumerate", comp_enumerate(["a", "b", "c"]))
    print("comp_default", comp_default([1, 2, 3]))
    print("loop", loop([1, 2, 3]))
    print("nested", nested([1, 2, 3]))
    print("rebind_after_def", rebind_after_def())
    print("rebind_after_lambda", rebind_after_lambda())
    print("counter", counter())
    print("named_lambda_returned", named_lambda_returned()())
    print("defs_in_loop", defs_in_loop([1, 2, 3]))
    print("expr_lambda_rebound_later", expr_lambda_rebound_later())
    print("expr_lambda_param_rebound", expr_lambda_param_rebound())
    print("two_comprehension_runs", two_comprehension_runs())
    print("sort_key_in_loop", sort_key_in_loop([(2, 1), (1, 2)]))
    print("with_capture", with_capture())
    print("with_multi", with_multi())
    print("except_rebound", except_rebound())
    print("except_name_capture", except_name_capture())
    print("del_capture", del_capture())
    print("comp_multi", comp_multi())
    print("comp_shadow_iter", comp_shadow_iter())
    print("comp_shadow_enumerate", comp_shadow_enumerate())
    print("dict_comp", dict_comp())
    print("set_comp", set_comp())
    print("gen_comp", gen_comp())
    print("nested_comp", nested_comp())
    print("param_shadow", param_shadow())
    print("comp_shadow_boxed", comp_shadow_boxed())
    print("argsort_shadow", argsort_shadow([3, 1, 2, 0], 2, 2))
    print("with_iter_lambda", with_iter_lambda())
    print("tuple_target_boxed", tuple_target_boxed())
    print("comp_reads_boxed", comp_reads_boxed([1, 5, 9]))


main()
'''

EXPECTED = """comp [3, 3, 3]
comp_enumerate [True, True, True]
comp_default [1, 2, 3]
loop [3, 3, 3]
nested [3, 3, 3]
rebind_after_def 2
rebind_after_lambda 2
counter 2
named_lambda_returned 2
defs_in_loop [3, 3, 3]
expr_lambda_rebound_later 2
expr_lambda_param_rebound 11
two_comprehension_runs [1, 1, 11, 11]
sort_key_in_loop [[(1, 2), (2, 1)], [(2, 1), (1, 2)]]
with_capture [20, 20, 20]
with_multi [(1, 11), (1, 11)]
except_rebound ['1', '1']
except_name_capture ['0', '1']
del_capture 5
comp_multi [(1, 1), (1, 1)]
comp_shadow_iter [6, 6]
comp_shadow_enumerate [8, 8]
dict_comp [('a', 'b'), ('b', 'b')]
set_comp [2, 2, 2]
gen_comp [2, 2, 2]
nested_comp [[(1, 1), (1, 1)], [(1, 1), (1, 1)]]
param_shadow (2, 105)
comp_shadow_boxed (10, [0, 2, 4])
argsort_shadow [[1, 0], [1, 0], [1, 0], [1, 0]]
with_iter_lambda [b'ab', b'cd', b'x', b'y']
tuple_target_boxed (['f1', 'g2'], ['<k', '<k'])
comp_reads_boxed [9, 1, 1]
"""


def test_lambda_captures_bind_late_under_every_collector(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "closure_late_binding.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == EXPECTED
    output = tmp_path / "closure_late_binding"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == EXPECTED, f"GC{backend}"
