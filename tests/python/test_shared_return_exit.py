"""Returns share one exit that leaves the function's GC root frames once.

Every rooted local is entered once at function entry, and every exit must
leave it. Each ``return`` used to repeat the whole leave sequence, so R
returns and S rooted slots cost R*S ``pcc_gc_frame_leave`` calls: the 2,072
line ``_emit_call`` dispatcher in pcc1 carried 43,560 of them. Returns now
branch to a shared exit that leaves every slot, so the count is per exit
kind, not per return. A return keeps its own cleanup-root protocol (a LIFO
frame entered at that return), so returns that need a root and returns that
do not join two separate exits and no call pays for a root it did not use.
The native program returns owned and borrowed values from straight-line
code, a loop and ``try/finally``, raises, and churns allocations so every
collector runs between calls.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

PROGRAM = '''class Node:
    def __init__(self, v):
        self.v = v

    def __repr__(self):
        return "Node(" + str(self.v) + ")"


def churn(n):
    out = []
    i = 0
    while i < n:
        out.append([i, str(i)])
        i += 1
    return len(out)


def pick(k, items):
    a = Node(k)
    b = [k, k + 1]
    c = {"k": k}
    d = str(k) * 2
    e = items[k % len(items)]
    f = (a, b)
    churn(50)
    if k == 0:
        return a
    if k == 1:
        return b
    if k == 2:
        return c
    if k == 3:
        return d
    if k == 4:
        return e
    if k == 5:
        return f
    for x in items:
        if x == k:
            return Node(x * 10)
    try:
        if k == 7:
            return "try"
    finally:
        churn(20)
    if k == 8:
        raise ValueError("eight")
    return None


def count(k) -> int:
    a = Node(k)
    b = [a]
    if k < 3:
        return 1
    if k < 6:
        return len(b) + 1
    return a.v


def main():
    items = [6, 9, Node(1), "s"]
    for k in range(11):
        try:
            print(k, pick(k, items))
        except ValueError as exc:
            print(k, "raised", exc)
        churn(100)
    print([count(k) for k in range(8)])


main()
'''


def _function(text: str, name: str) -> str:
    match = re.search(r"^define [^\n]*@" + re.escape(name) + r"\([^\n]*\).*?^}", text, re.M | re.S)
    assert match, name
    return match.group(0)


def test_returns_leave_root_frames_once_per_exit():
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    module = infer_module(parse_and_lift(PROGRAM, "exits.py", "exits"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    body = _function(str(codegen.generate(module)), "user_exits_pick")
    enters = len(re.findall(r"@pcc_gc_frame_enter\(", body))
    leaves = len(re.findall(r"@pcc_gc_frame_leave\(", body))
    returns = len(re.findall(r"^\s*ret ", body, re.M))
    assert enters >= 6, enters
    # One leave per slot on each shared return exit (rooted and unrooted
    # values), on the error exit and on the owned return from the loop, which
    # keeps its own exit because its pinned handoff must follow every frame
    # leave (pcc_gc_take_pinned_slot). Ten returns used to repeat all of them
    # (10 * enters).
    assert leaves <= 4 * enters, (enters, leaves, returns)
    # The shared value slot is never a function-entry root: only a return
    # whose value needs a cleanup root enters it, as a LIFO frame.
    shared = set(re.findall(r"(%\"?ret\.shared[\w.]*\"?) = alloca", body))
    assert shared, "pick must return through a shared exit"
    aliases = set(shared)
    for target, source in re.findall(r"(%[\w.\"]+) = bitcast ptr (%[\w.\"]+) to ptr", body):
        if source in aliases:
            aliases.add(target)
    for line in body.splitlines():
        if "@pcc_gc_frame_enter(" in line:
            assert not any(alias + ")" in line or alias + "," in line for alias in aliases), line


def test_shared_return_exit_matches_cpython_on_five_collectors(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "exits.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "exits"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr[-2000:]
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
