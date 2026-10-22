"""Positional args interleaved with a ``*splat`` keep source order.

``f(1, *x, 3)`` used to bind as ``f(1, 3, *x)`` under the native/no-libpython
direct-call path: _expand_direct_call_unpacks collected the plain positionals
into one list and appended the splat expansion at the END, dropping the
splat's source position. So ``def f(a, b, c): return (a, b, c)`` returned
``(1, 3, 2)`` for ``f(1, *[2], 3)`` (CPython: ``(1, 2, 3)``) and ``(1, 3, 2)``
for ``f(*[2], 1, 3)`` (CPython: ``(2, 1, 3)``). Pure ``f(*x)`` was already
correct.

Fix (frontend): _expand_direct_call_unpacks now records how many plain
positionals precede the splat (``star_prefix_count``) and splices the splat
expansion back in at that index instead of appending it.

Runs under ``--backend self --python-libpython=off`` in DEFAULT runtime mode.
Also unit-tests the resolver directly so the ordering is pinned even when no
current pcc1/self-backend toolchain is available.
"""
from __future__ import annotations

import os
import subprocess

from pcc.frontends.python.codegen.call_arg_lowering import CallArgLoweringMixin
from pcc.frontends.python.codegen.call_resolution_lowering import (
    CallResolutionLoweringMixin,
)
from pcc.frontends.python.py_ast import (
    Arg,
    Call,
    DynType,
    IntLit,
    IntType,
    ListType,
    Name,
    Subscript,
)


class _Resolver(CallResolutionLoweringMixin, CallArgLoweringMixin):
    class _Module:
        name = "test"

    module = _Module()


def _int_lit(value):
    return IntLit(span=None, ty=IntType(name="int"), value=value)


def _starred(name):
    src = Name(span=None, ty=ListType(name="list", elem=DynType(name="dyn")), ident=name)
    return Call(
        span=None,
        ty=DynType(name="dyn"),
        func=Name(span=None, ty=DynType(name="dyn"), ident="__starred__"),
        args=(src,),
        kwargs=(),
    )


_FORMALS_ABC = (
    Arg(name="a", annotation=DynType(name="dyn"), default=None, kind="pos"),
    Arg(name="b", annotation=DynType(name="dyn"), default=None, kind="pos"),
    Arg(name="c", annotation=DynType(name="dyn"), default=None, kind="pos"),
)


def test_resolver_splat_between_positionals_keeps_order():
    """f(1, *x, 3) -> [1, x[0], 3] (NOT [1, 3, x[0]])."""
    resolver = _Resolver()
    one = _int_lit(1)
    three = _int_lit(3)
    resolved = resolver._resolve_call_kwargs(
        (one, _starred("x"), three),
        (),
        _FORMALS_ABC,
    )
    assert len(resolved) == 3
    assert resolved[0] is one
    assert isinstance(resolved[1], Subscript)  # x[0]
    assert resolved[1].idx.value == 0
    assert resolved[2] is three


def test_resolver_splat_before_positionals_keeps_order():
    """f(*x, 1, 3) -> [x[0], 1, 3] (NOT [1, 3, x[0]])."""
    resolver = _Resolver()
    one = _int_lit(1)
    three = _int_lit(3)
    resolved = resolver._resolve_call_kwargs(
        (_starred("x"), one, three),
        (),
        _FORMALS_ABC,
    )
    assert len(resolved) == 3
    assert isinstance(resolved[0], Subscript)  # x[0]
    assert resolved[0].idx.value == 0
    assert resolved[1] is one
    assert resolved[2] is three


def test_resolver_pure_splat_still_correct():
    """f(*x) with a 3-arg list -> [x[0], x[1], x[2]] (regression guard)."""
    resolver = _Resolver()
    resolved = resolver._resolve_call_kwargs(
        (_starred("x"),),
        (),
        _FORMALS_ABC,
    )
    assert len(resolved) == 3
    for i in range(3):
        assert isinstance(resolved[i], Subscript)
        assert resolved[i].idx.value == i


def _run(tmp_path, source):
    src = tmp_path / "sp.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / "sp_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    b = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=420,
        env=env,
    )
    assert b.returncode == 0, b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    # Diff against CPython running the exact same source.
    ref = subprocess.run(
        ["python3", str(src)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ref.returncode == 0, ref.stderr
    assert r.stdout == ref.stdout, (r.stdout, ref.stdout)
    return r.stdout


def test_native_splat_arg_order_matches_cpython(tmp_path):
    out = _run(
        tmp_path,
        "def f(a, b, c):\n"
        "    return (a, b, c)\n"
        "def main():\n"
        "    x = [2]\n"
        "    print(f(1, *x, 3))\n"    # (1, 2, 3)
        "    print(f(*x, 1, 3))\n"    # (2, 1, 3)
        "    print(f(1, 2, *[3]))\n"  # (1, 2, 3)
        "    print(f(*[1, 2, 3]))\n"  # (1, 2, 3)
        "main()\n",
    )
    assert out.split("\n")[:4] == [
        "(1, 2, 3)",
        "(2, 1, 3)",
        "(1, 2, 3)",
        "(1, 2, 3)",
    ], out
