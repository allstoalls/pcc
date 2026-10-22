from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_tuple_hash_mixes_ordered_elements_for_dict_keys(tmp_path):
    src = tmp_path / "tuple_hash_mix.py"
    src.write_text(
        textwrap.dedent(
            """
            def main() -> None:
                a = (1, 2, "idx")
                b = (2, 1, "idx")
                d = {}
                d[a] = 11
                d[b] = 22
                print(hash(a) != hash(b))
                print(d[a])
                print(d[b])

            main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "tuple_hash_mix.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["True", "11", "22"]
