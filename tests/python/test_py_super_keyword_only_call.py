"""``super().__init__`` through the dynamic method path keeps its call shape.

A ``super()`` method reference resolves to a function object at runtime, so the
call goes through ``py_func_call``: the emitted positional slots are re-packed
into a Python argument tuple. ``_resolve_call_kwargs`` has already flattened
keyword-only arguments into positional slots for the native ABI, and packing
those flat slots into a tuple hands the runtime binder a keyword-only formal as
a positional -- which it correctly refuses. ``*args``/``**kwargs`` slots carry a
packed tuple/dict that has to be splatted back out for the same reason.
"""

import subprocess
import textwrap

from pcc.py_frontend.pipeline import compile_python


def _build_and_run(tmp_path, name, source):
    src = tmp_path / (name + ".py")
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    exe = tmp_path / name
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_super_init_binds_keyword_only_base_parameter(tmp_path):
    assert _build_and_run(
        tmp_path,
        "super_keyword_only",
        """
        class Base:
            def __init__(self, *, loop=None) -> None:
                self.loop = loop

        class Child(Base):
            def __init__(self, coro, *, loop=None) -> None:
                super().__init__(loop=loop)
                self.coro = coro

        c = Child(7, loop=42)
        print(c.coro)
        print(c.loop)
        """,
    ) == ["7", "42"]


def test_super_init_keyword_only_defaults_and_partial_binds(tmp_path):
    assert _build_and_run(
        tmp_path,
        "super_keyword_only_defaults",
        """
        class Base:
            def __init__(self, a, *, b=1, c=2) -> None:
                self.a = a
                self.b = b
                self.c = c

        class Omit(Base):
            def __init__(self) -> None:
                super().__init__(10)

        class Some(Base):
            def __init__(self) -> None:
                super().__init__(20, c=7)

        class All(Base):
            def __init__(self) -> None:
                super().__init__(30, c=8, b=9)

        def show(o) -> None:
            print(o.a)
            print(o.b)
            print(o.c)

        show(Omit())
        show(Some())
        show(All())
        """,
    ) == ["10", "1", "2", "20", "1", "7", "30", "9", "8"]


def test_super_init_splats_var_positional_and_var_keyword(tmp_path):
    assert _build_and_run(
        tmp_path,
        "super_var_args",
        """
        class VarKw:
            def __init__(self, x, **rest) -> None:
                self.x = x
                self.rest = rest

        class VarKwChild(VarKw):
            def __init__(self) -> None:
                super().__init__(40, y=5)

        class VarPos:
            def __init__(self, x, *rest, k=3) -> None:
                self.x = x
                self.rest = rest
                self.k = k

        class VarPosChild(VarPos):
            def __init__(self) -> None:
                super().__init__(50, 51, 52, k=6)

        v = VarKwChild()
        print(v.x)
        print(v.rest["y"])
        p = VarPosChild()
        print(p.x)
        print(p.rest)
        print(p.k)
        """,
    ) == ["40", "5", "50", "(51, 52)", "6"]
