from __future__ import annotations

import subprocess
import textwrap


def test_local_cache_method_decorator_is_noop_self_backend(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "cache_method_decorator.py"
    src.write_text(textwrap.dedent(
        """
        class Cache:
            @staticmethod
            def me(fn):
                return fn

        class Worker:
            @Cache.me
            def value(self):
                return 7

        print(Worker().value())
        """
    ), encoding="utf-8")
    exe = tmp_path / "cache_method_decorator.out"
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
    assert run.stdout == "7\n"
