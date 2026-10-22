"""Reproduce the pcc1 self-host crash with smaller pcc-Python programs.

The pcc1 binary (built via ``--backend self --python-libpython=off``) crashes
with nano-allocator heap corruption while compiling pcc/__main__.py. The
crash bt was inside Lifter._s_Return -> py_instance_new + 60 -> nanov2 guard.

The container helpers are exercised directly from C harnesses by
tests/test_gc_store_ptr_balance.py. So the regression
must be in the pcc-Python-compiled versions of those helpers — i.e.,
pcc's codegen for py_set.py / py_obj.py / py_class.py / etc. These tests
compile a small pcc-Python program through the SAME self backend that
builds pcc1 and assert it runs cleanly. If a test crashes with the same
heap-corruption signature, that's a minimum repro of the pcc1 regression.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


def _run_self_compile(src_text: str, tmp_path: Path, name: str = "stress") -> subprocess.CompletedProcess:
    from pcc.frontends.python.pipeline import compile_python
    src = tmp_path / f"{name}.py"
    exe = tmp_path / f"{name}.out"
    src.write_text(src_text, encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("Malloc") and "DYLD_INSERT" not in k}
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=env)


def test_self_compile_set_add_stress(tmp_path):
    """Many sets, many adds. Goes through pcc-py-compiled py_set_add."""
    src = textwrap.dedent("""
        def main() -> None:
            i: int = 0
            while i < 1000:
                s: set = set()
                j: int = 0
                while j < 32:
                    s.add(j)
                    j = j + 1
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "set_stress")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_set_string_negative_hash_stress(tmp_path):
    """String hashes are often negative; set probing must still terminate."""
    src = textwrap.dedent("""
        def main() -> None:
            i: int = 0
            while i < 200:
                s: set = set()
                s.add("attribute-error")
                s.add("post-call")
                s.add("runtime")
                s.add("builder")
                s.add("marshal")
                if "attribute-error" not in s:
                    print("missing")
                    return
                if "not-present" in s:
                    print("unexpected")
                    return
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "set_string_negative_hash_stress")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_dict_replace_stress(tmp_path):
    """Many dicts, set+replace many keys."""
    src = textwrap.dedent("""
        def main() -> None:
            i: int = 0
            while i < 500:
                d: dict = {}
                j: int = 0
                while j < 32:
                    d[j] = j
                    j = j + 1
                # replace
                k: int = 0
                while k < 32:
                    d[k] = 99
                    k = k + 1
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "dict_stress")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_list_set_stress(tmp_path):
    """Many lists with append + index reassign."""
    src = textwrap.dedent("""
        def main() -> None:
            i: int = 0
            while i < 1000:
                lst: list = []
                j: int = 0
                while j < 32:
                    lst.append(j)
                    j = j + 1
                k: int = 0
                while k < 32:
                    lst[k] = -1
                    k = k + 1
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "list_stress")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_class_instance_field_stress(tmp_path):
    """Many class instances with field stores. Mirrors the lifter
    pattern: each AST node is a class instance with fields."""
    src = textwrap.dedent("""
        class Node:
            def __init__(self, value: int, child) -> None:
                self.value: int = value
                self.child = child

        def main() -> None:
            i: int = 0
            while i < 2000:
                a = Node(1, None)
                b = Node(2, a)
                c = Node(3, b)
                # Force a chain free.
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "class_stress")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_instance_field_replacement(tmp_path):
    """Replace instance field many times — exercises pcc_gc_store_ptr's
    decref-old path on a non-NULL slot."""
    src = textwrap.dedent("""
        class Holder:
            def __init__(self) -> None:
                self.v: int = 0

        def main() -> None:
            h = Holder()
            i: int = 0
            while i < 100000:
                h.v = i
                i = i + 1
            print("ok")
            print(h.v)

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "inst_repl")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines == ["ok", "99999"], lines


def test_self_compile_nested_dataclass_chain(tmp_path):
    """Nested object chain — mirrors AST construction.  Fields hold
    other instances; cascading free must not corrupt heap."""
    src = textwrap.dedent("""
        from dataclasses import dataclass
        from typing import Optional

        @dataclass(frozen=True)
        class Leaf:
            v: int

        @dataclass(frozen=True)
        class Mid:
            leaf: Leaf

        @dataclass(frozen=True)
        class Top:
            mid: Mid

        def main() -> None:
            i: int = 0
            while i < 5000:
                top = Top(mid=Mid(leaf=Leaf(v=i)))
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "dc_chain")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_kwarg_constructor_chain(tmp_path):
    """Mirror lifter's ``pa.Return(span=..., value=...)`` construction.
    Tests pcc-py codegen for keyword-arg dataclass __init__ calls
    where each kwarg holds a heap-allocated object."""
    src = textwrap.dedent("""
        from dataclasses import dataclass
        from typing import Optional

        @dataclass(frozen=True)
        class Span:
            line: int

        @dataclass(frozen=True)
        class Name:
            span: Span
            ident: str

        @dataclass(frozen=True)
        class Return:
            span: Span
            value: Optional[Name]

        def make_return(line: int, ident: str) -> Return:
            return Return(span=Span(line=line), value=Name(span=Span(line=line), ident=ident))

        def main() -> None:
            i: int = 0
            while i < 5000:
                r = make_return(i, "x")
                # Touch fields so they don't get DCE'd
                if r.value is None:
                    print("bad")
                    return
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "kwarg_chain")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_lift_like_recursion(tmp_path):
    """Mimic Lifter's recursive descent: a Lifter class with dispatch
    methods that each construct AST nodes with kwargs."""
    src = textwrap.dedent("""
        from dataclasses import dataclass
        from typing import Optional

        @dataclass(frozen=True)
        class Span:
            line: int

        @dataclass(frozen=True)
        class Name:
            span: Span
            ident: str

        @dataclass(frozen=True)
        class Return:
            span: Span
            value: Optional[Name]

        @dataclass(frozen=True)
        class If:
            span: Span
            cond: Name
            body: tuple
            else_body: tuple

        @dataclass(frozen=True)
        class FuncDef:
            span: Span
            name: str
            body: tuple

        class Lifter:
            def __init__(self) -> None:
                self.counter: int = 0

            def make_span(self, line: int) -> Span:
                self.counter = self.counter + 1
                return Span(line=line)

            def make_return(self, line: int) -> Return:
                return Return(
                    span=self.make_span(line),
                    value=Name(span=self.make_span(line), ident="x"),
                )

            def make_if(self, line: int) -> If:
                ret_a = self.make_return(line + 1)
                ret_b = self.make_return(line + 2)
                return If(
                    span=self.make_span(line),
                    cond=Name(span=self.make_span(line), ident="c"),
                    body=(ret_a,),
                    else_body=(ret_b,),
                )

            def make_funcdef(self, line: int) -> FuncDef:
                if_node = self.make_if(line + 1)
                return FuncDef(span=self.make_span(line), name="f", body=(if_node,))

        def main() -> None:
            l = Lifter()
            i: int = 0
            while i < 2000:
                fd = l.make_funcdef(i)
                # Force the chain to not be DCE'd
                if fd.span.line < 0:
                    print("bad")
                    return
                if fd.name != "f":
                    print("bad-name")
                    return
                i = i + 1
            print("ok")
            print(l.counter)

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "lifter_like")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == "ok", lines


def test_self_compile_apply_attrs_pattern(tmp_path):
    """Mirror exactly the _apply_runtime_function_attrs pattern from
    runtime_abi.py: dict.get(name) -> frozenset; sorted; iterate +
    instance.attr.add(item) inside try/except."""
    src = textwrap.dedent("""
        class FAttrs:
            def __init__(self) -> None:
                self._attrs: set = set()

            def add(self, attr: str) -> None:
                self._attrs.add(attr)

        ATTR_TABLE: dict = {}

        def _apply_attrs(fa: FAttrs, name: str) -> None:
            attrs = ATTR_TABLE.get(name)
            if attrs is None:
                return
            for attr in sorted(attrs):
                try:
                    fa.add(attr)
                except ValueError:
                    pass

        def main() -> None:
            ATTR_TABLE["a"] = ["x", "y", "z"]
            ATTR_TABLE["b"] = ["p", "q"]
            ATTR_TABLE["c"] = ["m", "n", "o"]
            i: int = 0
            while i < 3000:
                fa = FAttrs()
                _apply_attrs(fa, "a")
                _apply_attrs(fa, "b")
                _apply_attrs(fa, "c")
                _apply_attrs(fa, "missing")
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "apply_attrs")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_getattr_with_default(tmp_path):
    """getattr(obj, name, default) — dynamic attribute lookup that
    pcc-py audit flagged. _apply_runtime_function_attrs uses this."""
    src = textwrap.dedent("""
        class Holder:
            def __init__(self) -> None:
                self.attrs: set = set()

        def main() -> None:
            i: int = 0
            while i < 5000:
                h = Holder()
                # dynamic getattr that should resolve to h.attrs
                a = getattr(h, "attrs", None)
                if a is None:
                    print("bad")
                    return
                a.add("x")
                # missing attr default path
                b = getattr(h, "missing", None)
                if b is not None:
                    print("bad2")
                    return
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "getattr_def")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_inherited_dataclass(tmp_path):
    """py_ast uses dataclass inheritance heavily (Expr -> IntLit, Stmt -> Return).
    Test pcc-py codegen for inherited frozen dataclass instance creation."""
    src = textwrap.dedent("""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Expr:
            line: int

        @dataclass(frozen=True)
        class IntLit(Expr):
            value: int

        @dataclass(frozen=True)
        class Name(Expr):
            ident: str

        @dataclass(frozen=True)
        class Stmt:
            line: int

        @dataclass(frozen=True)
        class Return(Stmt):
            value: Expr

        def read_expr(e: Expr) -> int:
            if isinstance(e, IntLit):
                return e.value
            return -1

        def main() -> None:
            i: int = 0
            while i < 5000:
                lit = IntLit(line=i, value=42)
                name = Name(line=i, ident="x")
                ret_a = Return(line=i, value=lit)
                ret_b = Return(line=i, value=name)
                if lit.value != 42:
                    print("bad-lit")
                    return
                if name.ident != "x":
                    print("bad-name")
                    return
                if read_expr(ret_a.value) != 42:
                    print("bad-narrow")
                    return
                # Force use to prevent DCE
                if ret_a.line < 0:
                    print("bad")
                    return
                if ret_b.line < 0:
                    print("bad")
                    return
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "inherited_dc")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_self_compile_dict_of_lists_pattern(tmp_path):
    """Dict where values are lists that get appended to (symbol-table-like)."""
    src = textwrap.dedent("""
        def main() -> None:
            i: int = 0
            while i < 200:
                d: dict = {}
                k: int = 0
                while k < 16:
                    d[k] = []
                    k = k + 1
                # Append into each list.
                kk: int = 0
                while kk < 16:
                    bucket: list = d[kk]
                    j: int = 0
                    while j < 8:
                        bucket.append(j)
                        j = j + 1
                    kk = kk + 1
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """).lstrip()
    r = _run_self_compile(src, tmp_path, "dict_of_lists")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
