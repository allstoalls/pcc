"""Local loads proven bound skip the UnboundLocalError check; all others keep it.

Every function local carries a ``.bound.<name>`` flag and every load used to
check it, including parameters and straight-line assignments: a 4-line
function held six UnboundLocalError blocks. The definite-assignment pass
marks only loads bound on every path; ``del``, ``except ... as`` cleanup,
loops, handlers and ``with`` suppression keep their checks.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from pcc.frontends.python.codegen.definite_assignment import definitely_bound_loads
from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.py_lift import lift_module
from pcc.frontends.python.py_parse import parse
from pcc.frontends.python.py_ast import FuncDef


def _function(source: str, name: str = "f"):
    module = lift_module(parse(dedent(source), "probe.py"), "probe.py", "probe")
    return next(node for node in module.body if isinstance(node, FuncDef) and node.name == name)


def _safe_lines(source: str, name: str = "f") -> set:
    """``(line, ident)`` of loads proven bound (lines relative to the source)."""
    return {(key[1], key[0]) for key in definitely_bound_loads(_function(source, name))}


def test_parameters_and_straight_line_locals_are_bound():
    safe = _safe_lines("""
        def f(items, key):
            total = len(items)
            value = items[key]
            return total, value
    """)
    assert {(3, "items"), (4, "items"), (4, "key"), (5, "total"), (5, "value")} <= safe


def test_branches_meet_and_terminating_branches_drop_out():
    safe = _safe_lines("""
        def f(c):
            if c:
                a = 1
                b = 1
            else:
                a = 2
            print(a)
            print(b)
            if c:
                d = 1
            else:
                raise ValueError
            print(d)
    """)
    assert (8, "a") in safe and (14, "d") in safe
    assert (9, "b") not in safe


def test_loop_deletion_on_a_later_iteration_keeps_the_check():
    safe = _safe_lines("""
        def f(n):
            x = 1
            while n:
                print(x)
                del x
                n -= 1
            y = 2
            for i in range(n):
                print(y, i)
            print(i)
    """)
    assert (5, "x") not in safe
    assert (10, "y") in safe and (10, "i") in safe
    assert (11, "i") not in safe


def test_handlers_with_and_except_names_keep_their_checks():
    safe = _safe_lines("""
        def f(g):
            a = 0
            try:
                b = g()
                del a
            except ValueError as e:
                print(a, b, e)
            print(e)
            with g() as h:
                c = h
            print(h, c)
            try:
                d = g()
            finally:
                print(d)
    """)
    assert (8, "e") in safe
    assert (8, "a") not in safe and (8, "b") not in safe
    assert (9, "e") not in safe
    assert (12, "h") in safe and (12, "c") not in safe
    assert (16, "d") not in safe


def test_synthesized_loads_without_spans_are_never_marked():
    from dataclasses import replace

    from pcc.frontends.python.codegen.definite_assignment import load_key
    from pcc.frontends.python.py_ast import ExprStmt, Name

    fd = _function("""
        def f(a):
            print(a)
    """)
    synthesized = Name(None, None, "a")
    fd = replace(fd, body=fd.body + (ExprStmt(None, synthesized),))
    assert load_key(synthesized) is None
    assert None not in definitely_bound_loads(fd)


_SEMANTICS = '''
def unbound_if(c):
    if c:
        v = 1
    return v


def deleted_in_loop(n):
    x = 1
    out = []
    while n:
        out.append(x)
        del x
        n -= 1
    return out


def handler_sees_partial_try(g):
    try:
        b = g()
    except ValueError:
        return b


def except_name_cleared():
    try:
        raise KeyError("k")
    except KeyError as e:
        pass
    return e


def zero_iteration_target(n):
    for i in range(n):
        pass
    return i


def suppressed_with():
    class Quiet:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return True

    with Quiet():
        raise RuntimeError("x")
        w = 1
    return w


def bound_everywhere(items, key):
    total = len(items)
    value = items[key]
    name = str(value)
    return total, name


def fail():
    raise ValueError("boom")


def run(label, call):
    try:
        print(label, call())
    except UnboundLocalError as exc:
        print(label, "UnboundLocalError", str(exc))


def main():
    run("if", lambda: unbound_if(False))
    run("if-bound", lambda: unbound_if(True))
    run("loop", lambda: deleted_in_loop(2))
    run("loop-one", lambda: deleted_in_loop(1))
    run("handler", lambda: handler_sees_partial_try(fail))
    run("except-name", except_name_cleared)
    run("zero-for", lambda: zero_iteration_target(0))
    run("one-for", lambda: zero_iteration_target(3))
    run("with", suppressed_with)
    run("bound", lambda: bound_everywhere([4, 5, 6], 1))


main()
'''


def test_unbound_local_semantics_match_cpython_natively(tmp_path):
    source = tmp_path / "unbound_semantics.py"
    source.write_text(_SEMANTICS, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], text=True, capture_output=True,
                              check=True, timeout=30).stdout
    exe = tmp_path / "unbound_semantics"
    compile_python(str(source), str(exe), libpython_mode="off", ir_scaffold_mode="on", backend="self")
    actual = subprocess.run([str(exe)], text=True, capture_output=True, check=True, timeout=30).stdout
    assert actual == expected


def test_bound_locals_emit_no_unbound_error_blocks(tmp_path):
    source = tmp_path / "bound_ir.py"
    source.write_text(dedent('''
        def work(items, key):
            total = len(items)
            value = items[key]
            name = str(value)
            return total, name


        def main():
            print(work([1, 2, 3], 1))


        main()
    '''), encoding="utf-8")
    ll = tmp_path / "bound_ir.ll"
    compile_python(str(source), str(ll), libpython_mode="off", ir_scaffold_mode="on", backend="self",
                   emit_llvm_only=True)
    text = ll.read_text()
    body = re.search(r"^define [^\n]*@user_bound_ir_work\(.*?^}", text, re.M | re.S).group(0)
    assert re.findall(r"^[\w.]+\.bound\.error\.\d+:", body, re.M) == []
