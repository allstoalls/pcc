"""*args forwarding: g(*xs) into def g(*args) under strict no-libpython.

g(*xs) where g collects *args returned EMPTY args (sum -> 0): the direct-call
unpack expander (_expand_direct_call_unpacks) statically expands a splat into a
known number of fixed positional slots, but a *args param needs the splat's
runtime-many elements — it computed needed=0 and silently DROPPED the splat.
(Direct g(1,2,3) and fixed-param f(*[1,2,3]) already worked.)

Fix (frontend, call_resolution_lowering.py): when a runtime-sized splat feeds
the *args param ENTIRELY (the plain positionals exactly fill the fixed
positional formals, no **kwargs), emit a __star_to_varargs__ marker; the
resolver sets the *args slot to tuple(star_src) (tuple(<seq>) is a working
builtin). Splats that also fill fixed slots / co-occur with **kwargs keep the
old static path. Blast radius is contained — calls with no unpack early-return.

Runs under ``--backend self --python-libpython=off`` in DEFAULT runtime mode.
"""
from __future__ import annotations
import os, subprocess


def _run(tmp_path, source):
    src = tmp_path / "p.py"; src.write_text(source, encoding="utf-8")
    exe = tmp_path / "p_bin"; env = os.environ.copy(); env.pop("LC_ALL", None)
    b = subprocess.run(["uv","run","pcc","--backend","self","--python-libpython=off","--ir-scaffold=on",str(src),"-o",str(exe)], text=True, capture_output=True, timeout=420, env=env)
    assert b.returncode == 0, b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_args_forwarding(tmp_path):
    out = _run(tmp_path,
        "def g(*args):\n"
        "    return sum(args)\n"
        "def h(a, *args):\n"
        "    return a + sum(args)\n"
        "def wrapper(*args):\n"
        "    return g(*args)\n"
        "def main():\n"
        "    xs = [1, 2, 3]\n"
        "    print(g(*xs))\n"            # 6
        "    print(g(*[10, 20]))\n"      # 30
        "    print(h(1, *xs))\n"         # 7
        "    print(wrapper(*xs))\n"      # 6
        "    print(g(*range(4)))\n"      # 6
        "main()\n")
    assert out.split("\n")[:5] == ["6", "30", "7", "6", "6"], out


def test_direct_and_fixed_regression(tmp_path):
    # Direct positional into *args, empty *args, and fixed-param splat must all
    # keep working (the static-expansion paths are unchanged).
    out = _run(tmp_path,
        "def g(*args):\n"
        "    return sum(args)\n"
        "def fixed(a, b, c):\n"
        "    return a + b + c\n"
        "def main():\n"
        "    print(g(1, 2, 3))\n"        # 6
        "    print(g())\n"               # 0
        "    print(fixed(*[4, 5, 6]))\n" # 15
        "    print(fixed(1, 2, 3))\n"    # 6
        "main()\n")
    assert out.split("\n")[:4] == ["6", "0", "15", "6"], out


def test_runtime_splat_tuple_materialization_is_helper_call(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "p.py"
    ll = tmp_path / "p.ll"
    src.write_text(
        "def g(*args):\n"
        "    return args[0] + args[1]\n"
        "def main():\n"
        "    xs = [1, 2]\n"
        "    print(g(*xs))\n"
        "main()\n",
        encoding="utf-8",
    )

    compile_python(
        str(src),
        str(ll),
        ir_scaffold_mode="on",
        libpython_mode="off",
        emit_llvm_only=True,
    )
    ir_text = ll.read_text(encoding="utf-8")
    assert "py_tuple_from_splat" in ir_text
    assert "tuple.src.len" not in ir_text
