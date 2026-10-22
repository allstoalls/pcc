"""Exact-int loop bodies must not drown the inline tagged fast path in protocol.

A three-statement loop (``total += i; i += 1; i < n``) lowered to 310 lines of
IR with 24 ``pcc_gc_load_ptr``, 19 unpin, 13 store_root, 10 pin, 3 LIFO frame
enters and a ``py_err_occurred`` on the *fast* path per iteration -- 1288
instructions per iteration against CPython's 718 (evidence
PERF-P0-PCC1-WORKER-OBJECT-PROTOCOL-TAX/001).  Root causes fixed here:

- the lhs was parked in a temporary root frame while the rhs was lowered,
  even when the rhs is a plain local/global load or a literal that cannot
  reach the runtime (nothing can collect in between);
- operand pins and the error check were emitted around the inline
  tagged fast path instead of only around the slow ``py_int_*`` call;
- ``i < n`` always called ``py_int_cmp`` instead of comparing tagged bits.

The IR ratchet pins the shape; the run test pins semantics on every GC
backend including tagged -> bignum promotion through the slow path.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

LOOP = (
    "import os\n"
    "N = int(os.environ.get('BENCH_N', '1000'))\n"
    "\n"
    "def main() -> None:\n"
    "    total = 0\n"
    "    i = 0\n"
    "    while i < N:\n"
    "        total += i\n"
    "        i += 1\n"
    "    print(total)\n"
    "\n"
    "main()\n"
)

PROMOTE = (
    "def main() -> None:\n"
    "    total = 1 << 60\n"
    "    i = 0\n"
    "    while i < 6:\n"
    "        total += total\n"
    "        i += 1\n"
    "    small = 7\n"
    "    j = 0\n"
    "    while j < 3:\n"
    "        small += j\n"
    "        j += 1\n"
    "    print(total, small, total > small, j < 3, j == 3, i >= 6)\n"
    "\n"
    "main()\n"
)


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def _compile(tmp_path: Path, name: str, source: str, *, emit_llvm: bool):
    src = tmp_path / f"{name}.py"
    src.write_text(source, encoding="utf-8")
    out = tmp_path / (f"{name}.ll" if emit_llvm else f"{name}.bin")
    cmd = ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", "--ir-scaffold=on"]
    cmd += [f"--emit-llvm={out}", str(src)] if emit_llvm else [str(src), "-o", str(out)]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=420, env=_env())
    assert proc.returncode == 0, proc.stderr
    return src, out


def _main_body(ll: str) -> str:
    m = re.search(r"define [^\n]*@user_[a-z0-9_]*main\([^\n]*\{\n(.*?)\n\}", ll, re.S)
    assert m, "main function not found"
    return m.group(1)


def _blocks(body: str) -> list[str]:
    return re.split(r"\n(?=[A-Za-z_.][A-Za-z0-9_.]*:\n)", body)


def test_exact_int_loop_protocol_ratchet(tmp_path):
    _, ll_path = _compile(tmp_path, "loop", LOOP, emit_llvm=True)
    body = _main_body(ll_path.read_text(encoding="utf-8"))
    counts = {
        name: len(re.findall(r"@" + name + r"\(", body))
        for name in (
            "pcc_gc_load_ptr", "pcc_gc_pin", "pcc_gc_unpin", "pcc_gc_store_root",
            "pcc_gc_store_root_take", "pcc_gc_release",
            "pcc_gc_frame_enter_lifo", "py_err_occurred", "py_int_cmp", "py_int_add",
        )
    }
    # Before: load_ptr 24 / pin 10 / unpin 19 / store_root 13 / lifo 3.
    # After:  load_ptr 9 (3 on the hot path, one per statement; the rest in
    # cold error/exit release blocks), pin 5 (all inside the two slow bignum
    # blocks), unpin 11 (slow blocks and their error-cleanup twins), no plain
    # store_root on any loop cycle (cold cleanup may clear roots), four
    # ownership-transferring pcc_gc_store_root_take sites, and lifo 0.
    assert counts["pcc_gc_frame_enter_lifo"] == 0, counts
    assert counts["pcc_gc_load_ptr"] <= 9, counts
    # The tagged loop cannot acquire an ordinary root-store protocol. Cold
    # owner cleanup now clears its slot before any terminal release callback.
    blocks = {}
    for block in _blocks(body):
        label = re.match(r"([A-Za-z_.][A-Za-z0-9_.]*):\n", block)
        if label:
            blocks[label.group(1)] = block
    edges = {name: re.findall(r"label %([A-Za-z0-9_.]+)", block)
             for name, block in blocks.items()}
    cyclic = set()
    for name in blocks:
        pending = list(edges.get(name, ()))
        seen = set()
        while pending:
            next_name = pending.pop()
            if next_name == name:
                cyclic.add(name)
                break
            if next_name not in seen:
                seen.add(next_name)
                pending.extend(edges.get(next_name, ()))
    assert cyclic, "the tagged-loop CFG must contain its actual backedge"
    for name, block in blocks.items():
        for line in block.splitlines():
            if "@pcc_gc_store_root(" not in line:
                continue
            assert name not in cyclic, block
            assert re.search(r"@pcc_gc_store_root\([^\n]*, ptr null\)", line), line
            leave = block.find("@pcc_gc_frame_leave")
            assert leave < 0 or block.index(line) < leave, block
    error = blocks.get("err.exit", "")
    finish = blocks.get("err.finish", "")
    assert "@pcc_gc_store_root(" in error and "br label %err.finish" in error
    assert "@pcc_gc_frame_leave" not in error
    assert "@pcc_gc_frame_leave" in finish
    assert counts["pcc_gc_store_root_take"] <= 4, counts
    assert counts["pcc_gc_pin"] <= 5, counts
    assert counts["pcc_gc_unpin"] <= 11, counts
    assert counts["pcc_gc_release"] <= 5, counts
    assert counts["py_int_add"] == 2, counts
    assert counts["py_int_cmp"] == 1, counts
    # Every error check must sit in a block that also holds the slow runtime
    # call it guards; the inline tagged fast path cannot raise.
    for block in _blocks(body):
        if "@py_err_occurred(" in block:
            assert "@py_int_add(" in block or "@py_int_cmp(" in block, block[:400]


def test_exact_int_loop_and_promotion_match_cpython_on_every_backend(tmp_path):
    for name, source, n in (("loop", LOOP, "100000"), ("promote", PROMOTE, "1")):
        src, exe = _compile(tmp_path, name, source, emit_llvm=False)
        env = _env()
        env["BENCH_N"] = n
        expected = subprocess.run([sys.executable, str(src)], text=True, capture_output=True, timeout=60, env=env).stdout
        for backend in ("0", "1", "2", "3", "4"):
            env["PCC_GC_BACKEND"] = backend
            run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
            assert run.returncode == 0, (name, backend, run.stderr)
            assert run.stdout == expected, (name, backend, run.stdout, expected)


def test_promotion_matches_cpython_with_the_c_runtime_mirror(tmp_path, monkeypatch):
    """The ownership-transferring root store has a C mirror (py_obj.c); the
    cc-mode link exercises it on the same tagged -> bignum replacement."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "promote_cc.py"
    exe = tmp_path / "promote_cc.out"
    src.write_text(PROMOTE, encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off", backend="self")
    expected = subprocess.run([sys.executable, str(src)], text=True, capture_output=True, timeout=60).stdout
    env = _env()
    for backend in ("0", "3", "4"):
        env["PCC_GC_BACKEND"] = backend
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
        assert run.returncode == 0, (backend, run.stderr)
        assert run.stdout == expected, (backend, run.stdout)
