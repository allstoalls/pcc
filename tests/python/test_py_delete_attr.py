from __future__ import annotations

import subprocess
import textwrap


def test_delete_attribute_statement_compiles_no_libpython_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "delete_attr.py"
    src.write_text(textwrap.dedent(
        """
        class Wrapper:
            pass

        wrapper = Wrapper()
        wrapper.value = 7
        print(wrapper.value)
        del wrapper.value
        print(hasattr(wrapper, "value"))
        """
    ), encoding="utf-8")
    exe = tmp_path / "delete_attr.out"
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "7\nFalse\n"
