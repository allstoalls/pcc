"""print(*args, flush=True/False) under strict no-libpython.

A bool-literal ``flush=`` kwarg on ``print`` used to force the CPython
fallback (PCC-PY-COMPILE-001 "requires libpython fallback" under
``--python-libpython=off``): the kwarg gate in _emit_print_call only
accepted ``sep`` / ``end`` and rejected everything else, and
_try_emit_native_file_stream_print also declined ``flush``.

Fix (frontend): the native ``py_print`` / ``py_print_many`` runtime already
flushes stdout per line, so a bool-literal ``flush=`` is a semantic no-op we
can accept and drop. _emit_print_call now routes ``print(..., flush=True|False)``
(alone or combined with sep/end, and through the *splat path) to the native
py_print_many emitter, dropping the flush= kwarg before the sep/end loop so it
never emits a dead truthiness value. A non-literal ``flush=<expr>`` still falls
back (evaluating it for truthiness could have side effects). ``file=`` still
forces fallback regardless of flush.

Runs under ``--backend self --python-libpython=off`` in DEFAULT runtime mode
(links the pcc-Python runtime ports), then diffs stdout against CPython.
"""
from __future__ import annotations
import os, subprocess

from pcc.frontends.python.pipeline import compile_python


def _run(tmp_path, source):
    src = tmp_path / "pf.py"; src.write_text(source, encoding="utf-8")
    exe = tmp_path / "pf_bin"; env = os.environ.copy(); env.pop("LC_ALL", None)
    b = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(src), "-o", str(exe)],
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert b.returncode == 0, b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_print_flush_true_false(tmp_path):
    # The slice's own probe: print('x', flush=True) + sep + flush=False.
    out = _run(
        tmp_path,
        "def main():\n"
        "    print('x', flush=True)\n"                   # x
        "    print('a', 'b', sep='-', flush=False)\n"    # a-b
        "main()\n",
    )
    assert out.split("\n")[:2] == ["x", "a-b"], out


def test_print_flush_with_sep_end_and_types(tmp_path):
    out = _run(
        tmp_path,
        "def main():\n"
        "    print(42, flush=True)\n"                            # 42
        "    print(3.5, flush=False)\n"                          # 3.5
        "    print('a', 'b', 'c', sep=', ', end='!\\n', flush=True)\n"  # a, b, c!
        "main()\n",
    )
    assert out.split("\n")[:3] == ["42", "3.5", "a, b, c!"], out


def test_print_flush_splat(tmp_path):
    out = _run(
        tmp_path,
        "def main():\n"
        "    items = [1, 2, 3]\n"
        "    print(*items, flush=True)\n"                        # 1 2 3
        "    print(*items, sep=', ', end='!\\n', flush=False)\n" # 1, 2, 3!
        "main()\n",
    )
    assert out.split("\n")[:2] == ["1 2 3", "1, 2, 3!"], out


def test_native_scalar_print_ir_has_no_printf_calls(tmp_path):
    """PCC-owned print paths must not create a second libc stdio buffer."""
    src = tmp_path / "print_ir.py"
    src.write_text(
        "def main():\n"
        "    print()\n"
        "    print(7)\n"
        "    print(True)\n"
        "    print(2.5)\n"
        "main()\n",
        encoding="utf-8",
    )
    out = tmp_path / "print_ir.ll"
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    llvm_ir = out.read_text(encoding="utf-8")
    assert "call i32 (ptr, ...) @printf" not in llvm_ir
    assert llvm_ir.count("call void (ptr) @py_print(") >= 3
    assert "call void (ptr, ptr, ptr) @py_print_many(" in llvm_ir
