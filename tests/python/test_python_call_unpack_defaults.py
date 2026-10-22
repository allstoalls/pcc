from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pcc1_gate import repo_root

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1
from pcc.frontends.python.codegen.call_arg_lowering import CallArgLoweringMixin
from pcc.frontends.python.codegen.call_resolution_lowering import CallResolutionLoweringMixin
from pcc.frontends.python.py_ast import (
    Arg,
    Call,
    DictExpr,
    DynType,
    IntType,
    Name,
    NoneLit,
    NoneType,
    Subscript,
    TupleType,
)


REPO = repo_root()


class _Resolver(CallResolutionLoweringMixin, CallArgLoweringMixin):
    class _Module:
        name = "test"

    module = _Module()


class _NoSlowResolver(_Resolver):
    def _expand_direct_call_unpacks(
        self,
        positional,
        kwargs_pairs,
        formal_args,
        skip_self,
    ):
        raise AssertionError("exact positional fast path should not expand unpacks")


def test_exact_positional_call_resolution_uses_fast_path():
    resolver = _NoSlowResolver()
    span = None
    arg = Name(span=span, ty=DynType(name="dyn"), ident="value")
    formals = (
        Arg(name="self", annotation=DynType(name="dyn"), default=None, kind="pos"),
        Arg(name="value", annotation=DynType(name="dyn"), default=None, kind="pos"),
    )

    assert resolver._resolve_call_kwargs((arg,), (), formals, skip_self=True) == [arg]


def test_exact_positional_fast_path_does_not_skip_missing_defaults():
    resolver = _Resolver()
    span = None
    arg = Name(span=span, ty=DynType(name="dyn"), ident="value")
    default = NoneLit(span=span, ty=NoneType(name="None"))
    formals = (
        Arg(name="value", annotation=DynType(name="dyn"), default=None, kind="pos"),
        Arg(
            name="detail",
            annotation=DynType(name="dyn"),
            default=default,
            kind="pos",
            has_default=True,
        ),
    )

    resolved = resolver._resolve_call_kwargs((arg,), (), formals)

    assert resolved == [arg, default]


def test_unknown_starred_positional_uses_required_args_and_defaults():
    resolver = _Resolver()
    span = None
    star_src = Name(
        span=span,
        ty=TupleType(name="tuple", elems=()),
        ident="items",
    )
    starred = Call(
        span=span,
        ty=DynType(name="dyn"),
        func=Name(span=span, ty=DynType(name="dyn"), ident="__starred__"),
        args=(star_src,),
        kwargs=(),
    )
    formals = (
        Arg(name="dtype", annotation=DynType(name="dyn"), default=None, kind="pos"),
        Arg(name="offset", annotation=IntType(name="int"), default=None, kind="pos"),
        Arg(
            name="title",
            annotation=DynType(name="dyn"),
            default=NoneLit(span=span, ty=NoneType(name="None")),
            kind="pos",
            has_default=True,
        ),
    )

    resolved = resolver._resolve_call_kwargs((starred,), (), formals)

    assert len(resolved) == 3
    assert isinstance(resolved[0], Subscript)
    assert isinstance(resolved[1], Subscript)
    assert isinstance(resolved[2], NoneLit)
    assert resolved[0].idx.value == 0
    assert resolved[1].idx.value == 1


def test_starred_unpack_accepts_nominal_call_name_nodes():
    resolver = _Resolver()
    span = None
    nominal_name_cls = type("Name", (), {})
    nominal_call_cls = type("Call", (), {})
    func = nominal_name_cls()
    func.ident = "__starred__"
    arg = nominal_call_cls()
    arg.func = func
    arg.args = (Name(span=span, ty=DynType(name="dyn"), ident="items"),)
    arg.kwargs = ()

    assert resolver._is_starred_unpack_expr(arg)


def test_multiple_starstar_kwargs_merge_for_missing_formals():
    resolver = _Resolver()
    span = None
    first = Name(span=span, ty=DynType(name="dyn"), ident="first")
    left = Name(span=span, ty=DynType(name="dyn"), ident="left")
    right = Name(span=span, ty=DynType(name="dyn"), ident="right")
    left_unpack = Call(
        span=span,
        ty=DynType(name="dyn"),
        func=Name(span=span, ty=DynType(name="dyn"), ident="**"),
        args=(left,),
        kwargs=(),
    )
    right_unpack = Call(
        span=span,
        ty=DynType(name="dyn"),
        func=Name(span=span, ty=DynType(name="dyn"), ident="**"),
        args=(right,),
        kwargs=(),
    )
    formals = (
        Arg(name="a", annotation=DynType(name="dyn"), default=None, kind="pos"),
        Arg(name="b", annotation=DynType(name="dyn"), default=None, kind="pos"),
        Arg(name="c", annotation=DynType(name="dyn"), default=None, kind="pos"),
    )

    resolved = resolver._resolve_call_kwargs(
        (first, left_unpack, right_unpack),
        (),
        formals,
    )

    assert resolved[0] is first
    assert isinstance(resolved[1], Subscript)
    assert isinstance(resolved[2], Subscript)
    # Several ** operands merge once, left to right, through the
    # duplicate-checking merge: a key in both is CPython's TypeError.
    merged = resolved[1].obj
    assert merged is resolved[2].obj
    assert isinstance(merged, Call) and merged.func.ident == "__pcc_kwargs_merge__"
    assert list(merged.args) == [left, right]


def test_parser_style_starstar_kwargs_merge_for_missing_formals():
    resolver = _Resolver()
    span = None
    first = Name(span=span, ty=DynType(name="dyn"), ident="first")
    left = Name(span=span, ty=DynType(name="dyn"), ident="left")
    right = Name(span=span, ty=DynType(name="dyn"), ident="right")
    formals = (
        Arg(name="a", annotation=DynType(name="dyn"), default=None, kind="pos"),
        Arg(name="b", annotation=DynType(name="dyn"), default=None, kind="pos"),
        Arg(name="c", annotation=DynType(name="dyn"), default=None, kind="pos"),
    )

    resolved = resolver._resolve_call_kwargs(
        (first,),
        (("**", left), ("**", right)),
        formals,
    )

    assert resolved[0] is first
    assert isinstance(resolved[1], Subscript)
    assert isinstance(resolved[2], Subscript)
    # Several ** operands merge once, left to right, through the
    # duplicate-checking merge: a key in both is CPython's TypeError.
    merged = resolved[1].obj
    assert merged is resolved[2].obj
    assert isinstance(merged, Call) and merged.func.ident == "__pcc_kwargs_merge__"
    assert list(merged.args) == [left, right]


def test_multiple_legacy_starstar_kwargs_use_duplicate_checking_merge():
    resolver = _Resolver()
    span = None
    first = Name(span=span, ty=DynType(name="dyn"), ident="first")
    left = Name(span=span, ty=DynType(name="dyn"), ident="left")
    right = Name(span=span, ty=DynType(name="dyn"), ident="right")
    left_unpack = Call(
        span=span,
        ty=DynType(name="dyn"),
        func=Name(span=span, ty=DynType(name="dyn"), ident="**"),
        args=(left,),
        kwargs=(),
    )
    right_unpack = Call(
        span=span,
        ty=DynType(name="dyn"),
        func=Name(span=span, ty=DynType(name="dyn"), ident="**"),
        args=(right,),
        kwargs=(),
    )

    # A plain dict merge would overwrite duplicate keys; the operands go
    # through the duplicate-checking merge, which raises CPython's call-time
    # TypeError for a key in both.
    positional, merged = resolver._split_starstar_kwargs_unpack((first, left_unpack, right_unpack))
    assert positional == (first,)
    assert isinstance(merged, Call) and merged.func.ident == "__pcc_kwargs_merge__"
    assert list(merged.args) == [left, right]


def test_pcc1_starred_unknown_positional_uses_required_args(tmp_path):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for starred call unpack regression"
        )

    src = tmp_path / "starred_unknown.py"
    src.write_text(
        "def unpack(dtype, offset, title=None):\n"
        "    return dtype, offset, title\n"
        "\n"
        "def caller(items):\n"
        "    fld_dtype, offset, title = unpack(*items)\n"
        "    print(fld_dtype)\n"
        "\n"
        "def main():\n"
        "    caller((1, 2))\n"
        "main()\n",
        encoding="utf-8",
    )
    out = tmp_path / "starred_unknown.ll"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_PYTHON_IR_PASSES"] = "off"
    proc = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "--emit-llvm=" + str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.exists()
