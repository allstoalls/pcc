from __future__ import annotations

import textwrap


def test_call_result_arithmetic_compiles_dynamic_dunder_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dynamic_dunder_arithmetic.py"
    src.write_text(textwrap.dedent(
        """
        class Box:
            def __add__(self, other):
                return 5

        def make():
            return Box()

        print(make() + Box())
        """
    ), encoding="utf-8")
    exe = tmp_path / "dynamic_dunder_arithmetic.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert exe.exists()
