from __future__ import annotations

import subprocess
import textwrap


def test_tuple_concat_after_singleton_tuple_rebind_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "tuple_concat_rebind.py"
    src.write_text(textwrap.dedent(
        """
        class Item:
            pass

        def pack(index: Item):
            index = index,
            return (Item(),) + index

        result = pack(Item())
        print(len(result))
        """
    ), encoding="utf-8")
    exe = tmp_path / "tuple_concat_rebind.out"
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
    assert run.stdout == "2\n"
