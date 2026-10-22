"""Method/field name lookup must not confuse prefix families.

`py_class.py`'s `_strs_eq` is the equality used by `_class_lookup_in_mro` and
`_lookup_field_index`, so it runs on every attribute access and method
dispatch.  It compares raw C strings, where a length-blind or early-terminating
compare silently resolves `ab` to `abc`'s method.  This runs in DEFAULT mode on
purpose: `PCC_RUNTIME_CC=cc` links the C sources (which use `strcmp`) and would
not exercise the pcc-Python port at all.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_prefix_family_method_and_field_names_resolve_exactly(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prefixnames.py"
    exe = tmp_path / "prefixnames.out"
    src.write_text(textwrap.dedent("""
        class Base:
            def a(self) -> str:
                return "base.a"

            def ab(self) -> str:
                return "base.ab"

            def abc(self) -> str:
                return "base.abc"

            def abcd(self) -> str:
                return "base.abcd"

            def p(self) -> str:
                return "base.p"

            def pq(self) -> str:
                return "base.pq"

            def pr(self) -> str:
                return "base.pr"

            def foo1(self) -> str:
                return "base.foo1"

            def foo2(self) -> str:
                return "base.foo2"


        class Mid(Base):
            def ab(self) -> str:
                return "mid.ab"

            def foo2(self) -> str:
                return "mid.foo2"


        class Leaf(Mid):
            def __init__(self) -> None:
                self.x = 1
                self.xy = 2
                self.xyz = 3
                self.xyzw = 4
                self.q = 5
                self.qr = 6

            def abcd(self) -> str:
                return "leaf.abcd"


        def main() -> None:
            leaf = Leaf()
            print(leaf.a())
            print(leaf.ab())
            print(leaf.abc())
            print(leaf.abcd())
            print(leaf.p())
            print(leaf.pq())
            print(leaf.pr())
            print(leaf.foo1())
            print(leaf.foo2())
            print(leaf.x, leaf.xy, leaf.xyz, leaf.xyzw, leaf.q, leaf.qr)
            mid = Mid()
            print(mid.ab(), mid.abcd(), mid.foo2())
            base = Base()
            print(base.ab(), base.foo2())

        main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython, (result.stdout, cpython)
    # Spot-check the shapes a broken compare would collapse.
    assert "mid.ab" in result.stdout
    assert "leaf.abcd" in result.stdout
    assert "base.abc" in result.stdout
    assert "1 2 3 4 5 6" in result.stdout
