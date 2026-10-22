from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_lambda_literal_in_dict_materializes_native_callable(tmp_path):
    src = tmp_path / "lambda_dict.py"
    src.write_text(
        textwrap.dedent(
            """
            funcs = {
                "add": lambda x, y: x + y,
            }
            fn = funcs["add"]
            print(fn(20, 22))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "lambda_dict.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "42\n"


def test_lambda_capture_callable_list_comprehension_self_backend(tmp_path):
    src = tmp_path / "lambda_capture_callable.py"
    src.write_text(
        textwrap.dedent(
            """
            class Wrapper:
                def values(self, items):
                    return items

                def wrap(self):
                    method = self.values
                    return lambda input: [_.strip() for _ in method(input)]

            fn = Wrapper().wrap()
            out = fn(["  a", "b  "])
            print(len(out))
            print(out[0])
            print(out[1])
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = tmp_path / "lambda_capture_callable.out"

    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["2", "a", "b"]
