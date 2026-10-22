"""Bucket 1: typed AST class field access lowering.

When ``fd: FuncDef`` is a function param and ``FuncDef`` is an
imported dataclass, ``fd.body`` should be typed ``tuple[Stmt, ...]``
in type_infer (not DynType). Codegen then emits typed-attribute
access (no py_cpy_*).

This is the biggest single Bucket 1 lever — layer1.py has 1342+
attribute access calls on AST nodes, all currently DynType-routed.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _multi_compile(srcs_mods, out_path: str) -> str:
    from pcc.frontends.python.pipeline import compile_python_multi

    srcs = [s for s, _ in srcs_mods]
    mods = [m for _, m in srcs_mods]
    compile_python_multi(
        srcs, out_path,
        emit_llvm_only=True,
        entry_module=mods[0],
        module_names=mods,
    )
    return Path(out_path).read_text(encoding="utf-8")


def test_typed_field_access_resolves_natively(tmp_path):
    """A user file that does ``fd.body`` where fd: FuncDef and
    FuncDef is from a sibling dataclass module should NOT emit
    ``py_cpy_getattr`` — the field type is known statically."""
    sibling = tmp_path / "ast_pkg.py"
    sibling.write_text(textwrap.dedent(
        """
        from dataclasses import dataclass

        @dataclass
        class FuncDef:
            name: str
            body: tuple
        """
    ), encoding="utf-8")
    user = tmp_path / "user.py"
    user.write_text(textwrap.dedent(
        """
        from ast_pkg import FuncDef

        def visit(fd: FuncDef) -> None:
            for stmt in fd.body:
                pass
        """
    ), encoding="utf-8")
    out = tmp_path / "combined.ll"
    text = _multi_compile(
        [(str(user), "user"), (str(sibling), "ast_pkg")],
        str(out),
    )
    # Find the visit function body
    m = re.search(
        r"define[^\n]+@user_user[^\n]*visit[^{]+\{(.+?)\n\}",
        text, re.DOTALL,
    )
    assert m is not None, f"visit fn not found in IR:\n{text[:500]}"
    body = m.group(1)
    # The current state (Bucket 1 not done): py_cpy_getattr for
    # fd.body. After the fix: should NOT contain py_cpy_getattr.
    assert "py_cpy_getattr" not in body, (
        f"fd.body should NOT route through py_cpy_getattr after the "
        f"Bucket 1 fix; got body:\n{body}"
    )


def test_local_typed_class_field_access_uses_slot_load(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "typed_local_field.py"
    src.write_text(textwrap.dedent(
        """
        class Box:
            def __init__(self) -> None:
                self.items: list = []

        def push(box: Box, item) -> None:
            box.items.append(item)
        """
    ), encoding="utf-8")
    out = tmp_path / "typed_local_field.ll"
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    text = out.read_text(encoding="utf-8")
    m_fn = re.search(
        r"define[^\n]+@user_typed_local_field[^\n]*push[^{]+\{(.+?)\n\}",
        text, re.DOTALL,
    )
    assert m_fn is not None, text[:500]
    body = m_fn.group(1)
    assert "py_instance_get_field" in body, body
    assert "py_obj_getattr" not in body, body


def test_instance_field_precedes_same_named_annotated_class_slot(tmp_path):
    """Typed receivers read the initialized instance slot before class data.

    This is the minimized shape of the former ``_InferCtx.globals`` self-host
    corruption: annotation-only class metadata and an ``__init__`` instance
    write share a name.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "annotated_instance_precedence.py"
    exe = tmp_path / "annotated_instance_precedence"
    src.write_text(
        textwrap.dedent(
            """
            class Context:
                value: object

                def __init__(self, value: object) -> None:
                    self.value = value

            def read(ctx: Context) -> object:
                return ctx.value

            print(read(Context("instance")))
            """
        ),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        backend="self",
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout == "instance\n"


def test_field_type_propagates_into_for_loop(tmp_path):
    """``for s in fd.body:`` — if fd.body is statically tuple, the
    for-loop should iterate via py_tuple, not py_cpy_iter."""
    sibling = tmp_path / "ast_pkg2.py"
    sibling.write_text(textwrap.dedent(
        """
        from dataclasses import dataclass

        @dataclass
        class Module2:
            stmts: tuple
        """
    ), encoding="utf-8")
    user = tmp_path / "user2.py"
    user.write_text(textwrap.dedent(
        """
        from ast_pkg2 import Module2

        def walk(m: Module2) -> int:
            n = 0
            for s in m.stmts:
                n = n + 1
            return n
        """
    ), encoding="utf-8")
    out = tmp_path / "combined2.ll"
    text = _multi_compile(
        [(str(user), "user2"), (str(sibling), "ast_pkg2")],
        str(out),
    )
    m_fn = re.search(
        r"define[^\n]+@user_user2[^\n]*walk[^{]+\{(.+?)\n\}",
        text, re.DOTALL,
    )
    assert m_fn is not None, text[:500]
    body = m_fn.group(1)
    # When fd.body is typed tuple, iter doesn't need py_cpy_iter.
    assert "py_cpy_iter" not in body, (
        f"typed-tuple iteration should not use py_cpy_iter; "
        f"got body:\n{body}"
    )
